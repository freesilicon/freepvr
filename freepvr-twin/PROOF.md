# FreePVR Volcanic GPU Digital Twin — Verification & Mesa Stage G3B Proof

## 1. Test & Self-Test Verification Summary

All offline tests and CLI self-tests pass with zero external dependencies (Python 3 stdlib only, 100% offline):

| Suite / Command | Result | Details |
| --- | --- | --- |
| `python3 -m unittest discover -s tests -v` | **PASS (22 / 22 in 0.072s)** | 13 `freecs-validate` positive/negative tests + 9 `freeusc-sim` ISA, 128-lane compute/subgroup/BDA/atomic/AF32, and Mesa Stage G3B tests |
| `./freecs-validate --self-test` | **PASS (1 positive + 10 negative)** | Full Volcanic bundle validated in `0.058 ms` (`< 5 ms` gate); catches 40B Rogue cCCB header, 16B Rogue `CCCB_CTL`, `config_flags` `0x00020004`, zero `hwperf_ctl_dma_addr`, invalid DCE opcode `0x02`, removed Rogue PPP inline PDS bit `[6]`, misaligned 16B Pixel Shader Table, PBE Rogue `shift="2"`, PDS Rogue `SLCMODE` bit 62, and TEXSTATE 14-bit width/height drift |
| `./freeusc-sim --self-test` | **PASS (4/4 clean kernels + Mesa proof)** | Clean 128-lane BDA FMA (`max_abs_err=0.0`), Bitwise+`BRLT` loop+`WGBARRIER` (`max_abs_err=0.0`), 128-wide Subgroup Broadcast+Add+Shuffle+Ballot (`max_abs_err=0.0`), Global BDA `ATOM32.ADD` (`8128` exact), plus Mesa `fma32` bit-exact PASS & Mesa `iadd_sg` real compiler bug catch |
| `REWARD-SCHEMA.json` Conformance | **PASS (100%)** | Every passing and failing verdict verified against `validate_verdict_schema()` (`schema_version="freesilicon.twin.v1"`, `domain` in `{"freecs","freeusc"}`, reward bounds, required fields, diagnostics) |

## 2. Host CPU Throughput Benchmarks

Target requirement: `>= 1,000` validations/simulations per minute and `< 5 ms` per `freecs-validate` stream.

| Benchmark Command | Iterations | Total Time | Latency (`ms/eval`) | Throughput (`evals/min`) | Gate |
| --- | ---: | ---: | ---: | ---: | --- |
| `./freecs-validate --bench 5000` | `5,000` | `77.36 ms` | **`0.0155 ms`** | **`3,878,236 / min`** | **PASS (`< 5 ms`, `3878×` target)** |
| `./freeusc-sim --bench 500` (128-lane `fma32`) | `500` | `1,726.29 ms` | **`3.4526 ms`** | **`17,378 / min`** | **PASS (`17.4×` target)** |

## 3. Killer Proof: Mesa `pco` Stage G3B Compute Binaries (`fma32` & `iadd_sg`)

Both actual ARM64 Mesa `pco` Stage G3B Volcanic compute binaries (`fma32` 352 B / 88 dwords and `iadd_sg` 276 B / 69 dwords) were decoded and executed strictly against the authoritative Volcanic hardware ISA specification (`VOLCANIC-USC-ISA-MAP.md` + `VOLCANIC-USC-OPCODES.md`) using the exact `vk_volcanic_compute_smoke` 128-lane SSBO descriptor setup (`sh0:sh1 = 40-bit VA 0x008010000000`, `sh2 = 4096`, `sh3 = 0`, `cs0 = workgroup_id.x = 0`, `vi0 = local_invocation_index = 0..127`).

### 3.1 `fma32` (352 B / 88 dwords) — **PASS (BIT-EXACT, 128 / 128 LANES)**

- **Shader**: `buf.data[i] = fma(x, 2.0, x + 1.0)` initialized with `x = (float)i` for `i in 0..127`.
- **Verdict**: `pass = true`, `reward = 0.997424`, `nmse = 0.0`, `max_abs_err = 0.0`, `ssbo_mismatches = 0 / 128`, `dynamic_steps = 33`, `temps_used = 5`, `shareds_used = 4`, `sram_bytes = 2588`, `est_hw_us = 0.166`, `eval_ms = 3.25 ms`.
- **Execution Trace Summary**:
  - `@00..@17`: Local invocation bounds check (`vi0 < 128`), `COND.CNDST`, `CTRL.BR bcc=1 (+69 -> @86)` not taken (all 128 lanes active).
  - `@19..@50`: Computes `gl_GlobalInvocationID.x * 4` via `INT.IMUL` (`sc61 = 4`) and 64-bit BDA address `(r1, r2) = sh0:sh1 + i*4 + signext(sh3)`.
  - `@53`: `DMA.LD dst=r1, base=r1:r2, bytes=4, fset=1` loads `x = (float)i`.
  - `@57`: `FOP.FADD fw=1 r2 = r1 + sc1(1.0f)`.
  - `@59`: `FOP.FMAD r4 = fma(r1, sc40(2.0f), r2)`.
  - `@61..@82`: Recomputes 64-bit address `(r2, r3)` and executes `DMA.ST data=r4, base=r2:r3, bytes=4, fset=1`.
  - `@86..@87`: `COND.CNDEND adj=1`, `CTRL.END`.

### 3.2 `iadd_sg` (276 B / 69 dwords) — **FAIL: REAL MESA `pco-volcanic` ENCODING BUG SURFACED OFFLINE**

- **Shader**: `buf.data[i] = (i + 1u) + gl_SubgroupID` (expected `i + 1` for `i in 0..127`, `gl_SubgroupID = vi0 >> 7 = 0`).
- **Verdict**: `pass = false`, `reward = -0.8`, `ssbo_mismatches = 128 / 128`, `max_abs_err = 7.0`, `nmse = 0.008868`, `eval_ms = 2.36 ms`.
- **Observed SSBO Output**: Every lane writes `(i + 1) + 7 = i + 8` (`lane 0: 0x00000008` vs expected `0x00000001`, `lane 1: 0x00000009` vs `0x00000002`, ..., `lane 127: 0x00000087` vs `0x00000080`).
- **Exact Faulting Instruction**:
  - **PC**: `@32` (`byte_offset = 0x0080 = 128`)
  - **Raw Dwords (4w `BIT_LUT32` `0x06`)**: `0x0155fe36 0x50040810 0x4000300a 0x8184031a`
  - **Hardware Spec Decode**:
    - `family = BIT_LUT32 (0x06)`
    - `lut = 0xcccccccc`
    - `sh_c = 3 (LSR)`
    - `dst0 = r1`, `src0 = vi0`, `src1 = sc59 (7)`, `src2 = sc0 (0)`, `src3 = sc0 (0)`, `cc = EXEC_MASK (1)`
- **Semantic Root Cause**:
  1. In Volcanic `ISA_BIT` (`VOLCANIC-USC-OPCODES.md` Section 2.3), the 5-input truth-table LUT maps `in0 = src0` (`0xaaaa`) and `in1 = src1` (`0xcccc`) — verified across `and` (`0x0008 = in0 & in1`), `bfi` (`0xd8d8 = (in1 & in0) | (in2 & ~in0)`), and `pco_binary_volcanic.c`'s own `vemit_cps` (`0xaaaacccc = p0 ? src0 : src1`).
  2. In `PCO_OP_SHIFT`, `pco_binary_volcanic.c` emitted `lut = 0xcccccccc` while placing the **value to shift (`vi0`) in `src0`**, the **shift count (`sc59 = 7`) in `src1`**, and **`sc0 (0)` in `src2`**.
  3. Consequently, the LUT (`0xcccccccc`) passes **`in1 = src1 = 7`** and completely discards **`in0 = src0 = vi0`**, and the post-shift `sh_c = 3 (LSR)` shifts by `src2 = 0`, producing `r1 = 7 >> 0 = 7` instead of `vi0 >> 7 = 0`! (Even if a shift unit sourced its shift count from `src1`, `lut = 0xcccccccc` would still discard `vi0` and evaluate `7 >> 7 = 0` even when `vi0 >= 128` where `gl_SubgroupID` must be `1..3`.)
  4. `fma32` escaped this bug only because `i << 2` hit `PCO_OP_SHIFT`'s constant-`LSL` fast path (`INT.IMUL` by `sc61 = 4`), whereas `iadd_sg`'s `vi0 >> 7` (`SHR`) fell through to the broken `BIT_LUT32` path.
- **Recommended Fix for Mesa `pco_binary_volcanic.c` (`PCO_OP_SHIFT`)**:
  - Use `lut = 0xaaaaaaaaU` (`in0 = src0`) with `src0 = value`, `src1 = sc0`, `src2 = shift_amt` — OR place `value` in `src1` (`lut = 0xccccccccU`) and `shift_amt` in `src2`.

### 3.3 Secondary Latent Mesa `pco` Finding (`ISA_MOV` `PCK`/`UNPCK` Bitmap Swap)

Cross-checking shipping `DXT-48-1536` `rgx.sh` `ISA_MOV` vectors (`0x408381ca 0x80531200`, `0x40938aca 0x00001810 0x80400038`) reveals:
- Hardware 10-bit field (`w1[22],w1[4],w1[17],w1[21],w1[12],w2[3],w1[16],w2[8],w2[9],w2[5]`) holds `conv_fmt` (`0x255` = `IMM 85` U32 unpack, `0x232` = `IMM 50` U32 pack, `0x200` = `IMM 0` F32 identity).
- Hardware 11-bit field (`w2[23],w2[10],w2[24:27],w2[11],w2[28:30],w2[12]`) holds `idx_mod` (`0x000` = `IMM 0`).
- `pco_binary_volcanic.c` has these two `ISA_MOV` bitmaps swapped. On `iadd_sg @19` and `fma32 @19` (`MOV r0 = cs0`, `0xc003804a`), both `conv_fmt` and `idx_mod` are `IMM 0`, so the bug is latent on plain register copies but will corrupt non-zero `PCK`/`UNPCK` conversion formats until fixed.
