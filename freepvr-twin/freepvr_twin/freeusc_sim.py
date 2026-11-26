# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 FreePVR Contributors
"""
Clean-room 128-lane PowerVR Volcanic USC Compute Shader Simulator (`freeusc-sim`).

Executes decoded Volcanic USC instruction streams across 128 lanes (`subgroupSize=128`)
with mock 40/64-bit byte-addressed global memory (BDA/SSBO), hardware `sr14` lane ID
(0..127), 16 cross-lane `SLOT` registers (`sl0..sl15`), `CS` workgroup shared memory,
hardware `EMC` execution masks (`COND`), `CTRL` branches/barriers/mutexes/LIMM,
`INT`/`FOP`/`AF32`/`BIT`/`MOV`/`DOT8`/`DMA` (LD/ST/ATOM32/ATOM64/IDF/MEMBARRIER).

Strictly honors `VOLCANIC-USC-ISA-MAP.md` and `VOLCANIC-USC-OPCODES.md` — never fudges
compiler encoding bugs to pass!
"""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .freeusc_isa import (
    SC_TABLE,
    Af32Op,
    AtomOp,
    Bank,
    CC,
    CondOp,
    CtrlOp,
    DecodedInst,
    DmaOp,
    FopOp,
    IntAluMode,
    IntFmt,
    IntOp,
    IntResFmt,
    Reg,
    decode_stream,
)
from .reward import VOLCANIC_USC_DEFAULT_SRAM_LIMIT, Diagnostic, make_verdict

SUBGROUP_SIZE = 128
U32_MASK = 0xFFFFFFFF
U64_MASK = 0xFFFFFFFFFFFFFFFF


def _u32_to_i32(v: int) -> int:
    v &= U32_MASK
    return v - 0x100000000 if (v & 0x80000000) else v


def _u32_to_f32(v: int) -> float:
    return struct.unpack("<f", struct.pack("<I", v & U32_MASK))[0]


def _f32_to_u32(f: float) -> int:
    try:
        return struct.unpack("<I", struct.pack("<f", float(f)))[0]
    except OverflowError:
        return 0x7F800000 if f > 0 else 0xFF800000


def _round_f32(f: float) -> float:
    return _u32_to_f32(_f32_to_u32(f))


class GlobalMemory:
    """Mock 40/64-bit byte-addressed global memory (BDA / SSBO regions)."""

    def __init__(self) -> None:
        self._regions: Dict[int, bytearray] = {}

    def map_region(self, base_va: int, size_bytes: int, init_data: Optional[bytes] = None) -> None:
        buf = bytearray(size_bytes)
        if init_data is not None:
            if len(init_data) > size_bytes:
                raise ValueError("init_data exceeds region size")
            buf[: len(init_data)] = init_data
        self._regions[int(base_va)] = buf

    def _locate(self, va: int, size: int) -> Tuple[bytearray, int]:
        for base, buf in self._regions.items():
            if base <= va and (va + size) <= (base + len(buf)):
                return buf, va - base
        raise MemoryError(f"Unmapped or out-of-bounds global VA 0x{va:012x} (size={size}B)")

    def read_bytes(self, va: int, size: int) -> bytes:
        buf, off = self._locate(va, size)
        return bytes(buf[off : off + size])

    def write_bytes(self, va: int, data: bytes) -> None:
        buf, off = self._locate(va, len(data))
        buf[off : off + len(data)] = data

    def read_u32(self, va: int) -> int:
        buf, off = self._locate(va, 4)
        return struct.unpack_from("<I", buf, off)[0]

    def write_u32(self, va: int, val: int) -> None:
        buf, off = self._locate(va, 4)
        struct.pack_into("<I", buf, off, val & U32_MASK)

    def read_u64(self, va: int) -> int:
        buf, off = self._locate(va, 8)
        return struct.unpack_from("<Q", buf, off)[0]

    def write_u64(self, va: int, val: int) -> None:
        buf, off = self._locate(va, 8)
        struct.pack_into("<Q", buf, off, val & U64_MASK)


@dataclass
class SimResult:
    passed: bool
    halted_cleanly: bool
    steps: int
    inst_counts: Dict[str, int]
    max_temp_reg: int
    max_sh_reg: int
    max_cs_reg: int
    sram_bytes: int
    est_hw_us: float
    fault_reason: Optional[str]
    diagnostics: List[Diagnostic]
    state: "VolcanicUSCState"


class VolcanicUSCState:
    """128-lane Volcanic USC architectural state."""

    def __init__(
        self,
        *,
        num_lanes: int = SUBGROUP_SIZE,
        workgroup_id: Tuple[int, int, int] = (0, 0, 0),
        num_workgroups: Tuple[int, int, int] = (1, 1, 1),
        local_id_base: int = 0,
        sh_init: Optional[Dict[int, int]] = None,
        cs_init: Optional[Dict[int, int]] = None,
        vi_init: Optional[Dict[int, Sequence[int]]] = None,
        memory: Optional[GlobalMemory] = None,
    ) -> None:
        if not (1 <= num_lanes <= SUBGROUP_SIZE):
            raise ValueError(f"num_lanes must be in 1..{SUBGROUP_SIZE}")
        self.num_lanes = num_lanes
        self.mem = memory if memory is not None else GlobalMemory()

        # Per-lane register files (indexed [reg][lane])
        self.temp: List[List[int]] = [[0] * num_lanes for _ in range(256)]
        self.internal: List[List[int]] = [[0] * num_lanes for _ in range(8)]
        self.vi: List[List[int]] = [[0] * num_lanes for _ in range(128)]
        self.sr: List[List[int]] = [[0] * num_lanes for _ in range(64)]
        self.pred: List[List[int]] = [[0] * num_lanes for _ in range(7)]
        self.emc: List[int] = [0] * num_lanes

        # Uniform / shared register files
        self.slot: List[int] = [0] * 16
        self.sh: List[int] = [0] * 512
        self.cs: List[int] = [0] * 1024

        # Initialize Vulkan compute inputs
        wg_x, wg_y, wg_z = workgroup_id
        nwg_x, nwg_y, nwg_z = num_workgroups
        self.cs[0] = wg_x & U32_MASK
        self.cs[1] = wg_y & U32_MASK
        self.cs[2] = wg_z & U32_MASK
        for lane in range(num_lanes):
            lid = (local_id_base + lane) & U32_MASK
            self.vi[0][lane] = lid
            self.sr[0][lane] = wg_x & U32_MASK
            self.sr[1][lane] = wg_y & U32_MASK
            self.sr[2][lane] = wg_z & U32_MASK
            self.sr[14][lane] = lane  # Volcanic sr14 = inst_num (0..127)
            self.sr[56][lane] = lane  # local_addr_inst_num
            self.sr[58][lane] = 0     # cluster_num

        if sh_init:
            for k, v in sh_init.items():
                self.sh[k] = int(v) & U32_MASK
        if cs_init:
            for k, v in cs_init.items():
                self.cs[k] = int(v) & U32_MASK
        if vi_init:
            for k, vals in vi_init.items():
                for lane in range(min(num_lanes, len(vals))):
                    self.vi[k][lane] = int(vals[lane]) & U32_MASK

        # Track peak register usage for SRAM footprint accounting
        self.max_temp_used = 0
        self.max_sh_used = max(sh_init.keys(), default=-1) + 1 if sh_init else 0
        self.max_cs_used = 3
        self.fences_pending: set[int] = set()
        self.mutex_locked: set[int] = set()
        self.wg_barriers: int = 0

    def active_lanes(self, cc: int) -> List[int]:
        if cc == CC.ALWAYS:
            return list(range(self.num_lanes))
        if cc == CC.EXEC_MASK:
            return [l for l in range(self.num_lanes) if self.emc[l] == 0]
        if cc == CC.NOT_P0:
            p0 = self.pred[0]
            return [l for l in range(self.num_lanes) if self.emc[l] == 0 and p0[l] == 0]
        if cc == CC.P0:
            p0 = self.pred[0]
            return [l for l in range(self.num_lanes) if self.emc[l] == 0 and p0[l] != 0]
        return []

    def read_reg(self, reg: Reg, lane: int, offset: int = 0) -> int:
        b = reg.bank
        idx = reg.index + offset
        if b == Bank.TEMP:
            self.max_temp_used = max(self.max_temp_used, idx + 1)
            return self.temp[idx][lane]
        if b == Bank.INTERNAL:
            return self.internal[idx][lane]
        if b == Bank.SLOT:
            return self.slot[idx]
        if b == Bank.VI:
            return self.vi[idx][lane]
        if b == Bank.SH:
            self.max_sh_used = max(self.max_sh_used, idx + 1)
            return self.sh[idx]
        if b == Bank.SC:
            return SC_TABLE[idx]
        if b == Bank.CS:
            self.max_cs_used = max(self.max_cs_used, idx + 1)
            return self.cs[idx]
        if b == Bank.SR:
            return self.sr[idx][lane]
        if b == Bank.EMC:
            return self.emc[lane]
        if b == Bank.IMM:
            return idx & U32_MASK
        if b == Bank.NONE or b == Bank.DMA_GLOBAL:
            return 0
        raise ValueError(f"Unsupported read bank {b}")

    def write_reg(self, reg: Reg, lane: int, val: int, offset: int = 0) -> None:
        v = val & U32_MASK
        b = reg.bank
        idx = reg.index + offset
        if b == Bank.NONE:
            return
        if b == Bank.TEMP:
            self.max_temp_used = max(self.max_temp_used, idx + 1)
            self.temp[idx][lane] = v
        elif b == Bank.INTERNAL:
            self.internal[idx][lane] = v
        elif b == Bank.SLOT:
            self.slot[idx] = v
        elif b == Bank.VI:
            self.vi[idx][lane] = v
        elif b == Bank.SH:
            self.max_sh_used = max(self.max_sh_used, idx + 1)
            self.sh[idx] = v
        elif b == Bank.CS:
            self.max_cs_used = max(self.max_cs_used, idx + 1)
            self.cs[idx] = v
        elif b == Bank.EMC:
            self.emc[lane] = v
        elif b == Bank.BIT_PRED_DST:
            self.pred[0][lane] = 1 if v else 0
        else:
            raise ValueError(f"Unsupported write bank {b}")


def _eval_lut5(lut: int, a0: int, a1: int, a2: int, a3: int, p0_bit: int) -> int:
    """Bit-exact 5-input truth-table LUT across 32 bits: idx = (p0<<4)|(a3<<3)|(a2<<2)|(a1<<1)|a0."""
    # Fast vector bitwise evaluation across all 32 bits simultaneously!
    res = 0
    base = 16 if p0_bit else 0
    # Optimize common 2-input cases (when a2==0 and a3==0)
    if a2 == 0 and a3 == 0:
        nibble = (lut >> base) & 0xF
        if nibble == 0xA:  # in0
            return a0 & U32_MASK
        if nibble == 0xC:  # in1
            return a1 & U32_MASK
        if nibble == 0x8:  # in0 & in1
            return (a0 & a1) & U32_MASK
        if nibble == 0x6:  # in0 ^ in1
            return (a0 ^ a1) & U32_MASK
        if nibble == 0xE:  # in0 | in1
            return (a0 | a1) & U32_MASK
        if nibble == 0x5:  # ~in0
            return (~a0) & U32_MASK
        if nibble == 0x7:  # ~(in0 & in1)
            return (~(a0 & a1)) & U32_MASK
        if nibble == 0x1:  # ~(in0 | in1)
            return (~(a0 | a1)) & U32_MASK
        if nibble == 0x9:  # ~(in0 ^ in1)
            return (~(a0 ^ a1)) & U32_MASK
        if nibble == 0x0:
            return 0
        if nibble == 0xF:
            return U32_MASK

    # Full general 4-input bitwise truth table over all 16 minterms
    na0 = (~a0) & U32_MASK
    na1 = (~a1) & U32_MASK
    na2 = (~a2) & U32_MASK
    na3 = (~a3) & U32_MASK
    sub = (lut >> base) & 0xFFFF
    for k in range(16):
        if (sub >> k) & 1:
            m = a0 if (k & 1) else na0
            m &= a1 if (k & 2) else na1
            m &= a2 if (k & 4) else na2
            m &= a3 if (k & 8) else na3
            res |= m
    return res & U32_MASK


def _apply_bit_post_shift(sh_c: int, val: int, amt_word: int) -> int:
    """Apply ISA_BIT post-shift sh_c (+0x74) to 32-bit LUT output."""
    val &= U32_MASK
    amt = amt_word & 0x1F
    if sh_c == 0:
        return val
    if sh_c == 1:  # LSL
        return (val << amt) & U32_MASK
    if sh_c == 2:  # UBFE (offset=amt_word[4:0], width=amt_word[12:8] or default 32)
        width = (amt_word >> 8) & 0x3F
        if width == 0 or width >= 32:
            return (val >> amt) & U32_MASK
        return (val >> amt) & ((1 << width) - 1)
    if sh_c == 3:  # LSR
        return (val >> amt) & U32_MASK
    if sh_c == 4:  # ASR
        return (_u32_to_i32(val) >> amt) & U32_MASK
    if sh_c == 5:  # IBFE
        width = (amt_word >> 8) & 0x3F
        if width == 0 or width >= 32:
            return (_u32_to_i32(val) >> amt) & U32_MASK
        extracted = (val >> amt) & ((1 << width) - 1)
        if extracted & (1 << (width - 1)):
            extracted |= (~((1 << width) - 1)) & U32_MASK
        return extracted & U32_MASK
    if sh_c == 6:  # ROL
        return ((val << amt) | (val >> ((32 - amt) & 31))) & U32_MASK
    return val


def _apply_atomic(atomop: int, old: int, val: int, cmp_val: int, bits: int) -> int:
    mask = U64_MASK if bits == 64 else U32_MASK
    sign_bit = 1 << (bits - 1)
    old &= mask
    val &= mask

    def to_signed(x: int) -> int:
        return x - (mask + 1) if (x & sign_bit) else x

    if atomop == AtomOp.ADD:
        return (old + val) & mask
    if atomop == AtomOp.SUB:
        return (old - val) & mask
    if atomop == AtomOp.XCHG:
        return val
    if atomop == AtomOp.CMPXCHG:
        return val if old == (cmp_val & mask) else old
    if atomop == AtomOp.UMIN:
        return min(old, val)
    if atomop == AtomOp.IMIN:
        return (min(to_signed(old), to_signed(val))) & mask
    if atomop == AtomOp.UMAX:
        return max(old, val)
    if atomop == AtomOp.IMAX:
        return (max(to_signed(old), to_signed(val))) & mask
    if atomop == AtomOp.AND:
        return (old & val) & mask
    if atomop == AtomOp.OR:
        return (old | val) & mask
    if atomop == AtomOp.XOR:
        return (old ^ val) & mask
    raise ValueError(f"Unsupported atomop={atomop}")


def simulate_words(
    words: Sequence[int],
    state: VolcanicUSCState,
    *,
    max_steps: int = 100_000,
) -> SimResult:
    """Decode and execute a Volcanic USC compute shader across 128 lanes."""
    insts, dec_errs = decode_stream(words)
    diags: List[Diagnostic] = []
    if dec_errs:
        for e in dec_errs:
            diags.append(Diagnostic(code="USC_DECODE_ERROR", severity="error", message=e))
        return SimResult(
            passed=False,
            halted_cleanly=False,
            steps=0,
            inst_counts={},
            max_temp_reg=0,
            max_sh_reg=0,
            max_cs_reg=0,
            sram_bytes=0,
            est_hw_us=0.0,
            fault_reason=f"USC_DECODE_ERROR: {dec_errs[0]}",
            diagnostics=diags,
            state=state,
        )

    pc_to_idx: Dict[int, int] = {inst.pc: idx for idx, inst in enumerate(insts)}
    inst_counts: Dict[str, int] = {}
    steps = 0
    idx = 0
    halted = False
    fault_reason: Optional[str] = None
    warned_pcs: set[int] = set()

    # Latency weights in USC clock cycles (6 USCs @ 1 GHz, 128-wide wave)
    cycle_cost = 0

    try:
        while idx < len(insts):
            if steps >= max_steps:
                fault_reason = f"USC_STEP_LIMIT_EXCEEDED: exceeded {max_steps} dynamic instructions"
                diags.append(Diagnostic(code="USC_STEP_LIMIT_EXCEEDED", severity="error", message=fault_reason, offset=insts[idx].pc * 4))
                break

            inst = insts[idx]
            f = inst.fields
            fam = inst.family
            steps += 1
            inst_counts[fam] = inst_counts.get(fam, 0) + 1

            # Handle inline fence_wait scoreboard clear
            fw = int(f.get("fence_wait", 0))
            if fw:
                state.fences_pending.discard(fw)
                cycle_cost += 4

            if fam == "CTRL":
                cycle_cost += 2
                op = int(f["op"])
                if op == CtrlOp.END:
                    halted = True
                    break
                if op == CtrlOp.NOP:
                    idx += 1
                    continue
                if op == CtrlOp.LIMM:
                    state.slot[int(f["dst"])] = int(f["imm"]) & U32_MASK
                    idx += 1
                    continue
                if op == CtrlOp.WGBARRIER:
                    state.wg_barriers += 1
                    cycle_cost += 16
                    idx += 1
                    continue
                if op == CtrlOp.MUTEX:
                    mid = int(f["mutex_id"])
                    if f["flag_a"]:
                        state.mutex_locked.add(mid)
                    else:
                        state.mutex_locked.discard(mid)
                    idx += 1
                    continue
                if op == CtrlOp.LAPC:
                    state.slot[int(f["dst"])] = (inst.pc + int(f["rel_offset"])) & U32_MASK
                    idx += 1
                    continue
                if op in (CtrlOp.BR, CtrlOp.BA, CtrlOp.BRLT):
                    bcc = int(f["bcc"])
                    take = False
                    if op == CtrlOp.BRLT:
                        dst_sl = int(f["dst"])
                        cnt = (state.slot[dst_sl] - 1) & U32_MASK
                        state.slot[dst_sl] = cnt
                        take = cnt != 0
                    elif bcc == 0:
                        take = True
                    elif bcc == 1:
                        # ALLINST (PCO_BRANCH_CND_ALLINST): branch if NO instances are active (all emc > 0)
                        take = all(e > 0 for e in state.emc)
                    elif bcc == 2:
                        # ANYINST / p0: branch if any active lane has p0 != 0 (or any lane active)
                        take = any(state.emc[l] == 0 and state.pred[0][l] != 0 for l in range(state.num_lanes))
                    else:
                        raise ValueError(f"Unsupported CTRL branch BCC={bcc} at @{inst.pc}")
                    if take:
                        if f["link"]:
                            state.slot[int(f["dst"])] = (inst.pc + inst.length) & U32_MASK
                        target_pc = int(f["imm"]) if op == CtrlOp.BA else (inst.pc + int(f["rel_offset"]))
                        if target_pc not in pc_to_idx:
                            raise ValueError(f"Branch at @{inst.pc} landed on non-instruction dword @{target_pc}")
                        idx = pc_to_idx[target_pc]
                        continue
                    idx += 1
                    continue
                if op in (CtrlOp.OLCHK, CtrlOp.PHAS):
                    idx += 1
                    continue
                raise ValueError(f"Unsupported CTRL op={op} at @{inst.pc}")

            elif fam == "CTRL_BP":
                halted = True
                diags.append(Diagnostic(code="USC_BREAKPOINT", severity="info", message=f"CTRL.BREAKPOINT at @{inst.pc}", offset=inst.pc * 4))
                break

            elif fam == "COND":
                cycle_cost += 2
                op = int(f["op"])
                cc2 = int(f["cc2"])
                p0_idx = int(f["p0"])
                neg0 = bool(f["neg0"])
                adj = int(f["adj"])
                p0_arr = state.pred[p0_idx]

                for lane in range(state.num_lanes):
                    if cc2 == 0:
                        cond = True
                    elif cc2 == 1:
                        cond = bool(p0_arr[lane])
                    elif cc2 == 2:
                        cond = False
                    else:
                        cond = bool(p0_arr[lane]) and bool(state.pred[int(f["p1"])][lane])
                    if neg0:
                        cond = not cond

                    if op == CondOp.CNDST:
                        if state.emc[lane] > 0 or not cond:
                            state.emc[lane] += adj
                    elif op == CondOp.CNDEF:
                        if state.emc[lane] == 0:
                            state.emc[lane] = adj
                        elif state.emc[lane] == adj:
                            state.emc[lane] = 0
                    elif op == CondOp.CNDEND:
                        state.emc[lane] = max(0, state.emc[lane] - adj)
                    elif op == CondOp.CNDSM:
                        state.emc[lane] = 0 if cond else max(1, adj)
                    elif op in (CondOp.CNDLT, CondOp.CNDLTB):
                        if state.emc[lane] == 0 and not cond:
                            state.emc[lane] += max(1, adj)
                idx += 1
                continue

            elif fam == "INT":
                cycle_cost += 4
                op = int(f["op"])
                mode = int(f["alu_mode"])
                rf = int(f["res_fmt"])
                s0f = int(f["src0_fmt"])
                s1f = int(f["src1_fmt"])
                is_signed0 = s0f in (IntFmt.S8, IntFmt.S16, IntFmt.S32, IntFmt.S64)
                is_signed1 = s1f in (IntFmt.S8, IntFmt.S16, IntFmt.S32, IntFmt.S64)
                d0: Reg = f["dst0"]  # type: ignore[assignment]
                d1: Reg = f["dst1"]  # type: ignore[assignment]
                s0: Reg = f["src0"]  # type: ignore[assignment]
                s1: Reg = f["src1"]  # type: ignore[assignment]
                s2: Reg = f["src2"]  # type: ignore[assignment]
                s0_neg, s0_abs = bool(f["src0_neg"]), bool(f["src0_abs"])
                s1_neg, s1_abs = bool(f["src1_neg"]), bool(f["src1_abs"])

                lanes = state.active_lanes(int(f["cc"]))
                for lane in lanes:
                    if rf == IntResFmt.BITS64 and mode == IntAluMode.ALU and op == IntOp.IADD:
                        lo0 = state.read_reg(s0, lane, 0)
                        hi0 = state.read_reg(s0, lane, 1) if s0f in (IntFmt.U64, IntFmt.S64) else (0xFFFFFFFF if (is_signed0 and (lo0 & 0x80000000)) else 0)
                        v0_64 = lo0 | (hi0 << 32)
                        lo1 = state.read_reg(s1, lane, 0)
                        hi1 = state.read_reg(s1, lane, 1) if s1f in (IntFmt.U64, IntFmt.S64) else (0xFFFFFFFF if (is_signed1 and (lo1 & 0x80000000)) else 0)
                        v1_64 = lo1 | (hi1 << 32)
                        res64 = (v0_64 + v1_64) & U64_MASK
                        state.write_reg(d0, lane, res64 & U32_MASK, 0)
                        state.write_reg(d0, lane, (res64 >> 32) & U32_MASK, 1)
                        continue

                    a = state.read_reg(s0, lane)
                    b = state.read_reg(s1, lane)
                    c = state.read_reg(s2, lane)
                    if is_signed0:
                        a_s = _u32_to_i32(a)
                        if s0_abs:
                            a_s = abs(a_s)
                        if s0_neg:
                            a_s = -a_s
                        a_val = a_s
                    else:
                        a_val = (-a) & U32_MASK if s0_neg else a
                    if is_signed1:
                        b_s = _u32_to_i32(b)
                        if s1_abs:
                            b_s = abs(b_s)
                        if s1_neg:
                            b_s = -b_s
                        b_val = b_s
                    else:
                        b_val = (-b) & U32_MASK if s1_neg else b

                    if mode == IntAluMode.CMP:
                        if op == IntOp.IMAD_OR_LT:
                            pred = a_val < b_val
                        elif op == IntOp.EQ_OR_MAX:
                            pred = (a & U32_MASK) == (b & U32_MASK)
                        elif op == IntOp.IMAD_ALT_OR_NEQ:
                            pred = (a & U32_MASK) != (b & U32_MASK)
                        elif op == IntOp.GTE:
                            pred = a_val >= b_val
                        elif op == IntOp.IMAD64_OR_GT_OR_MIN:
                            pred = a_val > b_val
                        elif op == IntOp.LTE:
                            pred = a_val <= b_val
                        else:
                            raise ValueError(f"Unsupported INT CMP op={op} at @{inst.pc}")
                        state.write_reg(d1, lane, 1 if pred else 0)
                        if d0.bank != Bank.NONE:
                            state.write_reg(d0, lane, U32_MASK if pred else 0)
                    elif mode == IntAluMode.MINMAX:
                        if op == IntOp.EQ_OR_MAX:
                            res = max(a_val, b_val)
                        elif op == IntOp.IMAD64_OR_GT_OR_MIN:
                            res = min(a_val, b_val)
                        else:
                            raise ValueError(f"Unsupported INT MINMAX op={op} at @{inst.pc}")
                        state.write_reg(d0, lane, res & U32_MASK)
                    else:  # ALU
                        if op == IntOp.IMOV or op == IntOp.MOV_OR_TIMER:
                            res = a_val
                        elif op == IntOp.IADD:
                            res = a_val + b_val
                        elif op == IntOp.IMUL:
                            res = a_val * b_val
                        elif op in (IntOp.IMAD_OR_LT, IntOp.IMAD_ALT_OR_NEQ, IntOp.IMAD64_OR_GT_OR_MIN):
                            c_val = _u32_to_i32(c) if is_signed0 else c
                            res = a_val * b_val + c_val
                        else:
                            raise ValueError(f"Unsupported INT ALU op={op} at @{inst.pc}")
                        if rf == IntResFmt.BITS64:
                            state.write_reg(d0, lane, res & U32_MASK, 0)
                            state.write_reg(d0, lane, (res >> 32) & U32_MASK, 1)
                        else:
                            state.write_reg(d0, lane, res & U32_MASK)
                idx += 1
                continue

            elif fam == "FOP":
                cycle_cost += 4
                op = int(f["op"])
                d0 = f["dst0"]  # type: ignore[assignment]
                d1 = f["dst1"]  # type: ignore[assignment]
                s0 = f["src0"]  # type: ignore[assignment]
                s1 = f["src1"]  # type: ignore[assignment]
                s2 = f["src2"]  # type: ignore[assignment]
                sat = bool(f["sat"])
                lanes = state.active_lanes(int(f["cc"]))

                for lane in lanes:
                    v0 = _u32_to_f32(state.read_reg(s0, lane))
                    if f["src0_abs"]:
                        v0 = abs(v0)
                    if f["src0_neg"]:
                        v0 = -v0
                    if f["src0_flr"]:
                        v0 = math.floor(v0)
                    v1 = _u32_to_f32(state.read_reg(s1, lane))
                    v2 = _u32_to_f32(state.read_reg(s2, lane))
                    if f["src2_abs"]:
                        v2 = abs(v2)
                    if f["src2_neg"]:
                        v2 = -v2
                    if f["src2_flr"]:
                        v2 = math.floor(v2)

                    if op == FopOp.FMAD:
                        res_f = _round_f32(math.fma(v0, v1, v2) if hasattr(math, "fma") else (v0 * v1 + v2))
                    elif op == FopOp.FMUL:
                        res_f = _round_f32(v0 * v1)
                    elif op == FopOp.FADD:
                        res_f = _round_f32(v0 + v1)
                    elif op == FopOp.FMOV:
                        res_f = v0
                    elif op == FopOp.FFRC:
                        res_f = _round_f32(v0 - math.floor(v0))
                    elif op == FopOp.FCOMP:
                        tst = int(f["tst_type"])
                        if tst == 4:
                            pred = v0 == v1
                        elif tst == 5:
                            pred = v0 != v1
                        elif tst == 1:
                            pred = v0 < v1
                        elif tst == 7:
                            pred = v0 <= v1
                        elif tst == 6:
                            pred = v0 > v1
                        elif tst == 0:
                            pred = v0 >= v1
                        else:
                            pred = not (math.isnan(v0) or math.isnan(v1))
                        state.pred[0][lane] = 1 if pred else 0
                        continue
                    elif op in (FopOp.FMOVC, FopOp.FMOVC_PRED):
                        tst = int(f["tst_type"])
                        if op == FopOp.FMOVC_PRED:
                            res_f = v0 if state.pred[0][lane] else v1
                        elif tst == 1:
                            res_f = min(v0, v1)
                        elif tst == 6:
                            res_f = max(v0, v1)
                        else:
                            res_f = v0 if v0 >= v1 else v1
                    else:
                        raise ValueError(f"Unsupported FOP op={op} at @{inst.pc}")

                    if sat:
                        res_f = 0.0 if (math.isnan(res_f) or res_f < 0.0) else (1.0 if res_f > 1.0 else res_f)
                    bits = _f32_to_u32(res_f)
                    state.write_reg(d0, lane, bits)
                    if d1.bank != Bank.NONE:
                        state.write_reg(d1, lane, bits)
                idx += 1
                continue

            elif fam == "AF32":
                cycle_cost += 8
                op = int(f["op"])
                # Enforce hardware BRN73233: FSIN/FCOS must not fold abs/neg onto AF32
                if op in (Af32Op.FSIN, Af32Op.FSIN_HP, Af32Op.FCOS, Af32Op.FCOS_HP) and (f["src_abs"] or f["src_neg"]):
                    raise ValueError(f"BRN73233 violation at @{inst.pc}: AF32 {Af32Op(op).name} cannot fold abs/neg")
                d = f["dst"]  # type: ignore[assignment]
                s = f["src"]  # type: ignore[assignment]
                for lane in state.active_lanes(int(f["cc"])):
                    v = _u32_to_f32(state.read_reg(s, lane))
                    if f["src_abs"]:
                        v = abs(v)
                    if f["src_neg"]:
                        v = -v
                    if op == Af32Op.FRCP:
                        r = math.copysign(math.inf, v) if v == 0.0 else (1.0 / v)
                    elif op == Af32Op.FRSQ:
                        r = math.inf if v == 0.0 else (math.nan if v < 0.0 else 1.0 / math.sqrt(v))
                    elif op == Af32Op.FLOG:
                        r = -math.inf if v == 0.0 else (math.nan if v < 0.0 else math.log2(v))
                    elif op == Af32Op.FEXP:
                        r = math.pow(2.0, v) if v < 128.0 else math.inf
                    elif op in (Af32Op.FSIN, Af32Op.FSIN_HP):
                        r = math.sin(v)
                    elif op in (Af32Op.FCOS, Af32Op.FCOS_HP):
                        r = math.cos(v)
                    elif op == Af32Op.FATAN:
                        r = math.atan(v)
                    else:
                        raise ValueError(f"Unsupported AF32 op={op} at @{inst.pc}")
                    state.write_reg(d, lane, _f32_to_u32(r))
                idx += 1
                continue

            elif fam in ("BIT_LUT32", "BIT_LUT16"):
                cycle_cost += 4
                d0 = f["dst0"]  # type: ignore[assignment]
                s0 = f["src0"]  # type: ignore[assignment]
                s1 = f["src1"]  # type: ignore[assignment]
                s2 = f["src2"]  # type: ignore[assignment]
                s3 = f["src3"]  # type: ignore[assignment]
                lut = int(f["lut"])
                sh_c = int(f["sh_c"])

                # Detect likely Mesa PCO_OP_SHIFT bug: LUT=0xcccccccc selects in1 (src1) while src2=sc0 & sh_c!=0
                if (
                    inst.pc not in warned_pcs
                    and sh_c in (1, 3, 4, 6)
                    and (lut & 0xFFFF) == 0xCCCC
                    and s2 == Reg(Bank.SC, 0)
                    and s0 != Reg(Bank.SC, 0)
                    and s1 != Reg(Bank.SC, 0)
                ):
                    warned_pcs.add(inst.pc)
                    diags.append(
                        Diagnostic(
                            code="USC_BIT_SHIFT_LUT_SRC_MISMATCH",
                            severity="warning",
                            offset=inst.pc * 4,
                            field="BIT.lut/src0/src1/src2",
                            message=(
                                f"PC @{inst.pc} ({' '.join(f'0x{w:08x}' for w in inst.raw_words)}): "
                                f"{fam} has sh_c={sh_c} with lut=0x{lut:08x} (which selects in1={s1}, discarding in0={s0}) "
                                f"and shift count src2={s2} (0). Hardware computes {s1} shift-by 0 = {s1}!"
                            ),
                        )
                    )

                for lane in state.active_lanes(int(f["cc"])):
                    a0 = state.read_reg(s0, lane)
                    a1 = state.read_reg(s1, lane)
                    a2 = state.read_reg(s2, lane)
                    a3 = state.read_reg(s3, lane)
                    p0_b = state.pred[0][lane] if f["use_lut32"] else 0
                    lut_out = _eval_lut5(lut, a0, a1, a2, a3, p0_b)
                    out_v = _apply_bit_post_shift(sh_c, lut_out, a2) if sh_c else lut_out
                    state.write_reg(d0, lane, out_v)
                idx += 1
                continue

            elif fam == "MOV":
                cycle_cost += 4
                src: Reg = f["src"]  # type: ignore[assignment]
                dst: Reg = f["dst"]  # type: ignore[assignment]
                bst: Reg = f["bst_len"]  # type: ignore[assignment]
                cnt = bst.index if bst.bank == Bank.IMM else max(1, state.read_reg(bst, 0))
                cnt = max(1, cnt)
                for lane in state.active_lanes(int(f["cc"])):
                    for k in range(cnt):
                        v = state.read_reg(src, lane, k)
                        state.write_reg(dst, lane, v, k)
                idx += 1
                continue

            elif fam == "DOT8":
                cycle_cost += 4
                sm = int(f["sign_mode"])
                s0_signed = sm in (1, 2)
                s1_signed = sm in (1, 3)
                for lane in state.active_lanes(int(f["cc"])):
                    a = state.read_reg(f["src0"], lane)  # type: ignore[arg-type]
                    b = state.read_reg(f["src1"], lane)  # type: ignore[arg-type]
                    c = state.read_reg(f["src2"], lane)  # type: ignore[arg-type]
                    acc = c
                    for bi in range(4):
                        x = (a >> (bi * 8)) & 0xFF
                        y = (b >> (bi * 8)) & 0xFF
                        if s0_signed and x >= 128:
                            x -= 256
                        if s1_signed and y >= 128:
                            y -= 256
                        acc = (acc + x * y) & U32_MASK
                    state.write_reg(f["dst0"], lane, acc)  # type: ignore[arg-type]
                idx += 1
                continue

            elif fam == "DMA":
                cycle_cost += 24
                op = int(f["op"])
                fset = int(f["fence_set"])
                if fset:
                    state.fences_pending.add(fset)
                if op in (DmaOp.MEMBARRIER0, DmaOp.MEMBARRIER1, DmaOp.MEMBARRIER2, DmaOp.MEMBARRIER3, DmaOp.IDF):
                    if op == DmaOp.IDF:
                        state.fences_pending.clear()
                    idx += 1
                    continue

                addr_base: Reg = f["addr_base"]  # type: ignore[assignment]
                addr_off: Reg = f["addr_off"]    # type: ignore[assignment]
                data_reg: Reg = f["data"]        # type: ignore[assignment]
                dst_reg: Reg = f["dst"]          # type: ignore[assignment]
                burst_reg: Reg = f["burst_bytes"]  # type: ignore[assignment]

                # Hardware invariant: SH 64-bit BDA base pair must be even-aligned!
                if addr_base.bank == Bank.SH and (addr_base.index & 1) != 0:
                    raise ValueError(f"DMA.ADDR_BASE SH register {addr_base} violates 2-reg even alignment at @{inst.pc}")

                nbytes = 2048 if (burst_reg.bank == Bank.IMM and burst_reg.index == 0) else (
                    burst_reg.index if burst_reg.bank == Bank.IMM else state.read_reg(burst_reg, 0)
                )
                ndwords = max(1, (nbytes + 3) // 4)

                for lane in state.active_lanes(int(f["cc"])):
                    lo = state.read_reg(addr_base, lane, 0)
                    hi = state.read_reg(addr_base, lane, 1) if addr_base.bank not in (Bank.NONE, Bank.DMA_GLOBAL) else 0
                    va = lo | (hi << 32)
                    if addr_off.bank != Bank.NONE:
                        va = (va + state.read_reg(addr_off, lane)) & U64_MASK

                    if op == DmaOp.LD:
                        raw_b = state.mem.read_bytes(va, nbytes)
                        for k in range(ndwords):
                            chunk = raw_b[k * 4 : (k + 1) * 4].ljust(4, b"\x00")
                            state.write_reg(dst_reg, lane, struct.unpack("<I", chunk)[0], k)
                    elif op == DmaOp.ST:
                        out_b = bytearray()
                        for k in range(ndwords):
                            out_b.extend(struct.pack("<I", state.read_reg(data_reg, lane, k)))
                        state.mem.write_bytes(va, bytes(out_b[:nbytes]))
                    elif op == DmaOp.ATOM32:
                        aop = int(f["atomop"])
                        old = state.mem.read_u32(va)
                        if aop == AtomOp.CMPXCHG:
                            cmp_v = state.read_reg(data_reg, lane, 0)
                            swp_v = state.read_reg(data_reg, lane, 1)
                            new = _apply_atomic(aop, old, swp_v, cmp_v, 32)
                        else:
                            new = _apply_atomic(aop, old, state.read_reg(data_reg, lane, 0), 0, 32)
                        state.mem.write_u32(va, new)
                        if dst_reg.bank != Bank.NONE:
                            state.write_reg(dst_reg, lane, old)
                    elif op == DmaOp.ATOM64:
                        aop = int(f["atomop"])
                        old = state.mem.read_u64(va)
                        v_lo = state.read_reg(data_reg, lane, 0)
                        v_hi = state.read_reg(data_reg, lane, 1)
                        val64 = v_lo | (v_hi << 32)
                        if aop == AtomOp.CMPXCHG:
                            s_lo = state.read_reg(data_reg, lane, 2)
                            s_hi = state.read_reg(data_reg, lane, 3)
                            new = _apply_atomic(aop, old, s_lo | (s_hi << 32), val64, 64)
                        else:
                            new = _apply_atomic(aop, old, val64, 0, 64)
                        state.mem.write_u64(va, new)
                        if dst_reg.bank != Bank.NONE:
                            state.write_reg(dst_reg, lane, old & U32_MASK, 0)
                            state.write_reg(dst_reg, lane, (old >> 32) & U32_MASK, 1)
                    else:
                        raise ValueError(f"Unsupported DMA op={op} at @{inst.pc}")
                idx += 1
                continue

            else:
                raise ValueError(f"Unsupported family {fam} in compute simulator at @{inst.pc}")

        if not halted and fault_reason is None:
            fault_reason = "USC_MISSING_CTRL_END: shader fell off end without CTRL.END"
            diags.append(Diagnostic(code="USC_MISSING_CTRL_END", severity="error", message=fault_reason))

    except Exception as exc:
        fault_reason = f"USC_EXEC_FAULT: {exc}"
        diags.append(Diagnostic(code="USC_EXEC_FAULT", severity="error", message=str(exc)))

    sram_bytes = (state.max_temp_used * state.num_lanes * 4) + (state.max_cs_used * 4) + (state.max_sh_used * 4)
    # 1.0 GHz USC clock -> cycles / 1000.0 us
    est_hw_us = cycle_cost / 1000.0
    return SimResult(
        passed=(fault_reason is None and halted),
        halted_cleanly=halted,
        steps=steps,
        inst_counts=inst_counts,
        max_temp_reg=state.max_temp_used,
        max_sh_reg=state.max_sh_used,
        max_cs_reg=state.max_cs_used,
        sram_bytes=sram_bytes,
        est_hw_us=est_hw_us,
        fault_reason=fault_reason,
        diagnostics=diags,
        state=state,
    )


def evaluate_compute_shader(
    words: Sequence[int],
    *,
    workload: str,
    target: str = "volcanic-dxt-48-1536",
    state: Optional[VolcanicUSCState] = None,
    ssbo_va: Optional[int] = None,
    expected_u32: Optional[Sequence[int]] = None,
    expected_f32: Optional[Sequence[float]] = None,
    f32_tol: float = 1e-4,
    max_steps: int = 100_000,
) -> Dict[str, Any]:
    """Run a 128-lane Volcanic USC simulation and emit a freesilicon.twin.v1 verdict."""
    t0 = time.perf_counter()
    st = state if state is not None else VolcanicUSCState()
    sim = simulate_words(words, st, max_steps=max_steps)

    diags = list(sim.diagnostics)
    passed = sim.passed
    fault_reason = sim.fault_reason
    nmse: Optional[float] = 0.0 if passed else None
    max_abs_err: Optional[float] = 0.0 if passed else None
    mismatches = 0
    first_mismatches: List[Dict[str, Any]] = []

    if passed and ssbo_va is not None and expected_u32 is not None:
        sq_err = 0.0
        sq_ref = 0.0
        max_e = 0.0
        for i, exp in enumerate(expected_u32):
            got = st.mem.read_u32(ssbo_va + i * 4)
            diff = abs(int(got) - int(exp))
            sq_err += float(diff * diff)
            sq_ref += float(int(exp) * int(exp))
            if diff > max_e:
                max_e = float(diff)
            if got != (exp & U32_MASK):
                mismatches += 1
                if len(first_mismatches) < 4:
                    first_mismatches.append({"lane": i, "got": f"0x{got:08x}", "expected": f"0x{exp:08x}"})
        nmse = sq_err / max(1.0, sq_ref)
        max_abs_err = max_e
        if mismatches > 0:
            passed = False
            fault_reason = f"USC_SSBO_MISMATCH: {mismatches}/{len(expected_u32)} u32 lanes mismatched (max_abs_err={max_e:g})"
            diags.append(
                Diagnostic(
                    code="USC_SSBO_MISMATCH",
                    severity="error",
                    message=f"{fault_reason}; sample={first_mismatches}",
                    field="ssbo_u32",
                )
            )

    elif passed and ssbo_va is not None and expected_f32 is not None:
        sq_err = 0.0
        sq_ref = 0.0
        max_e = 0.0
        for i, exp in enumerate(expected_f32):
            got = _u32_to_f32(st.mem.read_u32(ssbo_va + i * 4))
            if math.isnan(got) or math.isinf(got):
                mismatches += 1
                max_e = float("inf")
                if len(first_mismatches) < 4:
                    first_mismatches.append({"lane": i, "got": str(got), "expected": exp})
                continue
            diff = abs(got - float(exp))
            sq_err += diff * diff
            sq_ref += float(exp) * float(exp)
            if diff > max_e:
                max_e = diff
            if diff > f32_tol:
                mismatches += 1
                if len(first_mismatches) < 4:
                    first_mismatches.append({"lane": i, "got": got, "expected": float(exp)})
        if math.isinf(max_e):
            nmse = None
            max_abs_err = None
        else:
            nmse = sq_err / max(1e-12, sq_ref)
            max_abs_err = max_e
        if mismatches > 0:
            passed = False
            fault_reason = f"USC_SSBO_MISMATCH: {mismatches}/{len(expected_f32)} f32 lanes mismatched (max_abs_err={max_e})"
            diags.append(
                Diagnostic(
                    code="USC_SSBO_MISMATCH",
                    severity="error",
                    message=f"{fault_reason}; sample={first_mismatches}",
                    field="ssbo_f32",
                )
            )

    eval_ms = (time.perf_counter() - t0) * 1000.0
    checks: Dict[str, Any] = {
        "decode_ok": not any(d.code == "USC_DECODE_ERROR" for d in diags),
        "halted_cleanly": sim.halted_cleanly,
        "dynamic_steps": sim.steps,
        "lanes": st.num_lanes,
        "temps_used": sim.max_temp_reg,
        "shareds_used": sim.max_sh_reg,
        "common_store_used": sim.max_cs_reg,
        "wg_barriers": st.wg_barriers,
        "inst_histogram": sim.inst_counts,
        "ssbo_mismatches": mismatches,
    }
    if first_mismatches:
        checks["first_mismatches"] = first_mismatches

    return make_verdict(
        domain="freeusc",
        target=target,
        workload=workload,
        passed=passed,
        eval_ms=eval_ms,
        checks=checks,
        diagnostics=diags,
        fault_reason=fault_reason,
        fault_severity=0.8 if mismatches > 0 else 1.0,
        nmse=nmse,
        max_abs_err=max_abs_err,
        sram_bytes=sim.sram_bytes,
        sram_limit_bytes=VOLCANIC_USC_DEFAULT_SRAM_LIMIT,
        est_hw_us=sim.est_hw_us,
    )
