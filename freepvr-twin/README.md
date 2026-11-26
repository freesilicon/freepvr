# FreePVR Volcanic GPU Digital Twin (`freecs-validate` + `freeusc-sim`)

Clean-room, zero-external-dependency Python 3 digital twin for PowerVR Volcanic (`DXT-48-1536` / `CXTP-48-1536`) command streams, Laguna DDK 25.3 firmware ABI descriptors, and 128-lane Volcanic USC compute shaders.

Emits deterministic `freesilicon.twin.v1` JSON verdicts and GRPO scalar rewards matching `REWARD-SCHEMA.json`.

## Components

| Entrypoint / Module | Domain | Purpose |
| --- | --- | --- |
| `./freecs-validate` | `freecs` | Validates Laguna DDK 25.3 FW-ABI (`16B` cCCB header vs `40B` Rogue, `32B` `CCCB_CTL` vs `16B`, `config_flags` `0x00020450`, non-zero `hwperf_ctl_dma_addr`, `SOFT_RESET` mask/bit 37), `CDM`, 64-bit QWORD `VDM/DCE`, `PPP` (`0x00ff0d87` + 16B Pixel Shader Table), `PBE`/`ISP`/`ZLS v2`, `PDS` (`DOUTU`/`DOUTD`), and `TEXSTATE` (`32K` 15-bit dims, raw byte VA, `smpcnt[7:6]`). |
| `./freeusc-sim` | `freeusc` | Decodes 1..4 LE dword bit-31 EOI Volcanic USC binaries across all 17 families and executes 128-lane (`subgroupSize=128`) compute shaders with 40/64-bit BDA/SSBO memory, `sr14` lane ID, 16 cross-lane `SLOT` registers (`sl0..sl15`), `CTRL`, `COND`, `INT`, `FOP`, `AF32` (+ `BRN73233`), `BIT` (`0x06`/`0x38`), `MOV`, `DOT8`, and `DMA` (`LD`/`ST`/`ATOM32`/`ATOM64`/`IDF`/`MEMBARRIER`). |
| `freepvr_twin/freecs.py` | library | Importable `validate_bundle()` and per-surface validators. |
| `freepvr_twin/freeusc_isa.py` | library | Importable 17-family Volcanic USC framer, decoder, and clean-room encoder. |
| `freepvr_twin/freeusc_sim.py` | library | Importable 128-lane `VolcanicUSCState`, `GlobalMemory`, `simulate_words()`, and `evaluate_compute_shader()`. |
| `freepvr_twin/reward.py` | library | `freesilicon.twin.v1` deterministic reward scorer and schema validator. |
| `freepvr_twin/fixtures.py` | library | Clean-room command-stream/shader fixtures and Mesa Stage G3B `iadd_sg` / `fma32` harnesses. |
| `tests/test_freepvr_twin.py` | tests | 22-test offline `unittest` suite covering positive/negative `freecs` and `freeusc` cases. |

## CLI Usage

```sh
# Validate built-in positive + 10 negative Volcanic command-stream / FW-ABI cases
./freecs-validate --self-test

# Validate a JSON bundle or raw binary stream (cdm | dce | ppp)
./freecs-validate --input path/to/bundle.json --kind bundle --workload my-pass
./freecs-validate --input path/to/stream.bin --kind cdm --workload my-cdm

# Benchmark freecs-validate throughput
./freecs-validate --bench 5000

# Run 128-lane Volcanic USC simulator self-tests (clean kernels + Mesa Stage G3B proof)
./freeusc-sim --self-test

# Run Mesa Stage G3B iadd_sg (276B) & fma32 (352B) proof against hardware ISA spec
./freeusc-sim --mesa-proof

# Disassemble or run a fixture or raw binary
./freeusc-sim --fixture fma32 --disasm
./freeusc-sim --fixture iadd_sg --disasm
./freeusc-sim --bin path/to/shader.bin --workload my-shader

# Benchmark 128-lane freeusc-sim throughput
./freeusc-sim --bench 500

# Run full unit test suite
python3 -m unittest discover -s tests -v
```
