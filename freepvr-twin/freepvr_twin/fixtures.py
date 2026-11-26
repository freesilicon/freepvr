# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 FreePVR Contributors
"""
Clean-room Volcanic command-stream fixtures, hand-encoded USC compute kernels,
and Mesa Stage G3B (`iadd_sg` 276B & `fma32` 352B) binary test harnesses.
"""

from __future__ import annotations

import struct
from typing import Any, Dict, List, Tuple

from .freecs import (
    DCE_INDEXED_ARG_DESC,
    DCE_NON_INDEXED_ARG_DESC,
    LAGUNA_CCCB_CTL_SIZE,
    LAGUNA_CCCB_HDR_SIZE,
    LAGUNA_INICFG_DEFAULT,
    MMUV4_AXCACHE_UNCACHED,
    TA_HEADER_SINGLE_VP_PROOF,
    VOLCANIC_IRQ_OS0_EVENT_CLEAR_OFFSET,
    VOLCANIC_SOFT_RESET_GARTEN_EN,
    encode_mmuv4_pce,
    encode_mmuv4_pde,
    encode_mmuv4_pte,
)
from .freeusc_isa import (
    AtomOp,
    Bank,
    CC,
    CondOp,
    CtrlOp,
    DmaOp,
    FopOp,
    IntAluMode,
    IntFmt,
    IntOp,
    IntResFmt,
    Reg,
    REG_NONE,
    REG_P0,
    REG_SC0,
    encode_bit06,
    encode_cond,
    encode_ctrl,
    encode_dma,
    encode_fop,
    encode_int,
    encode_mov,
)
from .freeusc_sim import GlobalMemory, SUBGROUP_SIZE, VolcanicUSCState, evaluate_compute_shader

# ---------------------------------------------------------------------------
# Mesa pco Stage G3B Volcanic Compute Binaries (exact hex dwords)
# ---------------------------------------------------------------------------

MESA_STAGE_G3B_IADD_SG_WORDS: Tuple[int, ...] = (
    0x0000019C, 0x00008080, 0x80000003, 0x11124020, 0x01800019, 0x840A0A98, 0x02B04790, 0x00804009,
    0x840B0A99, 0x0001FE06, 0x78040808, 0xC100000F, 0x8020001E, 0x10304230, 0x04804009, 0x840A0A98,
    0x8021005E, 0x0006401C, 0x80000400, 0xC003804A, 0x0000019C, 0x00008080, 0x80000003, 0x52104000,
    0x01800129, 0x840A0A98, 0x10084120, 0x01800209, 0x840A0A98, 0x1810C1C0, 0x01800049, 0x840A0A98,
    0x0155FE36, 0x50040810, 0x4000300A, 0x8184031A, 0x1010C100, 0x01800209, 0x840A0A98, 0x1219C060,
    0x01C08329, 0x840A0A98, 0x00104060, 0x01A00009, 0x840A0A98, 0x0010C310, 0x01A00849, 0x840A0A98,
    0x13224390, 0x00804009, 0x840A0A98, 0x0001FE46, 0x7804097C, 0xC000000F, 0x5012C120, 0x01A00029,
    0x840A0A98, 0x111A40A0, 0x01800209, 0x840A0A98, 0x1081E540, 0x01800049, 0x840B0B98, 0x0404084E,
    0x18080218, 0x008003F0, 0x83000004, 0x8020029E, 0x8000031C,
)

MESA_STAGE_G3B_FMA32_WORDS: Tuple[int, ...] = (
    0x0000019C, 0x00008080, 0x80000003, 0x11124020, 0x01800019, 0x840A0A98, 0x02B04790, 0x00804009,
    0x840B0A99, 0x0001FE06, 0x78040808, 0xC100000F, 0x8020001E, 0x10304230, 0x04804009, 0x840A0A98,
    0x8021005E, 0x1001201C, 0x80000400, 0xC003804A, 0x0000019C, 0x00008080, 0x80000003, 0x52104000,
    0x01800129, 0x840A0A98, 0x10004120, 0x01800009, 0x840A0A98, 0x1210C020, 0x01C08129, 0x840A0A98,
    0x00184060, 0x01800009, 0x840A0A98, 0x00104310, 0x01A00809, 0x840A0A98, 0x13224390, 0x00804009,
    0x840A0A98, 0x0001FE46, 0x7804097C, 0xC000000F, 0x5010C160, 0x01800229, 0x840A0A98, 0x111A40A0,
    0x01800009, 0x840A0A98, 0x1088E540, 0x01800049, 0x840B0B98, 0x0005084E, 0x3808061E, 0x00800380,
    0x83000004, 0x0441A64F, 0x80C00808, 0x0520864B, 0x84400908, 0x00184060, 0x01800009, 0x840A0A98,
    0x00104310, 0x01A00809, 0x840A0A98, 0x13224390, 0x00804009, 0x840A0A98, 0x0001FE46, 0x7804097C,
    0xC000000F, 0x5018C160, 0x01800029, 0x840A0A98, 0x111240A0, 0x01800009, 0x840A0A98, 0x1080E500,
    0x01800249, 0x840B0B98, 0x0804084E, 0x18080A18, 0x008003F0, 0x83000004, 0x8020029E, 0x8000031C,
)

DEFAULT_SSBO_VA = 0x00000080_10000000  # 40-bit Volcanic device VA


def run_mesa_iadd_sg() -> Dict[str, Any]:
    """Run Mesa Stage G3B `iadd_sg` (276B) with exact vk_volcanic_compute_smoke SSBO setup."""
    mem = GlobalMemory()
    init_bytes = struct.pack(f"<{SUBGROUP_SIZE}I", *([0xDEADBEEF] * SUBGROUP_SIZE))
    mem.map_region(DEFAULT_SSBO_VA, 4096, init_bytes)
    sh = {
        0: DEFAULT_SSBO_VA & 0xFFFFFFFF,
        1: (DEFAULT_SSBO_VA >> 32) & 0xFFFFFFFF,
        2: 4096,
        3: 0,
    }
    st = VolcanicUSCState(num_lanes=SUBGROUP_SIZE, workgroup_id=(0, 0, 0), sh_init=sh, memory=mem)
    expected = [i + 1 for i in range(SUBGROUP_SIZE)]
    return evaluate_compute_shader(
        MESA_STAGE_G3B_IADD_SG_WORDS,
        workload="mesa-stage-g3b-iadd_sg",
        state=st,
        ssbo_va=DEFAULT_SSBO_VA,
        expected_u32=expected,
    )


def run_mesa_fma32() -> Dict[str, Any]:
    """Run Mesa Stage G3B `fma32` (352B) with exact vk_volcanic_compute_smoke SSBO setup."""
    mem = GlobalMemory()
    init_floats = [float(i) for i in range(SUBGROUP_SIZE)]
    mem.map_region(DEFAULT_SSBO_VA, 4096, struct.pack(f"<{SUBGROUP_SIZE}f", *init_floats))
    sh = {
        0: DEFAULT_SSBO_VA & 0xFFFFFFFF,
        1: (DEFAULT_SSBO_VA >> 32) & 0xFFFFFFFF,
        2: 4096,
        3: 0,
    }
    st = VolcanicUSCState(num_lanes=SUBGROUP_SIZE, workgroup_id=(0, 0, 0), sh_init=sh, memory=mem)
    expected = [float(i) * 2.0 + (float(i) + 1.0) for i in range(SUBGROUP_SIZE)]
    return evaluate_compute_shader(
        MESA_STAGE_G3B_FMA32_WORDS,
        workload="mesa-stage-g3b-fma32",
        state=st,
        ssbo_va=DEFAULT_SSBO_VA,
        expected_f32=expected,
    )


# ---------------------------------------------------------------------------
# Clean-room Hand-Encoded Volcanic Compute Shaders
# ---------------------------------------------------------------------------

def build_clean_bda_fma_kernel() -> Tuple[List[int], Dict[str, Any]]:
    """128-lane clean-room BDA LD -> FMA(x, 2.0, 1.0) -> ST kernel using sr14 + even sh0:sh1."""
    r = lambda i: Reg(Bank.TEMP, i)
    sr = lambda i: Reg(Bank.SR, i)
    sc = lambda i: Reg(Bank.SC, i)
    sh = lambda i: Reg(Bank.SH, i)

    words: List[int] = []
    # r0 = sr14 * 4 (byte offset)
    words += encode_int(IntOp.IMUL, r(0), sr(14), sc(61))
    # DMA LD r1 = *(sh0:sh1 + r0)
    words += encode_dma(DmaOp.LD, addr_base=sh(0), addr_off=r(0), dst=r(1), burst_bytes=4, fence_set=1)
    # FOP FMAD r2 = fma(r1, 2.0f(sc40), 1.0f(sc1)) waiting on fence 1
    words += encode_fop(FopOp.FMAD, r(2), r(1), sc(40), sc(1), fence_wait=1)
    # DMA ST *(sh2:sh3 + r0) = r2
    words += encode_dma(DmaOp.ST, addr_base=sh(2), addr_off=r(0), data=r(2), burst_bytes=4, fence_set=2)
    words += encode_ctrl(CtrlOp.END, fence_wait=2)
    return words, {"name": "clean-bda-fma"}


def build_clean_bitwise_loop_barrier_kernel() -> List[int]:
    """128-lane kernel exercising BIT06 (XOR/OR/LSL/LSR), BRLT loop, WGBARRIER, and BDA ST."""
    r = lambda i: Reg(Bank.TEMP, i)
    sr = lambda i: Reg(Bank.SR, i)
    sc = lambda i: Reg(Bank.SC, i)
    sl = lambda i: Reg(Bank.SLOT, i)
    sh = lambda i: Reg(Bank.SH, i)

    words: List[int] = []
    # r0 = sr14 (0..127), r1 = 0 (accumulator)
    words += encode_int(IntOp.IMOV, r(0), sr(14))
    words += encode_int(IntOp.IMOV, r(1), sc(0))
    # sl0 = 4 (loop trip count)
    words += encode_ctrl(CtrlOp.LIMM, dst=0, imm=4, bcc=4)
    loop_pc = len(words)
    # r1 = r1 + r0 (runs 4 times -> r1 = 4 * sr14)
    words += encode_int(IntOp.IADD, r(1), r(1), r(0))
    # BRLT sl0 -> loop_pc
    br_pc = len(words)
    rel = loop_pc - br_pc
    words += encode_ctrl(CtrlOp.BRLT, dst=0, imm=rel & 0x3FFFFFFF, bcc=1, min_words=2)
    # WGBARRIER
    words += encode_ctrl(CtrlOp.WGBARRIER)
    # BIT06 XOR: r2 = r1 ^ 0xff (sc34)
    words += encode_bit06(r(2), r(1), sc(34), lut=0x00060006)
    # BIT06 LSR by 1: put value in src0, lut=0xaaaaaaaa (in0!), shift count sc33 (1) in src2!
    words += encode_bit06(r(3), r(2), REG_SC0, lut=0xAAAAAAAA, src2=sc(33), sh_c=3)
    # Store r3 at sh0:sh1 + sr14*4
    words += encode_int(IntOp.IMUL, r(4), sr(14), sc(61))
    words += encode_dma(DmaOp.ST, addr_base=sh(0), addr_off=r(4), data=r(3), burst_bytes=4, fence_set=1)
    words += encode_ctrl(CtrlOp.END, fence_wait=1)
    return words


def build_clean_subgroup_ballot_add_shuffle_kernel() -> List[int]:
    """
    128-wide Volcanic subgroup kernel using ONLY native hardware primitives (`sr14` + `SLOT` `sl0..sl15`):
      - Broadcast lane 42's value (`1000 + 42`) to all 128 lanes via `sl1`
      - SubgroupAdd reduction across all 128 lanes (`sum_{k=0..127} k = 8128`) via loop + `sl2`
      - SubgroupShuffle reversing lane order (`lane -> 127 - lane`) via loop + `sl3`
      - 128-bit Ballot word 0 (lanes 0..31 where `(sr14 & 1) == 0` -> `0x55555555`) via `sl4`
      Stores per-lane results to SSBO: `[lane*4 + 0..3]` (4 u32s per lane or 4 separate buffers).
    """
    r = lambda i: Reg(Bank.TEMP, i)
    sr = lambda i: Reg(Bank.SR, i)
    sc = lambda i: Reg(Bank.SC, i)
    sl = lambda i: Reg(Bank.SLOT, i)
    sh = lambda i: Reg(Bank.SH, i)

    words: List[int] = []
    # r0 = sr14 (0..127)
    words += encode_int(IntOp.IMOV, r(0), sr(14))
    # --- 1. Broadcast lane 42: r1 = 1000 + sr14; if (sr14 == 42) sl1 = r1 ---
    words += encode_ctrl(CtrlOp.LIMM, dst=5, imm=1000, bcc=4)
    words += encode_int(IntOp.IADD, r(1), r(0), sl(5))
    words += encode_ctrl(CtrlOp.LIMM, dst=6, imm=42, bcc=4)
    words += encode_int(IntOp.EQ_OR_MAX, REG_NONE, r(0), sl(6), dst1=REG_P0, alu_mode=IntAluMode.CMP)
    words += encode_int(IntOp.IMOV, sl(1), r(1), cc=CC.P0)

    # --- 2. SubgroupAdd(sr14) + SubgroupShuffle(127 - sr14) over k = 0..127 ---
    # r2 = target_lane = 127 - sr14 (via LIMM sl7=127, IADD with src1_neg)
    words += encode_ctrl(CtrlOp.LIMM, dst=7, imm=127, bcc=4)
    words += encode_int(IntOp.IADD, r(2), sl(7), r(0), src1_neg=True)
    # sl2 = sum = 0; sl8 = k = 0; sl9 = 128 (counter for BRLT)
    words += encode_ctrl(CtrlOp.LIMM, dst=2, imm=0, bcc=4)
    words += encode_ctrl(CtrlOp.LIMM, dst=8, imm=0, bcc=4)
    words += encode_ctrl(CtrlOp.LIMM, dst=9, imm=128, bcc=4)
    loop_pc = len(words)
    # p0 = (sr14 == sl8); if p0: sl3 = r0 (broadcast lane k's value)
    words += encode_int(IntOp.EQ_OR_MAX, REG_NONE, r(0), sl(8), dst1=REG_P0, alu_mode=IntAluMode.CMP)
    words += encode_int(IntOp.IMOV, sl(3), r(0), cc=CC.P0)
    # sl2 = sl2 + sl3 (uniform accumulate — execute on lane 0 only via p0 = (sr14 == 0))
    words += encode_int(IntOp.EQ_OR_MAX, REG_NONE, r(0), sc(0), dst1=REG_P0, alu_mode=IntAluMode.CMP)
    words += encode_int(IntOp.IADD, sl(2), sl(2), sl(3), cc=CC.P0)
    # Shuffle capture: if (r2 == sl8) r3 = sl3
    words += encode_int(IntOp.EQ_OR_MAX, REG_NONE, r(2), sl(8), dst1=REG_P0, alu_mode=IntAluMode.CMP)
    words += encode_int(IntOp.IMOV, r(3), sl(3), cc=CC.P0)
    # sl8 = sl8 + 1 (on lane 0)
    words += encode_int(IntOp.EQ_OR_MAX, REG_NONE, r(0), sc(0), dst1=REG_P0, alu_mode=IntAluMode.CMP)
    words += encode_int(IntOp.IADD, sl(8), sl(8), sc(33), cc=CC.P0)
    # BRLT sl9 -> loop_pc
    br_pc = len(words)
    words += encode_ctrl(CtrlOp.BRLT, dst=9, imm=(loop_pc - br_pc) & 0x3FFFFFFF, bcc=1, min_words=2)

    # --- 3. 32-lane Ballot word 0 for even lanes `(sr14 & 1) == 0` ---
    # r4 = sr14 & 1; p0 = (r4 == 0); r5 = p0 ? (1 << sr14) : 0 (for lanes 0..31)
    words += encode_bit06(r(4), r(0), sc(33), lut=0x00080008)
    words += encode_int(IntOp.EQ_OR_MAX, REG_NONE, r(4), sc(0), dst1=REG_P0, alu_mode=IntAluMode.CMP)
    # r6 = 1 << sr14 (src0=sc33(1), lut=0xaaaaaaaa, src2=r0, sh_c=1 LSL)
    words += encode_bit06(r(6), sc(33), REG_SC0, lut=0xAAAAAAAA, src2=r(0), sh_c=1)
    words += encode_bit06(r(5), r(6), sc(0), lut=0xAAAACCCC)
    words += encode_ctrl(CtrlOp.LIMM, dst=4, imm=0, bcc=4)
    words += encode_ctrl(CtrlOp.LIMM, dst=8, imm=0, bcc=4)
    words += encode_ctrl(CtrlOp.LIMM, dst=9, imm=32, bcc=4)
    bloop_pc = len(words)
    words += encode_int(IntOp.EQ_OR_MAX, REG_NONE, r(0), sl(8), dst1=REG_P0, alu_mode=IntAluMode.CMP)
    words += encode_int(IntOp.IMOV, sl(10), r(5), cc=CC.P0)
    words += encode_int(IntOp.EQ_OR_MAX, REG_NONE, r(0), sc(0), dst1=REG_P0, alu_mode=IntAluMode.CMP)
    words += encode_bit06(sl(4), sl(4), sl(10), lut=0x000E000E, cc=CC.P0)
    words += encode_int(IntOp.IADD, sl(8), sl(8), sc(33), cc=CC.P0)
    bbr_pc = len(words)
    words += encode_ctrl(CtrlOp.BRLT, dst=9, imm=(bloop_pc - bbr_pc) & 0x3FFFFFFF, bcc=1, min_words=2)

    # Store 4 results per lane: [r10=sl1(bcast), r11=sl2(sum), r12=r3(shuf), r13=sl4(ballot0)] via 16B burst ST
    words += encode_int(IntOp.IMOV, r(10), sl(1))
    words += encode_int(IntOp.IMOV, r(11), sl(2))
    words += encode_int(IntOp.IMOV, r(12), r(3))
    words += encode_int(IntOp.IMOV, r(13), sl(4))
    # byte_off = sr14 * 16 (sc39 = 16)
    words += encode_int(IntOp.IMUL, r(14), r(0), sc(39))
    words += encode_dma(DmaOp.ST, addr_base=sh(0), addr_off=r(14), data=r(10), burst_bytes=16, fence_set=1)
    words += encode_ctrl(CtrlOp.END, fence_wait=1)
    return words


def build_clean_atomic_kernel() -> List[int]:
    """128-lane global BDA ATOM32.ADD kernel summing sr14 (0..127 -> 8128) into *sh0:sh1."""
    r = lambda i: Reg(Bank.TEMP, i)
    sr = lambda i: Reg(Bank.SR, i)
    sh = lambda i: Reg(Bank.SH, i)
    words: List[int] = []
    words += encode_int(IntOp.IMOV, r(0), sr(14))
    words += encode_dma(
        DmaOp.ATOM32,
        atomop=AtomOp.ADD,
        addr_base=sh(0),
        addr_off=REG_NONE,
        data=r(0),
        dst=r(1),
        burst_bytes=4,
        cache_coherent=True,
        fence_set=1,
    )
    words += encode_ctrl(CtrlOp.END, fence_wait=1)
    return words


# ---------------------------------------------------------------------------
# Clean-room Valid `freecs-validate` Bundle Builder
# ---------------------------------------------------------------------------

def build_valid_freecs_bundle() -> Dict[str, Any]:
    """Build a complete, bit-exact passing Volcanic FW-ABI + CDM + DCE + PPP + PBE + ISP + ZLS + PDS + TEXSTATE bundle."""
    # 1. Laguna 16B cCCB header + 16B payload (cmd_type=1 CDM, cmd_size=16)
    cccb_raw = struct.pack("<IIIIIIII", 1, 16, 0x101, 0x202, 0xC0000001, 0x00001000, 0x00002000, 0x00000001)

    # 2. CDM: 128-lane compute kernel (block_type=0, wg_mode=1) + Volcanic primary
    #    dealloc-fence-and-terminate block (block_type=3 + BIT(28) + BIT(27), sd_type=PDS, usc_common_shared=1)
    k0 = (0 << 30) | (1 << 20) | (160 << 12) | (1 << 7) | (1 << 1) | 1
    k1 = 0x00001000  # data_addr=0x1000, sd_type=NONE, usc_common_shared=0
    k2 = 0x00002000  # code_addr=0x2000 (16B aligned)
    k3, k4, k5 = 0, 0, 0
    k8 = (0 << 25) | (2 << 11) | 4  # max_instances=0 (128), event_type=2, usc_common_size=4
    k9 = (127 << 20) | (0 << 10) | 0  # 128x1x1 workgroup
    term_k = [
        (3 << 30) | (1 << 28) | (1 << 27) | (1 << 1),  # 0xD8000002
        0x00003000 | (1 << 2) | (1 << 1),              # data_addr=0x3000, sd_type=PDS, usc_common_shared=1
        0x00003010,                                    # code_addr=0x3010 (DOUTC 0xF4001003 + HALT)
        0,
        0,
        0,
        (1 << 25),                                     # max_instances=1
        0,
    ]

    # 3. DCE QWORD stream: PPP_STATE_UPDATE(23 dw) + SHADER_STATE(VS) + SCISSOR + DRAW(indexed) + BARRIER + PPP terminate(3 dw)
    dce_ppp = (0x01 << 56) | (23 << 46) | (0x8000300004 >> 2)
    dce_sh0 = (0x04 << 56) | (0 << 53) | (1 << 49) | (127 << 41) | (4 << 27) | (4 << 19) | (1 << 14) | (32 << 7)
    dce_sh1 = (8 << 54) | (2 << 33) | (1 << 28) | (0x4000 >> 4)
    dce_sh2 = 0x5000
    dce_sc_hdr = (0x09 << 56) | (0 << 4) | 0
    dce_sc_rect = (2423 << 48) | (0 << 32) | (1079 << 16) | 0
    dce_sc_emit = (0x0A << 56) | 0x3
    dce_draw0 = (0x06 << 56) | (0xFFFFFFFF << 24) | (1 << 23) | (4 << 19) | (1 << 7)
    dce_draw1 = (2 << 55) | (0x8000400000 >> 3)
    dce_draw2 = (0xFC << 48) | (0x8000400100 >> 2)
    dce_bar = 0x0C << 56
    dce_term = (0x01 << 56) | (3 << 46) | (0x8000300100 >> 2)
    dce_qwords = [dce_ppp, dce_sh0, dce_sh1, dce_sh2, dce_sc_hdr, dce_sc_rect, dce_sc_emit, dce_draw0, dce_draw1, dce_draw2, dce_bar, dce_term]
    dce_handle = (len(dce_qwords) << 32) | (0x00100000 >> 3)

    # 4. PPP_STATE exact 23-dw single-viewport proof vector 0x00ff0d87
    ppp_words = [0] * 23
    ppp_words[0] = TA_HEADER_SINGLE_VP_PROOF
    ppp_words[1] = (1 << 29) | (0xFF << 20) | (1 << 16)
    ppp_words[2] = (0 << 28) | (0 << 25) | (1 << 21) | (7 << 17) | (16 << 8)
    ppp_words[4] = 0x00003000  # 16B aligned PDS data addr
    ppp_words[5] = (1 << 29) | (0 << 27) | 42  # FSR 2x1, table index 42
    ppp_words[6] = (0 << 16) | 1023  # 10-bit 32K region clip X
    ppp_words[7] = (0 << 16) | 1023  # 10-bit 32K region clip Y
    ps_entry = [
        ((0x2000 >> 4) << 28) | (0x1000 >> 4),
        (2 << 27) | (2 << 17) | (1 << 9) | 2,
    ]

    # 5. PBE raw byte VA + REG_WORD0..2, ISP, ZLS v2
    pbe_va = 0x8000100005  # deliberately unaligned byte VA to prove shift=0!
    pbe_sw = [
        pbe_va & 0xFFFFFFFF,
        (1 << 29) | (2 << 23) | (1 << 22) | ((pbe_va >> 32) & 0x1FFF) | (((pbe_va >> 47) & 1) << 13),
        (1 << 2) | 0,
    ]
    pbe_rw = [
        (2048 << 39) | (12 << 29) | (11 << 24) | (1 << 22) | (1 << 8) | (1 << 3),
        (0 << 48) | (1079 << 32) | (0 << 16) | 2423,
        3 << 32,
    ]
    zls_words = [
        (12 << 29) | (11 << 24) | (1 << 22) | (1 << 3),
        (0 << 48) | (1079 << 32) | (0 << 16) | 2423,
        1087 << 32,
    ]

    # 6. PDS DOUTU / DOUTD + TEXSTATE 32K (width=32768, height=2424, raw byte VA, 4x MSAA)
    doutu_src0 = (0x12340 >> 2) | (1 << 30) | (1 << 32) | (4 << 33)
    doutd_src0 = (12 << 48) | 0x8000123456
    doutd_src1 = 0x80000000 | (1 << 28) | 4
    dout_inst = 0xF0000000 | (1 << 26) | (0 << 11) | (2 << 3)
    tex_va = 0x8000040003
    tex_w0 = (2423 << 42) | (32767 << 27) | (1 << 0)  # 32768 x 2424 2D
    tex_w1 = (1 << 47) | (tex_va & ((1 << 45) - 1)) | (((tex_va >> 47) & 1) << 45)
    tex_w2 = (0 << 10) | (2 << 8) | (2 << 6)  # smpcnt=2 -> 4x MSAA

    return {
        "fw_abi": {
            "cccb_hdr_size": LAGUNA_CCCB_HDR_SIZE,
            "cccb_ctl_size": LAGUNA_CCCB_CTL_SIZE,
            "cccb_stream": cccb_raw.hex(),
            "expect_nonzero_payload_head": True,
            "config_flags": LAGUNA_INICFG_DEFAULT,
            "hwperf_ctl_dma_addr": {"dev_addr": 0x8000010000, "fw_addr": 0xC0010000},
            "soft_reset": VOLCANIC_SOFT_RESET_GARTEN_EN,
            "irq_os0_event_clear_offset": VOLCANIC_IRQ_OS0_EVENT_CLEAR_OFFSET,
            "cmd_cdm": {
                "size_bytes": 120,
                "cdm_ctrl_stream_base": 0x8000068000,
                "cdm_context_state_base_addr": 0x8000043000,
                "cdm_stack_state_base": 0x8000043800,
            },
        },
        "cdm": {"words": [k0, k1, k2, k3, k4, k5, k8, k9] + term_k},
        "dce": {
            "qwords": dce_qwords,
            "handle_entries": [dce_handle],
            "draw_arg_descs": [
                {"indexed": False, "addr": 0x8000400000, "size_bytes": 16, "arg_desc_qwords": [DCE_NON_INDEXED_ARG_DESC]},
                {"indexed": True, "addr": 0x8000400010, "size_bytes": 36, "arg_desc_qwords": list(DCE_INDEXED_ARG_DESC), "index_type": 1},
            ],
        },
        "ppp": {
            "words": ppp_words,
            "pixel_shader_table": {"base_addr": 0x8000500000, "entries": [ps_entry]},
        },
        "pbe": {"state_words": pbe_sw, "reg_words": pbe_rw, "expected_va": pbe_va},
        "isp": {"isp_xtp_pipe_enable": 2, "isp_aa": 2, "te_aa": 5, "ppp_screen": (2423 << 16) | 1079},
        "zls_v2": {"zls_words": zls_words},
        "pds": {
            "doutu_src0": doutu_src0,
            "expected_exe_addr": 0x12340,
            "doutd_src0": doutd_src0,
            "doutd_src1": doutd_src1,
            "dout_insts": [dout_inst],
        },
        "texstate": {
            "image_words": [tex_w0, tex_w1, tex_w2],
            "expected_width": 32768,
            "expected_height": 2424,
            "expected_va": tex_va,
            "expected_samples": 4,
            "sampler_word1": 0x3A5,
        },
        "mmuv4": {
            "gpu_dma_coherent": False,
            "pt_sync_flushed": True,
            "pc_base_pa": 0x88EFE7000,
            "walk_va": 0x8000068000,
            "pce": encode_mmuv4_pce(0x88EFE8000),
            "expected_pd_pa": 0x88EFE8000,
            "pde": encode_mmuv4_pde(0x88EFE9000),
            "expected_pt_pa": 0x88EFE9000,
            "pte": encode_mmuv4_pte(0x88EFEA000, axcache=MMUV4_AXCACHE_UNCACHED),
            "expected_page_pa": 0x88EFEA000,
        },
    }
