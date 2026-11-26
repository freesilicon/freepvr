# FreePVR / FreeUSC — On-Device Vulkan Capture Manifest

Runbook and 1-command capture instructions to collect real PowerVR Volcanic compiled shader blobs from Pixel 10 (`DXT-48-1536`, `BVNC 71.2.2448.1212`) and Pixel 11 (`CXTP-48-1536 MC1`) silicon using our 33-probe 1-op paired-twin SPIR-V corpus (`probes/spirv/`).

> **Clean-Room Notice:** Keep all compiled `.spv` files and pulled device `.bin` captures strictly inside gitignored `local-captures/`. Host differential analysis runs offline via `tools/freeusc/volcanic_bitdiff.py`.

---

## 1. Stock `vulkan.powervr.so` Capture Surface Findings (Verified Offline)

Inspection of Pixel 10 stock `vulkan.powervr.so` (`DDK 25.3@6908880`) established:

| Vulkan Capture Surface | Supported on Pixel 10 `25.3@6908880`? | Notes |
| --- | --- | --- |
| `vkGetPipelineCacheData` | **YES** (`IMG_vkGetPipelineCacheData`) | Emits `36 B` header (`32 B` `VkPipelineCacheHeaderVersionOne` + `4 B` extra field at `+0x20`) + serialized pipeline cache payload. |
| `VK_KHR_pipeline_binary` | **YES** (`IMG_vkCreatePipelineBinariesKHR`, `IMG_vkGetPipelineBinaryDataKHR`, `IMG_vkGetPipelineKeyKHR`) | **Primary clean capture path**: extracts per-pipeline serialized shader binaries directly (`32 B` key + `CopyShader` payload) with zero cache overhead. |
| `VK_KHR_pipeline_executable_properties` | **NO** (`0` exported symbols/strings) | Probed dynamically at runtime by `vk_pipeline_cache_sweeper` in case Pixel 11 or future DDK builds enable it. |

---

## 2. Regenerate `.spv` Binaries & Build the ARM64 Sweeper (Host-Side, No Device Required)

### Step A — Compile & Validate All 33 SPIR-V Probes Locally

```bash
# Requires glslangValidator + spirv-val (Vulkan SDK or glslang-tools + spirv-tools)
python3 tools/freeusc/gen_spirv_probes.py --out-dir local-captures/spv --keep-disasm
```

### Step B — Build ARM64 Sweeper (Option 1: Android NDK, recommended for Android `/data/local/tmp`)

```bash
# Requires Android NDK r26+ (ships Vulkan 1.3/1.4 headers and libvulkan.so stub)
$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android34-clang \
  -std=c11 -O2 -Wall -Wextra -Werror \
  -o local-captures/vk_pipeline_cache_sweeper_android_arm64 \
  probes/vk_pipeline_cache_sweeper.c \
  -lvulkan
```

### Step B — Build ARM64 Sweeper (Option 2: Linux ARM64 Container / Native GCC)

```bash
gcc -std=c11 -O2 -Wall -Wextra -Werror \
  -o local-captures/vk_pipeline_cache_sweeper_linux_arm64 \
  probes/vk_pipeline_cache_sweeper.c -lvulkan
```

*(Note: Use Option 1's bionic-linked binary on stock Android; Option 2's glibc-linked binary is for Linux ARM64 chroot/Mesa host runs and CI compile verification.)*

---

## 3. 1-Command On-Device Capture

Replace `<SERIAL>` with your test Pixel 10 (`DXT-48-1536`) or Pixel 11 (`CXTP-48-1536 MC1`) ADB serial and `<TAG>` with e.g. `pixel10-dxt48` or `pixel11-cxtp48`:

```bash
SERIAL="<SERIAL>" TAG="<TAG>" && \
adb -s "$SERIAL" shell "rm -rf /data/local/tmp/freepvr_probes && mkdir -p /data/local/tmp/freepvr_probes/spv /data/local/tmp/freepvr_probes/out" && \
adb -s "$SERIAL" push local-captures/vk_pipeline_cache_sweeper_android_arm64 /data/local/tmp/freepvr_probes/sweeper && \
adb -s "$SERIAL" push local-captures/spv/. /data/local/tmp/freepvr_probes/spv/ && \
adb -s "$SERIAL" shell "chmod +x /data/local/tmp/freepvr_probes/sweeper && /data/local/tmp/freepvr_probes/sweeper --spv-dir /data/local/tmp/freepvr_probes/spv --out-dir /data/local/tmp/freepvr_probes/out" && \
mkdir -p "local-captures/captures/${TAG}" && \
adb -s "$SERIAL" pull /data/local/tmp/freepvr_probes/out/. "local-captures/captures/${TAG}/" && \
adb -s "$SERIAL" shell "rm -rf /data/local/tmp/freepvr_probes"
```

### Expected Output Artifacts in `local-captures/captures/<TAG>/`

| Artifact Pattern | Count | Description |
| --- | --- | --- |
| `capture_manifest.json` | 1 | Device metadata (`deviceName`, `vendorID=0x1010`, `deviceID`, `subgroupSize=128`, `pipelineCacheUUID`, enabled extensions) + per-probe status & sizes |
| `<probe>.pipeline_cache.bin` | 33 | Isolated single-pipeline `vkGetPipelineCacheData` blob per SPIR-V twin |
| `<probe>.pipeline_binary_0.bin` | 33 | Direct `VK_KHR_pipeline_binary` (`vkGetPipelineBinaryDataKHR`) serialized shader binary per twin |
| `<probe>.exec_*.{txt,bin}` | 0 (Pixel 10) / N (if enabled) | Internal representations if `VK_KHR_pipeline_executable_properties` is supported |

> **Privacy & Clean-Room Guardrail:** Keep all pulled `.bin` captures strictly inside gitignored `local-captures/captures/<TAG>/`. Never commit captured driver binaries or device serial numbers.

---

## 4. Host Differential Analysis (Post-Capture)

Once the capture directory is pulled, run the offline FreeUSC bit-diff & opcode-cluster sweep:

```bash
# Diff VK_KHR_pipeline_binary blobs across all 33 paired twins:
python3 tools/freeusc/volcanic_bitdiff.py diff-dir \
  local-captures/captures/<TAG> \
  --manifest probes/spirv/corpus_manifest.json \
  --suffix .pipeline_binary_0.bin \
  > local-captures/captures/<TAG>/bitdiff_pipeline_binary.json

# Cross-check against vkGetPipelineCacheData blobs:
python3 tools/freeusc/volcanic_bitdiff.py diff-dir \
  local-captures/captures/<TAG> \
  --manifest probes/spirv/corpus_manifest.json \
  --suffix .pipeline_cache.bin \
  > local-captures/captures/<TAG>/bitdiff_pipeline_cache.json

# Inspect a single paired twin (e.g., iadd32 vs isub32):
python3 tools/freeusc/volcanic_bitdiff.py diff \
  local-captures/captures/<TAG>/04_iadd32.pipeline_binary_0.bin \
  local-captures/captures/<TAG>/05_isub32.pipeline_binary_0.bin
```

---

## 5. Paired-Twin Corpus Inventory (`probes/spirv/`, 33 Probes)

All 33 probes compile from GLSL 4.60 (`local_size_x = 128`) to SPIR-V 1.5 (`--target-env vulkan1.2`) and pass `spirv-val --target-env vulkan1.2`.

| Group | Probes | Isolated Hardware Dimension |
| --- | --- | --- |
| `baseline` | `00_empty_nop`, `01_store_const_0`, `02_store_const_imm`, `02b_store_const_imm_alt` | Prologue/epilogue, USC `END`, SSBO `ST`, zero special-constant vs `imm32` bit positions |
| `mem_burst` | `03a_mov_ssbo_1dw`, `03b_mov_ssbo_2dw`, `03c_mov_ssbo_4dw` | Backend `LD`/`ST` burst length (`1`, `2`, `4` dwords) |
| `int_alu` | `04_iadd32`, `05_isub32`, `06_imul32`, `07_imad32` | Integer ALU opcodes & 3-source MAD encoding |
| `fp_alu` | `08_fadd32`, `09_fmul32`, `10_fma32`, `11_fadd32_neg_abs` | FP32 opcodes, FMA, and `neg`/`abs` source modifier bits |
| `bitwise` | `12_and32`, `13_or32`, `14_xor32`, `15_shl32`, `16_shr_u32`, `17_shr_s32` | Bitwise logic & logical/arithmetic shift opcode bits |
| `control_flow` | `18_cmp_eq_sel`, `19_branch_if_else`, `20_loop_4` | Predication/select vs divergent CFG branch vs dynamic loop back-edge |
| `sync` | `21_barrier_shared` | Workgroup shared memory addressing + control/memory `barrier()` |
| `atomics` | `22_atomic_add_ssbo`, `23_atomic_max_ssbo` | Backend atomic operation selector (`ADD` vs `MAX`) |
| `bda` | `24_bda_load_store_u32`, `25_bda_load_store_u64` | 40-bit global `PhysicalStorageBuffer` (`GL_EXT_buffer_reference`) `LD`/`ST` width (`u32` vs `u64`) |
| `subgroup` | `26_subgroup_id_128`, `27_subgroup_ballot_128`, `28_subgroup_add_128`, `29_subgroup_shuffle_128` | Volcanic 128-wide subgroup lane ID SR, 128-bit ballot, reduction, and shuffle |

### Dual-Silicon Comparison (`DXT-48-1536` vs `CXTP-48-1536 MC1`)

Running the exact same 33 `.spv` binaries on Pixel 10 (`DXT-48-1536`) and Pixel 11 (`CXTP-48-1536 MC1`) and diffing `<p10>/<probe>.pipeline_binary_0.bin` against `<p11>/<probe>.pipeline_binary_0.bin` immediately proves whether `CXTP` introduces any USC ISA encoding deltas, register-bank shifts, or subgroup-lowering changes relative to `DXT`.
