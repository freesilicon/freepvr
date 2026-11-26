# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 FreePVR Contributors
"""
Clean-room PowerVR Volcanic (DXT-48-1536 / CXTP-48-1536) Command-Stream & FW-ABI Validator.

Validates Volcanic command streams and Laguna DDK 25.3 firmware ABI descriptors against
the clean-room VDM/PPP/CDM/PBE/ISP/PDS/TEXSTATE and Laguna Stage G3 specifications:
  - FW-ABI / cCCB: 16B Laguna cCCB header vs 40B Rogue drift, 32B CCCB_CTL vs 16B Rogue,
    sysdata.config_flags (0x00020450), non-zero hwperf_ctl_dma_addr, SOFT_RESET mask & bit 37.
  - CDM: BLOCK_TYPE_COMPUTE_KERNEL_EXT=3, 8-bit usc_unified_size, usc_common_size,
    max_instances <= 128, CDMCTRL_STREAM_TERMINATE, aligned pointers.
  - VDM/DCE: 64-bit QWORD DCE opcodes {0x01, 0x03..0x0a, 0x0c}, handle buffer, 16B/36B draw args.
  - PPP / Pixel Shader Table: TA_STATE_HEADER (incl. 23-dw 0x00ff0d87 & removed inline PDS [6]/[9]/[5]),
    16B indirect Pixel Shader Table alignment & valid bit.
  - PBE / ISP / ZLS v2: PBE STATE_WORD0 raw byte VA (catching Rogue shift=2), ISP_CTL, ZLS v2 bounds.
  - PDS / TEXSTATE: Volcanic DOUTU/DOUTD (catching Rogue SLCMODE bit 62 & DOUTU shift),
    TEXSTATE 15-bit 32K width/height, raw byte VA, smpcnt[7:6].
"""

from __future__ import annotations

import struct
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .reward import Diagnostic, make_verdict

# ---------------------------------------------------------------------------
# Authoritative Volcanic / Laguna DDK 25.3 Constants
# ---------------------------------------------------------------------------

LAGUNA_CCCB_HDR_SIZE = 16
ROGUE_CCCB_HDR_SIZE = 40
LAGUNA_CCCB_CTL_SIZE = 32
ROGUE_CCCB_CTL_SIZE = 16
LAGUNA_CMDCDM_SIZE = 120
VOLCANIC_CDM_CTX_STATE_SIZE = 0x400    # 1024 B, 128 B-aligned (CR 0x0498)
VOLCANIC_CDM_CTX_STATE_ALIGN = 0x80
VOLCANIC_CDM_STACK_STATE_SIZE = 0x800  # 2048 B, 256 B-aligned (CR 0x0598 / CMDCDM+0x20)
VOLCANIC_CDM_STACK_STATE_ALIGN = 0x100

LAGUNA_INICFG_HWPERF_EN = 1 << 2
LAGUNA_INICFG_POW_RASCALDUST = 1 << 4
LAGUNA_INICFG_FBCDC_V3_1_EN = 1 << 6
LAGUNA_INICFG_PIPELINED_DM = 1 << 10
LAGUNA_INICFG_CTXSWITCH_MEDIUM = 2 << 16
LAGUNA_INICFG_DEFAULT = (
    LAGUNA_INICFG_POW_RASCALDUST
    | LAGUNA_INICFG_FBCDC_V3_1_EN
    | LAGUNA_INICFG_PIPELINED_DM
    | LAGUNA_INICFG_CTXSWITCH_MEDIUM
)  # 0x00020450

VOLCANIC_SOFT_RESET_MASKFULL = 0x7FFFFFE0007BDEFF
VOLCANIC_SOFT_RESET_GARTEN_EN = 1 << 37
VOLCANIC_IRQ_OS0_EVENT_CLEAR_OFFSET = 0x0BE0

VALID_DCE_OPCODES = frozenset({0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0C})
VALID_DCE_TOPOLOGIES = frozenset({0, 1, 2, 4, 5, 6, 7, 9, 10, 12, 13})
DCE_NON_INDEXED_ARG_DESC = 0x1000000000000000
DCE_INDEXED_ARG_DESC = (0x0000000000000000, 0x2000000000000004)
DCE_HANDLE_VA_LIMIT = 1 << 35  # 32 GiB lower window for (va >> 3) in [31:0]

# Valid Volcanic TA_STATE_HEADER mask (bits 5, 6, 9 removed on Volcanic!)
TA_HEADER_LEGACY_REMOVED_BITS = (1 << 5) | (1 << 6) | (1 << 9)
TA_HEADER_VALID_MASK = (
    (1 << 0)
    | (1 << 1)
    | (1 << 2)
    | (1 << 3)
    | (1 << 4)
    | (1 << 7)
    | (1 << 8)
    | (1 << 10)
    | (1 << 11)
    | (0xF << 12)
    | (1 << 16)
    | (1 << 17)
    | (1 << 18)
    | (1 << 19)
    | (1 << 20)
    | (1 << 21)
    | (1 << 22)
    | (1 << 23)
    | (1 << 25)
)
TA_HEADER_SINGLE_VP_PROOF = 0x00FF0D87
TA_HEADER_TERMINATE_PROOF = 0x02000000

VALID_TE_AA_MODES = frozenset({0, 1, 5, 7})
VALID_PDS_DOUT_OPCODES = frozenset({0xF0000000, 0xF0000002, 0xF0000004})


def _to_u32_list(data: Any) -> List[int]:
    if isinstance(data, (bytes, bytearray)):
        if len(data) % 4 != 0:
            raise ValueError(f"byte stream length {len(data)} not a multiple of 4")
        return list(struct.unpack(f"<{len(data) // 4}I", data))
    return [int(x) & 0xFFFFFFFF for x in data]


def _to_u64_list(data: Any) -> List[int]:
    if isinstance(data, (bytes, bytearray)):
        if len(data) % 8 != 0:
            raise ValueError(f"byte stream length {len(data)} not a multiple of 8")
        return list(struct.unpack(f"<{len(data) // 8}Q", data))
    return [int(x) & 0xFFFFFFFFFFFFFFFF for x in data]


def _err(diags: List[Diagnostic], code: str, message: str, offset: Any = None, field: Optional[str] = None) -> None:
    diags.append(Diagnostic(code=code, severity="error", message=message, offset=offset, field=field))


def _warn(diags: List[Diagnostic], code: str, message: str, offset: Any = None, field: Optional[str] = None) -> None:
    diags.append(Diagnostic(code=code, severity="warning", message=message, offset=offset, field=field))


# ---------------------------------------------------------------------------
# 1. FW-ABI / cCCB / Laguna SysData Validator
# ---------------------------------------------------------------------------

def validate_fw_abi(spec: Dict[str, Any], diags: List[Diagnostic], checks: Dict[str, Any]) -> None:
    """Validate Laguna DDK 25.3 FW-ABI descriptors & cCCB framing."""
    ok = True

    if "cccb_hdr_size" in spec:
        hdr_sz = int(spec["cccb_hdr_size"])
        if hdr_sz == ROGUE_CCCB_HDR_SIZE:
            ok = False
            _err(
                diags,
                "FWABI_CCCB_HDR_ROGUE_40B",
                "cCCB header size is 40B (Rogue layout with work_est_kick_data); Laguna DDK 25.3 requires 16B",
                field="cccb_hdr_size",
            )
        elif hdr_sz != LAGUNA_CCCB_HDR_SIZE:
            ok = False
            _err(
                diags,
                "FWABI_CCCB_HDR_SIZE",
                f"cCCB header size {hdr_sz} != 16B Laguna DDK 25.3 ABI",
                field="cccb_hdr_size",
            )

    if "cccb_ctl_size" in spec:
        ctl_sz = int(spec["cccb_ctl_size"])
        if ctl_sz == ROGUE_CCCB_CTL_SIZE:
            ok = False
            _err(
                diags,
                "FWABI_CCCB_CTL_ROGUE_16B",
                "CCCB_CTL size is 16B (Rogue layout); Laguna DDK 25.3 requires 32B",
                field="cccb_ctl_size",
            )
        elif ctl_sz != LAGUNA_CCCB_CTL_SIZE:
            ok = False
            _err(
                diags,
                "FWABI_CCCB_CTL_SIZE",
                f"CCCB_CTL size {ctl_sz} != 32B Laguna DDK 25.3 ABI",
                field="cccb_ctl_size",
            )

    if "cccb_stream" in spec:
        raw = spec["cccb_stream"]
        if isinstance(raw, (list, tuple)):
            raw = struct.pack(f"<{len(raw)}I", *[int(w) & 0xFFFFFFFF for w in raw])
        elif isinstance(raw, str):
            raw = bytes.fromhex(raw.replace(" ", ""))
        buf_len = len(raw)
        if buf_len < LAGUNA_CCCB_HDR_SIZE or (buf_len % 4) != 0:
            ok = False
            _err(diags, "FWABI_CCCB_TRUNCATED", f"cCCB stream length {buf_len}B invalid", offset=0)
        else:
            off = 0
            cmd_count = 0
            valid_cmd_types = set(spec.get("valid_cmd_types", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 101, 102, 103]))
            while off < buf_len:
                if off + LAGUNA_CCCB_HDR_SIZE > buf_len:
                    ok = False
                    _err(diags, "FWABI_CCCB_HDR_OOB", "Truncated 16B cCCB header at end of stream", offset=off)
                    break
                cmd_type, cmd_size, ext_ref, int_ref = struct.unpack_from("<IIII", raw, off)
                if cmd_type == 0 or (cmd_type > 0xFFFF and cmd_type not in valid_cmd_types):
                    ok = False
                    _err(
                        diags,
                        "FWABI_CCCB_UNKNOWN_CMD",
                        f"Unknown cCCB cmd_type=0x{cmd_type:08x} (possible 40B-vs-16B header shift)",
                        offset=off,
                        field="cmd_type",
                    )
                    break
                if (cmd_size % 4) != 0:
                    ok = False
                    _err(diags, "FWABI_CCCB_SIZE_ALIGN", f"cmd_size={cmd_size} not 4B-aligned", offset=off, field="cmd_size")
                    break
                total = LAGUNA_CCCB_HDR_SIZE + cmd_size
                if off + total > buf_len:
                    ok = False
                    _err(
                        diags,
                        "FWABI_CCCB_PAYLOAD_OOB",
                        f"cmd_size={cmd_size} exceeds remaining cCCB stream ({buf_len - off - LAGUNA_CCCB_HDR_SIZE}B)",
                        offset=off,
                        field="cmd_size",
                    )
                    break
                # Detect accidental 40-byte Rogue header: 24 zero bytes inserted before payload when caller passes expected_payload_offset
                if spec.get("detect_rogue_40b_padding", True) and cmd_size >= 24:
                    pad24 = raw[off + 16 : off + 40]
                    if all(b == 0 for b in pad24) and cmd_size > 24 and spec.get("expect_nonzero_payload_head", False):
                        ok = False
                        _err(
                            diags,
                            "FWABI_CCCB_ROGUE_40B_SHIFT",
                            "24 zero padding bytes after 16B header indicate Rogue 40B cCCB header drift",
                            offset=off + 16,
                            field="work_est_kick_data",
                        )
                off += total
                cmd_count += 1
            checks["cccb_commands_parsed"] = cmd_count

    if "config_flags" in spec:
        cf = int(spec["config_flags"]) & 0xFFFFFFFF
        required_set = LAGUNA_INICFG_POW_RASCALDUST | LAGUNA_INICFG_FBCDC_V3_1_EN | LAGUNA_INICFG_PIPELINED_DM
        missing = required_set & ~cf
        if missing:
            ok = False
            names = []
            if missing & LAGUNA_INICFG_POW_RASCALDUST:
                names.append("POW_RASCALDUST(BIT4)")
            if missing & LAGUNA_INICFG_FBCDC_V3_1_EN:
                names.append("FBCDC_V3_1_EN(BIT6)")
            if missing & LAGUNA_INICFG_PIPELINED_DM:
                names.append("PIPELINED_DM(BIT10)")
            _err(
                diags,
                "FWABI_CONFIG_FLAGS_MISSING",
                f"sysdata->config_flags=0x{cf:08x} missing {', '.join(names)} (expected 0x{LAGUNA_INICFG_DEFAULT:08x})",
                field="sysdata.config_flags",
            )
        if cf & LAGUNA_INICFG_HWPERF_EN:
            ok = False
            _err(
                diags,
                "FWABI_HWPERF_EN_SET",
                f"sysdata->config_flags=0x{cf:08x} sets HWPERF_EN(BIT2); must be clear (0x{LAGUNA_INICFG_DEFAULT:08x})",
                field="sysdata.config_flags",
            )
        if cf != LAGUNA_INICFG_DEFAULT and not missing and not (cf & LAGUNA_INICFG_HWPERF_EN):
            _warn(
                diags,
                "FWABI_CONFIG_FLAGS_NONDEFAULT",
                f"sysdata->config_flags=0x{cf:08x} differs from shipping default 0x{LAGUNA_INICFG_DEFAULT:08x}",
                field="sysdata.config_flags",
            )

    if "hwperf_ctl_dma_addr" in spec:
        dma = spec["hwperf_ctl_dma_addr"]
        if isinstance(dma, dict):
            dev_addr = int(dma.get("dev_addr", 0))
            fw_addr = int(dma.get("fw_addr", 0))
            if dev_addr == 0 or fw_addr == 0:
                ok = False
                _err(
                    diags,
                    "FWABI_HWPERF_DMA_ZERO",
                    f"hwperf_ctl_dma_addr has zero field (dev_addr=0x{dev_addr:x}, fw_addr=0x{fw_addr:x}); causes boot META DMA MMU fault",
                    field="hwperf_rtcfg.hwperf_ctl_dma_addr",
                )
        else:
            if int(dma) == 0:
                ok = False
                _err(
                    diags,
                    "FWABI_HWPERF_DMA_ZERO",
                    "hwperf_ctl_dma_addr is 0; causes boot META DMA MMU fault at VA=0x0",
                    field="hwperf_rtcfg.hwperf_ctl_dma_addr",
                )

    if "soft_reset" in spec:
        sr = int(spec["soft_reset"])
        invalid_bits = sr & ~VOLCANIC_SOFT_RESET_MASKFULL
        if invalid_bits:
            ok = False
            _err(
                diags,
                "CR_SOFT_RESET_INVALID_BITS",
                f"RGX_CR_SOFT_RESET=0x{sr:016x} sets invalid bits 0x{invalid_bits:016x} outside MASKFULL 0x{VOLCANIC_SOFT_RESET_MASKFULL:016x}",
                field="RGX_CR_SOFT_RESET",
            )

    if "irq_os0_event_clear_offset" in spec:
        irq_off = int(spec["irq_os0_event_clear_offset"])
        if irq_off != VOLCANIC_IRQ_OS0_EVENT_CLEAR_OFFSET:
            ok = False
            _err(
                diags,
                "CR_IRQ_CLEAR_OFFSET_MISMATCH",
                f"IRQ_OS0_EVENT_CLEAR offset 0x{irq_off:04x} != 0x{VOLCANIC_IRQ_OS0_EVENT_CLEAR_OFFSET:04x} (legacy 0x0BD8 drift)",
                field="IRQ_OS0_EVENT_CLEAR",
            )

    if "cmd_cdm" in spec:
        cmd_cdm = spec["cmd_cdm"]
        if "size_bytes" in cmd_cdm:
            sz = int(cmd_cdm["size_bytes"])
            if sz != LAGUNA_CMDCDM_SIZE:
                ok = False
                _err(
                    diags,
                    "FWABI_CMDCDM_SIZE",
                    f"CMDCDM payload size {sz} != {LAGUNA_CMDCDM_SIZE}B (136B with 16B Laguna cCCB header)",
                    field="cmd_cdm.size_bytes",
                )
        cs_base = int(cmd_cdm.get("cdm_ctrl_stream_base", 0))
        if cs_base == 0 or (cs_base & 0x3) != 0 or cs_base >= (1 << 40):
            ok = False
            _err(
                diags,
                "CMDCDM_CTRL_STREAM_BASE_INVALID",
                f"cdm_ctrl_stream_base=0x{cs_base:x} must be non-zero 4B-aligned 40-bit GPU VA (CR 0x04e8)",
                field="cmd_cdm.cdm_ctrl_stream_base",
            )
        ctx_base = int(cmd_cdm.get("cdm_context_state_base_addr", 0))
        if ctx_base == 0 or (ctx_base & (VOLCANIC_CDM_CTX_STATE_ALIGN - 1)) != 0 or ctx_base >= (1 << 40):
            ok = False
            _err(
                diags,
                "CMDCDM_CTX_STATE_BASE_INVALID",
                f"cdm_context_state_base_addr=0x{ctx_base:x} must be non-zero 128B-aligned 40-bit GPU VA (CR 0x0498)",
                field="cmd_cdm.cdm_context_state_base_addr",
            )
        # CMDCDM + 0x20 (regs + 0x18 -> CR 0x0598/0x059c): Volcanic CDM Stack State Base (2048B, 256B-aligned).
        # Leaving this 0 (Rogue non-UMQ cdm_cb_queue omission) faults hardware CDM(0x1b) at va=0x180!
        stack_base = int(cmd_cdm.get("cdm_stack_state_base", cmd_cdm.get("cdm_cb_queue", 0)))
        if stack_base == 0:
            ok = False
            _err(
                diags,
                "CMDCDM_STACK_STATE_BASE_ZERO",
                "CMDCDM+0x20 (CR 0x0598 Volcanic CDM Stack State Base) is 0; hardware CDM(0x1b) faults at va=0x180 (non-zero 2048B, 256B-aligned GPU R/W scratchpad required)",
                field="cmd_cdm.cdm_stack_state_base",
            )
        else:
            if (stack_base & (VOLCANIC_CDM_STACK_STATE_ALIGN - 1)) != 0 or stack_base >= (1 << 40):
                ok = False
                _err(
                    diags,
                    "CMDCDM_STACK_STATE_UNALIGNED",
                    f"cdm_stack_state_base=0x{stack_base:x} must be 256B-aligned 40-bit GPU VA (CR 0x0598)",
                    field="cmd_cdm.cdm_stack_state_base",
                )
            if ctx_base != 0:
                ctx_end = ctx_base + VOLCANIC_CDM_CTX_STATE_SIZE
                stack_end = stack_base + VOLCANIC_CDM_STACK_STATE_SIZE
                if not (stack_end <= ctx_base or stack_base >= ctx_end):
                    ok = False
                    _err(
                        diags,
                        "CMDCDM_STACK_STATE_OVERLAPS_CTX_STATE",
                        f"CDM Stack State [0x{stack_base:x}, 0x{stack_end:x}) overlaps 1024B CDM Context State [0x{ctx_base:x}, 0x{ctx_end:x})",
                        field="cmd_cdm.cdm_stack_state_base",
                    )
        checks["cmd_cdm_stack_state_base"] = f"0x{stack_base:x}"

    checks["fw_abi_ok"] = ok


# ---------------------------------------------------------------------------
# 2. CDM (Compute Data Master) Stream Validator
# ---------------------------------------------------------------------------

def validate_cdm_stream(words_in: Any, diags: List[Diagnostic], checks: Dict[str, Any], require_terminate: bool = True) -> None:
    """Validate Volcanic CDM control stream dwords."""
    ok = True
    try:
        words = _to_u32_list(words_in)
    except ValueError as exc:
        _err(diags, "CDM_UNALIGNED_STREAM", str(exc), offset=0)
        checks["cdm_ok"] = False
        return

    if not words:
        _err(diags, "CDM_EMPTY_STREAM", "CDM control stream is empty", offset=0)
        checks["cdm_ok"] = False
        return

    i = 0
    kernels = 0
    ext_kernels = 0
    terminated = False

    while i < len(words):
        w0 = words[i]
        block_type = (w0 >> 30) & 0x3
        off_bytes = i * 4

        if block_type == 2:  # CDMCTRL_STREAM_TERMINATE
            terminated = True
            i += 1
            if i < len(words):
                _warn(diags, "CDM_TRAILING_AFTER_TERM", f"{len(words) - i} trailing dwords after STREAM_TERMINATE", offset=i * 4)
            break

        if block_type == 1:  # STREAM_LINK (2 dwords)
            if i + 2 > len(words):
                ok = False
                _err(diags, "CDM_LINK_TRUNCATED", "Truncated CDM STREAM_LINK (requires 2 dwords)", offset=off_bytes)
                break
            w1 = words[i + 1]
            addr_lo = w1 & 0xFFFFFFFF
            if (addr_lo & 0x3) != 0:
                ok = False
                _err(diags, "CDM_LINK_UNALIGNED", f"STREAM_LINK addr_lo=0x{addr_lo:08x} not 4B-aligned", offset=off_bytes + 4, field="link_addr")
            i += 2
            continue

        if block_type in (0, 3):  # COMPUTE_KERNEL / COMPUTE_KERNEL_EXT
            indirect = (w0 >> 29) & 1
            event_obj = (w0 >> 28) & 1
            usc_unified_size = (w0 >> 12) & 0xFF
            pds_temp_size = (w0 >> 7) & 0x1F
            pds_data_size = (w0 >> 1) & 0x3F
            # Direct kernel: K0, K1, K2, K3, K4, K5, K8, K9 = 8 dwords (indirect: K0, K1, K2, K6, K7, K8, K9 = 7 dwords)
            # Note: some streams omit K9 when workgroup_size is not emitted, check >= 7/8
            needed = 7 if indirect else 8
            if i + needed > len(words):
                ok = False
                _err(
                    diags,
                    "CDM_KERNEL_TRUNCATED",
                    f"CDM kernel at byte +{off_bytes} requires {needed} dwords, only {len(words) - i} available",
                    offset=off_bytes,
                )
                break

            k1 = words[i + 1]
            k2 = words[i + 2]
            data_addr_lo = k1 & 0xFFFFFFF0
            sd_type = (k1 >> 2) & 0x3
            usc_common_shared = (k1 >> 1) & 1
            code_addr_lo = k2 & 0xFFFFFFF0

            if (k1 & 0x1) != 0:
                ok = False
                _err(diags, "CDM_K1_RESERVED_BIT0", f"KERNEL1 reserved bit[0] set (0x{k1:08x})", offset=off_bytes + 4, field="KERNEL1")
            if (k2 & 0xF) != 0:
                ok = False
                _err(diags, "CDM_K2_CODE_UNALIGNED", f"KERNEL2 code_addr=0x{k2:08x} not 16B-aligned", offset=off_bytes + 8, field="code_addr")
            if data_addr_lo == 0 and pds_data_size > 0:
                _warn(diags, "CDM_K1_ZERO_DATA_ADDR", "KERNEL1 data_addr is 0 with non-zero pds_data_size", offset=off_bytes + 4)

            if indirect:
                k6 = words[i + 3]
                k7 = words[i + 4]
                if (k7 & 0x3) != 0:
                    ok = False
                    _err(diags, "CDM_INDIRECT_UNALIGNED", f"KERNEL7 indirect_addr_lo=0x{k7:08x} not 4B-aligned", offset=off_bytes + 16)
                k8 = words[i + 5]
                k9 = words[i + 6]
            else:
                wg_x, wg_y, wg_z = words[i + 3], words[i + 4], words[i + 5]
                k8 = words[i + 6]
                k9 = words[i + 7]

            max_inst_m1 = (k8 >> 25) & 0x7F
            max_instances = max_inst_m1 + 1
            event_type = (k8 >> 11) & 0x3
            usc_common_size = k8 & 0x7FF

            # Bit [24:13] of KERNEL8 are reserved
            if (k8 >> 13) & 0xFFF:
                ok = False
                _err(
                    diags,
                    "CDM_K8_RESERVED",
                    f"KERNEL8 reserved bits [24:13] non-zero (0x{k8:08x}); check max_instances <= 128",
                    offset=off_bytes + (5 if indirect else 6) * 4,
                    field="KERNEL8",
                )

            if max_instances > 128:
                ok = False
                _err(diags, "CDM_MAX_INSTANCES_OOB", f"max_instances={max_instances} > 128", offset=off_bytes, field="max_instances")

            ws_x = ((k9 >> 20) & 0x3FF) + 1
            ws_y = ((k9 >> 10) & 0x3FF) + 1
            ws_z = (k9 & 0x3FF) + 1
            if (k9 >> 30) != 0:
                ok = False
                _err(diags, "CDM_K9_RESERVED", f"KERNEL9 reserved bits [31:30] non-zero (0x{k9:08x})", offset=off_bytes + (needed - 1) * 4)
            if ws_x * ws_y * ws_z > 512 and not usc_common_shared:
                ok = False
                _err(
                    diags,
                    "CDM_WORKGROUP_INVOC_OOB",
                    f"workgroup_size ({ws_x}x{ws_y}x{ws_z}={ws_x*ws_y*ws_z}) exceeds Volcanic max 512 invocations",
                    offset=off_bytes + (needed - 1) * 4,
                    field="workgroup_size",
                )

            kernels += 1
            if block_type == 3:
                ext_kernels += 1
            i += needed
            continue

    if require_terminate and not terminated:
        ok = False
        _err(diags, "CDM_MISSING_TERMINATE", "CDM stream missing CDMCTRL_STREAM_TERMINATE (block_type=2)", offset=len(words) * 4)

    checks["cdm_ok"] = ok
    checks["cdm_kernels"] = kernels
    checks["cdm_ext_kernels"] = ext_kernels
    checks["cdm_terminated"] = terminated


# ---------------------------------------------------------------------------
# 3. VDM / 64-bit QWORD DCE Stream & Handle Buffer Validator
# ---------------------------------------------------------------------------

def validate_dce_stream(
    qwords_in: Any,
    diags: List[Diagnostic],
    checks: Dict[str, Any],
    *,
    handle_entries: Optional[Sequence[int]] = None,
    draw_arg_descs: Optional[Sequence[Dict[str, Any]]] = None,
) -> None:
    """Validate Volcanic 64-bit QWORD DCE component stream, handle buffer, and draw-arg descriptors."""
    ok = True

    if handle_entries is not None:
        for idx, he in enumerate(handle_entries):
            he = int(he) & 0xFFFFFFFFFFFFFFFF
            qw_cnt = (he >> 32) & 0xFFFFFFFF
            addr_shifted = he & 0xFFFFFFFF
            dev_va = addr_shifted << 3
            if qw_cnt == 0:
                ok = False
                _err(diags, "DCE_HANDLE_ZERO_COUNT", f"handle[{idx}] has qword_count=0", offset=idx * 8, field="qword_count")
            if dev_va == 0 or dev_va >= DCE_HANDLE_VA_LIMIT:
                ok = False
                _err(
                    diags,
                    "DCE_HANDLE_VA_RANGE",
                    f"handle[{idx}] stream_addr=0x{dev_va:x} outside (0, 32GiB) 35-bit VDM/DCE heap window",
                    offset=idx * 8,
                    field="stream_addr",
                )

    if draw_arg_descs is not None:
        for idx, desc in enumerate(draw_arg_descs):
            indexed = bool(desc.get("indexed", False))
            addr = int(desc.get("addr", 0))
            size_bytes = int(desc.get("size_bytes", 36 if indexed else 16))
            words = desc.get("arg_desc_qwords")
            if (addr & 0x7) != 0:
                ok = False
                _err(diags, "DCE_ARG_DESC_UNALIGNED", f"draw_arg_desc[{idx}] addr=0x{addr:x} not 8B-aligned", field="arg_desc_addr")
            expected_sz = 36 if indexed else 16
            if size_bytes < expected_sz:
                ok = False
                _err(
                    diags,
                    "DCE_DRAW_DATA_SIZE",
                    f"draw_arg_desc[{idx}] draw_data size {size_bytes}B < {expected_sz}B ({'indexed' if indexed else 'non-indexed'})",
                    field="draw_data_size",
                )
            if words is not None:
                qws = [int(x) & 0xFFFFFFFFFFFFFFFF for x in words]
                if not indexed and qws != [DCE_NON_INDEXED_ARG_DESC]:
                    ok = False
                    _err(
                        diags,
                        "DCE_NON_INDEXED_ARG_DESC_BAD",
                        f"non-indexed arg_desc {[hex(x) for x in qws]} != [0x{DCE_NON_INDEXED_ARG_DESC:016x}]",
                        field="arg_desc",
                    )
                if indexed and tuple(qws) != DCE_INDEXED_ARG_DESC:
                    ok = False
                    _err(
                        diags,
                        "DCE_INDEXED_ARG_DESC_BAD",
                        f"indexed arg_desc {[hex(x) for x in qws]} != {[hex(x) for x in DCE_INDEXED_ARG_DESC]}",
                        field="arg_desc",
                    )
            if indexed and "index_type" in desc:
                it = int(desc["index_type"])
                if it not in (0, 1, 2):
                    ok = False
                    _err(diags, "DCE_INDEX_TYPE_INVALID", f"indexed draw index_type={it} not in {{0(U16),1(U32),2(U8)}}", field="index_type")

    try:
        qwords = _to_u64_list(qwords_in)
    except ValueError as exc:
        _err(diags, "DCE_UNALIGNED_STREAM", str(exc), offset=0)
        checks["dce_ok"] = False
        return

    if not qwords:
        _err(diags, "DCE_EMPTY_STREAM", "DCE QWORD stream is empty", offset=0)
        checks["dce_ok"] = False
        return

    i = 0
    pkt_count = 0
    has_terminate = False

    while i < len(qwords):
        qw0 = qwords[i]
        op = (qw0 >> 56) & 0xFF
        off_bytes = i * 8
        if op not in VALID_DCE_OPCODES:
            ok = False
            _err(
                diags,
                "DCE_INVALID_OPCODE",
                f"Invalid DCE QWORD opcode 0x{op:02x} (valid: 0x01, 0x03..0x0a, 0x0c)",
                offset=off_bytes,
                field="opcode",
            )
            break

        if op == 0x01:  # DCE_PPP_STATE_UPDATE (1 QW)
            wc = (qw0 >> 46) & 0x3FF
            addr = (qw0 & ((1 << 46) - 1)) << 2
            if wc == 0:
                ok = False
                _err(diags, "DCE_PPP_ZERO_WORDS", "DCE_PPP_STATE_UPDATE has word_count=0", offset=off_bytes, field="word_count")
            if addr == 0:
                ok = False
                _err(diags, "DCE_PPP_ZERO_ADDR", "DCE_PPP_STATE_UPDATE has addr=0", offset=off_bytes, field="addr")
            if wc == 3:
                has_terminate = True
            i += 1
        elif op == 0x03:  # DCE_TESS_DDM_CONFIG (1 QW)
            if (qw0 >> 6) & ((1 << 50) - 1):
                _warn(diags, "DCE_TESS_RESERVED_BITS", "DCE_TESS_DDM_CONFIG has non-zero bits above [5:0]", offset=off_bytes)
            i += 1
        elif op == 0x04:  # DCE_SHADER_STATE_UPDATE (3 QW)
            if i + 3 > len(qwords):
                ok = False
                _err(diags, "DCE_SHADER_TRUNCATED", "DCE_SHADER_STATE_UPDATE requires 3 QWORDs", offset=off_bytes)
                break
            stage = (qw0 >> 53) & 0x7
            max_inst_m1 = (qw0 >> 41) & 0xFF
            qw2 = qwords[i + 2]
            if stage > 4:
                ok = False
                _err(diags, "DCE_SHADER_BAD_STAGE", f"DCE_SHADER_STATE stage={stage} > 4", offset=off_bytes, field="stage")
            if max_inst_m1 > 127:
                ok = False
                _err(diags, "DCE_SHADER_MAX_INST", f"DCE_SHADER_STATE max_instances={max_inst_m1 + 1} > 128", offset=off_bytes, field="max_instances_m1")
            if (qw2 & 0xF) != 0 or (qw2 >> 32) != 0:
                ok = False
                _err(
                    diags,
                    "DCE_SHADER_PDS_DATA_ALIGN",
                    f"DCE_SHADER_STATE2=0x{qw2:016x} must be 16B-aligned in [31:4] with [63:32]==0",
                    offset=off_bytes + 16,
                    field="pds_data_addr",
                )
            i += 3
        elif op == 0x05:  # DCE_PREDICATE_WRAPPER (2 QW)
            if i + 2 > len(qwords):
                ok = False
                _err(diags, "DCE_PRED_TRUNCATED", "DCE_PREDICATE_WRAPPER requires 2 QWORDs", offset=off_bytes)
                break
            wrapped = (qwords[i + 1] >> 32) & 0xFFFFFFFF
            if i + 2 + wrapped > len(qwords):
                ok = False
                _err(
                    diags,
                    "DCE_PRED_WRAP_OOB",
                    f"DCE_PREDICATE_WRAPPER wrapped_qword_count={wrapped} exceeds stream",
                    offset=off_bytes + 8,
                    field="wrapped_qword_count",
                )
            i += 2
        elif op == 0x06:  # DCE_DRAW_COMMAND (3..5 QW)
            ind_cnt = (qw0 >> 1) & 1
            multi = qw0 & 1
            needed = 3 + ind_cnt + multi
            if i + needed > len(qwords):
                ok = False
                _err(diags, "DCE_DRAW_TRUNCATED", f"DCE_DRAW_COMMAND requires {needed} QWORDs", offset=off_bytes)
                break
            topo = (qw0 >> 19) & 0xF
            if topo not in VALID_DCE_TOPOLOGIES:
                ok = False
                _err(diags, "DCE_DRAW_BAD_TOPOLOGY", f"Invalid DCE_PRIM_TOPOLOGY={topo}", offset=off_bytes, field="primitive_topology")
            qw1 = qwords[i + 1]
            qw2 = qwords[i + 2]
            arg_cnt = (qw1 >> 55) & 0x7
            arg_addr = (qw1 & ((1 << 37) - 1)) << 3
            draw_addr = (qw2 & ((1 << 46) - 1)) << 2
            if arg_cnt not in (1, 2):
                ok = False
                _err(diags, "DCE_DRAW_BAD_ARG_COUNT", f"arg_desc_count={arg_cnt} not in {{1, 2}}", offset=off_bytes + 8, field="arg_desc_count")
            if arg_addr == 0:
                ok = False
                _err(diags, "DCE_DRAW_ZERO_ARG_ADDR", "arg_desc_addr is 0", offset=off_bytes + 8, field="arg_desc_addr")
            if draw_addr == 0:
                ok = False
                _err(diags, "DCE_DRAW_ZERO_DATA_ADDR", "draw_data_addr is 0", offset=off_bytes + 16, field="draw_data_addr")
            i += needed
        elif op == 0x07:  # DCE_DEPTH_BIAS_UPDATE (2 QW)
            if i + 2 > len(qwords):
                ok = False
                _err(diags, "DCE_DBIAS_TRUNCATED", "DCE_DEPTH_BIAS_UPDATE requires 2 QWORDs", offset=off_bytes)
                break
            i += 2
        elif op == 0x08:  # DCE_SAMPLE_MASK_UPDATE (1 QW)
            i += 1
        elif op == 0x09:  # DCE_SCISSOR_ARRAY_UPDATE (1 + count QW)
            table_sel = qw0 & 0xF
            cnt = ((qw0 >> 4) & 0xF) + 1
            if table_sel > 1:
                ok = False
                _err(diags, "DCE_SCISSOR_BAD_TABLE", f"scissor table_select={table_sel} > 1", offset=off_bytes, field="table_select")
            if i + 1 + cnt > len(qwords):
                ok = False
                _err(diags, "DCE_SCISSOR_TRUNCATED", f"DCE_SCISSOR_ARRAY_UPDATE requires {1 + cnt} QWORDs", offset=off_bytes)
                break
            for r in range(cnt):
                rect = qwords[i + 1 + r]
                min_x = rect & 0xFFFF
                max_x = (rect >> 16) & 0xFFFF
                min_y = (rect >> 32) & 0xFFFF
                max_y = (rect >> 48) & 0xFFFF
                if min_x > max_x or min_y > max_y:
                    ok = False
                    _err(
                        diags,
                        "DCE_SCISSOR_INVERTED",
                        f"scissor[{r}] inverted ({min_x}..{max_x}, {min_y}..{max_y})",
                        offset=off_bytes + (1 + r) * 8,
                    )
            i += 1 + cnt
        elif op == 0x0A:  # DCE_SCISSOR_DBIAS_EMIT (1 QW)
            i += 1
        elif op == 0x0C:  # DCE_BARRIER (1 QW)
            if (qw0 & ((1 << 56) - 1)) != 0:
                ok = False
                _err(diags, "DCE_BARRIER_NONZERO_RSVD", f"DCE_BARRIER reserved bits [55:0] non-zero (0x{qw0:016x})", offset=off_bytes)
            i += 1
        pkt_count += 1

    checks["dce_ok"] = ok
    checks["dce_packets"] = pkt_count
    checks["dce_has_terminate"] = has_terminate


# ---------------------------------------------------------------------------
# 4. PPP_STATE & Indirect 16B Pixel Shader Table Validator
# ---------------------------------------------------------------------------

def ppp_expected_dwords(header: int) -> int:
    """Compute expected Volcanic PPP_STATE dword count from TA_STATE_HEADER."""
    cnt = 1
    if header & (1 << 0):
        cnt += 1  # ISPCTL
    if header & (1 << 1):
        cnt += 1  # ISPA front
    if header & (1 << 2):
        cnt += 1  # ISPB front
    if header & (1 << 3):
        cnt += 1  # ISPA back
    if header & (1 << 4):
        cnt += 1  # ISPB back
    if header & (1 << 7):
        cnt += 1  # PDS_DATA_ADDR
    if header & (1 << 8):
        cnt += 1  # PDS_SHADER_TABLE_FSR
    if header & (1 << 10):
        cnt += 2  # REGION_CLIP0..1
    if header & (1 << 11):
        vp_cnt = ((header >> 12) & 0xF) + 1
        cnt += 6 * vp_cnt
    if header & (1 << 16):
        cnt += 1  # WCLAMP
    if header & (1 << 17):
        cnt += 1  # OUTPUT_SEL
    if header & (1 << 18):
        cnt += 1  # VARYING0
    if header & (1 << 19):
        cnt += 1  # VARYING1
    if header & (1 << 20):
        cnt += 1  # VARYING2
    if header & (1 << 21):
        cnt += 1  # PPP_CTRL
    if header & (1 << 22):
        cnt += 1  # STREAM_OUT0
    if header & (1 << 23):
        cnt += 2  # STREAM_OUT1..2
    if header & (1 << 25):
        cnt += 2  # TERMINATE0..1
    return cnt


def validate_ppp_state(
    words_in: Any,
    diags: List[Diagnostic],
    checks: Dict[str, Any],
    *,
    pixel_shader_table: Optional[Dict[str, Any]] = None,
) -> None:
    """Validate Volcanic PPP_STATE buffer & optional 16B indirect Pixel Shader Table."""
    ok = True
    try:
        words = _to_u32_list(words_in)
    except ValueError as exc:
        _err(diags, "PPP_UNALIGNED_STREAM", str(exc), offset=0)
        checks["ppp_ok"] = False
        return

    if not words:
        _err(diags, "PPP_EMPTY", "PPP_STATE buffer is empty", offset=0)
        checks["ppp_ok"] = False
        return

    hdr = words[0]
    legacy = hdr & TA_HEADER_LEGACY_REMOVED_BITS
    if legacy:
        ok = False
        bits = [b for b in (5, 6, 9) if legacy & (1 << b)]
        _err(
            diags,
            "PPP_ROGUE_INLINE_PDS_BIT",
            f"TA_STATE_HEADER=0x{hdr:08x} sets removed Rogue bit(s) {bits} (inline PDS/DBSC dropped on Volcanic)",
            offset=0,
            field="TA_STATE_HEADER",
        )
    unknown = hdr & ~(TA_HEADER_VALID_MASK | TA_HEADER_LEGACY_REMOVED_BITS)
    if unknown:
        ok = False
        _err(diags, "PPP_HEADER_RESERVED_BITS", f"TA_STATE_HEADER=0x{hdr:08x} sets unknown bits 0x{unknown:08x}", offset=0, field="TA_STATE_HEADER")

    exp_dw = ppp_expected_dwords(hdr)
    if len(words) != exp_dw:
        ok = False
        _err(
            diags,
            "PPP_WORD_COUNT_MISMATCH",
            f"PPP_STATE has {len(words)} dwords, header 0x{hdr:08x} requires {exp_dw} dwords",
            offset=0,
            field="word_count",
        )
    else:
        cursor = 1
        bpres = False
        if hdr & (1 << 0):
            ispctl = words[cursor]
            bpres = bool((ispctl >> 18) & 1)
            cursor += 1
        if hdr & (1 << 1):
            ispa = words[cursor]
            passtype = (ispa >> 25) & 0x7
            if passtype not in (0, 1, 2, 5):
                _warn(diags, "PPP_ISPA_UNUSUAL_PASSTYPE", f"TA_STATE_ISPA passtype={passtype}", offset=cursor * 4)
            cursor += 1
        if hdr & (1 << 2):
            cursor += 1
        if hdr & (1 << 3):
            if (hdr & 1) and not bpres:
                _warn(diags, "PPP_BACK_FACE_WITHOUT_BPRES", "pres_ispctl_ba set while ISPCTL.bpres=0", offset=cursor * 4)
            cursor += 1
        if hdr & (1 << 4):
            cursor += 1
        if hdr & (1 << 7):
            pds_data = words[cursor]
            if (pds_data & 0xF) != 0:
                ok = False
                _err(
                    diags,
                    "PPP_PDS_DATA_UNALIGNED",
                    f"TA_STATE_PDS_DATA_ADDR=0x{pds_data:08x} not 16B-aligned",
                    offset=cursor * 4,
                    field="pds_data_addr",
                )
            cursor += 1
        if hdr & (1 << 8):
            fsr = words[cursor]
            fw = (fsr >> 29) & 0x3
            fh = (fsr >> 27) & 0x3
            fc0 = (fsr >> 24) & 0x7
            fc1 = (fsr >> 21) & 0x7
            if fw > 2 or fh > 2:
                ok = False
                _err(diags, "PPP_FSR_SIZE_INVALID", f"FSR size log2 ({fw}, {fh}) > 2 (max 4px)", offset=cursor * 4, field="fsr_size")
            if fc0 > 4 or fc1 > 4:
                ok = False
                _err(diags, "PPP_FSR_COMBINER_INVALID", f"FSR combiner ({fc0}, {fc1}) > 4", offset=cursor * 4, field="fsr_combiner")
            cursor += 1
        if hdr & (1 << 10):
            rc0, rc1 = words[cursor], words[cursor + 1]
            left, right = (rc0 >> 16) & 0x3FF, rc0 & 0x3FF
            top, bottom = (rc1 >> 16) & 0x3FF, rc1 & 0x3FF
            if left > right or top > bottom:
                ok = False
                _err(diags, "PPP_REGION_CLIP_INVERTED", f"region_clip ({left}..{right}, {top}..{bottom}) inverted", offset=cursor * 4)
            cursor += 2

    if pixel_shader_table is not None:
        base_va = int(pixel_shader_table.get("base_addr", 0))
        if (base_va & 0xF) != 0:
            ok = False
            _err(
                diags,
                "PPP_PSTBL_UNALIGNED",
                f"Pixel Shader Table base_addr=0x{base_va:x} not 16B-aligned",
                field="pixel_shader_table.base_addr",
            )
        entries = pixel_shader_table.get("entries", [])
        for idx, ent in enumerate(entries):
            if len(ent) != 2:
                ok = False
                _err(diags, "PPP_PSTBL_ENTRY_LEN", f"PS table entry[{idx}] must be 2 QWORDs (16B)", offset=idx * 16)
                continue
            e0, e1 = int(ent[0]) & 0xFFFFFFFFFFFFFFFF, int(ent[1]) & 0xFFFFFFFFFFFFFFFF
            if ((e1 >> 9) & 1) == 0:
                ok = False
                _err(
                    diags,
                    "PPP_PSTBL_VALID_BIT_CLEAR",
                    f"PS table entry[{idx}] entry_valid_flag (bit 9) is 0",
                    offset=idx * 16 + 8,
                    field="entry_valid_flag",
                )

    checks["ppp_ok"] = ok
    checks["ppp_header"] = f"0x{hdr:08x}"
    checks["ppp_dwords"] = len(words)


# ---------------------------------------------------------------------------
# 5. PBE / ISP / ZLS v2 Validator
# ---------------------------------------------------------------------------

def validate_pbe_isp_zls(spec: Dict[str, Any], diags: List[Diagnostic], checks: Dict[str, Any]) -> None:
    """Validate Volcanic PBE STATE_WORD0..2 + REG_WORD0..2, ISP_CTL, and ZLS v2."""
    ok = True

    if "pbe" in spec:
        pbe = spec["pbe"]
        sw = _to_u32_list(pbe.get("state_words", []))
        rw = _to_u64_list(pbe.get("reg_words", []))
        if len(sw) != 3:
            ok = False
            _err(diags, "PBE_STATE_WORDS_LEN", f"PBE requires 3 state words, got {len(sw)}", field="pbe.state_words")
        else:
            sw0, sw1, sw2 = sw
            emptytile = bool((sw1 >> 14) & 1)
            va = sw0 | ((sw1 & 0x1FFF) << 32) | (((sw1 >> 13) & 1) << 47)
            if "expected_va" in pbe:
                exp_va = int(pbe["expected_va"])
                if va == (exp_va >> 2) and exp_va != 0:
                    ok = False
                    _err(
                        diags,
                        "PBE_ROGUE_SHIFT2_VA",
                        f"PBE STATE_WORD0..1 VA=0x{va:x} equals expected_va>>2 (0x{exp_va:x}>>2); Volcanic PBE uses raw byte VA (shift=0)",
                        field="PBESTATE_STATE_WORD0",
                    )
                elif va != exp_va:
                    ok = False
                    _err(diags, "PBE_VA_MISMATCH", f"PBE VA=0x{va:x} != expected 0x{exp_va:x}", field="PBESTATE_STATE_WORD0")
            if emptytile and (sw0 != 0 or sw2 != 0):
                ok = False
                _err(diags, "PBE_EMPTYTILE_NONZERO", "PBE emptytile=1 requires STATE_WORD0=0 and STATE_WORD2=0", field="emptytile")
            if not emptytile and va == 0 and "expected_va" not in pbe:
                ok = False
                _err(diags, "PBE_ZERO_VA", "Non-empty PBE emit has zero surface VA", field="PBESTATE_STATE_WORD0")
            if (sw2 >> 6) != 0:
                _warn(diags, "PBE_SW2_EXTRA_BITS", f"STATE_WORD2 has bits above [5:0] set (0x{sw2:08x})", field="PBESTATE_STATE_WORD2")

        if rw:
            if len(rw) != 3:
                ok = False
                _err(diags, "PBE_REG_WORDS_LEN", f"PBE requires 3 64-bit reg words, got {len(rw)}", field="pbe.reg_words")
            else:
                rw0, rw1, _rw2 = rw
                sx = (rw0 >> 24) & 0x1F
                sy = (rw0 >> 29) & 0x1F
                sz = (rw0 >> 34) & 0x1F
                if sx > 15 or sy > 15 or sz > 15:
                    ok = False
                    _err(diags, "PBE_LOG2_DIM_OOB", f"PBE log2 dims ({sx},{sy},{sz}) exceed 15 (32K)", field="PBESTATE_REG_WORD0")
                min_x = (rw1 >> 48) & 0xFFFF
                max_x = (rw1 >> 32) & 0xFFFF
                min_y = (rw1 >> 16) & 0xFFFF
                max_y = rw1 & 0xFFFF
                if min_x > max_x or min_y > max_y or max_x >= 32768 or max_y >= 32768:
                    ok = False
                    _err(
                        diags,
                        "PBE_CLIP_BOUNDS",
                        f"PBE clip ({min_x}..{max_x}, {min_y}..{max_y}) invalid or >= 32768",
                        field="PBESTATE_REG_WORD1",
                    )

    if "isp" in spec:
        isp = spec["isp"]
        if "isp_xtp_pipe_enable" in isp:
            mode = int(isp["isp_xtp_pipe_enable"]) & 0x3
            if mode > 2 or (int(isp["isp_xtp_pipe_enable"]) >> 2) != 0:
                ok = False
                _err(diags, "ISP_XTP_PIPE_MODE_OOB", f"CR_ISP_XTP_PIPE_ENABLE=0x{int(isp['isp_xtp_pipe_enable']):x} invalid (valid 0..2)", field="CR_ISP_XTP_PIPE_ENABLE")
        if "isp_aa" in isp and int(isp["isp_aa"]) > 3:
            ok = False
            _err(diags, "ISP_AA_INVALID", f"CR_ISP_AA={isp['isp_aa']} > 3", field="CR_ISP_AA")
        if "te_aa" in isp and int(isp["te_aa"]) not in VALID_TE_AA_MODES:
            ok = False
            _err(diags, "TE_AA_INVALID", f"CR_TE_AA={isp['te_aa']} not in {{0,1,5,7}}", field="CR_TE_AA")
        if "ppp_screen" in isp:
            ps = int(isp["ppp_screen"])
            if (ps & (1 << 15)) or (ps & (1 << 31)):
                ok = False
                _err(diags, "PPP_SCREEN_RESERVED", f"CR_PPP_SCREEN=0x{ps:08x} sets reserved bit 15 or 31", field="CR_PPP_SCREEN")

    if "zls_v2" in spec:
        zls = spec["zls_v2"]
        zwords = _to_u64_list(zls.get("zls_words", []))
        if len(zwords) != 3:
            ok = False
            _err(diags, "ZLS_WORDS_LEN", f"ZLS v2 requires 3 64-bit words, got {len(zwords)}", field="zls_v2.zls_words")
        else:
            zw0, zw1, zw2 = zwords
            sx = (zw0 >> 24) & 0x1F
            sy = (zw0 >> 29) & 0x1F
            twiddled = bool((zw0 >> 22) & 1)
            min_x = (zw1 >> 48) & 0xFFFF
            max_x_m1 = (zw1 >> 32) & 0xFFFF
            min_y = (zw1 >> 16) & 0xFFFF
            max_y_m1 = zw1 & 0xFFFF
            stride_m1 = (zw2 >> 32) & 0xFFFF
            if sx > 15 or sy > 15:
                ok = False
                _err(diags, "ZLS_LOG2_DIM_OOB", f"ZLS_WORD0 log2 dims ({sx},{sy}) > 15 (32K)", field="CR_ZLS_WORD0")
            if min_x > max_x_m1 or min_y > max_y_m1 or max_x_m1 >= 32768 or max_y_m1 >= 32768:
                ok = False
                _err(
                    diags,
                    "ZLS_CLIP_BOUNDS",
                    f"ZLS_WORD1 bounds ({min_x}..{max_x_m1}, {min_y}..{max_y_m1}) invalid or >= 32768",
                    field="CR_ZLS_WORD1",
                )
            if (not twiddled) and stride_m1 == 0 and max_x_m1 > 0:
                ok = False
                _err(diags, "ZLS_LINEAR_ZERO_STRIDE", "Linear ZLS_WORD2 has stride_m1=0 for width > 1", field="CR_ZLS_WORD2")

    checks["pbe_isp_zls_ok"] = ok


# ---------------------------------------------------------------------------
# 6. PDS & TEXSTATE Validator
# ---------------------------------------------------------------------------

def validate_pds_texstate(spec: Dict[str, Any], diags: List[Diagnostic], checks: Dict[str, Any]) -> None:
    """Validate Volcanic PDS DOUTU/DOUTD and TEXSTATE 32K / raw-byte-VA / smpcnt descriptors."""
    ok = True

    if "pds" in spec:
        pds = spec["pds"]
        if "doutu_src0" in pds:
            src0 = int(pds["doutu_src0"]) & 0xFFFFFFFFFFFFFFFF
            exe_addr = (src0 & 0x3FFFFFFF) << 2
            temps = (src0 >> 33) & 0x7F
            if "expected_exe_addr" in pds:
                exp = int(pds["expected_exe_addr"])
                if (exp & 0x3) != 0:
                    ok = False
                    _err(diags, "PDS_DOUTU_EXE_UNALIGNED", f"expected_exe_addr 0x{exp:x} not 4B-aligned", field="doutu.exe_addr")
                elif exe_addr != exp:
                    # Catch Rogue [31:2] packing mistake where unshifted addr lands in [31:2]
                    if (src0 & 0xFFFFFFFF) == exp:
                        _err(
                            diags,
                            "PDS_DOUTU_ROGUE_UNSHIFTED",
                            f"DOUTU SRC0=0x{src0:016x} placed raw unshifted exe_addr in [31:0] (Rogue layout); Volcanic requires exe_addr>>2 in [29:0]",
                            field="DOUTU_SRC0",
                        )
                    else:
                        _err(diags, "PDS_DOUTU_ADDR_MISMATCH", f"DOUTU exe_addr=0x{exe_addr:x} != expected 0x{exp:x}", field="DOUTU_SRC0")
                    ok = False
            if temps > 128:
                ok = False
                _err(diags, "PDS_DOUTU_TEMPS_OOB", f"DOUTU temps={temps} exceeds 7-bit range", field="DOUTU_SRC0.temps")

        if "doutd_src0" in pds:
            d0 = int(pds["doutd_src0"]) & 0xFFFFFFFFFFFFFFFF
            # Rogue SLCMODE lives at bit 62 — must be 0 on Volcanic!
            if (d0 >> 62) & 1:
                ok = False
                _err(
                    diags,
                    "PDS_DOUTD_ROGUE_SLCMODE_BIT62",
                    f"DOUTD SRC0=0x{d0:016x} sets bit 62 (Rogue SLCMODE); forbidden on Volcanic",
                    field="DOUTD_SRC0",
                )
            if (d0 >> 61) & 1:
                ok = False
                _err(diags, "PDS_DOUTD_RESERVED_BIT61", f"DOUTD SRC0=0x{d0:016x} sets reserved bit 61", field="DOUTD_SRC0")
            sbase = d0 & ((1 << 48) - 1)
            if sbase == 0:
                ok = False
                _err(diags, "PDS_DOUTD_ZERO_SBASE", "DOUTD SRC0 sbase[47:0] is 0", field="DOUTD_SRC0.sbase")

        if "doutd_src1" in pds:
            d1 = int(pds["doutd_src1"]) & 0xFFFFFFFF
            bsize = d1 & 0xFFF
            rsvd = (d1 >> 12) & 0xFFFF
            if bsize == 0:
                ok = False
                _err(diags, "PDS_DOUTD_ZERO_BSIZE", "DOUTD SRC1 bsize[11:0] is 0", field="DOUTD_SRC1.bsize")
            if rsvd != 0:
                ok = False
                _err(diags, "PDS_DOUTD_SRC1_RSVD", f"DOUTD SRC1 reserved bits [27:12] non-zero (0x{d1:08x})", field="DOUTD_SRC1")

        if "dout_insts" in pds:
            for idx, inst in enumerate(_to_u32_list(pds["dout_insts"])):
                op = inst & 0xF0000007
                if op not in VALID_PDS_DOUT_OPCODES:
                    ok = False
                    _err(
                        diags,
                        "PDS_DOUT_INVALID_OPCODE",
                        f"PDS DOUT inst[{idx}]=0x{inst:08x} opcode 0x{op:08x} not in DOUTD/DOUTU/DOUTI",
                        offset=idx * 4,
                    )

    if "texstate" in spec:
        ts = spec["texstate"]
        img = _to_u64_list(ts.get("image_words", []))
        if len(img) < 3 or len(img) > 4:
            ok = False
            _err(diags, "TEXSTATE_IMAGE_WORDS_LEN", f"TEXSTATE requires 3 or 4 64-bit image words, got {len(img)}", field="texstate.image_words")
        else:
            w0, w1, w2 = img[0], img[1], img[2]
            textype = w0 & 0x7
            width_m1 = (w0 >> 27) & 0x7FFF
            height_m1 = (w0 >> 42) & 0x7FFF
            top_junk = (w0 >> 57) & 0x7F
            if textype > 5:
                ok = False
                _err(diags, "TEXSTATE_BAD_TEXTYPE", f"IMAGE_WORD0 textype={textype} > 5", field="IMAGE_WORD0.textype")
            if top_junk != 0:
                ok = False
                _err(
                    diags,
                    "TEXSTATE_UPSTREAM_14B_WIDTH_HEIGHT_DRIFT",
                    f"IMAGE_WORD0 bits [63:57]=0x{top_junk:02x} non-zero; indicates upstream 14b [62:49] height packing instead of DXT-48-1536 15b [56:42]/[41:27]",
                    field="IMAGE_WORD0",
                )
            if "expected_width" in ts and (width_m1 + 1) != int(ts["expected_width"]):
                ok = False
                _err(
                    diags,
                    "TEXSTATE_WIDTH_MISMATCH",
                    f"IMAGE_WORD0 width={width_m1 + 1} != expected {ts['expected_width']}",
                    field="IMAGE_WORD0.width",
                )
            if "expected_height" in ts and (height_m1 + 1) != int(ts["expected_height"]):
                ok = False
                _err(
                    diags,
                    "TEXSTATE_HEIGHT_MISMATCH",
                    f"IMAGE_WORD0 height={height_m1 + 1} != expected {ts['expected_height']}",
                    field="IMAGE_WORD0.height",
                )

            texaddr = (w1 & ((1 << 45) - 1)) | (((w1 >> 45) & 1) << 47)
            if "expected_va" in ts:
                exp_va = int(ts["expected_va"])
                if texaddr == (exp_va >> 2) and exp_va != 0:
                    ok = False
                    _err(
                        diags,
                        "TEXSTATE_ROGUE_SHIFT2_VA",
                        f"IMAGE_WORD1 texaddr=0x{texaddr:x} equals expected_va>>2; DXT-48-1536 uses raw byte VA ([44:0]+[45]=VA[47])",
                        field="IMAGE_WORD1.texaddr",
                    )
                elif texaddr != exp_va:
                    ok = False
                    _err(diags, "TEXSTATE_VA_MISMATCH", f"IMAGE_WORD1 texaddr=0x{texaddr:x} != expected 0x{exp_va:x}", field="IMAGE_WORD1.texaddr")
            elif texaddr == 0:
                ok = False
                _err(diags, "TEXSTATE_ZERO_VA", "IMAGE_WORD1 texaddr is 0", field="IMAGE_WORD1.texaddr")

            smpcnt = (w2 >> 6) & 0x3
            checks["texstate_samples"] = 1 << smpcnt
            if "expected_samples" in ts and (1 << smpcnt) != int(ts["expected_samples"]):
                ok = False
                _err(
                    diags,
                    "TEXSTATE_SMPCNT_MISMATCH",
                    f"IMAGE_WORD2.smpcnt[7:6] encodes {1 << smpcnt}x != expected {ts['expected_samples']}x",
                    field="IMAGE_WORD2.smpcnt",
                )

        if "sampler_word1" in ts:
            sw1 = int(ts["sampler_word1"]) & 0xFFFFFFFFFFFFFFFF
            if (sw1 >> 12) != 0:
                ok = False
                _err(diags, "TEXSTATE_SAMPLER1_RSVD", f"SAMPLER_WORD1=0x{sw1:016x} has bits above [11:0] set", field="SAMPLER_WORD1")

    checks["pds_texstate_ok"] = ok


# ---------------------------------------------------------------------------
# 7. Volcanic MMUv4 Page-Table Walk, PxE Encoding & Laguna Coherency Validator
# ---------------------------------------------------------------------------

MMUV4_VA_BITS = 40
MMUV4_PA_BITS = 40
MMUV4_AXCACHE_SHIFT = 58
MMUV4_AXCACHE_MASK = 0xF << MMUV4_AXCACHE_SHIFT
MMUV4_AXCACHE_UNCACHED = 0x3  # Normal Non-Cacheable Bufferable
MMUV4_AXCACHE_CACHED = 0xF    # Write-Back Read/Write-Allocate

MMUV4_LEVEL_NAMES = {0: "PT", 1: "PD", 2: "PC", 3: "RSVD"}


def _jones_req_name(req_id: int) -> str:
    if req_id == 0x00:
        return "IPP"
    if 0x01 <= req_id <= 0x0E:
        return "DCE"
    if 0x0F <= req_id <= 0x13:
        return "TDM"
    if req_id == 0x14:
        return "PM"
    if 0x15 <= req_id <= 0x1F:
        return "CDM"
    if req_id == 0x20:
        return "FW"
    if req_id == 0x21:
        return "FW_DMA"
    if 0x22 <= req_id <= 0x31:
        return "TE"
    if 0x32 <= req_id <= 0x34:
        return "RCE"
    return f"REQ_0x{req_id:02x}"


def decode_mmu_fault_status(status1: int, status2: int) -> Dict[str, Any]:
    """Decode Volcanic S7_TOP MMU_FAULT_STATUS1 (0xE150) & STATUS2 (0xE158)."""
    s1 = int(status1) & 0xFFFFFFFFFFFFFFFF
    s2 = int(status2) & 0xFFFFFFFFFFFFFFFF
    level = (s1 >> 62) & 0x3
    req_id = (s1 >> 56) & 0x3F
    pc_ctx = (s1 >> 48) & 0xFF
    va = s1 & 0x0000FFFFFFFFFFF0
    rnw = bool((s1 >> 3) & 1)
    ftype = (s1 >> 1) & 0x3
    valid = bool(s1 & 1)
    bank = (s2 >> 12) & 0xFF
    wb = bool((s2 >> 20) & 1)
    fbm = bool((s2 >> 21) & 1)
    req_name = _jones_req_name(req_id) if bank == 8 else f"BANK{bank}_0x{req_id:02x}"
    return {
        "valid": valid,
        "read": rnw,
        "type": ftype,
        "va": va,
        "pc_ctx": pc_ctx,
        "req_id": req_id,
        "req_name": req_name,
        "level": level,
        "level_name": MMUV4_LEVEL_NAMES.get(level, "UNK"),
        "bank": bank,
        "writeback": wb,
        "fbm_fault": fbm,
    }


def encode_mmuv4_pce(pd_pa: int, *, valid: bool = True) -> int:
    """Encode 32-bit Volcanic MMUv4 L2 Page Catalogue Entry (PD_BASE[31:4] | VALID[0])."""
    return (((int(pd_pa) >> 12) & 0xFFFFFFF) << 4) | (1 if valid else 0)


def encode_mmuv4_pde(pt_pa: int, *, valid: bool = True) -> int:
    """Encode 64-bit Volcanic MMUv4 L1 Page Directory Entry (PT_BASE[39:12] | VALID[0])."""
    return (int(pt_pa) & 0xFFFFFFF000) | (1 if valid else 0)


def encode_mmuv4_pte(
    page_pa: int,
    *,
    valid: bool = True,
    read_only: bool = False,
    coherent: bool = False,
    pm_src: bool = False,
    pm_meta_protect: bool = False,
    axcache: int = MMUV4_AXCACHE_UNCACHED,
) -> int:
    """Encode 64-bit Volcanic MMUv4 L0 Page Table Entry with AXCACHE[61:58]."""
    val = int(page_pa) & 0xFFFFFFF000
    if valid:
        val |= 1 << 0
    if read_only:
        val |= 1 << 1
    if coherent:
        val |= 1 << 2
    if pm_src:
        val |= 1 << 4
    if pm_meta_protect:
        val |= 1 << 62
    val |= (int(axcache) & 0xF) << MMUV4_AXCACHE_SHIFT
    return val


def validate_mmuv4(
    mmu: Dict[str, Any],
    diags: List[Diagnostic],
    checks: Dict[str, Any],
) -> None:
    """Validate Volcanic MMUv4 PxE bitfields, Laguna non-coherent DMA, and 3-level VA walk."""
    ok = True

    if mmu.get("gpu_dma_coherent") is True:
        ok = False
        _err(
            diags,
            "MMUV4_LAGUNA_FALSE_DMA_COHERENT",
            "gpu0 declares dma-coherent=true, but Laguna DXT-48-1536 has eCacheSnoopingMode=0 "
            "(non-coherent); ARM64 dma_sync becomes a no-op, leaving PC/PD/PT writes in CPU cache "
            "and causing immediate level=PC(2) translation faults",
            field="mmu.gpu_dma_coherent",
        )

    if mmu.get("pt_sync_flushed") is False:
        ok = False
        _err(
            diags,
            "MMUV4_UNFLUSHED_PT_DRAM",
            "Page table updates not flushed to DRAM before GPU kick; non-coherent MMUv4 walker sees stale zeros",
            field="mmu.pt_sync_flushed",
        )

    pc_base_pa = mmu.get("pc_base_pa")
    if pc_base_pa is not None:
        pa = int(pc_base_pa)
        if pa == 0 or (pa & 0xFFF) != 0 or pa >= (1 << MMUV4_PA_BITS):
            ok = False
            _err(
                diags,
                "MMUV4_PC_BASE_INVALID",
                f"pc_base_pa=0x{pa:x} must be non-zero, 4KiB-aligned, and within 40-bit PA",
                field="mmu.pc_base_pa",
            )

    if "fault_status1" in mmu or "fault_status2" in mmu:
        dec = decode_mmu_fault_status(mmu.get("fault_status1", 0), mmu.get("fault_status2", 0))
        checks["mmuv4_decoded_fault"] = dec

    pce_val = mmu.get("pce")
    pde_val = mmu.get("pde")
    pte_val = mmu.get("pte")

    walk_va = mmu.get("walk_va")
    if walk_va is not None:
        va = int(walk_va)
        if va >= (1 << MMUV4_VA_BITS):
            ok = False
            _err(diags, "MMUV4_VA_OUT_OF_RANGE", f"walk_va=0x{va:x} exceeds 40-bit Volcanic VA", field="mmu.walk_va")
        checks["mmuv4_walk_indices"] = {
            "pc_idx": (va >> 30) & 0x3FF,
            "pd_idx": (va >> 21) & 0x1FF,
            "pt_idx": (va >> 12) & 0x1FF,
            "page_off": va & 0xFFF,
        }

    if pce_val is not None:
        pce = int(pce_val)
        if pce < 0 or pce > 0xFFFFFFFF:
            ok = False
            _err(diags, "MMUV4_PCE_WIDTH", f"MMUv4 PC entry must be 32-bit, got 0x{pce:x}", field="mmu.pce")
        if (pce & 1) == 0:
            ok = False
            _err(diags, "MMUV4_PC_FAULT_INVALID", f"PCE=0x{pce:08x} VALID bit 0 clear -> level=PC(2) fault", field="mmu.pce")
        if (pce >> 1) & 1:
            ok = False
            _err(diags, "MMUV4_PC_ROGUE_PENDING_BIT", f"PCE=0x{pce:08x} sets Rogue PENDING bit 1 (dropped in MMUv4)", field="mmu.pce")
        if (pce >> 2) & 0x3:
            ok = False
            _err(diags, "MMUV4_PC_RSVD_BITS", f"PCE=0x{pce:08x} has reserved bits [3:2] set", field="mmu.pce")
        pd_pa_from_pce = ((pce >> 4) & 0xFFFFFFF) << 12
        checks["mmuv4_pce_pd_pa"] = f"0x{pd_pa_from_pce:x}"
        if "expected_pd_pa" in mmu and pd_pa_from_pce != int(mmu["expected_pd_pa"]):
            ok = False
            _err(
                diags,
                "MMUV4_PCE_PD_PA_MISMATCH",
                f"PCE PD_BASE=0x{pd_pa_from_pce:x} != expected 0x{int(mmu['expected_pd_pa']):x}",
                field="mmu.pce",
            )

    if pde_val is not None:
        pde = int(pde_val) & 0xFFFFFFFFFFFFFFFF
        if (pde & 1) == 0:
            ok = False
            _err(diags, "MMUV4_PD_FAULT_INVALID", f"PDE=0x{pde:016x} VALID bit 0 clear -> level=PD(1) fault", field="mmu.pde")
        if (pde >> 40) & 1:
            ok = False
            _err(diags, "MMUV4_PD_ROGUE_PENDING_BIT", f"PDE=0x{pde:016x} sets Rogue ENTRY_PENDING bit 40 (dropped in MMUv4)", field="mmu.pde")
        if (pde >> 1) & 0x7:
            ok = False
            _err(
                diags,
                "MMUV4_PD_ROGUE_PAGESIZE_BITS",
                f"PDE=0x{pde:016x} sets Rogue PAGE_SIZE[3:1]={((pde >> 1) & 0x7)} (MMUv4 uses MMU_PAGE_SIZE_RANGE)",
                field="mmu.pde",
            )
        pt_pa_from_pde = pde & 0xFFFFFFFFF0
        if (pt_pa_from_pde & 0xFFF) != 0:
            ok = False
            _err(diags, "MMUV4_PDE_PT_PA_UNALIGNED", f"PDE PT_BASE=0x{pt_pa_from_pde:x} not 4KiB aligned", field="mmu.pde")
        checks["mmuv4_pde_pt_pa"] = f"0x{pt_pa_from_pde:x}"
        if "expected_pt_pa" in mmu and pt_pa_from_pde != int(mmu["expected_pt_pa"]):
            ok = False
            _err(
                diags,
                "MMUV4_PDE_PT_PA_MISMATCH",
                f"PDE PT_BASE=0x{pt_pa_from_pde:x} != expected 0x{int(mmu['expected_pt_pa']):x}",
                field="mmu.pde",
            )

    if pte_val is not None:
        pte = int(pte_val) & 0xFFFFFFFFFFFFFFFF
        if (pte & 1) == 0:
            ok = False
            _err(diags, "MMUV4_PT_FAULT_INVALID", f"PTE=0x{pte:016x} VALID bit 0 clear -> level=PT(0) fault", field="mmu.pte")
        if (pte >> 3) & 1:
            ok = False
            _err(
                diags,
                "MMUV4_PT_ROGUE_SLC_BYPASS_BIT",
                f"PTE=0x{pte:016x} sets Rogue SLC_BYPASS bit 3 (MMUv4 uses AXCACHE[61:58])",
                field="mmu.pte",
            )
        if (pte >> 5) & 1:
            ok = False
            _err(diags, "MMUV4_PT_ROGUE_PENDING_BIT", f"PTE=0x{pte:016x} sets Rogue PENDING bit 5 (dropped in MMUv4)", field="mmu.pte")
        axcache = (pte >> MMUV4_AXCACHE_SHIFT) & 0xF
        if axcache not in (MMUV4_AXCACHE_UNCACHED, MMUV4_AXCACHE_CACHED):
            ok = False
            _err(
                diags,
                "MMUV4_PT_AXCACHE_INVALID",
                f"PTE AXCACHE[61:58]=0x{axcache:x} not in {{0x3 (uncached), 0xf (cached)}}",
                field="mmu.pte.axcache",
            )
        page_pa = pte & 0xFFFFFFF000
        checks["mmuv4_pte_page_pa"] = f"0x{page_pa:x}"
        checks["mmuv4_pte_axcache"] = f"0x{axcache:x}"
        if "expected_page_pa" in mmu and page_pa != int(mmu["expected_page_pa"]):
            ok = False
            _err(
                diags,
                "MMUV4_PTE_PA_MISMATCH",
                f"PTE page PA=0x{page_pa:x} != expected 0x{int(mmu['expected_page_pa']):x}",
                field="mmu.pte",
            )

    checks["mmuv4_ok"] = ok


# ---------------------------------------------------------------------------
# Unified Validation Entrypoint
# ---------------------------------------------------------------------------

def validate_bundle(
    payload: Dict[str, Any],
    *,
    workload: str = "volcanic-cs-bundle",
    target: str = "volcanic-dxt-48-1536",
) -> Dict[str, Any]:
    """Validate any combination of FW-ABI, CDM, DCE, PPP, PBE/ISP/ZLS, PDS/TEXSTATE, MMUv4."""
    t0 = time.perf_counter()
    diags: List[Diagnostic] = []
    checks: Dict[str, Any] = {}

    if "fw_abi" in payload:
        validate_fw_abi(payload["fw_abi"], diags, checks)
    if "cdm" in payload:
        cdm_obj = payload["cdm"]
        if isinstance(cdm_obj, dict):
            validate_cdm_stream(
                cdm_obj["words"],
                diags,
                checks,
                require_terminate=bool(cdm_obj.get("require_terminate", True)),
            )
        else:
            validate_cdm_stream(cdm_obj, diags, checks)
    if "dce" in payload:
        dce_obj = payload["dce"]
        if isinstance(dce_obj, dict):
            validate_dce_stream(
                dce_obj["qwords"],
                diags,
                checks,
                handle_entries=dce_obj.get("handle_entries"),
                draw_arg_descs=dce_obj.get("draw_arg_descs"),
            )
        else:
            validate_dce_stream(dce_obj, diags, checks)
    if "ppp" in payload:
        ppp_obj = payload["ppp"]
        if isinstance(ppp_obj, dict):
            validate_ppp_state(
                ppp_obj["words"],
                diags,
                checks,
                pixel_shader_table=ppp_obj.get("pixel_shader_table"),
            )
        else:
            validate_ppp_state(ppp_obj, diags, checks)
    if any(k in payload for k in ("pbe", "isp", "zls_v2")):
        validate_pbe_isp_zls(payload, diags, checks)
    if any(k in payload for k in ("pds", "texstate")):
        validate_pds_texstate(payload, diags, checks)
    if "mmuv4" in payload:
        validate_mmuv4(payload["mmuv4"], diags, checks)

    if not checks:
        _err(diags, "FREECS_EMPTY_PAYLOAD", "No recognized Volcanic command-stream sections in input")

    errors = [d for d in diags if d.severity == "error"]
    passed = len(errors) == 0
    fault_reason = None if passed else f"{errors[0].code}: {errors[0].message}"
    eval_ms = (time.perf_counter() - t0) * 1000.0

    return make_verdict(
        domain="freecs",
        target=target,
        workload=workload,
        passed=passed,
        eval_ms=eval_ms,
        checks=checks,
        diagnostics=diags,
        fault_reason=fault_reason,
        fault_severity=min(1.0, 0.4 + 0.15 * len(errors)) if not passed else 0.0,
    )
