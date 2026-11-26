# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 FreePVR Contributors
"""
Clean-room PowerVR Volcanic USC Shader ISA Framing, Decoder & Encoder.

Authoritative hardware specification:
  - VOLCANIC-USC-ISA-MAP.md  (17-family W0[6:0] prefix tree, bit-31 EOI framing,
    trailing default-word elision, 16 canonical banks, SR 0..60, SC 0..127,
    ISA_CTRL + 32-bit IMM permutation, ISA_COND)
  - VOLCANIC-USC-OPCODES.md  (FOP, AF32, INT, DOT8, MOV, BIT 0x38 & 0x06,
    DMA LD/ST/ATOM32/ATOM64/IDF/MEMBARRIER, EMI, ITR, PSB_WR, PSB_RD, SMP, TAP)
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, List, Optional, Sequence, Tuple

EOI_BIT = 0x80000000
PAYLOAD_MASK = 0x7FFFFFFF


class Bank(IntEnum):
    NONE = 0
    INTERNAL = 1      # i0..i7
    SLOT = 2          # sl0..sl15 (128-wide cross-lane uniform)
    POUT = 3          # pout0..pout31
    TEMP = 4          # r0..r255
    VI = 5            # vi0..vi127
    CF = 6            # cf0..cf131
    SH = 7            # sh0..sh511
    SC = 8            # sc0..sc127
    CS = 9            # cs0..cs1023 (common store / local memory)
    SR = 10           # sr0..sr60 (61 = UNDEF)
    EMC = 11          # execution mask counter
    IMM = 12          # inline immediate
    BIT_PRED_DST = 13 # p0 predicate destination
    DMA_GLOBAL = 14   # zero/unbased DMA sentinel
    INDEXED = 15


BANK_PREFIX = {
    Bank.NONE: "NONE",
    Bank.INTERNAL: "i",
    Bank.SLOT: "sl",
    Bank.POUT: "pout",
    Bank.TEMP: "r",
    Bank.VI: "vi",
    Bank.CF: "cf",
    Bank.SH: "sh",
    Bank.SC: "sc",
    Bank.CS: "cs",
    Bank.SR: "sr",
    Bank.EMC: "emc",
    Bank.IMM: "#",
    Bank.BIT_PRED_DST: "p0",
    Bank.DMA_GLOBAL: "global",
    Bank.INDEXED: "idx",
}


@dataclass(frozen=True)
class Reg:
    bank: Bank
    index: int = 0

    def __str__(self) -> str:
        if self.bank in (Bank.NONE, Bank.EMC, Bank.BIT_PRED_DST, Bank.DMA_GLOBAL):
            return BANK_PREFIX[self.bank]
        return f"{BANK_PREFIX[self.bank]}{self.index}"


REG_NONE = Reg(Bank.NONE, 0)
REG_SC0 = Reg(Bank.SC, 0)
REG_P0 = Reg(Bank.BIT_PRED_DST, 0)


class CC(IntEnum):
    ALWAYS = 0
    EXEC_MASK = 1
    NOT_P0 = 2
    P0 = 3


class CtrlOp(IntEnum):
    BR = 0
    BA = 1
    BRLT = 2
    LAPC = 3
    SAVL = 4
    BREAKPOINT = 5
    LIMM = 6
    OLCHK = 7
    OP8 = 8
    MUTEX = 9
    NOP = 10
    PHAS = 11
    END = 12
    WGBARRIER = 13


class CondOp(IntEnum):
    CNDST = 0
    CNDEF = 1
    CNDSM = 2
    CNDLT = 3
    CNDEND = 4
    CNDLTB = 5


class FopOp(IntEnum):
    FMAD = 0
    FMUL = 1
    FADD = 2
    FDSX_FINE = 3
    FDSY_FINE = 4
    FOP5 = 5
    FOP6 = 6
    FMOVC_PRED = 7
    FMOV = 8
    FCOMP = 9
    FMOVC = 10
    FFRC = 11


class FopFmt(IntEnum):
    F16_LO = 0
    F16_HI = 1
    F32 = 2
    F16X2 = 3


class Af32Op(IntEnum):
    FRCP = 0
    FRSQ = 1
    OP2 = 2
    FLOG = 3
    OP4 = 4
    FEXP = 5
    OP6 = 6
    OP7 = 7
    FSIN = 8
    FSIN_HP = 9
    FCOS = 10
    FCOS_HP = 11
    FATAN = 12


class IntOp(IntEnum):
    IMOV = 0
    IADD = 1
    IMUL = 2
    IMAD_OR_LT = 3
    EQ_OR_MAX = 4
    IMAD_ALT_OR_NEQ = 5
    GTE = 6
    IMAD64_OR_GT_OR_MIN = 7
    LTE = 8
    OP9 = 9
    OP10 = 10
    MOV_OR_TIMER = 11


class IntAluMode(IntEnum):
    ALU = 0
    CMP = 1
    MINMAX = 2
    SPECIAL = 3


class IntFmt(IntEnum):
    U8 = 0
    S8 = 1
    U16 = 2
    S16 = 3
    U32 = 4
    S32 = 5
    U64 = 6
    S64 = 7


class IntResFmt(IntEnum):
    BITS8 = 0
    BITS16 = 1
    BITS32 = 2
    BITS64 = 3
    F16_TO_U8 = 4
    UNORM8_TO_F16 = 5


class DmaOp(IntEnum):
    MEMBARRIER0 = 0
    MEMBARRIER1 = 1
    MEMBARRIER2 = 2
    MEMBARRIER3 = 3
    ST = 4
    LD = 5
    ATOM32 = 6
    ATOM64 = 7
    IDF = 8
    OP9 = 9
    OP10 = 10


class AtomOp(IntEnum):
    ADD = 0
    SUB = 1
    XCHG = 2
    CMPXCHG = 3
    UMIN = 4
    IMIN = 5
    UMAX = 6
    IMAX = 7
    AND = 8
    OR = 9
    XOR = 10


# Complete 128-entry Volcanic Special Constant (SC) hardware table (bank 8)
SC_TABLE: Tuple[int, ...] = (
    0x00000000, 0x3F800000, 0x40003C00, 0x42003800,
    0x34000000, 0x1C000400, 0xBC004000, 0x49004400,
    0x4C005BF8, 0x3E004600, 0xBC004500, 0x48004D00,
    0x3C000000, 0x5C002C00, 0x80000000, 0x38003C00,
    0xC4804900, 0x30000001, 0x42485800, 0x3B461C04,
    0x10002E66, 0x3A003266, 0xB8004700, 0x389E0100,
    0x54000102, 0x28005640, 0x344A3C00, 0x8000C248,
    0x33F83C00, 0xC0002000, 0x211F3555, 0x410021B0,
    0x02041001, 0x00000001, 0x000000FF, 0x00001000,
    0xFFFFFFFF, 0x3F13CD3A, 0x38D1B717, 0x00000010,
    0x40000000, 0xFD400803, 0x3F000000, 0x3F3504F3,
    0x3ED105EC, 0x00000002, 0x00000008, 0x3DCCCCCD,
    0x00000018, 0x3E4CCCCD, 0x00000003, 0x3A83126F,
    0x0000FFFF, 0x3B808081, 0x3F5105EC, 0x20803C0C,
    0x3E99999A, 0x3C008081, 0xBF800000, 0x00000007,
    0x3D4CCCCD, 0x00000004, 0x3C23D70A, 0x3EAAAAAB,
    0x3D6147AE, 0x3ED55555, 0x414EB852, 0x3F870A3D,
    0x3B4D2E1C, 0x41700000, 0x3E800000, 0x41800000,
    0x42A00000, 0x501890A0, 0x3F333333, 0x48804480,
    0x3F19999A, 0x000000C8, 0x0000001F, 0x00000006,
    0x00000005, 0x3FB8AA3B, 0x3FB33333, 0x40400000,
    0x3EA8F5C3, 0x3ECCCCCD, 0x4019999A, 0x3E19999A,
    0x3F72A76F, 0x3D9E8391, 0x3D55891A, 0x3D25AEE6,
    0x3CBC2D0D, 0x3C37457C, 0x3BEE0683, 0x3B85DA0A,
    0x3B46FCA9, 0x3B2DD769, 0x3AFFFFFF, 0x7FFFFFFF,
    0x0730601C, 0x4B80E000, 0x00000F60, 0x400CCCCD,
    0x40A00000, 0x3FC90FDB, 0xFE281470, 0x41000000,
    0x40C90FDB, 0x3AA94F9A, 0x477FFF00, 0x0000000F,
    0x3CCCCCCD, 0x3F4CCCCD, 0x49800006, 0x00010000,
    0x40490FDB, 0x358637BD, 0x6424067C, 0x3CBD9B6C,
    0x3C9CF09C, 0x3C460B7D, 0x3C163B8B, 0x3BD3097D,
    0x3BCFDF10, 0x3B9DAFBE, 0x3B357A76, 0x3B09AA38,
)
SC_LOOKUP: Dict[int, int] = {v: i for i, v in enumerate(SC_TABLE)}

# 17-family max words & 31-bit trailing defaults (VOLCANIC-USC-ISA-MAP.md Sec 1.2)
FAMILY_SPEC: Dict[str, Tuple[int, Tuple[int, int, int, int]]] = {
    "FOP":       (3, (0, 0x00400808, 0x00000400, 0)),
    "DOT8":      (3, (0, 0x04048010, 0x00000000, 0)),
    "INT":       (4, (0, 0x01902008, 0x040C0A98, 0)),
    "BIT_LUT32": (4, (0, 0x78040A61, 0x4060080F, 0x01840301)),
    "BIT_LUT16": (4, (0, 0x25B60800, 0x1A008240, 0x00000200)),
    "MOV":       (4, (0, 0x00000800, 0x00400030, 0)),
    "ITR":       (3, (0, 0x21000080, 0x00002000, 0)),
    "DMA":       (4, (0, 0, 0, 0)),
    "EMI":       (2, (0, 0x0000304A, 0, 0)),
    "RSTORE":    (2, (0, 0, 0, 0)),
    "PSB_WR":    (3, (0, 0x00010030, 0x00001008, 0)),
    "PSB_RD":    (2, (0, 0, 0, 0)),
    "RLOAD":     (2, (0, 0, 0, 0)),
    "SMP":       (4, (0, 0x42400008, 0x000C0100, 0)),
    "AF32":      (2, (0, 0x00000200, 0, 0)),
    "CTRL_BP":   (4, (0, 0, 0, 0)),
    "CTRL":      (4, (0, 0, 0, 0)),
    "COND":      (4, (0, 0x00000020, 0, 0)),
    "TAP":       (2, (0, 0x00040024, 0, 0)),
}


def classify_w0(w0: int) -> str:
    """Classify Volcanic USC family from W0[6:0] (exact 17-family prefix tree)."""
    p7 = w0 & 0x7F
    if p7 & 1:
        return "FOP"
    if (p7 & 0x0F) == 0x00:
        return "INT"
    if (p7 & 0x0F) == 0x06:
        return "BIT_LUT32"
    if p7 == 0x3A:
        return "EMI"
    if p7 == 0x3E:
        return "SMP"
    if p7 == 0x3C:
        return "TAP"
    p6 = p7 & 0x3F
    return {
        0x18: "DOT8",
        0x38: "BIT_LUT16",
        0x0A: "MOV",
        0x0C: "ITR",
        0x0E: "DMA",
        0x02: "RSTORE",
        0x2A: "PSB_WR",
        0x2C: "PSB_RD",
        0x12: "RLOAD",
        0x1A: "AF32",
        0x08: "CTRL_BP",
        0x1C: "CTRL",
        0x1E: "COND",
    }.get(p6, "INVALID")


# ---------------------------------------------------------------------------
# Bit scatter/gather and unified slot bank-offset codecs
# ---------------------------------------------------------------------------

def _vget(w: Sequence[int], bmap: Sequence[Tuple[int, int]]) -> int:
    val = 0
    for i, (wi, b) in enumerate(bmap):
        val |= ((w[wi] >> b) & 1) << i
    return val


def _vput(w: List[int], bmap: Sequence[Tuple[int, int]], val: int) -> None:
    for i, (wi, b) in enumerate(bmap):
        if (val >> i) & 1:
            w[wi] |= (1 << b)
        else:
            w[wi] &= ~(1 << b)


def _bit_set(w: List[int], wi: int, b: int, flag: bool) -> None:
    if flag:
        w[wi] |= (1 << b)
    else:
        w[wi] &= ~(1 << b)


def _pack_elide(fam: str, w: Sequence[int], min_words: int = 1) -> List[int]:
    max_w, defs = FAMILY_SPEC[fam]
    floor_w = max(1, min(min_words, max_w))
    length = max_w
    while length > floor_w and (w[length - 1] & PAYLOAD_MASK) == defs[length - 1]:
        length -= 1
    out = [w[i] & PAYLOAD_MASK for i in range(length)]
    out[-1] |= EOI_BIT
    return out


SlotRanges = Tuple[Tuple[Bank, int, int], ...]


def _slot_decode(ranges: SlotRanges, packed: int) -> Optional[Reg]:
    for bank, base, max_idx in ranges:
        if base <= packed <= base + max_idx:
            return Reg(bank, packed - base)
    return None


def _slot_encode(ranges: SlotRanges, reg: Reg) -> int:
    for bank, base, max_idx in ranges:
        if bank == reg.bank and 0 <= reg.index <= max_idx:
            return base + reg.index
    raise ValueError(f"Register {reg} not legal in target operand slot")


CC_SWAPPED_DEC = (CC.ALWAYS, CC.NOT_P0, CC.EXEC_MASK, CC.P0)
CC_SWAPPED_ENC = (0, 2, 1, 3)
SF_STD_DEC = (2, 1, 0, 3)  # raw -> canonical (2=NONE, 0=SF0, 1=SF1)
SF_STD_ENC = (2, 1, 0, 3)
SF_FOP_DEC = (2, 0, 1, 3)
SF_FOP_ENC = (1, 2, 0, 3)

# Slot range tables
FOP_DST0_RANGES: SlotRanges = (
    (Bank.TEMP, 0x000, 255),
    (Bank.VI, 0x100, 127),
    (Bank.SLOT, 0x180, 15),
    (Bank.NONE, 0x1A0, 0),
    (Bank.INTERNAL, 0x1C0, 7),
)
FOP_SRC_RANGES: SlotRanges = (
    (Bank.SH, 0x000, 511),
    (Bank.VI, 0x200, 127),
    (Bank.SLOT, 0x300, 15),
    (Bank.TEMP, 0x400, 255),
    (Bank.INTERNAL, 0x500, 7),
    (Bank.SC, 0x600, 127),
)
AF32_RANGES: SlotRanges = (
    (Bank.TEMP, 0x000, 255),
    (Bank.VI, 0x100, 127),
    (Bank.SLOT, 0x180, 15),
)
INT_DST0_RANGES: SlotRanges = (
    (Bank.SH, 0x000, 511),
    (Bank.TEMP, 0x200, 255),
    (Bank.VI, 0x300, 127),
    (Bank.SLOT, 0x380, 15),
    (Bank.NONE, 0x3A0, 0),
    (Bank.INTERNAL, 0x3C0, 7),
)
INT_DST1_RANGES: SlotRanges = (
    (Bank.SLOT, 0x00, 15),
    (Bank.INTERNAL, 0x10, 7),
    (Bank.BIT_PRED_DST, 0x18, 0),
    (Bank.NONE, 0x1C, 0),
)
INT_SRC01_RANGES: SlotRanges = (
    (Bank.SH, 0x000, 511),
    (Bank.VI, 0x200, 127),
    (Bank.SR, 0x300, 63),
    (Bank.TEMP, 0x400, 255),
    (Bank.SLOT, 0x500, 15),
    (Bank.SC, 0x600, 127),
    (Bank.INTERNAL, 0x700, 7),
)
INT_SRC2_RANGES: SlotRanges = (
    (Bank.TEMP, 0x000, 127),
    (Bank.VI, 0x080, 127),
    (Bank.SC, 0x100, 127),
    (Bank.SR, 0x180, 63),
    (Bank.SLOT, 0x1C0, 15),
    (Bank.INTERNAL, 0x1E0, 7),
)
INT_SRC3_RANGES: SlotRanges = (
    (Bank.SLOT, 0x00, 15),
    (Bank.INTERNAL, 0x10, 7),
    (Bank.SC, 0x18, 0),
)
BIT_SRC2_RANGES: SlotRanges = (
    (Bank.TEMP, 0x000, 255),
    (Bank.SC, 0x100, 127),
    (Bank.SLOT, 0x180, 15),
    (Bank.VI, 0x200, 127),
    (Bank.INTERNAL, 0x280, 7),
    (Bank.SR, 0x300, 63),
)
MOV_SRC_RANGES: SlotRanges = (
    (Bank.CS, 0x000, 1023),
    (Bank.TEMP, 0x400, 255),
    (Bank.VI, 0x600, 127),
    (Bank.SH, 0x800, 511),
    (Bank.EMC, 0xA00, 0),
    (Bank.CF, 0xC00, 131),
)
MOV_DST_RANGES: SlotRanges = (
    (Bank.CS, 0x000, 1023),
    (Bank.TEMP, 0x400, 255),
    (Bank.VI, 0x500, 127),
    (Bank.EMC, 0x580, 0),
    (Bank.CF, 0x600, 131),
    (Bank.SLOT, 0x700, 15),
    (Bank.NONE, 0x780, 0),
)
MOV_IDXADDR_RANGES: SlotRanges = (
    (Bank.IMM, 0x0000, 28671),
    (Bank.TEMP, 0x7000, 255),
    (Bank.NONE, 0x7800, 0),
)
MOV_IDXMOD_RANGES: SlotRanges = (
    (Bank.IMM, 0x000, 1023),
    (Bank.SH, 0x400, 511),
    (Bank.TEMP, 0x600, 255),
    (Bank.VI, 0x700, 127),
    (Bank.SLOT, 0x780, 15),
)
MOV_CONVFMT_RANGES: SlotRanges = (
    (Bank.SH, 0x000, 511),
    (Bank.IMM, 0x200, 255),
    (Bank.SLOT, 0x300, 15),
)
MOV_BSTLEN_RANGES: SlotRanges = (
    (Bank.SH, 0x000, 511),
    (Bank.IMM, 0x200, 63),
    (Bank.SLOT, 0x300, 15),
)
DMA_DATA_RANGES: SlotRanges = (
    (Bank.CS, 0x000, 1023),
    (Bank.SH, 0x400, 511),
    (Bank.TEMP, 0x600, 255),
    (Bank.VI, 0x700, 127),
    (Bank.NONE, 0x780, 0),
)
DMA_IDX_RANGES: SlotRanges = (
    (Bank.SLOT, 0x00, 15),
    (Bank.NONE, 0x10, 0),
)
DMA_ADDRBASE_RANGES: SlotRanges = (
    (Bank.SH, 0x000, 511),
    (Bank.TEMP, 0x200, 255),
    (Bank.VI, 0x300, 127),
    (Bank.SLOT, 0x380, 15),
    (Bank.NONE, 0x3A0, 0),
    (Bank.DMA_GLOBAL, 0x3C0, 0),
)
DMA_ADDROFF_RANGES: SlotRanges = (
    (Bank.TEMP, 0x000, 255),
    (Bank.VI, 0x100, 127),
    (Bank.NONE, 0x180, 0),
)
DMA_DST_RANGES: SlotRanges = (
    (Bank.CS, 0x000, 1023),
    (Bank.SH, 0x400, 511),
    (Bank.TEMP, 0x600, 255),
    (Bank.VI, 0x700, 127),
    (Bank.POUT, 0x780, 0),
    (Bank.NONE, 0x7C0, 0),
)
DMA_STRIDE_RANGES: SlotRanges = (
    (Bank.IMM, 0x000, 2047),
    (Bank.SH, 0x800, 511),
    (Bank.VI, 0xA00, 127),
    (Bank.NONE, 0xB00, 0),
    (Bank.TEMP, 0xC00, 255),
    (Bank.SLOT, 0xE00, 15),
)
DMA_AUX_RANGES: SlotRanges = (
    (Bank.SH, 0x000, 511),
    (Bank.SLOT, 0x200, 15),
    (Bank.NONE, 0x300, 0),
)

# Exact bitmaps per family
CTRL_IMM_MAP = (
    (0, 16), (0, 14), (0, 13), (0, 15), (0, 17), (0, 18), (0, 28), (1, 7),
    (1, 9), (1, 12), (1, 11), (1, 8), (1, 6), (1, 5), (1, 4), (1, 3),
    (0, 30), (0, 29), (0, 26), (0, 19), (0, 21), (0, 24), (0, 23), (0, 22),
    (0, 20), (0, 27), (0, 25), (1, 0), (1, 2), (1, 1), (1, 17), (1, 19),
)

FOP_DST0_BITS = ((0, 4), (0, 16), (0, 24), (1, 1), (1, 10), (1, 13), (0, 18), (0, 8), (0, 5))
FOP_SRC0_BITS = ((0, 6), (0, 12), (0, 20), (0, 19), (0, 29), (0, 28), (1, 14), (2, 0), (0, 30), (0, 11), (0, 15))
FOP_SRC1_BITS = ((0, 22), (1, 0), (1, 6), (1, 8), (1, 18), (1, 26), (2, 7), (2, 18), (1, 2), (0, 1), (1, 11))
FOP_SRC2_BITS = ((0, 14), (0, 21), (0, 25), (0, 27), (1, 5), (1, 7), (1, 19), (2, 2), (1, 4), (0, 2), (1, 3))
FOP_OP_BITS = ((0, 7), (0, 13), (0, 17), (1, 30))
FOP_CC_BITS = ((1, 27), (2, 10))
FOP_SF_BITS = ((0, 23), (1, 9))
FOP_FSET_BITS = ((2, 1), (2, 4), (2, 5))
FOP_FWAIT_BITS = ((1, 23), (1, 24))
FOP_TST_BITS = ((1, 12), (1, 16), (1, 17))
FOP_CMP_BITS = ((1, 21), (1, 29))
FOP_D0FMT_BITS = ((0, 3), (2, 8))
FOP_D1FMT_BITS = ((2, 9), (2, 12))
FOP_S0FMT_BITS = ((0, 9), (2, 17))
FOP_S1FMT_BITS = ((0, 10), (2, 16))
FOP_S2FMT_BITS = ((0, 26), (2, 15))
FOP_OP_TO_RAW = (0x0, 0x1, 0x2, 0x3, 0x8, 0x9, 0xA, 0xB, 0x4, 0x5, 0x6, 0x7)
FOP_RAW_TO_OP = (0, 1, 2, 3, 8, 9, 10, 11, 4, 5, 6, 7)
FOP_FMT_SWAP = (0, 2, 1, 3)
FOP_TST_SWAP = (0, 2, 1, 3, 4, 6, 5, 7)

AF32_DST_BITS = ((0, 7), (0, 8), (0, 16), (0, 20), (0, 30), (1, 2), (1, 6), (0, 26), (0, 23))
AF32_SRC_BITS = ((0, 9), (0, 13), (0, 19), (0, 22), (1, 1), (1, 4), (1, 7), (0, 29), (0, 27))
AF32_OP_BITS = ((0, 11), (0, 17), (0, 25), (1, 0))
AF32_CC_BITS = ((1, 5), (1, 9))
AF32_SF_BITS = ((0, 14), (1, 11))
AF32_FSET_BITS = ((0, 21), (0, 24), (0, 28))
AF32_FWAIT_BITS = ((0, 10), (0, 15))
AF32_DFMT_BITS = ((0, 18), (1, 12))
AF32_OP_TO_RAW = (0x0, 0x2, 0x4, 0x6, 0x8, 0xA, 0xC, 0xE, 0x1, 0x3, 0x5, 0x7, 0x9)
AF32_RAW_TO_OP = {raw: i for i, raw in enumerate(AF32_OP_TO_RAW)}

INT_DST0_BITS = ((0, 19), (1, 9), (1, 21), (1, 27), (2, 6), (1, 14), (1, 11), (0, 9), (0, 4), (2, 3))
INT_DST1_BITS = ((2, 28), (3, 0), (1, 24), (2, 4), (2, 11))
INT_SRC0_BITS = ((0, 6), (0, 16), (0, 17), (1, 4), (1, 16), (0, 27), (2, 14), (2, 24), (0, 24), (0, 7), (0, 28))
INT_SRC1_BITS = ((0, 15), (1, 6), (1, 5), (1, 8), (1, 15), (1, 22), (2, 2), (2, 12), (0, 30), (0, 5), (0, 20))
INT_SRC2_BITS = ((1, 10), (1, 29), (1, 25), (2, 10), (2, 5), (0, 22), (1, 19), (1, 2), (1, 23))
INT_SRC3_BITS = ((2, 25), (3, 5), (3, 25), (2, 7), (2, 19))
INT_CC_BITS = ((2, 1), (2, 26))
INT_SF_BITS = ((0, 18), (1, 13))
INT_FSET_BITS = ((2, 22), (2, 23), (2, 30))
INT_FWAIT_BITS = ((0, 29), (1, 7))
INT_OP_BITS = ((0, 8), (0, 25), (1, 26), (1, 30))
INT_MOVC_BITS = ((3, 3), (3, 17), (3, 18))
INT_ALUMODE_BITS = ((0, 12), (0, 21))
INT_RESFMT_BITS = ((0, 13), (1, 3), (3, 21))
INT_SUBMODE_BITS = ((3, 11), (3, 16))
INT_S0FMT_BITS = ((0, 10), (2, 8), (2, 9))
INT_S1FMT_BITS = ((0, 14), (0, 23), (2, 13))
INT_S2FMT_BITS = ((1, 0), (2, 0), (2, 27))
INT_S3FMT_BITS = ((2, 16), (2, 17), (3, 8))
INT_S0ELEM_BITS = ((3, 1), (3, 7))
INT_S1ELEM_BITS = ((3, 6), (3, 10))
INT_S2ELEM_BITS = ((3, 12), (3, 15))
INT_S12FMT_ENC = (0, 2, 4, 6, 1, 3, 5, 7)
INT_S12FMT_DEC = (0, 4, 1, 5, 2, 6, 3, 7)
INT_S3FMT_ENC = (0, 1, 4, 5, 2, 3)
INT_S3FMT_DEC = (0, 1, 4, 5, 2, 3)
INT_MOVC_ENC = (0x0, 0x4, 0x1, 0x5, 0x2)
INT_MOVC_DEC = {v: i for i, v in enumerate(INT_MOVC_ENC)}

BIT06_DST0_BITS = ((1, 4), (1, 10), (1, 8), (1, 16), (1, 17), (0, 7), (0, 6), (1, 5), (1, 6), (1, 18))
BIT06_SRC0_BITS = ((1, 2), (2, 23), (2, 24), (2, 25), (2, 26), (1, 3), (2, 27), (2, 28), (2, 29), (2, 30), (3, 0))
BIT06_SRC1_BITS = ((0, 4), (3, 1), (3, 2), (3, 3), (3, 4), (0, 5), (3, 5), (3, 6), (3, 7), (3, 8), (3, 9))
BIT06_SRC2_BITS = tuple((3, 10 + i) for i in range(10))
BIT06_SRC3_BITS = tuple((3, 20 + i) for i in range(5))
BIT06_LUT_BITS = (
    (1, 19), (1, 20), (0, 9), (0, 10), (1, 21), (1, 22), (0, 11), (0, 12),
    (1, 23), (1, 24), (0, 13), (0, 14), (1, 25), (1, 26), (0, 15), (0, 16),
    (0, 17), (1, 27), (0, 18), (1, 28), (0, 19), (1, 29), (0, 20), (1, 30),
    (0, 21), (2, 0), (0, 22), (2, 1), (0, 23), (2, 2), (0, 24), (2, 3),
)
BIT06_CC_BITS = ((1, 11), (1, 12))
BIT06_SF_BITS = ((0, 30), (1, 9))
BIT06_FSET_BITS = ((1, 7), (3, 26), (3, 27))
BIT06_FWAIT_BITS = ((1, 0), (1, 1))
BIT06_SFT1_BITS = ((2, 4), (2, 5), (2, 6), (2, 7))
BIT06_MASKM_BITS = ((0, 25), (2, 8), (2, 9), (2, 10))
BIT06_SFT2_BITS = ((2, 16), (2, 17), (2, 18), (2, 19))
BIT06_TST0_BITS = ((0, 26), (0, 27), (2, 20))
BIT06_SRCSEL_BITS = ((2, 21), (2, 22))
BIT06_CNTOP_BITS = ((1, 14), (1, 15))
BIT06_SHC_BITS = ((2, 12), (2, 13), (2, 14))

BIT38_DST0_BITS = ((0, 8), (0, 14), (0, 19), (1, 1), (1, 9), (1, 10), (1, 8), (0, 20), (0, 16), (0, 6))
BIT38_DST1_BITS = ((3, 5), (3, 12), (2, 27), (2, 28), (3, 9))
BIT38_SRC0_BITS = ((0, 13), (0, 17), (1, 2), (0, 29), (1, 3), (1, 7), (1, 16), (3, 2), (0, 7), (0, 9), (1, 20))
BIT38_SRC1_BITS = ((0, 25), (1, 4), (1, 12), (1, 15), (2, 1), (2, 10), (3, 7), (3, 13), (1, 5), (0, 15), (1, 21))
BIT38_SRC2_BITS = ((1, 22), (1, 19), (1, 25), (2, 20), (2, 16), (2, 8), (3, 10), (2, 0), (1, 17), (2, 12))
BIT38_SRC3_BITS = ((2, 19), (3, 1), (3, 21), (1, 29), (2, 15))
BIT38_CC_BITS = ((1, 14), (2, 6))
BIT38_SF_BITS = ((0, 10), (0, 23))
BIT38_FSET_BITS = ((2, 7), (2, 11), (2, 13))
BIT38_FWAIT_BITS = ((0, 11), (0, 18))
BIT38_SHC_BITS = ((1, 13), (2, 21), (3, 0))
BIT38_SHC_SWAP = (0, 2, 1, 3, 4, 6, 5, 7)

MOV_SRC_BITS = ((0, 18), (0, 22), (0, 21), (0, 25), (1, 7), (1, 18), (1, 26), (1, 28), (2, 0), (1, 19), (0, 7), (0, 13))
MOV_DST_BITS = ((0, 9), (0, 10), (0, 23), (0, 24), (1, 15), (1, 24), (1, 27), (1, 29), (0, 14), (1, 5), (0, 30))
MOV_IDXADDR_BITS = ((0, 27), (0, 26), (0, 28), (1, 0), (1, 2), (1, 6), (1, 8), (1, 14), (1, 23), (1, 1), (1, 30), (0, 6), (0, 15), (0, 16), (0, 17))
# Verified against shipping DXT-48-1536 rgx.sh MOV vectors:
# 11b w2[23]..w2[12] is idx_mod (0 in shipping MOVs); 10b w1[22]..w2[5] is conv_fmt (0x255=U32 unpack, 0x232=U32 pack, 0x200=F32 identity)
MOV_IDXMOD_BITS = ((2, 23), (2, 10), (2, 24), (2, 25), (2, 26), (2, 27), (2, 11), (2, 28), (2, 29), (2, 30), (2, 12))
MOV_CONVFMT_BITS = ((1, 22), (1, 4), (1, 17), (1, 21), (1, 12), (2, 3), (1, 16), (2, 8), (2, 9), (2, 5))
MOV_BSTLEN_BITS = ((1, 11), (1, 25), (1, 20), (2, 16), (2, 17), (2, 18), (2, 19), (2, 20), (2, 21), (2, 22))
MOV_CC_BITS = ((2, 1), (2, 4))
MOV_SF_BITS = ((0, 11), (0, 19))
MOV_FSET_BITS = ((1, 3), (1, 9), (1, 13))
MOV_FWAIT_BITS = ((0, 20), (0, 29))
MOV_ELEMSZ_BITS = ((2, 2), (2, 6))
MOV_MODE_BITS = ((0, 12), (1, 10))
MOV_TSTOP_BITS = ((2, 13), (2, 14), (3, 1), (3, 2))


@dataclass
class DecodedInst:
    pc: int  # dword index
    length: int  # 1..4 dwords
    family: str
    raw_words: Tuple[int, ...]
    fields: Dict[str, object] = field(default_factory=dict)

    def format_asm(self) -> str:
        f = self.fields
        fam = self.family
        if fam == "CTRL":
            op = CtrlOp(f["op"]).name
            if f["op"] == CtrlOp.BR:
                rel = f["rel_offset"]
                return f"CTRL.BR bcc={f['bcc']} rel={rel:+d} (->@{self.pc + rel}) fw={f['fence_wait']}"
            if f["op"] == CtrlOp.LIMM:
                return f"CTRL.LIMM sl{f['dst']} = 0x{f['imm']:08x}"
            if f["op"] == CtrlOp.END:
                return f"CTRL.END fw={f['fence_wait']}"
            if f["op"] == CtrlOp.NOP:
                return f"CTRL.NOP ({self.length}w) fw={f['fence_wait']}"
            if f["op"] == CtrlOp.WGBARRIER:
                return f"CTRL.WGBARRIER fw={f['fence_wait']}"
            if f["op"] == CtrlOp.MUTEX:
                return f"CTRL.MUTEX id={f['mutex_id']} {'LOCK' if f['flag_a'] else 'REL'}"
            return f"CTRL.{op} dst=sl{f['dst']} imm=0x{f['imm']:x} bcc={f['bcc']}"
        if fam == "COND":
            op = CondOp(f["op"]).name
            return f"COND.{op} cc={CC(f['cc']).name} cc2={f['cc2']} neg0={int(f['neg0'])} adj={f['adj']}"
        if fam == "INT":
            return (
                f"INT op={IntOp(f['op']).name} mode={IntAluMode(f['alu_mode']).name} "
                f"rf={IntResFmt(f['res_fmt']).name} d0={f['dst0']} d1={f['dst1']} "
                f"s0={f['src0']} s1={f['src1']} s2={f['src2']} cc={CC(f['cc']).name}"
            )
        if fam == "FOP":
            return (
                f"FOP.{FopOp(f['op']).name} d0={f['dst0']} s0={f['src0']} s1={f['src1']} "
                f"s2={f['src2']} cc={CC(f['cc']).name} fw={f['fence_wait']}"
            )
        if fam == "AF32":
            return f"AF32.{Af32Op(f['op']).name} d={f['dst']} s={f['src']} cc={CC(f['cc']).name}"
        if fam in ("BIT_LUT32", "BIT_LUT16"):
            return (
                f"{fam} lut=0x{f['lut']:08x} sh_c={f['sh_c']} d0={f['dst0']} "
                f"s0={f['src0']} s1={f['src1']} s2={f['src2']} cc={CC(f['cc']).name}"
            )
        if fam == "MOV":
            return f"MOV d={f['dst']} s={f['src']} fmt={f['conv_fmt']} bst={f['bst_len']} cc={CC(f['cc']).name}"
        if fam == "DMA":
            opn = DmaOp(f["op"]).name
            extra = f" atom={AtomOp(f['atomop']).name}" if f["op"] in (DmaOp.ATOM32, DmaOp.ATOM64) else ""
            return (
                f"DMA.{opn}{extra} data={f['data']} base={f['addr_base']} off={f['addr_off']} "
                f"dst={f['dst']} bytes={f['burst_bytes']} fset={f['fence_set']}"
            )
        return f"{fam} ({self.length}w)"


# ---------------------------------------------------------------------------
# Stream Framer & Full Instruction Decoder
# ---------------------------------------------------------------------------

def decode_stream(words: Sequence[int]) -> Tuple[List[DecodedInst], List[str]]:
    """Frame and decode a Volcanic USC dword stream strictly per hardware ISA spec."""
    insts: List[DecodedInst] = []
    errors: List[str] = []
    n = len(words)
    pc = 0
    while pc < n:
        w0 = int(words[pc]) & 0xFFFFFFFF
        fam = classify_w0(w0)
        if fam == "INVALID":
            errors.append(f"@{pc}: invalid W0[6:0] prefix 0x{w0 & 0x7F:02x} (W0=0x{w0:08x})")
            break
        max_w, defs = FAMILY_SPEC[fam]
        end = pc
        while end < n and (end - pc) < 4 and not (words[end] & EOI_BIT):
            end += 1
        if end >= n or not (words[end] & EOI_BIT):
            errors.append(f"@{pc}: unterminated {fam} packet (no EOI bit 31 within 4 dwords)")
            break
        length = (end - pc) + 1
        if length > max_w:
            errors.append(f"@{pc}: {fam} packet length {length} exceeds max_words {max_w}")
            break
        raw = tuple(int(words[pc + i]) & 0xFFFFFFFF for i in range(length))
        w = [(raw[i] & PAYLOAD_MASK) if i < length else defs[i] for i in range(4)]

        try:
            fields = _decode_family(fam, w, pc, length)
        except ValueError as exc:
            errors.append(f"@{pc} ({fam}): {exc}")
            break

        insts.append(DecodedInst(pc=pc, length=length, family=fam, raw_words=raw, fields=fields))
        pc += length
    return insts, errors


def _decode_family(fam: str, w: List[int], pc: int, length: int) -> Dict[str, object]:
    if fam == "CTRL":
        op = (w[0] >> 6) & 0xF
        if op > 13 or op == 8:
            raise ValueError(f"invalid CTRL op={op}")
        bcc = ((w[1] >> 10) & 1) | (((w[0] >> 12) & 1) << 1) | (((w[1] >> 15) & 1) << 2)
        if bcc > 6:
            raise ValueError(f"invalid CTRL bcc={bcc}")
        if (w[2] >> 2) != 0 or w[3] != 0:
            raise ValueError(f"non-zero reserved bits in CTRL W2/W3 (W2=0x{w[2]:08x}, W3=0x{w[3]:08x})")
        imm = _vget(w, CTRL_IMM_MAP)
        rel = imm - (1 << 30) if (imm & (1 << 29)) else (imm & 0x3FFFFFFF)
        return {
            "op": op,
            "fence_wait": (w[0] >> 10) & 0x3,
            "fence_set": ((w[1] >> 18) & 1) | (((w[1] >> 20) & 0x3) << 1),
            "bcc": bcc,
            "dst": ((w[1] >> 14) & 1) | (((w[1] >> 16) & 1) << 1) | ((w[2] & 0x3) << 2),
            "link": bool((w[1] >> 13) & 1),
            "phas_end": bool((w[1] >> 22) & 1),
            "flag_b": bool((w[1] >> 23) & 1),
            "mutex_id": (w[1] >> 24) & 0xF,
            "flag_a": bool((w[1] >> 28) & 1),
            "trigger": bool((w[1] >> 29) & 1),
            "phase_rate": bool((w[1] >> 30) & 1),
            "imm": imm,
            "rel_offset": rel,
        }

    if fam == "CTRL_BP":
        return {
            "op": int(CtrlOp.BREAKPOINT),
            "fence_wait": (w[0] >> 6) & 0x3,
            "fence_set": (w[0] >> 10) & 0x7,
            "bp_imm": (w[0] >> 13) & 0x3FFFF,
        }

    if fam == "COND":
        op = ((w[0] >> 10) & 1) | (((w[0] >> 15) & 1) << 1) | (((w[0] >> 7) & 1) << 2)
        p0 = ((w[0] >> 12) & 0x3) | (((w[0] >> 20) & 1) << 2)
        p1 = ((w[0] >> 18) & 1) | (((w[0] >> 22) & 0x3) << 1)
        sf = ((w[0] >> 8) & 1) | (((w[1] >> 4) & 0x3) << 1)
        if op > 5 or p0 > 6 or p1 > 6 or sf > 5 or (w[1] >> 9) != 0:
            raise ValueError(f"invalid COND fields op={op} p0={p0} p1={p1} sf={sf}")
        adj = (
            ((w[0] >> 21) & 1)
            | (((w[0] >> 19) & 1) << 1)
            | (((w[0] >> 24) & 0x1F) << 2)
            | ((w[1] & 0xF) << 7)
        )
        return {
            "op": op,
            "cc": (w[0] >> 29) & 0x3,
            "cc2": ((w[0] >> 6) & 1) | (((w[0] >> 14) & 1) << 1),
            "sf": sf,
            "fence_wait": ((w[0] >> 9) & 1) | (((w[0] >> 11) & 1) << 1),
            "fence_set": (w[1] >> 6) & 0x7,
            "p0": p0,
            "p1": p1,
            "neg0": bool((w[0] >> 16) & 1),
            "neg1": bool((w[0] >> 17) & 1),
            "adj": adj,
        }

    if fam == "FOP":
        op_raw = _vget(w, FOP_OP_BITS)
        if op_raw >= len(FOP_RAW_TO_OP):
            raise ValueError(f"invalid FOP raw op={op_raw}")
        dst0 = _slot_decode(FOP_DST0_RANGES, _vget(w, FOP_DST0_BITS))
        src0 = _slot_decode(FOP_SRC_RANGES, _vget(w, FOP_SRC0_BITS))
        src1 = _slot_decode(FOP_SRC_RANGES, _vget(w, FOP_SRC1_BITS))
        src2 = _slot_decode(FOP_SRC_RANGES, _vget(w, FOP_SRC2_BITS))
        if None in (dst0, src0, src1, src2):
            raise ValueError("invalid FOP operand slot encoding")
        dst1 = REG_NONE if ((w[1] >> 22) & 1) else Reg(Bank.INTERNAL, ((w[2] >> 3) & 1) | (((w[2] >> 19) & 0x3) << 1))
        return {
            "op": FOP_RAW_TO_OP[op_raw],
            "cc": int(CC_SWAPPED_DEC[_vget(w, FOP_CC_BITS)]),
            "sf": SF_FOP_DEC[_vget(w, FOP_SF_BITS)],
            "fence_set": _vget(w, FOP_FSET_BITS),
            "fence_wait": _vget(w, FOP_FWAIT_BITS),
            "tst_type": FOP_TST_SWAP[_vget(w, FOP_TST_BITS)],
            "cmp_mode": _vget(w, FOP_CMP_BITS),
            "dst0_fmt": FOP_FMT_SWAP[_vget(w, FOP_D0FMT_BITS)],
            "dst1_fmt": _vget(w, FOP_D1FMT_BITS),
            "src0_fmt": FOP_FMT_SWAP[_vget(w, FOP_S0FMT_BITS)],
            "src1_fmt": FOP_FMT_SWAP[_vget(w, FOP_S1FMT_BITS)],
            "src2_fmt": FOP_FMT_SWAP[_vget(w, FOP_S2FMT_BITS)],
            "sat": bool((w[2] >> 11) & 1),
            "rtz": bool((w[2] >> 22) & 1),
            "src0_neg": bool((w[1] >> 28) & 1),
            "src0_abs": bool((w[1] >> 15) & 1),
            "src0_flr": bool((w[2] >> 14) & 1),
            "src2_neg": bool((w[2] >> 6) & 1),
            "src2_abs": bool((w[1] >> 20) & 1),
            "src2_flr": bool((w[1] >> 25) & 1),
            "dst0": dst0,
            "dst1": dst1,
            "src0": src0,
            "src1": src1,
            "src2": src2,
        }

    if fam == "AF32":
        op_raw = _vget(w, AF32_OP_BITS)
        if op_raw not in AF32_RAW_TO_OP:
            raise ValueError(f"invalid AF32 raw op={op_raw}")
        dst = _slot_decode(AF32_RANGES, _vget(w, AF32_DST_BITS))
        src = _slot_decode(AF32_RANGES, _vget(w, AF32_SRC_BITS))
        if dst is None or src is None:
            raise ValueError("invalid AF32 operand slot encoding")
        return {
            "op": AF32_RAW_TO_OP[op_raw],
            "cc": int(CC_SWAPPED_DEC[_vget(w, AF32_CC_BITS)]),
            "sf": SF_STD_DEC[_vget(w, AF32_SF_BITS)],
            "fence_set": _vget(w, AF32_FSET_BITS),
            "fence_wait": _vget(w, AF32_FWAIT_BITS),
            "dst_fmt": _vget(w, AF32_DFMT_BITS),
            "src_f16": bool((w[0] >> 6) & 1),
            "src_elem_hi": bool((w[1] >> 10) & 1),
            "src_abs": bool((w[1] >> 3) & 1),
            "src_neg": bool((w[1] >> 8) & 1),
            "dst": dst,
            "src": src,
        }

    if fam == "INT":
        op = _vget(w, INT_OP_BITS)
        if op > 11:
            raise ValueError(f"invalid INT op={op}")
        dst0 = _slot_decode(INT_DST0_RANGES, _vget(w, INT_DST0_BITS))
        dst1 = _slot_decode(INT_DST1_RANGES, _vget(w, INT_DST1_BITS))
        src0 = _slot_decode(INT_SRC01_RANGES, _vget(w, INT_SRC0_BITS))
        src1 = _slot_decode(INT_SRC01_RANGES, _vget(w, INT_SRC1_BITS))
        src2 = _slot_decode(INT_SRC2_RANGES, _vget(w, INT_SRC2_BITS))
        src3 = _slot_decode(INT_SRC3_RANGES, _vget(w, INT_SRC3_BITS))
        if None in (dst0, dst1, src0, src1, src2, src3):
            raise ValueError("invalid INT operand slot encoding")
        s3f_raw = _vget(w, INT_S3FMT_BITS)
        return {
            "op": op,
            "cc": int(CC_SWAPPED_DEC[_vget(w, INT_CC_BITS)]),
            "sf": SF_STD_DEC[_vget(w, INT_SF_BITS)],
            "fence_set": _vget(w, INT_FSET_BITS),
            "fence_wait": _vget(w, INT_FWAIT_BITS),
            "alu_mode": int(CC_SWAPPED_DEC[_vget(w, INT_ALUMODE_BITS)]),
            "res_fmt": _vget(w, INT_RESFMT_BITS),
            "movc": INT_MOVC_DEC.get(_vget(w, INT_MOVC_BITS), 0),
            "sat": bool((w[3] >> 13) & 1),
            "carry_sel": bool((w[2] >> 15) & 1),
            "sub_mode": _vget(w, INT_SUBMODE_BITS),
            "src0_fmt": _vget(w, INT_S0FMT_BITS),
            "src1_fmt": INT_S12FMT_DEC[_vget(w, INT_S1FMT_BITS)],
            "src2_fmt": INT_S12FMT_DEC[_vget(w, INT_S2FMT_BITS)],
            "src3_fmt": INT_S3FMT_DEC[s3f_raw] if s3f_raw < len(INT_S3FMT_DEC) else 0,
            "src0_elem": _vget(w, INT_S0ELEM_BITS),
            "src1_elem": _vget(w, INT_S1ELEM_BITS),
            "src2_elem": _vget(w, INT_S2ELEM_BITS),
            "src0_neg": bool((w[2] >> 29) & 1),
            "src0_abs": bool((w[2] >> 21) & 1),
            "src1_neg": bool((w[3] >> 4) & 1),
            "src1_abs": bool((w[2] >> 20) & 1),
            "src2_neg": bool((w[3] >> 9) & 1),
            "src2_abs": bool((w[3] >> 2) & 1),
            "dst0": dst0,
            "dst1": dst1,
            "src0": src0,
            "src1": src1,
            "src2": src2,
            "src3": src3,
        }

    if fam == "BIT_LUT32":
        dst0 = _slot_decode(INT_DST0_RANGES, _vget(w, BIT06_DST0_BITS))
        src0 = _slot_decode(INT_SRC01_RANGES, _vget(w, BIT06_SRC0_BITS))
        src1 = _slot_decode(INT_SRC01_RANGES, _vget(w, BIT06_SRC1_BITS))
        src2 = _slot_decode(BIT_SRC2_RANGES, _vget(w, BIT06_SRC2_BITS))
        src3 = _slot_decode(INT_SRC3_RANGES, _vget(w, BIT06_SRC3_BITS))
        if None in (dst0, src0, src1, src2, src3):
            raise ValueError("invalid BIT_LUT32 operand slot encoding")
        return {
            "use_lut32": True,
            "cc": _vget(w, BIT06_CC_BITS),
            "sf": SF_STD_DEC[_vget(w, BIT06_SF_BITS)],
            "fence_set": _vget(w, BIT06_FSET_BITS),
            "fence_wait": _vget(w, BIT06_FWAIT_BITS),
            "sft1_mode": _vget(w, BIT06_SFT1_BITS),
            "mask_mode": _vget(w, BIT06_MASKM_BITS),
            "mask_en": bool((w[2] >> 11) & 1),
            "sft2_mode": _vget(w, BIT06_SFT2_BITS),
            "tst_op0": _vget(w, BIT06_TST0_BITS),
            "src_sel": _vget(w, BIT06_SRCSEL_BITS),
            "flag60": bool((w[0] >> 8) & 1),
            "cnt_en": bool((w[1] >> 13) & 1),
            "cnt_op": _vget(w, BIT06_CNTOP_BITS),
            "sh_c": _vget(w, BIT06_SHC_BITS),
            "lut": _vget(w, BIT06_LUT_BITS),
            "dst0": dst0,
            "dst1": REG_NONE,
            "src0": src0,
            "src1": src1,
            "src2": src2,
            "src3": src3,
        }

    if fam == "BIT_LUT16":
        dst0 = _slot_decode(INT_DST0_RANGES, _vget(w, BIT38_DST0_BITS))
        dst1 = _slot_decode(INT_DST1_RANGES, _vget(w, BIT38_DST1_BITS))
        src0 = _slot_decode(INT_SRC01_RANGES, _vget(w, BIT38_SRC0_BITS))
        src1 = _slot_decode(INT_SRC01_RANGES, _vget(w, BIT38_SRC1_BITS))
        src2 = _slot_decode(BIT_SRC2_RANGES, _vget(w, BIT38_SRC2_BITS))
        src3 = _slot_decode(INT_SRC3_RANGES, _vget(w, BIT38_SRC3_BITS))
        if None in (dst0, dst1, src0, src1, src2, src3):
            raise ValueError("invalid BIT_LUT16 operand slot encoding")
        return {
            "use_lut32": False,
            "cc": int(CC_SWAPPED_DEC[_vget(w, BIT38_CC_BITS)]),
            "sf": SF_STD_DEC[_vget(w, BIT38_SF_BITS)],
            "fence_set": _vget(w, BIT38_FSET_BITS),
            "fence_wait": _vget(w, BIT38_FWAIT_BITS),
            "mask_en": bool((w[1] >> 11) & 1),
            "cnt_en": bool((w[2] >> 26) & 1),
            "sh_c": BIT38_SHC_SWAP[_vget(w, BIT38_SHC_BITS)],
            "lut": 0xCCCCCCCC,  # default pass-through when 0x38 LUT bits are not individually unpacked
            "dst0": dst0,
            "dst1": dst1,
            "src0": src0,
            "src1": src1,
            "src2": src2,
            "src3": src3,
        }

    if fam == "MOV":
        src = _slot_decode(MOV_SRC_RANGES, _vget(w, MOV_SRC_BITS))
        dst = _slot_decode(MOV_DST_RANGES, _vget(w, MOV_DST_BITS))
        idx_addr = _slot_decode(MOV_IDXADDR_RANGES, _vget(w, MOV_IDXADDR_BITS))
        idx_mod = _slot_decode(MOV_IDXMOD_RANGES, _vget(w, MOV_IDXMOD_BITS))
        conv_fmt = _slot_decode(MOV_CONVFMT_RANGES, _vget(w, MOV_CONVFMT_BITS))
        bst_len = _slot_decode(MOV_BSTLEN_RANGES, _vget(w, MOV_BSTLEN_BITS))
        if None in (src, dst, idx_addr, idx_mod, conv_fmt, bst_len):
            raise ValueError("invalid MOV operand slot encoding")
        return {
            "cc": int(CC_SWAPPED_DEC[_vget(w, MOV_CC_BITS)]),
            "sf": SF_STD_DEC[_vget(w, MOV_SF_BITS)],
            "fence_set": _vget(w, MOV_FSET_BITS),
            "fence_wait": _vget(w, MOV_FWAIT_BITS),
            "elem_size": int(CC_SWAPPED_DEC[_vget(w, MOV_ELEMSZ_BITS)]),
            "mov_mode": _vget(w, MOV_MODE_BITS),
            "pck_dir": bool(w[3] & 1),
            "tst_op": _vget(w, MOV_TSTOP_BITS),
            "src": src,
            "dst": dst,
            "idx_addr": idx_addr,
            "idx_mod": idx_mod,
            "conv_fmt": conv_fmt,
            "bst_len": bst_len,
        }

    if fam == "DMA":
        op = (w[0] >> 16) & 0xF
        atomop = (w[0] >> 21) & 0xF
        if op > 10 or atomop > 10:
            raise ValueError(f"invalid DMA op={op} atomop={atomop}")
        data = _slot_decode(DMA_DATA_RANGES, ((w[0] >> 25) & 0x3F) | ((w[1] & 0x1F) << 6))
        idx = _slot_decode(DMA_IDX_RANGES, (w[1] >> 5) & 0x1F)
        base = _slot_decode(DMA_ADDRBASE_RANGES, (w[1] >> 10) & 0x3FF)
        aoff = _slot_decode(DMA_ADDROFF_RANGES, (w[1] >> 20) & 0x1FF)
        dst = _slot_decode(DMA_DST_RANGES, ((w[1] >> 29) & 0x3) | ((w[2] & 0x1FF) << 2))
        stride = _slot_decode(DMA_STRIDE_RANGES, (w[2] >> 9) & 0xFFF)
        burst = _slot_decode(DMA_STRIDE_RANGES, ((w[2] >> 21) & 0x3FF) | ((w[3] & 0x3) << 10))
        ecnt = _slot_decode(DMA_STRIDE_RANGES, (w[3] >> 2) & 0xFFF)
        aux = _slot_decode(DMA_AUX_RANGES, (w[3] >> 16) & 0x3FF)
        if None in (data, idx, base, aoff, dst, stride, burst, ecnt, aux):
            raise ValueError("invalid DMA operand slot encoding")
        return {
            "op": op,
            "atomop": atomop,
            "cc": (w[0] >> 6) & 0x3,
            "sf": (w[0] >> 8) & 0x7,
            "fence_set": (w[0] >> 11) & 0x7,
            "fence_wait": (w[0] >> 14) & 0x3,
            "op_flag": bool((w[0] >> 20) & 1),
            "cache_bypass": bool((w[3] >> 26) & 1),
            "cache_coherent": bool((w[3] >> 27) & 1),
            "persist": (w[3] >> 28) & 0x3,
            "data": data,
            "index": idx,
            "addr_base": base,
            "addr_off": aoff,
            "dst": dst,
            "stride_src": stride,
            "burst_bytes": burst,
            "elem_count": ecnt,
            "aux_state": aux,
        }

    if fam == "DOT8":
        dst0 = _slot_decode(FOP_DST0_RANGES, ((w[0] >> 11) & 0xFF) | (((w[0] >> 19) & 1) << 8))
        dst1 = REG_NONE if ((w[0] >> 6) & 1) else Reg(Bank.INTERNAL, (w[0] >> 20) & 0x7)
        s0_reg = ((w[0] >> 26) & 0x1F) | ((w[1] & 0xF) << 5)
        s0 = _slot_decode(FOP_SRC_RANGES, s0_reg | (((w[1] >> 4) & 0x3) << 9))
        s1 = _slot_decode(FOP_SRC_RANGES, ((w[1] >> 6) & 0x1FF) | (((w[1] >> 15) & 0x3) << 9))
        s2 = _slot_decode(FOP_SRC_RANGES, ((w[1] >> 17) & 0x1FF) | (((w[1] >> 26) & 0x3) << 9))
        if None in (dst0, s0, s1, s2):
            raise ValueError("invalid DOT8 operand slot encoding")
        return {
            "cc": (w[0] >> 9) & 0x3,
            "sign_mode": (w[0] >> 7) & 0x3,
            "fence_set": (w[1] >> 28) & 0x7,
            "fence_wait": w[2] & 0x3,
            "dst0": dst0,
            "dst1": dst1,
            "src0": s0,
            "src1": s1,
            "src2": s2,
        }

    # Remaining graphics-only families (EMI/ITR/PSB_WR/PSB_RD/SMP/TAP/RLOAD/RSTORE): framed cleanly
    return {"raw_w": tuple(w[:length])}


# ---------------------------------------------------------------------------
# Clean-room Volcanic USC Instruction Encoders (for test shaders & round-trips)
# ---------------------------------------------------------------------------

def encode_ctrl(
    op: CtrlOp,
    *,
    imm: int = 0,
    dst: int = 0,
    bcc: int = 0,
    link: bool = False,
    mutex_id: int = 0,
    flag_a: bool = False,
    flag_b: bool = False,
    fence_wait: int = 0,
    fence_set: int = 0,
    min_words: int = 1,
) -> List[int]:
    w = [0x1C, 0, 0, 0]
    w[0] |= (int(op) & 0xF) << 6
    w[0] |= (fence_wait & 0x3) << 10
    _bit_set(w, 0, 12, bool((bcc >> 1) & 1))
    _bit_set(w, 1, 10, bool(bcc & 1))
    _bit_set(w, 1, 15, bool((bcc >> 2) & 1))
    _vput(w, CTRL_IMM_MAP, imm & 0xFFFFFFFF)
    _bit_set(w, 1, 13, link)
    _bit_set(w, 1, 14, bool(dst & 1))
    _bit_set(w, 1, 16, bool((dst >> 1) & 1))
    _bit_set(w, 1, 18, bool(fence_set & 1))
    w[1] |= ((fence_set >> 1) & 0x3) << 20
    _bit_set(w, 1, 23, flag_b)
    w[1] |= (mutex_id & 0xF) << 24
    _bit_set(w, 1, 28, flag_a)
    w[2] |= (dst >> 2) & 0x3
    return _pack_elide("CTRL", w, min_words)


def encode_cond(
    op: CondOp,
    *,
    cc: CC = CC.ALWAYS,
    cc2: int = 0,
    p0: int = 0,
    p1: int = 0,
    neg0: bool = False,
    neg1: bool = False,
    adj: int = 1,
    sf: int = 4,
    fence_wait: int = 0,
    fence_set: int = 0,
    min_words: int = 1,
) -> List[int]:
    w = [0x1E, 0, 0, 0]
    _bit_set(w, 0, 6, bool(cc2 & 1))
    _bit_set(w, 0, 14, bool((cc2 >> 1) & 1))
    _bit_set(w, 0, 10, bool(int(op) & 1))
    _bit_set(w, 0, 15, bool((int(op) >> 1) & 1))
    _bit_set(w, 0, 7, bool((int(op) >> 2) & 1))
    _bit_set(w, 0, 8, bool(sf & 1))
    w[1] |= ((sf >> 1) & 0x3) << 4
    _bit_set(w, 0, 9, bool(fence_wait & 1))
    _bit_set(w, 0, 11, bool((fence_wait >> 1) & 1))
    w[0] |= (p0 & 0x3) << 12
    _bit_set(w, 0, 20, bool((p0 >> 2) & 1))
    _bit_set(w, 0, 16, neg0)
    _bit_set(w, 0, 17, neg1)
    _bit_set(w, 0, 18, bool(p1 & 1))
    w[0] |= ((p1 >> 1) & 0x3) << 22
    _bit_set(w, 0, 21, bool(adj & 1))
    _bit_set(w, 0, 19, bool((adj >> 1) & 1))
    w[0] |= ((adj >> 2) & 0x1F) << 24
    w[1] |= (adj >> 7) & 0xF
    w[0] |= (int(cc) & 0x3) << 29
    w[1] |= (fence_set & 0x7) << 6
    return _pack_elide("COND", w, min_words)


def encode_int(
    op: IntOp,
    dst0: Reg,
    src0: Reg,
    src1: Reg = REG_SC0,
    src2: Reg = REG_SC0,
    *,
    dst1: Reg = REG_NONE,
    src3: Reg = REG_SC0,
    alu_mode: IntAluMode = IntAluMode.ALU,
    res_fmt: IntResFmt = IntResFmt.BITS32,
    src0_fmt: IntFmt = IntFmt.U32,
    src1_fmt: IntFmt = IntFmt.U32,
    src2_fmt: IntFmt = IntFmt.U32,
    src3_fmt: IntFmt = IntFmt.U32,
    cc: CC = CC.EXEC_MASK,
    src0_neg: bool = False,
    src1_neg: bool = False,
    src0_abs: bool = False,
    src1_abs: bool = False,
    sat: bool = False,
    fence_wait: int = 0,
    fence_set: int = 0,
) -> List[int]:
    w = [0, 0, 0, 0]
    _vput(w, INT_CC_BITS, CC_SWAPPED_ENC[int(cc)])
    _vput(w, INT_SF_BITS, SF_STD_ENC[2])
    _vput(w, INT_FSET_BITS, fence_set)
    _vput(w, INT_FWAIT_BITS, fence_wait)
    _vput(w, INT_OP_BITS, int(op))
    _vput(w, INT_MOVC_BITS, INT_MOVC_ENC[0])
    _vput(w, INT_ALUMODE_BITS, CC_SWAPPED_ENC[int(alu_mode)])
    _vput(w, INT_RESFMT_BITS, int(res_fmt))
    _bit_set(w, 3, 13, sat)
    _vput(w, INT_S0FMT_BITS, int(src0_fmt))
    _bit_set(w, 2, 29, src0_neg)
    _bit_set(w, 2, 21, src0_abs)
    _vput(w, INT_S1FMT_BITS, INT_S12FMT_ENC[int(src1_fmt)])
    _bit_set(w, 3, 4, src1_neg)
    _bit_set(w, 2, 20, src1_abs)
    _vput(w, INT_S2FMT_BITS, INT_S12FMT_ENC[int(src2_fmt)])
    _vput(w, INT_S3FMT_BITS, INT_S3FMT_ENC[int(src3_fmt)])
    _vput(w, INT_DST0_BITS, _slot_encode(INT_DST0_RANGES, dst0))
    _vput(w, INT_DST1_BITS, _slot_encode(INT_DST1_RANGES, dst1))
    _vput(w, INT_SRC0_BITS, _slot_encode(INT_SRC01_RANGES, src0))
    _vput(w, INT_SRC1_BITS, _slot_encode(INT_SRC01_RANGES, src1))
    _vput(w, INT_SRC2_BITS, _slot_encode(INT_SRC2_RANGES, src2))
    _vput(w, INT_SRC3_BITS, _slot_encode(INT_SRC3_RANGES, src3))
    return _pack_elide("INT", w, 1)


def encode_fop(
    op: FopOp,
    dst0: Reg,
    src0: Reg,
    src1: Reg = REG_SC0,
    src2: Reg = REG_SC0,
    *,
    cc: CC = CC.EXEC_MASK,
    tst_type: int = 0,
    cmp_mode: int = 0,
    sat: bool = False,
    src0_neg: bool = False,
    src0_abs: bool = False,
    src0_flr: bool = False,
    src2_neg: bool = False,
    fence_wait: int = 0,
    fence_set: int = 0,
) -> List[int]:
    w = [1, 0, 0, 0]
    _vput(w, FOP_CC_BITS, CC_SWAPPED_ENC[int(cc)])
    _vput(w, FOP_SF_BITS, SF_FOP_ENC[2])
    _vput(w, FOP_FSET_BITS, fence_set)
    _vput(w, FOP_FWAIT_BITS, fence_wait)
    _vput(w, FOP_OP_BITS, FOP_OP_TO_RAW[int(op)])
    _vput(w, FOP_TST_BITS, FOP_TST_SWAP[tst_type & 7])
    _vput(w, FOP_CMP_BITS, cmp_mode & 3)
    f32_enc = FOP_FMT_SWAP[int(FopFmt.F32)]
    _vput(w, FOP_D0FMT_BITS, f32_enc)
    _vput(w, FOP_S0FMT_BITS, f32_enc)
    _vput(w, FOP_S1FMT_BITS, f32_enc)
    _vput(w, FOP_S2FMT_BITS, f32_enc)
    _bit_set(w, 2, 11, sat)
    _bit_set(w, 1, 28, src0_neg)
    _bit_set(w, 1, 15, src0_abs)
    _bit_set(w, 2, 14, src0_flr)
    _bit_set(w, 2, 6, src2_neg)
    _bit_set(w, 1, 22, True)  # dst1 = NONE
    _vput(w, FOP_DST0_BITS, _slot_encode(FOP_DST0_RANGES, dst0))
    _vput(w, FOP_SRC0_BITS, _slot_encode(FOP_SRC_RANGES, src0))
    _vput(w, FOP_SRC1_BITS, _slot_encode(FOP_SRC_RANGES, src1))
    _vput(w, FOP_SRC2_BITS, _slot_encode(FOP_SRC_RANGES, src2))
    return _pack_elide("FOP", w, 1)


def encode_af32(
    op: Af32Op,
    dst: Reg,
    src: Reg,
    *,
    cc: CC = CC.EXEC_MASK,
    src_abs: bool = False,
    src_neg: bool = False,
    fence_wait: int = 0,
    fence_set: int = 0,
) -> List[int]:
    w = [0x1A, 0, 0, 0]
    _vput(w, AF32_CC_BITS, CC_SWAPPED_ENC[int(cc)])
    _vput(w, AF32_SF_BITS, SF_STD_ENC[2])
    _vput(w, AF32_FSET_BITS, fence_set)
    _vput(w, AF32_FWAIT_BITS, fence_wait)
    _vput(w, AF32_DFMT_BITS, int(FopFmt.F32))
    _bit_set(w, 1, 3, src_abs)
    _bit_set(w, 1, 8, src_neg)
    _vput(w, AF32_OP_BITS, AF32_OP_TO_RAW[int(op)])
    _vput(w, AF32_DST_BITS, _slot_encode(AF32_RANGES, dst))
    _vput(w, AF32_SRC_BITS, _slot_encode(AF32_RANGES, src))
    return _pack_elide("AF32", w, 1)


def encode_bit06(
    dst0: Reg,
    src0: Reg,
    src1: Reg,
    lut: int,
    *,
    src2: Reg = REG_SC0,
    src3: Reg = REG_SC0,
    sh_c: int = 0,
    cc: CC = CC.EXEC_MASK,
    fence_wait: int = 0,
    fence_set: int = 0,
) -> List[int]:
    w = [0x06, 0, 0, 0]
    _vput(w, BIT06_CC_BITS, int(cc) & 3)
    _vput(w, BIT06_SF_BITS, SF_STD_ENC[2])
    _vput(w, BIT06_FSET_BITS, fence_set)
    _vput(w, BIT06_FWAIT_BITS, fence_wait)
    _vput(w, BIT06_SHC_BITS, sh_c & 7)
    _vput(w, BIT06_LUT_BITS, lut & 0xFFFFFFFF)
    _vput(w, BIT06_DST0_BITS, _slot_encode(INT_DST0_RANGES, dst0))
    _vput(w, BIT06_SRC0_BITS, _slot_encode(INT_SRC01_RANGES, src0))
    _vput(w, BIT06_SRC1_BITS, _slot_encode(INT_SRC01_RANGES, src1))
    _vput(w, BIT06_SRC2_BITS, _slot_encode(BIT_SRC2_RANGES, src2))
    _vput(w, BIT06_SRC3_BITS, _slot_encode(INT_SRC3_RANGES, src3))
    return _pack_elide("BIT_LUT32", w, 1)


def encode_mov(
    dst: Reg,
    src: Reg,
    *,
    conv_fmt: int = 0,
    bst_len: int = 1,
    cc: CC = CC.EXEC_MASK,
    fence_wait: int = 0,
    fence_set: int = 0,
) -> List[int]:
    w = [0x0A, 0, 0, 0]
    _vput(w, MOV_CC_BITS, CC_SWAPPED_ENC[int(cc)])
    _vput(w, MOV_SF_BITS, SF_STD_ENC[2])
    _vput(w, MOV_FSET_BITS, fence_set)
    _vput(w, MOV_FWAIT_BITS, fence_wait)
    _vput(w, MOV_SRC_BITS, _slot_encode(MOV_SRC_RANGES, src))
    _vput(w, MOV_DST_BITS, _slot_encode(MOV_DST_RANGES, dst))
    _vput(w, MOV_IDXADDR_BITS, _slot_encode(MOV_IDXADDR_RANGES, REG_NONE))
    _vput(w, MOV_IDXMOD_BITS, _slot_encode(MOV_IDXMOD_RANGES, Reg(Bank.IMM, 0)))
    _vput(w, MOV_CONVFMT_BITS, _slot_encode(MOV_CONVFMT_RANGES, Reg(Bank.IMM, conv_fmt)))
    _vput(w, MOV_BSTLEN_BITS, _slot_encode(MOV_BSTLEN_RANGES, Reg(Bank.IMM, bst_len)))
    return _pack_elide("MOV", w, 1)


def encode_dma(
    op: DmaOp,
    *,
    addr_base: Reg = REG_NONE,
    addr_off: Reg = REG_NONE,
    data: Reg = REG_NONE,
    dst: Reg = REG_NONE,
    burst_bytes: int = 4,
    atomop: AtomOp = AtomOp.ADD,
    cc: CC = CC.EXEC_MASK,
    fence_wait: int = 0,
    fence_set: int = 1,
    cache_bypass: bool = False,
    cache_coherent: bool = False,
) -> List[int]:
    w = [0x0E, 0, 0, 0]
    w[0] |= (int(cc) & 0x3) << 6
    w[0] |= (fence_set & 0x7) << 11
    w[0] |= (fence_wait & 0x3) << 14
    w[0] |= (int(op) & 0xF) << 16
    w[0] |= (int(atomop) & 0xF) << 21

    d_enc = _slot_encode(DMA_DATA_RANGES, data)
    idx_enc = _slot_encode(DMA_IDX_RANGES, REG_NONE)
    base_enc = _slot_encode(DMA_ADDRBASE_RANGES, addr_base)
    aoff_enc = _slot_encode(DMA_ADDROFF_RANGES, addr_off)
    dst_enc = _slot_encode(DMA_DST_RANGES, dst)
    stride_enc = _slot_encode(DMA_STRIDE_RANGES, Reg(Bank.IMM, 1))
    b_imm = 0 if burst_bytes == 2048 else burst_bytes
    burst_enc = _slot_encode(DMA_STRIDE_RANGES, Reg(Bank.IMM, b_imm))
    ecnt_enc = _slot_encode(DMA_STRIDE_RANGES, Reg(Bank.IMM, 1))
    aux_enc = _slot_encode(DMA_AUX_RANGES, REG_NONE)

    w[0] |= (d_enc & 0x3F) << 25
    w[1] |= (d_enc >> 6) & 0x1F
    w[1] |= (idx_enc & 0x1F) << 5
    w[1] |= (base_enc & 0x3FF) << 10
    w[1] |= (aoff_enc & 0x1FF) << 20
    w[1] |= (dst_enc & 0x3) << 29
    w[2] |= (dst_enc >> 2) & 0x1FF
    w[2] |= (stride_enc & 0xFFF) << 9
    w[2] |= (burst_enc & 0x3FF) << 21
    w[3] |= (burst_enc >> 10) & 0x3
    w[3] |= (ecnt_enc & 0xFFF) << 2
    w[3] |= (aux_enc & 0x3FF) << 16
    _bit_set(w, 3, 26, cache_bypass)
    _bit_set(w, 3, 27, cache_coherent)
    return _pack_elide("DMA", w, 1)


def bytes_to_words(data: bytes) -> List[int]:
    if len(data) % 4 != 0:
        raise ValueError(f"USC binary length {len(data)} is not a multiple of 4 bytes")
    return list(struct.unpack(f"<{len(data) // 4}I", data))


def words_to_bytes(words: Sequence[int]) -> bytes:
    return struct.pack(f"<{len(words)}I", *[int(w) & 0xFFFFFFFF for w in words])
