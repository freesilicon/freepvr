#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 FreePVR Contributors
"""
FreePVR / FreeCS — PowerVR Volcanic Command-Stream & Control-Register Offline Validator.

Clean-room bitfield packer, decoder, and proof-vector validator implementing the
Volcanic (`DXT-48-1536` / `CXTP-48-1536 MC1`) command-stream and state layouts
specified in `spec/freecs/VDM-PPP-CLEANROOM-SPEC.md`:

  - 64-bit QWORD DCE (Draw Call Engine) packets (`0x01..0x0c`) & Handle Buffer entries
  - Volcanic `PPP_STATE` (`TA_STATE_HEADER`) dword-count & single-viewport proof vector
  - Volcanic `PBESTATE_STATE_WORD0..2` raw byte-addressed 48-bit VA encoding
  - Volcanic `TEXSTATE_IMAGE_WORD0..2` (15-bit 32K width/height, raw byte VA, `smpcnt`)
  - Volcanic `RGX_CR_SOFT_RESET` (`0x0100`) valid mask & 6-step `RGXStart` boot sequence
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from typing import Dict, List


# --- DCE (Draw Call Engine) QWORD opcodes in QWORD0[63:56] ---
DCE_OPCODES: Dict[int, str] = {
    0x01: "DCE_PPP_STATE_UPDATE",
    0x03: "DCE_TESS_DDM_CONFIG",
    0x04: "DCE_SHADER_STATE_UPDATE",
    0x05: "DCE_PREDICATE_WRAPPER",
    0x06: "DCE_DRAW_COMMAND",
    0x07: "DCE_DEPTH_BIAS_UPDATE",
    0x08: "DCE_SAMPLE_MASK_UPDATE",
    0x09: "DCE_SCISSOR_ARRAY_UPDATE",
    0x0A: "DCE_SCISSOR_DBIAS_EMIT",
    0x0C: "DCE_BARRIER",
}

# --- RGX_CR_SOFT_RESET (0x0100) GPL-confirmed masks ---
RGX_CR_SOFT_RESET_OFFSET = 0x0100
RGX_CR_SOFT_RESET_MASKFULL = 0x7FFFFFE0_007BDEFF
RGX_CR_SOFT_RESET_GARTEN_EN = 1 << 37  # 0x00000020_00000000 (FW CPU)
RGX_CR_SOFT_RESET_INTERCONNECT_GROUP = 0x00003840_00000800
RGX_CR_SOFT_RESET_JONES_CORE_GROUP = 0x00000000_000080F2

RGX_START_SOFT_RESET_STEPS: List[int] = [
    RGX_CR_SOFT_RESET_GARTEN_EN | RGX_CR_SOFT_RESET_INTERCONNECT_GROUP,
    RGX_CR_SOFT_RESET_GARTEN_EN | RGX_CR_SOFT_RESET_INTERCONNECT_GROUP | RGX_CR_SOFT_RESET_JONES_CORE_GROUP,
    RGX_CR_SOFT_RESET_GARTEN_EN | RGX_CR_SOFT_RESET_JONES_CORE_GROUP,
    RGX_CR_SOFT_RESET_GARTEN_EN,
    0x00000000_00000000,
]


@dataclass(frozen=True)
class DcePppUpdate:
    word_count: int
    addr: int


def pack_dce_handle_entry(dev_va: int, dword_count: int) -> int:
    """Pack a 64-bit DCE_HANDLE_ENTRY (`qword_count[63:32] | ((dev_va + 7) >> 3)`)."""
    if (dev_va & 0x7) != 0:
        raise ValueError(f"DCE component stream VA must be 8-byte aligned: 0x{dev_va:x}")
    if (dword_count & 0x1) != 0:
        raise ValueError(f"DCE component stream dword_count must be even: {dword_count}")
    va_shifted = dev_va >> 3
    if va_shifted >= (1 << 32):
        raise ValueError(f"DCE component stream VA exceeds 35-bit lower window: 0x{dev_va:x}")
    qwords = dword_count >> 1
    return ((qwords & 0xFFFFFFFF) << 32) | va_shifted


def pack_dce_ppp_state_update(addr: int, word_count: int) -> int:
    """Pack DCE opcode 0x01 (`DCE_PPP_STATE_UPDATE`, 1 QWORD)."""
    if (addr & 0x3) != 0:
        raise ValueError(f"PPP state VA must be 4-byte aligned: 0x{addr:x}")
    if not (0 <= word_count < (1 << 10)):
        raise ValueError(f"PPP word_count out of 10-bit range: {word_count}")
    addr_dw = addr >> 2
    if addr_dw >= (1 << 46):
        raise ValueError(f"PPP state VA exceeds 48-bit address space: 0x{addr:x}")
    return (0x01 << 56) | (word_count << 46) | addr_dw


def unpack_dce_ppp_state_update(qword: int) -> DcePppUpdate:
    op = (qword >> 56) & 0xFF
    if op != 0x01:
        raise ValueError(f"Expected DCE_PPP_STATE_UPDATE opcode 0x01, got 0x{op:02x}")
    return DcePppUpdate(
        word_count=(qword >> 46) & 0x3FF,
        addr=(qword & ((1 << 46) - 1)) << 2,
    )


def ppp_state_header_dword_count(header: int) -> int:
    """
    Compute total emitted dwords (including header) for a Volcanic `TA_STATE_HEADER`.

    Legacy Rogue bits `[5]` (`dbsc`), `[6]` (`pds_ptr0`), and `[9]` (`pds_ptr3`) are
    unused on Volcanic `DXT-48-1536` and rejected if set.
    """
    removed_mask = (1 << 5) | (1 << 6) | (1 << 9)
    if header & removed_mask:
        raise ValueError(f"Legacy Rogue-only TA_STATE_HEADER bits set: 0x{header & removed_mask:08x}")

    dwords = 1  # header itself
    bit_weights = {
        0: 1,   # pres_ispctl
        1: 1,   # pres_ispctl_fa
        2: 1,   # pres_ispctl_fb
        3: 1,   # pres_ispctl_ba
        4: 1,   # pres_ispctl_bb
        7: 1,   # pres_pds_data_addr
        8: 1,   # pres_pds_shader_table_fsr
        10: 2,  # pres_region_clip (CLIP0..1)
        16: 1,  # pres_wclamp
        17: 1,  # pres_outselects
        18: 1,  # pres_varying_word0
        19: 1,  # pres_varying_word1
        20: 1,  # pres_varying_word2
        21: 1,  # pres_ppp_ctrl
        22: 1,  # pres_stream_out_size
        23: 2,  # pres_stream_out_program
        25: 2,  # pres_terminate
    }
    for bit, weight in bit_weights.items():
        if (header >> bit) & 1:
            dwords += weight
    if (header >> 11) & 1:  # pres_viewport
        vp_cnt = ((header >> 12) & 0xF) + 1
        dwords += 6 * vp_cnt
    return dwords


def pack_pbe_state_va(dev_va: int) -> Dict[str, int]:
    """
    Pack a 48-bit Volcanic device VA into `PBESTATE_STATE_WORD0` and `STATE_WORD1` address bits.

    Volcanic `PBESTATE_STATE_WORD0` holds raw byte `dev_va[31:0]` (`shift=0`, NOT `shift=2`).
    """
    w0_addr_lo = dev_va & 0xFFFFFFFF
    w1_addr_hi = (dev_va >> 32) & 0x1FFF       # bits [12:0] = dev_va[44:32]
    w1_addr_top = ((dev_va >> 47) & 0x1) << 13  # bit  [13]   = dev_va[47]
    return {
        "state_word0": w0_addr_lo,
        "state_word1_addr_bits": w1_addr_top | w1_addr_hi,
    }


def unpack_pbe_state_va(state_word0: int, state_word1: int) -> int:
    lo = state_word0 & 0xFFFFFFFF
    hi = state_word1 & 0x1FFF
    top = (state_word1 >> 13) & 0x1
    # Canonical 48-bit sign/top-bit reconstruction (bits [46:45] mirror top or zero in 40/48-bit VA)
    return lo | (hi << 32) | (top << 47)


def pack_texstate_image_word0_dims(width: int, height: int) -> int:
    """Pack 15-bit 32K Volcanic `TEXSTATE_IMAGE_WORD0` (`width=[41:27]`, `height=[56:42]`)."""
    if not (1 <= width <= 32768) or not (1 <= height <= 32768):
        raise ValueError(f"Volcanic 15-bit texture dimensions out of range [1..32768]: {width}x{height}")
    return ((height - 1) << 42) | ((width - 1) << 27)


def decode_texstate_sample_count(image_word2_lo: int) -> int:
    """Clean-room `usclib_volcanic_tex_state_samples`: `1u << ((word2_lo >> 6) & 3)`."""
    return 1 << ((image_word2_lo >> 6) & 0x3)


def validate_soft_reset_sequence() -> Dict[str, object]:
    """Validate GPL-confirmed 6-step `RGX_CR_SOFT_RESET` sequence against `MASKFULL`."""
    for idx, val in enumerate(RGX_START_SOFT_RESET_STEPS):
        invalid = val & ~RGX_CR_SOFT_RESET_MASKFULL
        if invalid != 0:
            raise AssertionError(f"Step {idx} sets invalid SOFT_RESET bits: 0x{invalid:016x}")
    # GARTEN_EN (bit 37) must stay asserted through steps 0..3 and clear alone on final release
    for idx in range(4):
        assert (RGX_START_SOFT_RESET_STEPS[idx] & RGX_CR_SOFT_RESET_GARTEN_EN) != 0
    assert RGX_START_SOFT_RESET_STEPS[3] == RGX_CR_SOFT_RESET_GARTEN_EN
    assert RGX_START_SOFT_RESET_STEPS[4] == 0
    return {
        "register_offset": f"0x{RGX_CR_SOFT_RESET_OFFSET:04x}",
        "maskfull": f"0x{RGX_CR_SOFT_RESET_MASKFULL:016x}",
        "steps": [f"0x{v:016x}" for v in RGX_START_SOFT_RESET_STEPS],
    }


def run_self_test() -> int:
    """Run deterministic offline self-tests against Volcanic FreeCS proof vectors."""
    # 1. DCE Handle Entry & PPP State Update round-trip
    h = pack_dce_handle_entry(0x0000_0001_2345_6780, 64)
    assert (h >> 32) == 32
    assert (h & 0xFFFFFFFF) == (0x0000_0001_2345_6780 >> 3)

    ppp_va = 0x0000_0080_1000_0040
    pkt = pack_dce_ppp_state_update(ppp_va, 3)  # terminate packet
    dec = unpack_dce_ppp_state_update(pkt)
    assert dec == DcePppUpdate(word_count=3, addr=ppp_va), dec

    # 2. Exact PPP Single-Viewport proof vector: 0x00ff_0d87 -> 23 dwords (0x17)
    assert ppp_state_header_dword_count(0x00FF_0D87) == 23
    # Stream terminate PPP state: pres_terminate (bit 25) -> 1 hdr + 2 terminate = 3 dwords
    assert ppp_state_header_dword_count(0x0200_0000) == 3
    try:
        ppp_state_header_dword_count(1 << 6)  # legacy Rogue pres_pds_state_ptr0 must fail
        raise AssertionError("Expected rejection of legacy Rogue TA_STATE_HEADER bit 6")
    except ValueError:
        pass

    # 3. PBESTATE raw byte-addressed VA (no shift="2" bug)
    test_va = 0x0000_0F12_3456_7893  # intentionally non-4-byte aligned to prove shift=0
    pbe = pack_pbe_state_va(test_va)
    assert pbe["state_word0"] == 0x3456_7893
    assert unpack_pbe_state_va(pbe["state_word0"], pbe["state_word1_addr_bits"]) == test_va

    # 4. TEXSTATE 15-bit 32K width/height & MSAA smpcnt
    w0 = pack_texstate_image_word0_dims(32768, 16384)
    assert ((w0 >> 27) & 0x7FFF) == 32767
    assert ((w0 >> 42) & 0x7FFF) == 16383
    assert [decode_texstate_sample_count(s << 6) for s in range(4)] == [1, 2, 4, 8]

    # 5. RGX_CR_SOFT_RESET valid mask & 6-step RGXStart sequence
    validate_soft_reset_sequence()

    print("[self-test] PASS: FreeCS DCE, PPP (0x00ff_0d87=23dw), PBE byte VA, TEXSTATE 32K/smpcnt, SOFT_RESET")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="FreePVR / FreeCS — PowerVR Volcanic Command-Stream & CR Offline Validator"
    )
    ap.add_argument("--self-test", action="store_true", help="Run built-in offline FreeCS proof-vector tests")
    ap.add_argument("--decode-ppp-header", type=lambda s: int(s, 0), default=None, help="Decode TA_STATE_HEADER dword count")
    ap.add_argument("--show-soft-reset", action="store_true", help="Print GPL-confirmed RGX_CR_SOFT_RESET sequence as JSON")
    args = ap.parse_args()

    if args.self_test:
        return run_self_test()
    if args.decode_ppp_header is not None:
        dw = ppp_state_header_dword_count(args.decode_ppp_header)
        print(json.dumps({"header": f"0x{args.decode_ppp_header:08x}", "dwords": dw}, indent=2))
        return 0
    if args.show_soft_reset:
        print(json.dumps(validate_soft_reset_sequence(), indent=2))
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
