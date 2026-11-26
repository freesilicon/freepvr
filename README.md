# FreePVR

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-3ECF8E?style=flat-square)](LICENSE)
[![Target: Volcanic DXT-48-1536 & CXTP-48-1536](https://img.shields.io/badge/silicon-DXT--48--1536%20%7C%20CXTP--48--1536-38BDF8?style=flat-square)](spec/freeusc/VOLCANIC-USC-ISA-MAP.md)
[![CI: Offline Tests & Clean-Room Audit](https://img.shields.io/badge/CI-Tests%20%26%20Clean--Room%20Audit-3ECF8E?style=flat-square)](.github/workflows/ci.yml)

**Clean-room PowerVR Volcanic GPU ISA specifications, command-stream decoders, and differential SPIR-V probe tooling for Google Pixel 10 (`DXT-48-1536`, `BVNC 71.2.2448.1212`) and Pixel 11 (`CXTP-48-1536 MC1`).**

FreePVR documents the hardware interfaces required to bring up open-source Vulkan and Linux DRM drivers on Imagination PowerVR Volcanic silicon—feeding upstream **Mesa (`pvr` / `pco` / `csbgen`)** and **Linux (`drm/imagination`)** with strictly clean-room specifications and reproducible offline verification tools.

---

## Plain-English Overview

Modern Pixel Tensor SoCs pair Google's custom CPU/TPU blocks with **Imagination PowerVR Volcanic** GPUs (`DXT-48-1536` on Tensor G5 / Pixel 10, `CXTP-48-1536 MC1` on Tensor G6 / Pixel 11). While upstream Linux (`drm/imagination`) and Mesa (`pvr` + `pco`) already support earlier PowerVR **Rogue** GPUs, Volcanic introduces major breaking hardware changes:

- **Completely new shader machine code (`FreeUSC`)**: Replaces Rogue's byte-aligned 3-phase instruction groups with variable-length 1–4 × 32-bit little-endian words framed by bit 31 (`EOI`), 17 opcode families, 16 canonical register banks, re-indexed special registers/constants, inline data fences, and **128-wide SIMD subgroups**.
- **Completely new geometry command stream & state descriptors (`FreeCS`)**: Replaces Rogue's 32-bit `VDMCTRL` stream with a two-level **64-bit QWORD Draw Call Engine (DCE)**, an indirect 16-byte Pixel Shader Descriptor Table, 32K (15-bit) texture/surface coordinates, raw byte-addressed 48-bit virtual addresses in `PBESTATE`/`TEXSTATE`, multi-word ZLS v2, and a new 6-step `RGX_CR_SOFT_RESET` sequence.

FreePVR packages these two clean-room tracks plus a 33-shader differential SPIR-V probe bench into one standalone, zero-blob repository:

| Component | Path | Scope | Upstream Consumer |
| --- | --- | --- | --- |
| **`FreeUSC` Specs** | [`spec/freeusc/`](spec/freeusc/VOLCANIC-USC-ISA-MAP.md) | 17-family Volcanic USC word framing, 16 canonical banks, `SR 0..60`, `SC 0..127`, `CTRL`/`COND`, full ALU/Bitwise/DMA/SMP/ITR/EMI/PSB opcodes, 40/64-bit BDA, 128-wide subgroups, active BRNs | Mesa `src/imagination/pco/` |
| **`FreeCS` Specs** | [`spec/freecs/`](spec/freecs/VDM-PPP-CLEANROOM-SPEC.md) | 64-bit QWORD DCE (`vdm`), Volcanic `PPP_STATE` + 16B Pixel Shader Table, `RGX_CR_SOFT_RESET`, `S7_TOP` ISP/ZLS v2, `PBESTATE`, `TEXSTATE`, `CDM`/`PDS` deltas | Mesa `src/imagination/csbgen/volcanic/`, `pvr/`, Linux `drm/imagination` |
| **`FreeUSC` Tools** | [`tools/freeusc/`](tools/freeusc/volcanic_bitdiff.py) | Offline Volcanic USC instruction framer, Vulkan pipeline-cache / `VK_KHR_pipeline_binary` bit-diff & opcode clusterer, `rgx.sh` container parser, 33-probe SPIR-V generator | Offline host verification & compiler bring-up |
| **`FreeCS` Tools** | [`tools/freecs/`](tools/freecs/freecs_packet_validator.py) | Offline packer/decoder and proof-vector validator for DCE, `TA_STATE_HEADER`, `PBESTATE`, `TEXSTATE`, and `RGX_CR_SOFT_RESET` | `csbgen` XML cross-check |
| **Differential Probes** | [`probes/`](probes/CAPTURE-MANIFEST.md) | 33 clean-room GLSL 4.60 1-op paired-twin compute shaders (`local_size_x = 128`), standalone C11 Vulkan capture sweeper, on-device capture manifest | Pixel 10 (`DXT`) vs. Pixel 11 (`CXTP`) silicon validation |

---

## 60-Second Offline Quickstart (No Phone Required)

Every validator, parser, packet check, and clean-room audit runs offline on any macOS or Linux laptop with Python 3.9+ (standard library only):

```bash
# 1. Run the clean-room boundary & SPDX audit (must pass with 0 violations)
./scripts/clean-room-audit.sh

# 2. Run the full offline unit test suite (FreeUSC + FreeCS + probe corpus parity)
python3 -m unittest discover -s tests -v

# 3. Run individual CLI self-tests
python3 tools/freeusc/volcanic_bitdiff.py --self-test
python3 tools/freeusc/rgx_sh_parser.py --self-test
python3 tools/freeusc/gen_spirv_probes.py --self-test
python3 tools/freecs/freecs_packet_validator.py --self-test
```

### Optional: Compile SPIR-V Probes & On-Device Sweeper

Binary `.spv` files are intentionally **not committed** to this repository; only the 33 human-readable GLSL `.comp` sources are tracked under [`probes/spirv/`](probes/spirv/). To regenerate and validate all 33 SPIR-V 1.5 (`vulkan1.2`) binaries locally (requires `glslangValidator` and `spirv-val`):

```bash
python3 tools/freeusc/gen_spirv_probes.py --out-dir local-captures/spv --keep-disasm
```

To build the standalone C11 Vulkan capture sweeper (requires Vulkan headers and `libvulkan`):

```bash
gcc -std=c11 -O2 -Wall -Wextra -Werror \
  -o local-captures/vk_pipeline_cache_sweeper \
  probes/vk_pipeline_cache_sweeper.c -lvulkan
```

See [`probes/CAPTURE-MANIFEST.md`](probes/CAPTURE-MANIFEST.md) for the Android NDK cross-compile and 1-command on-device capture workflow.

---

## Upstream Mesa & Linux `drm/imagination` Handoff Highlights

All 13 hardware interface gaps needed for Volcanic Phase 4 bring-up are closed in the clean-room specifications with explicit provenance tags (`[V-GPL]`, `[V-BIN]`, `[V-ROUNDTRIP]`, `[OPEN]`):

1. **Critical Upstream `csbgen/volcanic/` Silicon Fixes Identified**:
   - `pbestate.xml`: `STATE_WORD0` is a raw byte address (`shift="0"`, NOT `shift="2"`); full `STATE_WORD0..2` + 64-bit `REG_WORD0..2` specified in [`VDM-PPP-CLEANROOM-SPEC.md`](spec/freecs/VDM-PPP-CLEANROOM-SPEC.md).
   - `texstate.xml` & `pco/usclib/tex.cl`: `DXT-48-1536` widens `IMAGE_WORD0` to 15-bit 32K dimensions (`width=[41:27]`, `height=[56:42]`), uses raw byte VA in `IMAGE_WORD1`, adds `IMAGE_WORD2[7:6]` = `smpcnt` (`1u << ((word2_lo >> 6u) & 3u)`), plus `IMAGE_WORD3` and `SAMPLER_WORD1`.
   - `cdm.xml`: 5 `DXT-48-1536` deltas (`KERNEL0.block_type=3`, 8-bit `usc_unified_size[19:12]`, `KERNEL1[1]`, `KERNEL8` common-store/event fields); `pds.xml` verified 100% clean.
2. **New `csbgen/volcanic/` Streams & Control Registers**:
   - Two-level 64-bit QWORD DCE (`Handle Buffer` + 10 opcodes `0x01..0x0c`), Volcanic `PPP_STATE` with indirect 16-byte Pixel Shader Descriptor Table (`0x00ff_0d87` = 23-dword single-viewport proof vector), `S7_TOP` ISP + 4-word ZLS v2, and the 6-step `RGX_CR_SOFT_RESET` (`0x0100`, mask `0x7fffffe0_007bdeff`, bit `37` = `GARTEN_EN`) boot sequence.
3. **Volcanic `pco` Shader Backend**:
   - Bit-exact 1–4 dword LE word framing (`bit 31` EOI, default-word elision, `CTRL.NOP` alignment, `CTRL.END = 0x8000031c`), 16 canonical register banks, Volcanic `SR 0..60` & `SC 0..127`, full execution families (`FOP`, `AF32`, `INT`, `DOT8`, `MOV`, `BIT`, `DMA`, `SMP`, `TAP`, `ITR`, `EMI`, `PSB_WR`/`RD`, `CTRL`, `COND`), 40/64-bit BDA, 128-wide `SLOT`-based subgroup lowering, and active Volcanic BRN catalog.
   - Framing and `ISA_CTRL` round-trip verified with **0 errors** across **1,927 real `DXT-48-1536` shaders (38,967 instructions)** (`4,587/4,587` `CTRL` round-trips, `139/139` valid relative `BR` target landings).

---

## Repository Layout

```text
.
├── LICENSE                                  # Apache-2.0
├── README.md
├── .github/workflows/ci.yml                 # Clean-room audit, offline tests, C11 & SPIR-V validation
├── probes/
│   ├── CAPTURE-MANIFEST.md                  # On-device VK_KHR_pipeline_binary capture runbook
│   ├── vk_pipeline_cache_sweeper.c          # Standalone C11 Vulkan 1.2 probe sweeper
│   └── spirv/                               # 33 clean-room GLSL 4.60 .comp sources + manifest (zero .spv blobs)
├── scripts/
│   └── clean-room-audit.sh                  # Enforces zero blobs, zero vendor code, zero PII/paths
├── spec/
│   ├── freecs/
│   │   └── VDM-PPP-CLEANROOM-SPEC.md        # FreeCS: DCE, PPP, CR, ISP/ZLS v2, PBE, TEXSTATE, CDM/PDS
│   └── freeusc/
│       ├── VOLCANIC-USC-ISA-MAP.md          # FreeUSC Core: framing, banks, SR/SC, CTRL, COND
│       ├── VOLCANIC-USC-OPCODES.md          # FreeUSC Ops: ALU, bitwise, DMA/BDA, SMP/ITR/EMI/PSB, subgroups
│       └── RGX-SH-INVENTORY.md              # Structural inventory of TQ shader container format
├── tests/
│   └── test_freepvr_offline.py              # Offline unittest/pytest suite (zero hardware needed)
└── tools/
    ├── freecs/
    │   └── freecs_packet_validator.py       # FreeCS packet/CR bitfield packer & proof-vector validator
    └── freeusc/
        ├── gen_spirv_probes.py              # 33-probe 1-op paired-twin GLSL/SPIR-V generator
        ├── rgx_sh_parser.py                 # rgx.sh container parser & chunk extractor
        └── volcanic_bitdiff.py              # Pipeline-cache parser, USC framer & paired-twin bit-differ
```

---

## Clean-Room Policy

- **Zero proprietary artifacts**: This repository contains **zero** vendor binaries (`.so`, `.bin`, `.fw`, `.ko`, `rgx.sh.*`, `.spv`), zero vendor source code, zero proprietary headers, and zero verbatim decompiled output.
- **Automated enforcement**: Every commit and pull request is gated by [`scripts/clean-room-audit.sh`](scripts/clean-room-audit.sh) and [`.github/workflows/ci.yml`](.github/workflows/ci.yml).
- **Local captures stay local**: Any user-extracted vendor files or on-device Vulkan pipeline captures belong strictly under gitignored `local-captures/` and must never be committed.

---

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
