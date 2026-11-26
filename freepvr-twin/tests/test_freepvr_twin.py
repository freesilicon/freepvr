# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 FreePVR Contributors
"""Comprehensive offline unit tests for FreePVR Volcanic Digital Twin (`freecs-validate` + `freeusc-sim`)."""

from __future__ import annotations

import copy
import math
import struct
import sys
import unittest
from pathlib import Path

_PKG_ROOT = str(Path(__file__).resolve().parent.parent)
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from freepvr_twin.fixtures import (
    DEFAULT_SSBO_VA,
    MESA_STAGE_G3B_FMA32_WORDS,
    MESA_STAGE_G3B_IADD_SG_WORDS,
    build_clean_atomic_kernel,
    build_clean_bda_fma_kernel,
    build_clean_bitwise_loop_barrier_kernel,
    build_clean_subgroup_ballot_add_shuffle_kernel,
    build_valid_freecs_bundle,
    run_mesa_fma32,
    run_mesa_iadd_sg,
)
from freepvr_twin.freecs import (
    MMUV4_AXCACHE_CACHED,
    ROGUE_CCCB_CTL_SIZE,
    ROGUE_CCCB_HDR_SIZE,
    TA_HEADER_SINGLE_VP_PROOF,
    decode_mmu_fault_status,
    encode_mmuv4_pce,
    encode_mmuv4_pde,
    encode_mmuv4_pte,
    ppp_expected_dwords,
    validate_bundle,
)
from freepvr_twin.freeusc_isa import (
    Af32Op,
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
    SC_TABLE,
    classify_w0,
    decode_stream,
    encode_af32,
    encode_bit06,
    encode_cond,
    encode_ctrl,
    encode_dma,
    encode_fop,
    encode_int,
    encode_mov,
)
from freepvr_twin.freeusc_sim import (
    GlobalMemory,
    SUBGROUP_SIZE,
    VolcanicUSCState,
    _u32_to_f32,
    evaluate_compute_shader,
    simulate_words,
)
from freepvr_twin.reward import validate_verdict_schema


class TestFreeCSValidate(unittest.TestCase):
    def setUp(self) -> None:
        self.base = build_valid_freecs_bundle()

    def test_valid_full_bundle_passes_under_5ms(self) -> None:
        v = validate_bundle(self.base, workload="unit-valid-bundle")
        self.assertTrue(v["pass"], v)
        self.assertLess(v["eval_ms"], 5.0)
        self.assertGreater(v["reward"], 0.0)
        self.assertEqual(v["checks"]["texstate_samples"], 4)
        self.assertEqual(ppp_expected_dwords(TA_HEADER_SINGLE_VP_PROOF), 23)
        self.assertEqual(validate_verdict_schema(v), [])

    def test_catches_40b_rogue_cccb_header_drift(self) -> None:
        b = copy.deepcopy(self.base)
        b["fw_abi"]["cccb_hdr_size"] = ROGUE_CCCB_HDR_SIZE
        v = validate_bundle(b, workload="neg-40b-cccb")
        self.assertFalse(v["pass"])
        self.assertLess(v["eval_ms"], 5.0)
        self.assertIn("FWABI_CCCB_HDR_ROGUE_40B", [d["code"] for d in v["diagnostics"]])
        self.assertEqual(validate_verdict_schema(v), [])

    def test_catches_rogue_40b_padded_cccb_stream(self) -> None:
        b = copy.deepcopy(self.base)
        # 16B header + 24B Rogue zero work_est_kick_data + 16B payload
        b["fw_abi"]["cccb_stream"] = struct.pack("<IIII", 1, 40, 1, 2) + (b"\x00" * 24) + struct.pack("<IIII", 0xC0000001, 0x1000, 0x2000, 1)
        b["fw_abi"]["expect_nonzero_payload_head"] = True
        v = validate_bundle(b, workload="neg-40b-cccb-stream")
        self.assertFalse(v["pass"])
        self.assertIn("FWABI_CCCB_ROGUE_40B_SHIFT", [d["code"] for d in v["diagnostics"]])

    def test_catches_16b_rogue_cccb_ctl_drift(self) -> None:
        b = copy.deepcopy(self.base)
        b["fw_abi"]["cccb_ctl_size"] = ROGUE_CCCB_CTL_SIZE
        v = validate_bundle(b, workload="neg-16b-ctl")
        self.assertFalse(v["pass"])
        self.assertIn("FWABI_CCCB_CTL_ROGUE_16B", [d["code"] for d in v["diagnostics"]])

    def test_catches_laguna_config_flags_retest4_regression(self) -> None:
        b = copy.deepcopy(self.base)
        b["fw_abi"]["config_flags"] = 0x00020004  # Retest #4 regression!
        v = validate_bundle(b, workload="neg-config-flags")
        self.assertFalse(v["pass"])
        codes = {d["code"] for d in v["diagnostics"]}
        self.assertIn("FWABI_CONFIG_FLAGS_MISSING", codes)
        self.assertIn("FWABI_HWPERF_EN_SET", codes)

    def test_catches_zero_hwperf_dma_addr(self) -> None:
        b = copy.deepcopy(self.base)
        b["fw_abi"]["hwperf_ctl_dma_addr"] = {"dev_addr": 0, "fw_addr": 0xC0010000}
        v = validate_bundle(b, workload="neg-hwperf-dma-zero")
        self.assertFalse(v["pass"])
        self.assertIn("FWABI_HWPERF_DMA_ZERO", [d["code"] for d in v["diagnostics"]])

    def test_catches_soft_reset_invalid_bits_and_irq_offset_drift(self) -> None:
        b = copy.deepcopy(self.base)
        b["fw_abi"]["soft_reset"] = 1 << 63  # reserved bit
        b["fw_abi"]["irq_os0_event_clear_offset"] = 0x0BD8  # old offset
        v = validate_bundle(b, workload="neg-cr-drift")
        self.assertFalse(v["pass"])
        codes = {d["code"] for d in v["diagnostics"]}
        self.assertIn("CR_SOFT_RESET_INVALID_BITS", codes)
        self.assertIn("CR_IRQ_CLEAR_OFFSET_MISMATCH", codes)

    def test_catches_cdm_missing_terminate_and_unaligned_code_addr(self) -> None:
        b = copy.deepcopy(self.base)
        b["cdm"]["words"][2] |= 0x4  # misaligned code_addr
        b["cdm"]["words"] = b["cdm"]["words"][:8]  # remove primary terminate kernel
        v = validate_bundle(b, workload="neg-cdm")
        self.assertFalse(v["pass"])
        codes = {d["code"] for d in v["diagnostics"]}
        self.assertIn("CDM_K2_CODE_UNALIGNED", codes)
        self.assertIn("CDM_MISSING_TERMINATE", codes)

    def test_catches_volcanic_primary_stream_return_0x80000000_retest8(self) -> None:
        # 1. Catch Retest #8: primary CDM stream ending in block_type=2 (0x80000000) pops CR 0x0598+0x180 -> va=0x0!
        b = copy.deepcopy(self.base)
        b["cdm"]["words"] = b["cdm"]["words"][:8] + [0x80000000]
        v = validate_bundle(b, workload="neg-retest8-stream-return")
        self.assertFalse(v["pass"])
        self.assertLess(v["eval_ms"], 5.0)
        self.assertIn("CDM_PRIMARY_STREAM_RETURN_FAULT", [d["code"] for d in v["diagnostics"]])

        # 2. Sub-stream explicitly marked is_substream=True allows STREAM_RETURN (0x80000000)
        b_sub = copy.deepcopy(self.base)
        b_sub["cdm"] = {"words": b["cdm"]["words"][:8] + [0x80000000], "is_substream": True}
        v_sub = validate_bundle(b_sub, workload="pos-retest8-substream-return")
        self.assertTrue(v_sub["pass"], v_sub)
        self.assertTrue(v_sub["checks"]["cdm_returned"])

        # 3. Volcanic fence PDS DOUTC (0xF4001003) + DOUTD with non-zero doffset validate cleanly
        b_pds = copy.deepcopy(self.base)
        b_pds["pds"]["dout_insts"] = [0xF0001000, 0xF4001003]  # DOUTD + DOUTC
        b_pds["pds"]["doutd_src1"] = 0x90100004  # last=1, dest=COMMON(1), doffset=128, bsize=4
        v_pds = validate_bundle(b_pds, workload="pos-retest8-doutc-pds")
        self.assertTrue(v_pds["pass"], v_pds)

        # 4. Warn when 128-invocation compute kernel leaves wg_mode=0
        b_wg = copy.deepcopy(self.base)
        b_wg["cdm"]["words"][0] &= ~(0x3 << 20)
        v_wg = validate_bundle(b_wg, workload="warn-wg128-wg-mode")
        self.assertTrue(v_wg["pass"], v_wg)
        self.assertIn("CDM_WG128_WG_MODE_ZERO", [d["code"] for d in v_wg["diagnostics"]])

        # 5. Decode live Retest #8 MMU_FAULT_STATUS1/2 telemetry (va=0x0, req=CDM(0x15), pc=21/22)
        for status1, expected_pc in ((0x9515000000000009, 21), (0x9516000000000009, 22)):
            dec8 = decode_mmu_fault_status(status1, 0x8000)
            self.assertTrue(dec8["valid"])
            self.assertTrue(dec8["read"])
            self.assertEqual(dec8["va"], 0x0)
            self.assertEqual(dec8["level"], 2)
            self.assertEqual(dec8["level_name"], "PC")
            self.assertEqual(dec8["pc_ctx"], expected_pc)
            self.assertEqual(dec8["req_id"], 0x15)
            self.assertEqual(dec8["req_name"], "CDM")
            self.assertEqual(dec8["bank"], 8)

    def test_catches_cdm_workgroup_invocations_over_512(self) -> None:
        b = copy.deepcopy(self.base)
        b["cdm"]["words"][1] &= ~(1 << 1)  # usc_common_shared = 0
        b["cdm"]["words"][7] = (511 << 20) | (1 << 10) | 0  # 512 x 2 x 1 = 1024 > 512
        v = validate_bundle(b, workload="neg-cdm-wg-oob")
        self.assertFalse(v["pass"])
        self.assertIn("CDM_WORKGROUP_INVOC_OOB", [d["code"] for d in v["diagnostics"]])

    def test_catches_dce_invalid_opcode_and_bad_topology_and_handle_va(self) -> None:
        b = copy.deepcopy(self.base)
        b["dce"]["qwords"][0] = (0x0B << 56) | 1  # invalid opcode 0x0b
        b["dce"]["handle_entries"] = [(4 << 32) | 0x0]  # VA = 0
        v = validate_bundle(b, workload="neg-dce")
        self.assertFalse(v["pass"])
        codes = {d["code"] for d in v["diagnostics"]}
        self.assertIn("DCE_INVALID_OPCODE", codes)
        self.assertIn("DCE_HANDLE_VA_RANGE", codes)

    def test_catches_ppp_removed_rogue_pds_bits_and_pstbl_alignment(self) -> None:
        b = copy.deepcopy(self.base)
        b["ppp"]["words"][0] |= (1 << 6) | (1 << 9)
        b["ppp"]["pixel_shader_table"]["base_addr"] = 0x8000500008
        b["ppp"]["pixel_shader_table"]["entries"][0][1] &= ~(1 << 9)
        v = validate_bundle(b, workload="neg-ppp")
        self.assertFalse(v["pass"])
        codes = {d["code"] for d in v["diagnostics"]}
        self.assertIn("PPP_ROGUE_INLINE_PDS_BIT", codes)
        self.assertIn("PPP_PSTBL_UNALIGNED", codes)
        self.assertIn("PPP_PSTBL_VALID_BIT_CLEAR", codes)

    def test_catches_pbe_rogue_shift2_and_zls_bounds(self) -> None:
        b = copy.deepcopy(self.base)
        exp_va = b["pbe"]["expected_va"]
        b["pbe"]["state_words"][0] = (exp_va >> 2) & 0xFFFFFFFF
        b["pbe"]["state_words"][1] = (b["pbe"]["state_words"][1] & ~0x3FFF) | (((exp_va >> 2) >> 32) & 0x1FFF)
        b["zls_v2"]["zls_words"][1] = (100 << 48) | (50 << 32) | (0 << 16) | 100  # min_x > max_x_m1
        v = validate_bundle(b, workload="neg-pbe-zls")
        self.assertFalse(v["pass"])
        codes = {d["code"] for d in v["diagnostics"]}
        self.assertIn("PBE_ROGUE_SHIFT2_VA", codes)
        self.assertIn("ZLS_CLIP_BOUNDS", codes)

    def test_catches_pds_rogue_slcmode_and_texstate_14b_and_shift2(self) -> None:
        b = copy.deepcopy(self.base)
        b["pds"]["doutd_src0"] |= 1 << 62
        b["texstate"]["image_words"][0] |= 1 << 61
        exp_va = b["texstate"]["expected_va"]
        b["texstate"]["image_words"][1] = (exp_va >> 2) & ((1 << 45) - 1)
        v = validate_bundle(b, workload="neg-pds-texstate")
        self.assertFalse(v["pass"])
        codes = {d["code"] for d in v["diagnostics"]}
        self.assertIn("PDS_DOUTD_ROGUE_SLCMODE_BIT62", codes)
        self.assertIn("TEXSTATE_UPSTREAM_14B_WIDTH_HEIGHT_DRIFT", codes)
        self.assertIn("TEXSTATE_ROGUE_SHIFT2_VA", codes)

    def test_catches_laguna_false_dma_coherent_and_mmuv4_rogue_bits(self) -> None:
        # 1. Catch Retest #6 false dma-coherent on Laguna gpu0 + Rogue MMU bits
        b = copy.deepcopy(self.base)
        b["mmuv4"]["gpu_dma_coherent"] = True
        b["mmuv4"]["pce"] = encode_mmuv4_pce(0x88EFE8000) | (1 << 1)  # Rogue PC PENDING
        b["mmuv4"]["pde"] = encode_mmuv4_pde(0x88EFE9000) | (1 << 40) | (1 << 1)  # Rogue PD PENDING + PAGESIZE
        b["mmuv4"]["pte"] = encode_mmuv4_pte(0x88EFEA000, axcache=MMUV4_AXCACHE_CACHED) | (1 << 3) | (1 << 5)
        v = validate_bundle(b, workload="neg-mmuv4-retest6")
        self.assertFalse(v["pass"])
        codes = {d["code"] for d in v["diagnostics"]}
        self.assertIn("MMUV4_LAGUNA_FALSE_DMA_COHERENT", codes)
        self.assertIn("MMUV4_PC_ROGUE_PENDING_BIT", codes)
        self.assertIn("MMUV4_PD_ROGUE_PENDING_BIT", codes)
        self.assertIn("MMUV4_PD_ROGUE_PAGESIZE_BITS", codes)
        self.assertIn("MMUV4_PT_ROGUE_SLC_BYPASS_BIT", codes)
        self.assertIn("MMUV4_PT_ROGUE_PENDING_BIT", codes)

        # 2. Decode live Retest #6 MMU_FAULT_STATUS1/2 telemetry
        dec = decode_mmu_fault_status(0x9515008000068009, 0x80B0)
        self.assertTrue(dec["valid"])
        self.assertTrue(dec["read"])
        self.assertEqual(dec["va"], 0x8000068000)
        self.assertEqual(dec["level"], 2)
        self.assertEqual(dec["level_name"], "PC")
        self.assertEqual(dec["pc_ctx"], 21)
        self.assertEqual(dec["req_id"], 0x15)
        self.assertEqual(dec["req_name"], "CDM")
        self.assertEqual(dec["bank"], 8)
        self.assertEqual(dec["type"], 0)

    def test_catches_volcanic_cdm_stack_state_zero_and_overlap_retest7(self) -> None:
        # 1. Catch Retest #7: CMDCDM+0x20 (CR 0x0598 CDM Stack State Base) == 0 -> CDM(0x1b) faults at va=0x180
        b = copy.deepcopy(self.base)
        b["fw_abi"]["cmd_cdm"]["cdm_stack_state_base"] = 0
        v = validate_bundle(b, workload="neg-retest7-stack-zero")
        self.assertFalse(v["pass"])
        self.assertLess(v["eval_ms"], 5.0)
        self.assertIn("CMDCDM_STACK_STATE_BASE_ZERO", [d["code"] for d in v["diagnostics"]])

        # 2. Catch misalignment (<256B) and overlap with 1024B CDM Context State [0x8000043000..0x8000043400)
        b2 = copy.deepcopy(self.base)
        b2["fw_abi"]["cmd_cdm"]["cdm_stack_state_base"] = 0x8000043280
        v2 = validate_bundle(b2, workload="neg-retest7-stack-align-overlap")
        self.assertFalse(v2["pass"])
        codes2 = {d["code"] for d in v2["diagnostics"]}
        self.assertIn("CMDCDM_STACK_STATE_UNALIGNED", codes2)
        self.assertIn("CMDCDM_STACK_STATE_OVERLAPS_CTX_STATE", codes2)

        # 3. Decode live Retest #7 MMU_FAULT_STATUS1/2 telemetry (va=0x180, req=CDM(0x1b))
        dec7 = decode_mmu_fault_status(0x9B15000000000189, 0x8400)
        self.assertTrue(dec7["valid"])
        self.assertTrue(dec7["read"])
        self.assertEqual(dec7["va"], 0x180)
        self.assertEqual(dec7["level"], 2)
        self.assertEqual(dec7["level_name"], "PC")
        self.assertEqual(dec7["pc_ctx"], 21)
        self.assertEqual(dec7["req_id"], 0x1B)
        self.assertEqual(dec7["req_name"], "CDM")
        self.assertEqual(dec7["bank"], 8)


class TestFreeUSCSim(unittest.TestCase):
    def test_17_family_prefix_tree_and_invalid_codes(self) -> None:
        invalid_codes = {
            0x04, 0x14, 0x22, 0x24, 0x28, 0x2E, 0x32, 0x34, 0x44, 0x54,
            0x62, 0x64, 0x68, 0x6E, 0x72, 0x74, 0x7A, 0x7C, 0x7E,
        }
        valid_count = 0
        for code in range(128):
            fam = classify_w0(code)
            if code in invalid_codes:
                self.assertEqual(fam, "INVALID", f"code 0x{code:02x}")
            else:
                self.assertNotEqual(fam, "INVALID", f"code 0x{code:02x}")
                valid_count += 1
        self.assertEqual(valid_count, 109)

    def test_shipping_dxt_48_1536_vectors_decode_cleanly(self) -> None:
        vectors = [
            [0x0600D641, 0x0000080C, 0x80000408],  # FOP fmad
            [0x0414A76B, 0x80408808],              # FOP fadd
            [0x091023F0, 0x81854008],              # INT iadd
            [0x00069278, 0xA5B608A0],              # BIT38
            [0x408381CA, 0x80531200],              # MOV
            [0x0005834E, 0x5808021E, 0x01000380, 0x83000004],  # DMA ld
            [0x040445CE, 0x18080218, 0x010003F0, 0x83000004],  # DMA st
            [0x1002181C, 0x80000200],              # CTRL.BR rel=+336
            [0x8000031C],                          # CTRL.END
        ]
        for vec in vectors:
            insts, errs = decode_stream(vec)
            self.assertEqual(errs, [], f"failed vector {[hex(x) for x in vec]}")
            self.assertEqual(len(insts), 1)
        br_inst, _ = decode_stream([0x1002181C, 0x80000200])
        self.assertEqual(br_inst[0].fields["rel_offset"], 336)

    def test_clean_bda_fma_128_lanes(self) -> None:
        words, _ = build_clean_bda_fma_kernel()
        mem = GlobalMemory()
        in_va, out_va = DEFAULT_SSBO_VA, DEFAULT_SSBO_VA + 0x1000
        mem.map_region(in_va, 4096, struct.pack(f"<{SUBGROUP_SIZE}f", *[float(i) for i in range(SUBGROUP_SIZE)]))
        mem.map_region(out_va, 4096)
        st = VolcanicUSCState(
            sh_init={0: in_va & 0xFFFFFFFF, 1: in_va >> 32, 2: out_va & 0xFFFFFFFF, 3: out_va >> 32},
            memory=mem,
        )
        v = evaluate_compute_shader(
            words,
            workload="unit-clean-bda-fma",
            state=st,
            ssbo_va=out_va,
            expected_f32=[float(i) * 2.0 + 1.0 for i in range(SUBGROUP_SIZE)],
        )
        self.assertTrue(v["pass"], v)
        self.assertEqual(v["max_abs_err"], 0.0)
        self.assertEqual(validate_verdict_schema(v), [])

    def test_clean_bitwise_loop_barrier_128_lanes(self) -> None:
        words = build_clean_bitwise_loop_barrier_kernel()
        mem = GlobalMemory()
        mem.map_region(DEFAULT_SSBO_VA, 4096)
        st = VolcanicUSCState(
            sh_init={0: DEFAULT_SSBO_VA & 0xFFFFFFFF, 1: DEFAULT_SSBO_VA >> 32},
            memory=mem,
        )
        expected = [((4 * i) ^ 0xFF) >> 1 for i in range(SUBGROUP_SIZE)]
        v = evaluate_compute_shader(words, workload="unit-bitwise-loop", state=st, ssbo_va=DEFAULT_SSBO_VA, expected_u32=expected)
        self.assertTrue(v["pass"], v)
        self.assertEqual(v["checks"]["wg_barriers"], 1)

    def test_clean_128wide_subgroup_broadcast_add_shuffle_ballot(self) -> None:
        words = build_clean_subgroup_ballot_add_shuffle_kernel()
        mem = GlobalMemory()
        mem.map_region(DEFAULT_SSBO_VA, 4096)
        st = VolcanicUSCState(
            sh_init={0: DEFAULT_SSBO_VA & 0xFFFFFFFF, 1: DEFAULT_SSBO_VA >> 32},
            memory=mem,
        )
        expected = []
        for lane in range(SUBGROUP_SIZE):
            expected.extend([1042, 8128, 127 - lane, 0x55555555])
        v = evaluate_compute_shader(words, workload="unit-subgroup-128", state=st, ssbo_va=DEFAULT_SSBO_VA, expected_u32=expected)
        self.assertTrue(v["pass"], v)
        self.assertEqual(v["max_abs_err"], 0.0)

    def test_clean_bda_atomic_add_and_even_sh_alignment_fault(self) -> None:
        words = build_clean_atomic_kernel()
        mem = GlobalMemory()
        mem.map_region(DEFAULT_SSBO_VA, 64)
        st = VolcanicUSCState(sh_init={0: DEFAULT_SSBO_VA & 0xFFFFFFFF, 1: DEFAULT_SSBO_VA >> 32}, memory=mem)
        v = evaluate_compute_shader(words, workload="unit-atomic-add", state=st, ssbo_va=DEFAULT_SSBO_VA, expected_u32=[8128])
        self.assertTrue(v["pass"], v)

        # Odd SH register base (sh1) must fault per hardware even-alignment rule!
        bad_words = encode_dma(DmaOp.LD, addr_base=Reg(Bank.SH, 1), dst=Reg(Bank.TEMP, 0), burst_bytes=4) + encode_ctrl(CtrlOp.END)
        v_bad = evaluate_compute_shader(bad_words, workload="neg-odd-sh-bda")
        self.assertFalse(v_bad["pass"])
        self.assertIn("even alignment", v_bad["fault_reason"])

    def test_af32_transcendentals_and_brn73233(self) -> None:
        r = lambda i: Reg(Bank.TEMP, i)
        # r0 = 4.0f (0x40800000), AF32.FRSQ -> 0.5f, AF32.FLOG -> 2.0f
        words = (
            encode_ctrl(CtrlOp.LIMM, dst=0, imm=0x40800000, bcc=4)
            + encode_int(IntOp.IMOV, r(0), Reg(Bank.SLOT, 0))
            + encode_af32(Af32Op.FRSQ, r(1), r(0))
            + encode_af32(Af32Op.FLOG, r(2), r(0))
            + encode_ctrl(CtrlOp.END)
        )
        st = VolcanicUSCState(num_lanes=4)
        sim = simulate_words(words, st)
        self.assertTrue(sim.passed, sim.fault_reason)
        self.assertAlmostEqual(_u32_to_f32(st.temp[1][0]), 0.5, places=5)
        self.assertAlmostEqual(_u32_to_f32(st.temp[2][0]), 2.0, places=5)

        # BRN73233: FSIN with src_neg=True must fault!
        bad_af32 = encode_af32(Af32Op.FSIN, r(1), r(0), src_neg=True) + encode_ctrl(CtrlOp.END)
        sim_bad = simulate_words(bad_af32, VolcanicUSCState(num_lanes=4))
        self.assertFalse(sim_bad.passed)
        self.assertIn("BRN73233", sim_bad.fault_reason or "")

    def test_mesa_stage_g3b_fma32_bit_exact_128_lanes(self) -> None:
        """KILLER PROOF 1: Actual Mesa Stage G3B fma32 (352B / 88 dwords) computes bit-exact fma(x, 2, x+1)."""
        self.assertEqual(len(MESA_STAGE_G3B_FMA32_WORDS), 88)
        v = run_mesa_fma32()
        self.assertTrue(v["pass"], v)
        self.assertEqual(v["max_abs_err"], 0.0)
        self.assertEqual(v["nmse"], 0.0)
        self.assertEqual(v["checks"]["lanes"], 128)
        self.assertEqual(v["checks"]["ssbo_mismatches"], 0)
        self.assertEqual(validate_verdict_schema(v), [])

    def test_mesa_stage_g3b_iadd_sg_surfaces_pco_shift_bug(self) -> None:
        """KILLER PROOF 2: Actual Mesa Stage G3B iadd_sg (276B / 69 dwords) surfaces real PCO_OP_SHIFT bug at PC @32."""
        self.assertEqual(len(MESA_STAGE_G3B_IADD_SG_WORDS), 69)
        v = run_mesa_iadd_sg()
        self.assertFalse(v["pass"])
        self.assertEqual(v["checks"]["ssbo_mismatches"], 128)
        self.assertEqual(v["max_abs_err"], 7.0)
        codes = [d["code"] for d in v["diagnostics"]]
        self.assertIn("USC_BIT_SHIFT_LUT_SRC_MISMATCH", codes)
        self.assertIn("USC_SSBO_MISMATCH", codes)
        # Confirm exact PC @32 (byte offset 0x80 = 128)
        shift_diag = next(d for d in v["diagnostics"] if d["code"] == "USC_BIT_SHIFT_LUT_SRC_MISMATCH")
        self.assertEqual(shift_diag["offset"], 128)
        self.assertIn("0x0155fe36", shift_diag["message"])
        self.assertEqual(validate_verdict_schema(v), [])


if __name__ == "__main__":
    unittest.main()
