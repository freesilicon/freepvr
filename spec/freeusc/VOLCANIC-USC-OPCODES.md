# FreeUSC — PowerVR Volcanic USC Execution Opcodes Reference (`FreePVR` Track 2B)

- **Targets:** Imagination PowerVR Volcanic **`DXT-48-1536`** (`71.2.2448.1212`) & **`CXTP-48-1536 MC1`** (`25.3@6908880`)
- **Companion Specification:** `VOLCANIC-USC-ISA-MAP.md` (`FreeUSC` Track 2A — 17-family prefix tree, EOI bit 31 framing, canonical register banks `0..15`, `SR 0..60`, `SC 0..127`, `CC 0..3`, `SF`/`fence_wait`/`fence_set`, `ISA_CTRL`, `ISA_COND`)
- **Purpose:** Clean-room, bit-exact execution opcode tables (Main ALU, Bitwise, Pack/Unpack, Backend `DMA`/`SMP`/`TAP`/`ITR`/`EMI`/`PSB`, 40-bit/64-bit Global BDA, and 128-wide Subgroup hardware lowering) for Phase 4 `pco-volcanic`.

## Provenance Legend

| Tag | Meaning |
|---|---|
| `[V-BIN]` | Bit-exact field/operand/table encoding verified directly against the Volcanic instruction encoder & compiler lowering tables. |
| `[V-ROUNDTRIP]` | Cross-checked against `1,378` shipping `DXT-48-1536` (`71.2.2448.1212`) shader binaries (`30,495` decoded instructions, `0` invalid opcodes). |
| `[OPEN]` | Exact bit positions and legal value ranges are known `[V-BIN]`, while specific semantic sub-enum labels await live compiler bitdiff confirmation (non-blocking for Phase 4 minimal compute/graphics). |

## Gap Closure Scorecard

| Gap / Requirement | Status | Summary |
|---|---|---|
| `[GAP-ISA-2]` Main ALU, Bitwise & Pack/Unpack Opcodes | **CLOSED** `[V-BIN]` `[V-ROUNDTRIP]` | Full bitmaps & encodings for `ISA_FOP` (`0`), `ISA_DOT8` (`1`), `ISA_INT` (`2`), `ISA_BIT` (`3` — dual `0x38` & `0x06` encodings + Truth-Table LUT), `ISA_MOV` (`4` — full 18-format `PCK`/`UNPCK` table + `LDMOV`/`STMOV` + indexing), `ISA_AF32` (`13`). |
| `[GAP-ISA-5]` Backend `LD`/`ST`/`ATOMIC`/`SMP`/`ITR`/`UVSW`/`PSB` | **CLOSED** `[V-BIN]` `[V-ROUNDTRIP]` | Full bitmaps & encodings for `ISA_DMA` (`6` — `LD`/`ST`/`ATOM32`/`ATOM64` all 11 `atomop`s / `IDF` / `MEMBARRIER` / cache & persistence), `ISA_SMP` (`12`), `ISA_TAP` (`16`), `ISA_ITR` (`5`), `ISA_EMI` (`7` — `UVSW` / `TESSW` / `ISPFEEDBACK` / `ISPREAD`), `ISA_PSB_WR` (`9`), `ISA_PSB_RD` (`10`). |
| 40-bit / 64-bit Global BDA Addressing | **CLOSED** `[V-BIN]` `[V-ROUNDTRIP]` | 64-bit base register pair `(reg_lo, reg_lo+1)` in `DMA.ADDR_BASE` (`SH` enforces 2-reg even alignment) + dynamic offset (`ADDR_MEMOFFSET`) + `SLOT` index + byte-count burst (`op54`) + flat-address high-word tagging. |
| 128-Wide Subgroup Ops & Silicon Quirks | **CLOSED** `[V-BIN]` | Hardware `SLOT` (`sl0..sl15`, bank 2) cross-lane broadcast register file + `sr14` (`inst_num` `0..127`) lowering for `Ballot`/`Broadcast`/`Shuffle`/`ScanReduce`; active Volcanic BRNs (`71823`, `71237`, `73233`, `74247`, `68994`, `76829`) catalogued; Rogue BRNs `65104`/`69700`/`74812`/`77899` confirmed absent. |

---

## 0. Conventions & Rogue-vs-Volcanic Execution Delta

### 0.1 Encoding Conventions (Aligned with `VOLCANIC-USC-ISA-MAP.md`)
- Instructions are **1–4 little-endian `u32` dwords** (`W0..W3`). **Bit 31 (`0x80000000`)** of each dword is **EOI** (`0` = more words follow, `1` = final word).
- Trailing words equal to the family's **default word** (masked to bits `[30:0]`) are elided; minimum length is 1 dword (`W0 | 0x80000000`).
- **Bit notation:** `wN[hi:lo]` or `wN[b]` denotes bits in dword `N` (LSB first when listing bit maps `b0->..., b1->...`). Multi-word fields are listed MSB-to-LSB in `bits=...` unless explicitly expanded as `b0->...`.
- **Canonical Register Banks (`0..15`)** (identical to `VOLCANIC-USC-ISA-MAP.md`):
  - `0: NONE`, `1: INTERNAL (i0..7)`, `2: SLOT (sl0..15)`, `3: POUT (pout0..31)`, `4: TEMP (r0..255)`, `5: VI (vi0..127)`, `6: CF (cf0..131, /3 in ITR/MOV)`, `7: SH (sh0..511)`, `8: SC (sc0..127)`, `9: CS (cs0..1023)`, `10: SR (sr0..63)`, `11: EMC`, `12: IMM`, `13: BIT_PRED_DST (p0)`, `14: DMA_GLOBAL`, `15: INDEXED`.
- **Operand Encoding Formula:** Each operand slot encodes a single packed integer `V = bank_offset + reg_index`, split into `hi_bits` (top bits) and `reg_bits` (`b0..bK`). Each table lists exact `hi_bits` and `lo` (`per_bank: bank:hi=...,lo=...`) so an encoder computes `packed = (hi << (K+1)) | (lo + reg_index)` and scatters bits via `reg_bits` and `hi_bits`.
- **Universal Header Fields (present in every execution family):**
  - `CC` (`+0x04`, 2 bits): `0 = ALWAYS`, `1 = EXEC_MASK`, `2 = !p0`, `3 = p0`.
  - `SF` (`+0x08`, 2 bits; `+0x0c` secondary flag): `0 = SF0`, `1 = SF1`, `2 = SF_NONE` (encodes as `0x0` in raw bits).
  - `fence_set` (`+0x10`, 3 bits, `0..7`): Inline scoreboard fence slot written on completion (replaces Rogue `DRC0`/`DRC1`).
  - `fence_wait` (`+0x14`, 2 bits, `0..3`): Inline scoreboard wait mask (replaces Rogue `WDF`).

### 0.2 Rogue vs. Volcanic Execution Architecture Delta

| Dimension | Mesa Rogue (`pco_isa.py` / `pco_ops.py`) | PowerVR Volcanic (`FreeUSC`) |
|---|---|---|
| Instruction framing | Byte-aligned groups (`hdr` + `p0`/`p1`/`p2` + `S0..S5` + `ISS` + `W0..W1`) | 1–4 × 32-bit LE dwords per single-family instruction, bit 31 EOI, default-word elision |
| Main ALU pipelines | Split `p0` (`FMUL`/`FADD`/`FMAD`/transcendentals), `p1`, `p2_pck`/`p2_tst`/`p2_mov`, `IADD64`/`IMADD64` | Dedicated families: `ISA_FOP` (`0`), `ISA_AF32` (`13`), `ISA_INT` (`2`), `ISA_DOT8` (`1`), `ISA_MOV` (`4`) |
| Transcendentals | `p0` (`FRCP`/`FRSQ`/`FLOG`/`FEXP`), software-lowered `FSIN`/`FCOS` | Dedicated `ISA_AF32` (`0x1a`) with native `FRCP`/`FRSQ`/`FLOG`/`FEXP`/`FSIN`/`FCOS`/`FATAN` |
| Bitwise unit | 5-phase `BitwiseInstGrouper` (`bm`, `sft1`, `lop`, `sft2`, `tst`) | Unified `ISA_BIT` (`0x38` 16b-LUT / `0x06` 32b-LUT) with arbitrary truth-table LUT + shift + count + dual test |
| Pack / Unpack | Main ALU `p2_pck` (`PCK`) / `UNPCK` | `ISA_MOV` (`0x0a`) with 10-bit `conv_fmt` (`0..95`) + `ISA_FOP` (`f16` maskable) + `ISA_INT` (`f16<->u8`) |
| Memory / Atomics | Backend `LD`/`ST`/`ATOMIC` with `drc0`/`drc1` + `WDF` | `ISA_DMA` (`0x0e`) with 40/64-bit BDA pairs, 11 `atomop`s (`ATOM32`/`ATOM64`), inline `fence_set`/`fence_wait` |
| Pixel Output | Write `sr32..35`/`sr164..167` + `EMITPIX` | Dedicated `ISA_PSB_WR` (`0x2a`, with hardware blend unit) & `ISA_PSB_RD` (`0x2c`) on `POUT` bank 3 (`pout0..31`) |
| Vertex Output & ISP | Separate `UVSW` / `ATST` / `DEPTHF` backend ops | Unified `ISA_EMI` (`0x3a`) covering `UVSW`/`TESSW` (`op 14..20`), `ISPFEEDBACK`/`ATST`/`DEPTHF` (`0..11`), `ISPREAD` (`12..13`) |
| Subgroups | 32-wide (`sr51` `inst_num`), common-store / cross-lane ops | **128-wide** (`sr14` `inst_num 0..127`), **16 hardware `SLOT` (`sl0..sl15`, bank 2)** cross-lane broadcast registers |

---

## 1. Main ALU Execution Families (`[GAP-ISA-2]`)

### 1.1 `ISA_FOP` — Floating-Point ALU (F32 & Packed F16x2) `[V-BIN]` `[V-ROUNDTRIP]`

- **Family ID:** `0` | **Prefix:** `W0[0] = 1` (occupies all 64 odd `W0[6:0]` codes) | **Length:** `1–3` dwords
- **Default Words (for elision):** `W1 = 0x00400808`, `W2 = 0x00000400`
- **Base All-Zero-Init 3-Dword Form:** `W0=0x00800001, W1=0x00000000, W2=0x80000000` (with `dst0=TEMP0, dst1=INT0, src0..2=SH0`)

#### Header & Control Fields
| Field | Struct Off | Bit Positions | Encoded Values & Semantics | Prov |
|---|---|---|---|---|
| `CC` | `+0x04` | `w1[27], w2[10]` | `0->0x0 (ALWAYS)`, `1->0x2 (EXEC)`, `2->0x1 (!p0)`, `3->0x3 (p0)` | `[V-BIN]` |
| `SF` | `+0x08` | `w0[23], w1[9]` | `0->0x1`, `1->0x2`, `2->0x0 (SF_NONE)` (`+0x0c` toggles `w1[9]`) | `[V-BIN]` |
| `fence_set` | `+0x10` | `b0->w2[1], b1->w2[4], b2->w2[5]` | `0..7` | `[V-BIN]` |
| `fence_wait` | `+0x14` | `b0->w1[23], b1->w1[24]` | `0..3` | `[V-BIN]` |
| **`op`** | **`+0x64`** | **`w0[7], w0[13], w0[17], w1[30]`** | **12 opcodes `0..11`** (see table below; note non-linear bit mapping!) | `[V-BIN]` |
| `tst_type` | `+0x68` | `w1[12], w1[17:16]` | `0..7`: `0->0x0, 1->0x2, 2->0x1, 3->0x3, 4->0x4, 5->0x6, 6->0x5, 7->0x7` | `[V-BIN]` |
| `cmp_mode` | `+0x6c` | `w1[21], w1[29]` | `0..3`: `0->0x0, 1->0x1, 2->0x2, 3->0x3` | `[V-BIN]` |
| `dst0_fmt` | `+0x28` | `w0[3], w2[8]` | `0->0x0 (F16_LO)`, `1->0x2 (F16_HI)`, `2->0x1 (F32)`, `3->0x3 (F16X2)` | `[V-BIN]` |
| `sat` (`SAT01`) | `+0x2c` | `w2[11]` | `0` = off, `1` = clamp result to `[0.0, 1.0]` | `[V-BIN]` |
| `rtz` (round) | `+0x2d` | `w2[22]` | `0` = RTNE (default), `1` = RTZ / alternate round flag | `[V-BIN]` |
| `dst1_fmt` / `pck` | `+0x30` | `w2[9], w2[12]` | `0..3` (`0->0x0, 1->0x1, 2->0x2, 3->0x3`) | `[V-BIN]` |
| `src0_fmt` | `+0x3c` | `w0[9], w2[17]` | `0->0x0 (F16_LO)`, `1->0x2 (F16_HI)`, `2->0x1 (F32)`, `3->0x3 (F16X2)` | `[V-BIN]` |
| `src0_mod` | `+0x40/+0x41/+0x42` | `w1[28]`, `w1[15]`, `w2[14]` | 3 source modifier bits (`neg`, `abs`, `flr`) | `[V-BIN]` |
| `src1_fmt` | `+0x4c` | `w0[10], w2[16]` | `0->0x0 (F16_LO)`, `1->0x2 (F16_HI)`, `2->0x1 (F32)`, `3->0x3 (F16X2)` | `[V-BIN]` |
| `src1_mod` | `+0x50` (bytes) | `w2[20:13]` | `src1` modifier flags (packed byte vector) | `[V-BIN]` |
| `src2_fmt` | `+0x5c` | `w0[26], w2[15]` | `0->0x0 (F16_LO)`, `1->0x2 (F16_HI)`, `2->0x1 (F32)`, `3->0x3 (F16X2)` | `[V-BIN]` |
| `src2_mod` | `+0x60/+0x61/+0x62` | `w2[6]`, `w1[20]`, `w1[25]` | 3 source modifier bits (`neg`, `abs`, `flr`) | `[V-BIN]` |

#### `ISA_FOP` Opcode Table (`+0x64`, raw 4-bit value across `w0[7], w0[13], w0[17], w1[30]`)
| Canonical `op` | Raw Bits `(w0[7],w0[13],w0[17],w1[30])` | Clean-Room Mnemonic | Semantics & Operands | `rgx.sh` Count | Prov |
|---:|---:|---|---|---:|---|
| `0` | `0x0` (`0000`) | `fmad` / `fma` | `dst0 = src0 * src1 + src2` (F32 or F16x2) | `356` | `[V-ROUNDTRIP]` |
| `1` | `0x1` (`0001`) | `fmul` | `dst0 = src0 * src1` (also `IR 0x0a`/`0x20`) | `582` | `[V-ROUNDTRIP]` |
| `2` | `0x2` (`0010`) | `fadd` | `dst0 = src0 + src1` (also `IR 0x1e`; sub via `src1.neg`) | `880` | `[V-ROUNDTRIP]` |
| `3` | `0x3` (`0011`) | `fdsx_fine` / `fop3` | Inline FOP from `IR 0x4c` (`DSX_FINE` / coarse-fine derivative) | `0` | `[V-BIN]` `[OPEN]` |
| `4` | `0x8` (`1000`) | `fdsy_fine` / `fop4` | Inline FOP from `IR 0x4d` (`DSY_FINE`) | `0` | `[V-BIN]` `[OPEN]` |
| `5` | `0x9` (`1001`) | `fop5` | Inline FOP from `IR 0x4e` | `0` | `[V-BIN]` `[OPEN]` |
| `6` | `0xa` (`1010`) | `fop6` | Inline FOP from `IR 0x4f` | `0` | `[V-BIN]` `[OPEN]` |
| `7` | `0xb` (`1011`) | `fmovc_pred` / `fmin_max` | Predicated select (`ConvertMOVC_PREDToUscasm`) & `FOP_OP=6` (`fmin`/`fmax`/`fsel`) | `184` | `[V-ROUNDTRIP]` |
| `8` | `0x4` (`0100`) | `fmov` / `fbyp` / `fsat01` / `fpck_f16` | `dst0 = mod(src0)` (also `MOVPRED`, `FSAT01` with `sat=1`, maskable F16 pack, and `SLOT` cross-lane writes!) | `6,803` | `[V-ROUNDTRIP]` |
| `9` | `0x5` (`0101`) | `fcomp` / `ftst` | Floating-point compare writing predicate `p0` (`ConvertFCOMPToUseasm`, `tst_type` from `0x313e8=[4,5,1,7,6,0,2,3]`) | `168` | `[V-ROUNDTRIP]` |
| `10` | `0x6` (`0110`) | `fmovc` / `fmin` / `fmax` | Conditional move / min / max using `tst_type` & `cmp_mode` (`0x2d5b0=[2,3,0,1]`) | `352` | `[V-ROUNDTRIP]` |
| `11` | `0x7` (`0111`) | `ffrc` / `fround` | Fractional / floor / round (`IR 0x22`, `FOP_OP=5`) | `48` | `[V-ROUNDTRIP]` |

- **Compare `tst_type` (`+0x68`) Mapping (`ConvertFCOMPToUseasm` table `0x313e8`):** Maps standard float compare tests (`EQ`, `GT`, `GTE`, `LT`, `LTE`, `NEQ`, `ORD`, `UNORD` / zero-tests) via `[4, 5, 1, 7, 6, 0, 2, 3]` (`4 = true/EQ`, `5 = NEQ`, `1 = LT`, `7 = LTE`, `6 = GT`, `0 = GTE/false`, `2/3 = ordered/unordered`; verify exact polarity of `2`/`3` via bitdiff `[OPEN]`).

#### `ISA_FOP` Operand Bitmaps (`[V-BIN]`)
| Slot | Width | Bit Layout (`reg_bits` = `b0..bK`; `hi_bits`) | Bank Offsets (`bank: (hi, lo)`) |
|---|---:|---|---|
| `dst0` | 9b | `b0->w0[4], b1->w0[16], b2->w0[24], b3->w1[1], b4->w1[10], b5->w1[13], b6->w0[18], b7->w0[8]`; `hi=w0[5]` | `4(TEMP):(0,0x00)`, `5(VI):(1,0x00)`, `2(SLOT):(1,0x80)`, `0(NONE):(1,0xa0)`, `1(INT):(1,0xc0)` |
| `dst1` | 4b | `reg=w2[3],w2[20:19]` (`0..7`); `none=w1[22]` (`1=NONE, 0=INT`) | `1(INT 0..7): w1[22]=0`, `0(NONE): w1[22]=1` |
| `src0` | 11b | `b0->w0[6], b1->w0[12], b2->w0[20], b3->w0[19], b4->w0[29], b5->w0[28], b6->w1[14], b7->w2[0], b8->w0[30]`; `hi=w0[11],w0[15]` | `7(SH):(0,0x000)`, `5(VI):(1,0x000)`, `2(SLOT):(1,0x100)`, `4(TEMP):(2,0x000)`, `1(INT):(2,0x100)`, `8(SC):(3,0x000)` |
| `src1` | 11b | `b0->w0[22], b1->w1[0], b2->w1[6], b3->w1[8], b4->w1[18], b5->w1[26], b6->w2[7], b7->w2[18], b8->w1[2]`; `hi=w0[1],w1[11]` | Same as `src0` |
| `src2` | 11b | `b0->w0[14], b1->w0[21], b2->w0[25], b3->w0[27], b4->w1[5], b5->w1[7], b6->w1[19], b7->w2[2], b8->w1[4]`; `hi=w0[2],w1[3]` | Same as `src0` |

---

### 1.2 `ISA_AF32` — Floating-Point Transcendental Unit `[V-BIN]`

- **Family ID:** `13` | **Prefix:** `W0[5:0] = 0x1a` | **Length:** `1–2` dwords
- **Default Word:** `W1 = 0x00000200` | **Base Form:** `W0=0x0000001a, W1=0x80000000`

| Field | Struct Off | Bit Positions | Encoded Values & Semantics | Prov |
|---|---|---|---|---|
| `CC` | `+0x04` | `w1[5], w1[9]` | `0->0x0, 1->0x2, 2->0x1, 3->0x3` | `[V-BIN]` |
| `SF` | `+0x08` | `w0[14], w1[11]` | `0->0x2, 1->0x1, 2->0x0 (NONE)` | `[V-BIN]` |
| `fence_set` | `+0x10` | `b0->w0[21], b1->w0[24], b2->w0[28]` | `0..7` | `[V-BIN]` |
| `fence_wait` | `+0x14` | `b0->w0[10], b1->w0[15]` | `0..3` | `[V-BIN]` |
| `dst_fmt` | `+0x20` | `w0[18], w1[12]` | `0..3` (`0=F16_LO, 1=F16_HI, 2=F32, 3=F16X2`) | `[V-BIN]` |
| `src_fmt` | `+0x2c` | `w0[6]` | `0 = F32`, `1 = F16` | `[V-BIN]` |
| `src_elem` | `+0x30` | `w1[10]` | `0 = low F16`, `1 = high F16` (must be `0` when `src_fmt=0`) | `[V-BIN]` |
| `src_abs` | `+0x34` | `w1[3]` | `0 = off`, `1 = abs(src)` | `[V-BIN]` |
| `src_neg` | `+0x35` | `w1[8]` | `0 = off`, `1 = -src` (see **BRN73233** for `FSIN`/`FCOS`!) | `[V-BIN]` |
| **`op`** | **`+0x38`** | **`w0[11], w0[17], w0[25], w1[0]`** | **13 opcodes `0..12`** (table below) | `[V-BIN]` |

#### `ISA_AF32` Opcode Table (`+0x38`)
| Canonical `op` | Raw Bits `(w0[11],w0[17],w0[25],w1[0])` | Clean-Room Mnemonic | Source IR Op | Prov |
|---:|---:|---|---|---|
| `0` | `0x0` | `frcp` | `IR 64 (0x40)` (`RECIP`) | `[V-BIN]` |
| `1` | `0x2` | `frsq` | `IR 65 (0x41)` (`RSQRT`) | `[V-BIN]` |
| `2` | `0x4` | `af32_op2` (`fsqrt`/`fexp_neg`?) | `IR 69 (0x45)` | `[V-BIN]` `[OPEN]` |
| `3` | `0x6` | `flog` (`log2`) | `IR 66 (0x42)` (`LOG`) | `[V-BIN]` |
| `4` | `0x8` | `af32_op4` (`flog_neg`?) | `IR 67 (0x43)` | `[V-BIN]` `[OPEN]` |
| `5` | `0xa` | `fexp` (`exp2`) | `IR 68 (0x44)` (`EXP`) | `[V-BIN]` |
| `6` | `0xc` | `af32_op6` | `IR 70 (0x46)` | `[V-BIN]` `[OPEN]` |
| `7` | `0xe` | `af32_op7` | `IR 71 (0x47)` | `[V-BIN]` `[OPEN]` |
| `8` | `0x1` | `fsin` (fast / reduced range) | `IR 72 (0x48)` (`SIN`) | `[V-BIN]` |
| `9` | `0x3` | `fsin_hp` (high precision) | `IR 72 (0x48)` (`SIN`, high-prec) | `[V-BIN]` |
| `10` | `0x5` | `fcos` (fast / reduced range) | `IR 73 (0x49)` (`COS`) | `[V-BIN]` |
| `11` | `0x7` | `fcos_hp` (high precision) | `IR 73 (0x49)` (`COS`, high-prec) | `[V-BIN]` |
| `12` | `0x9` | `fatan` (`atan` core) | `IR 251 (0xfb)` (`ConvertAtanF32HWSupport`) | `[V-BIN]` |

#### `ISA_AF32` Operand Bitmaps (`[V-BIN]`)
- `dst` (9b): `b0->w0[7], b1->w0[8], b2->w0[16], b3->w0[20], b4->w0[30], b5->w1[2], b6->w1[6], b7->w0[26]`; `hi=w0[23]`. Banks: `4(TEMP):(0,0x00)`, `5(VI):(1,0x00)`, `2(SLOT):(1,0x80)`.
- `src` (9b): `b0->w0[9], b1->w0[13], b2->w0[19], b3->w0[22], b4->w1[1], b5->w1[4], b6->w1[7], b7->w0[29]`; `hi=w0[27]`. Banks: `4(TEMP):(0,0x00)`, `5(VI):(1,0x00)`, `2(SLOT):(1,0x80)`.

---

### 1.3 `ISA_INT` — Integer ALU (8/16/32/64-bit, Compare, Min/Max, Select) `[V-BIN]` `[V-ROUNDTRIP]`

- **Family ID:** `2` | **Prefix:** `W0[3:0] = 0x0` (8 codes `0x00, 0x10, ..., 0x70`) | **Length:** `1–4` dwords
- **Default Words:** `W1 = 0x01902008`, `W2 = 0x040c0a98`, `W3 = 0x00000000`
- **Base Form:** `W0=0x00000000, W1=0x00002000, W2=0x00000000, W3=0x80000000`

#### Header & Control Fields
| Field | Struct Off | Bit Positions | Encoded Values & Semantics | Prov |
|---|---|---|---|---|
| `CC` | `+0x04` | `w2[1], w2[26]` | `0->0x0, 1->0x2, 2->0x1, 3->0x3` | `[V-BIN]` |
| `SF` | `+0x08` | `w0[18], w1[13]` | `0->0x2, 1->0x1, 2->0x0 (NONE)` (`+0x0c`: `w0[18]`) | `[V-BIN]` |
| `fence_set` | `+0x10` | `b0->w2[22], b1->w2[23], b2->w2[30]` | `0..7` | `[V-BIN]` |
| `fence_wait` | `+0x14` | `b0->w0[29], b1->w1[7]` | `0..3` | `[V-BIN]` |
| **`op`** | **`+0x84`** | **`w0[8], w0[25], w1[26], w1[30]`** | **12 opcodes `0..11`** (direct 4-bit identity `0->0x0 .. 11->0xb`; see table below) | `[V-BIN]` |
| `movc` | `+0x88` | `w3[3], w3[18:17]` | `0..4`: `0->0x0 (NONE)`, `1->0x4`, `2->0x1`, `3->0x5`, `4->0x2` (cannot pair `movc!=0` with `dst0!=NONE`) | `[V-BIN]` |
| `alu_mode` | `+0x90` | `w0[12], w0[21]` | `0->0x0 (ALU)`, `1->0x2 (CMP/TST)`, `2->0x1 (MIN/MAX)`, `3->0x3 (SPECIAL/TIMER)` | `[V-BIN]` |
| `res_fmt` | `+0x70` | `w0[13], w1[3], w3[21]` | `0..5`: `0=U8/S8`, `1=U16/S16`, `2=U32/S32`, `3=U64/S64` (writes `dst0,dst0+1`), `4=F16_TO_U8`, `5=UNORM8_TO_F16` | `[V-BIN]` |
| `sat` | `+0x78` | `w3[13]` | `0 = wrap`, `1 = saturate` | `[V-BIN]` |
| `carry_sel` | `+0x7c` | `w2[15]` | `0 = src1/default`, `1 = src3` (64-bit carry/borrow path) | `[V-BIN]` |
| `sub_mode` | `+0x80` | `w3[11], w3[16]` | `0..3` (carry-in / rounding / shift-out submode) | `[V-BIN]` |
| `src0_fmt`/`elem`/`neg`/`abs` | `+0x30/+0x34/+0x38/+0x39` | `w0[10],w2[9:8]` / `w3[1],w3[7]` / `w2[29]` / `w2[21]` | Format `0..7`: `0=U8, 1=S8, 2=U16, 3=S16, 4=U32, 5=S32, 6=U64, 7=S64` (`g_asFormat`); elem `0..3` | `[V-BIN]` |
| `src1_fmt`/`elem`/`neg`/`abs` | `+0x44/+0x48/+0x4c/+0x4d` | `w0[14],w0[23],w2[13]` (`0,2,4,6,1,3,5,7`) / `w3[6],w3[10]` / `w3[4]` / `w2[20]` | Format `0..7` (same `g_asFormat` enum); elem `0..3` | `[V-BIN]` |
| `src2_fmt`/`elem`/`neg`/`abs` | `+0x58/+0x5c/+0x60/+0x61` | `w1[0],w2[0],w2[27]` (`0,2,4,6,1,3,5,7`) / `w3[12],w3[15]` / `w3[9]` / `w3[2]` | Format `0..5` (`U8..S32`); elem `0..3` | `[V-BIN]` |
| `src3_fmt` | `+0x6c` | `w2[17:16], w3[8]` (`0,1,4,5,2,3`) | Format `0..5` (must match `src1_fmt`; `neg`/`abs` must match or be 0) | `[V-BIN]` |

#### `ISA_INT` Opcode & Test Table (`+0x84` = `0..11`)
| `op (+0x84)` | Primary Role (`alu_mode=0`) | Compare/MinMax Role (`alu_mode=1/2`, `0x2f288`/`0x2f228`) | `rgx.sh` Count | Prov |
|---:|---|---|---:|---|
| `0` | `imov` / `ibyp` / `iabs` (`g_asOpcode[0]`, also `F16_TO_U8`, `UNORM8_TO_F16`) | — | `884` | `[V-ROUNDTRIP]` |
| `1` | `iadd` / `iadd32` / `iadd64` (`g_asOpcode[1]`, `sub` via `src1.neg`) | — | `20` | `[V-ROUNDTRIP]` |
| `2` | `imul` / `umul` (32×32->32/64) | — | `0` | `[V-BIN]` |
| `3` | `imad` / `umad` (`g_asOpcode[3]`: `src0 * src1 + src2`) | `LT` (signed/unsigned by `src_fmt`) | `0` | `[V-BIN]` |
| `4` | `imad` / `cmp_eq` | **`EQ`** (`0x2f288[0]`) / **`MAX`** (`0x2f228[6..11]`, `alu_mode=2`) | `708` | `[V-ROUNDTRIP]` |
| `5` | `imad_alt` (`g_asOpcode[2]`: `src0 * src1 + src2`) | **`NEQ`** (`0x2f288[1]`) | `40` | `[V-ROUNDTRIP]` |
| `6` | `icmp_ge` | **`GTE`** (`0x2f288[5]`) | `0` | `[V-BIN]` |
| `7` | `imad64` / `umad64` (`g_asOpcode[4]`: 3-src 64-bit) | **`GT`** (`0x2f288[3]`) / **`MIN`** (`0x2f228[0..5]`, `alu_mode=2`) | `2,404` | `[V-ROUNDTRIP]` |
| `8` | `icmp_le` | **`LTE`** (`0x2f288[4]`) | `0` | `[V-BIN]` |
| `9` | `int_op9` | — | `0` | `[V-BIN]` `[OPEN]` |
| `10` | `int_op10` | — | `0` | `[V-BIN]` `[OPEN]` |
| `11` | `iadd` / `mov` / `read_timer` (`ConvertREAD_TIMERToUscasm`: `op=11, res_fmt=3, alu_mode=2`) | Constant/register pass & 64-bit `sr46/sr47` timer read | `745` | `[V-ROUNDTRIP]` |

- **64-Bit Integer Rule (`res_fmt = 3`):** `dst0` writes the low 32-bit word (`rN`) and hardware implicitly writes the high 32-bit word to `rN+1` (`psD1->uNumber == psD0->uNumber + 1`, same bank `TEMP`/`VI`).
- **Integer Compare to Predicate:** Set `alu_mode = 1`, `dst0 = NONE`, and `dst1 = bank 13 (BIT_PRED_DST, p0)` with `op` from `{3:LT, 4:EQ, 5:NEQ, 6:GTE, 7:GT, 8:LTE}`.

#### `ISA_INT` Operand Bitmaps (`[V-BIN]`)
| Slot | Width | Bit Layout (`reg_bits` = `b0..bK`; `hi_bits`) | Bank Offsets (`bank: (hi, lo)`) |
|---|---:|---|---|
| `dst0` | 10b | `b0->w0[19], b1->w1[9], b2->w1[21], b3->w1[27], b4->w2[6], b5->w1[14], b6->w1[11], b7->w0[9], b8->w0[4]`; `hi=w2[3]` | `7(SH):(0,0x000)`, `4(TEMP):(1,0x000)`, `5(VI):(1,0x100)`, `2(SLOT):(1,0x180)`, `0(NONE):(1,0x1a0)`, `1(INT):(1,0x1c0)` |
| `dst1` | 5b | `b0->w2[28], b1->w3[0], b2->w1[24], b3->w2[4]`; `hi=w2[11]` | `2(SLOT):(0,0x0)`, `1(INT):(1,0x0)`, `13(PRED p0):(1,0x8)`, `0(NONE):(1,0xc)` |
| `src0` | 11b | `b0->w0[6], b1->w0[16], b2->w0[17], b3->w1[4], b4->w1[16], b5->w0[27], b6->w2[14], b7->w2[24], b8->w0[24]`; `hi=w0[7],w0[28]` | `7(SH):(0,0x000)`, `5(VI):(1,0x000)`, `10(SR):(1,0x100)`, `4(TEMP):(2,0x000)`, `2(SLOT):(2,0x100)`, `8(SC):(3,0x000)`, `1(INT):(3,0x100)` |
| `src1` | 11b | `b0->w0[15], b1->w1[6], b2->w1[5], b3->w1[8], b4->w1[15], b5->w1[22], b6->w2[2], b7->w2[12], b8->w0[30]`; `hi=w0[5],w0[20]` | Same as `src0` |
| `src2` | 9b | `b0->w1[10], b1->w1[29], b2->w1[25], b3->w2[10], b4->w2[5], b5->w0[22], b6->w1[19]`; `hi=w1[2],w1[23]` | `4(TEMP 0..127):(0,0x00)`, `5(VI):(1,0x00)`, `8(SC):(2,0x00)`, `10(SR):(3,0x00)`, `2(SLOT):(3,0x40)`, `1(INT):(3,0x60)` |
| `src3` | 5b | `b0->w2[25], b1->w3[5], b2->w3[25], b3->w2[7]`; `hi=w2[19]` | `2(SLOT):(0,0x0)`, `1(INT):(1,0x0)`, `8(SC0):(1,0x8)` |

---

### 1.4 `ISA_DOT8` — Packed 4×8-Bit Integer Dot-Product Accumulate `[V-BIN]`

- **Family ID:** `1` | **Prefix:** `W0[5:0] = 0x18` | **Length:** `1–3` dwords | **Default:** `W1 = 0x04048010`, `W2 = 0`
- **Semantics:** `dst0 = dot4x8(src0, src1) + src2` (optional secondary write to `dst1` internal register). New in Volcanic (not in Rogue).
- **Fields:** `CC` = `w0[10:9]`, `SF` = `w0[23],w0[25]` (`0->2, 1->1, 2->0`), `fence_set` = `w1[30:28]`, `fence_wait` = `w2[1:0]`, **`sign_mode` (`+0x28`) = `w0[8:7]`** (`0..3`: `U8×U8`, `S8×S8`, `S8×U8`, `U8×S8`).
- **Operands:**
  - `dst0` (9b): `reg=w0[18:11]`, `hi=w0[19]`; banks `4(TEMP):(0,0x00), 5(VI):(1,0x00), 2(SLOT):(1,0x80), 0(NONE):(1,0xa0), 1(INT):(1,0xc0)`.
  - `dst1` (4b): `reg=w0[22:20]`, `none=w0[6]` (`1=NONE, 0=INT`).
  - `src0` (11b): `reg=w1[3:0]:w0[30:26]`, `hi=w1[5:4]`; `src1` (11b): `reg=w1[14:6]`, `hi=w1[16:15]`; `src2` (11b): `reg=w1[25:17]`, `hi=w1[27:26]`. All three share `FOP.src` bank offsets (`SH/VI/SLOT/TEMP/INT/SC`).

---

### 1.5 `ISA_MOV` — Register Move, Pack/Unpack, Burst `LDMOV`/`STMOV`, Dynamic Indexing & `EMC` `[V-BIN]` `[V-ROUNDTRIP]`

- **Family ID:** `4` | **Prefix:** `W0[5:0] = 0x0a` | **Length:** `1–4` dwords
- **Default Words:** `W1 = 0x00000800`, `W2 = 0x00400030`, `W3 = 0x00000000`
- **Base Form:** `W0=0x4003c04a, W1=0x20000020, W2=0x00000000, W3=0x80000000`

#### Header & Control Fields
| Field | Struct Off | Bit Positions | Semantics | Prov |
|---|---|---|---|---|
| `CC` | `+0x04` | `w2[1], w2[4]` | `0->0x0, 1->0x2, 2->0x1, 3->0x3` | `[V-BIN]` |
| `SF` | `+0x08` | `w0[11], w0[19]` | `0->0x2, 1->0x1, 2->0x0 (NONE)` | `[V-BIN]` |
| `fence_set` | `+0x10` | `b0->w1[3], b1->w1[9], b2->w1[13]` | `0..7` | `[V-BIN]` |
| `fence_wait` | `+0x14` | `b0->w0[20], b1->w0[29]` | `0..3` | `[V-BIN]` |
| `elem_size` / `idx_type` | `+0x28` | `w2[2], w2[6]` | `0->0x0, 1->0x2, 2->0x1` (`LDMOV`: `1`; `MOVHW` coeff elem `0..2`) | `[V-BIN]` |
| `mov_mode` | `+0x2c` | `w0[12], w1[10]` | `0 = normal/PCK/MOVEMC`, `1 = LDMOV (local/reg load)`, `2 = STMOV (local/reg store)` | `[V-BIN]` |
| `pck_dir` / `flag3c` | `+0x3c` | `w3[0]` | `0` = default, `1` = alternate conversion/mask flag | `[V-BIN]` |
| `tst_op` | `+0x58` | `w2[14:13], w3[2:1]` | `0..11` (direct `0->0x0..11->0xb`; `0` for all `614` MOV insts in `rgx.sh`) | `[V-ROUNDTRIP]` |

#### `ISA_MOV` Operand Bitmaps (`[V-BIN]`)
| Slot | Width | Bit Layout (`reg_bits` = `b0..bK`; `hi_bits`) | Bank Offsets (`bank: (hi, lo)`) |
|---|---:|---|---|
| `dst` | 12b | `b0->w0[18], b1->w0[22], b2->w0[21], b3->w0[25], b4->w1[7], b5->w1[18], b6->w1[26], b7->w1[28], b8->w2[0], b9->w1[19]`; `hi=w0[7],w0[13]` | `9(CS 0..1023):(0,0x000)`, `4(TEMP):(1,0x000)`, `5(VI):(1,0x200)`, `7(SH):(2,0x000)`, `11(EMC):(2,0x200)`, `6(CF /3):(3,0x000)` |
| `src` | 11b | `b0->w0[9], b1->w0[10], b2->w0[23], b3->w0[24], b4->w1[15], b5->w1[24], b6->w1[27], b7->w1[29], b8->w0[14], b9->w1[5]`; `hi=w0[30]` | `9(CS 0..1023):(0,0x000)`, `4(TEMP):(1,0x000)`, `5(VI):(1,0x100)`, `11(EMC):(1,0x180)`, `6(CF /3):(1,0x200)`, `2(SLOT):(1,0x300)`, `0(NONE):(1,0x380)` |
| `bst_len` | 15b | `b0->w0[27], b1->w0[26], b2->w0[28], b3->w1[0], b4->w1[2], b5->w1[6], b6->w1[8], b7->w1[14], b8->w1[23], b9->w1[1], b10->w1[30], b11->w0[6]`; `hi=w0[15:17]` | `12(IMM 0..4096):(0,0x000)`, `4(TEMP):(7,0x000)`, `0(NONE):(7,0x800)` |
| **`conv_fmt`** | 11b | `b0->w2[23], b1->w2[10], b2->w2[24], b3->w2[25], b4->w2[26], b5->w2[27], b6->w2[11], b7->w2[28], b8->w2[29], b9->w2[30]`; `hi=w2[12]` | `12(IMM 0..1023):(0,0x000)`, `7(SH):(1,0x000)`, `4(TEMP):(1,0x200)`, `5(VI):(1,0x300)`, `2(SLOT):(1,0x380)` |
| `idx_addr` | 10b | `b0->w1[22], b1->w1[4], b2->w1[17], b3->w1[21], b4->w1[12], b5->w2[3], b6->w1[16], b7->w2[8], b8->w2[9]`; `hi=w2[5]` | `7(SH):(0,0x000)`, `12(IMM 0..255):(1,0x000)`, `2(SLOT):(1,0x100)` (`LDMOV` elem size: `4B->0, 2B->1, 1B->2`) |
| `idx_mod` | 10b | `b0->w1[11], b1->w1[25], b2->w1[20], b3->w2[16], b4->w2[17], b5->w2[18], b6->w2[19], b7->w2[20], b8->w2[21]`; `hi=w2[22]` | `7(SH):(0,0x000)`, `12(IMM 0..63):(1,0x000)`, `2(SLOT):(1,0x100)` |

#### Complete Pack / Unpack `conv_fmt` Table (`IMM` values `0..95`, `96` = invalid) `[V-BIN]`
From `EncodeFormatArg` + `g_asPackFormats` + `g_asPackFormatDesc` (`scale=0`: raw/unscaled integer; `scale=1`: normalized `[0,1]`/`[-1,1]`; `rz=1`: round-toward-zero):

| Format ID | Format Name | Pack (`scale=0, rz=0`) | Pack (`scale=1, rz=0`) | Pack (`scale=1, rz=1`) | Unpack (`scale=0, rz=0`) | Unpack (`scale=1, rz=0`) | Unpack (`rz=1`) |
|---:|---|---:|---:|---:|---:|---:|---:|
| `0` | `U8888` | `54` | `53` | `59` | `90` | `90` | `95` |
| `1` | `S8888` | `32` | `31` | `41` | `70` | `70` | `78` |
| `2` | `U1616` | `48` | `47` | `57` | `82` | `82` | `93` |
| `3` | `S1616` | `26` | `25` | `39` | `64` | `64` | `76` |
| `4` | `U32` | `50` | `49` | — | `85` | `84` | — |
| `5` | `S32` | `30` | `29` | — | `67` | `66` | — |
| `6` | `U1010102` | `44` | `43` | `55` | `80` | `80` | `91` |
| `7` | `S1010102` | `22` | `21` | `37` | `62` | `62` | `74` |
| `8` | `F111110` | `13` | `12` | — | `5` | `5` | — |
| `9` | `F16F16` | `15` | `14` | — | `6` | `6` | — |
| `10` | `F32` (identity/bypass) | `0` | `0` | — | `0` | `0` | — |
| `11..14` | `BF16`, `F8E5`, `F8E4`, `F4` | `0` (special path) | `0` | `0` | `0` | `0` | `0` |
| `16` | `D24S8` | `11` | `10` | — | `4` | `3` | — |
| `17` | `S8D24` | `36` | `35` | — | `73` | `72` | — |
| `20` | `U8888_ALT` | `52` | `51` | `58` | `89` | `89` | `94` |

- **Additional `EncodeFormatArg` Direct Codes:** `0x01` (mode 3), `0x02` (mode 4), `0x09` (mode 5), `0x44` (`68`, mode 7/fmt 3), `0x54`/`0x55` (`84`/`85`, mode 6/fmt 10 `F32` with `scale=0/1`).

---

## 2. Bitwise Execution Family (`ISA_BIT`, Family `3`) `[V-BIN]` `[V-ROUNDTRIP]`

Volcanic replaces Rogue's 5-phase `BitwiseInstGrouper` with a **dual-encoding unified bitwise instruction** combining:
1. Pre-shift / rotate / mask generation (`+0x48`, `+0x4c`, `+0x50`, `+0x54`, `+0x5c`),
2. An arbitrary **4-input (16-bit) or 5-input (32-bit, with `p0`) Truth-Table LUT** (`+0x68..+0x6b`),
3. An integrated **Bit-Count Unit** (`cnt_en +0x6c`, `cnt_op +0x70`: `CBS`/`FTB`/`CTZ`/`POPCNT`),
4. Post-shift / BFE / BFI / ROL (`+0x74`), and
5. Dual predicate/zero tests (`+0x58`, `+0x80`).

The encoder automatically selects **`BIT_NO_P0_LUT` (`0x38`)** whenever `p0_lut_en (+0x64) == 0` (16-bit LUT; allows `dst1` and `tst_op1`), and falls back to **`BIT_NO_DST1_TST_OP1` (`0x06`)** when `p0_lut_en == 1` (32-bit LUT, e.g., `CPS`; requires `dst1 = NONE`).

### 2.1 Encoding A — `BIT_NO_P0_LUT` (`W0[5:0] = 0x38`, 1–4 dwords) `[V-BIN]` `[V-ROUNDTRIP]`
- **Default Words:** `W1 = 0x25b60800`, `W2 = 0x1a008240`, `W3 = 0x00000200` (100% of `6,698` BIT insts in `rgx.sh`)
- **Base Form:** `W0=0x00118278, W1=0x20320400, W2=0x18008000, W3=0x80000200`

| Field | Struct Off | Bit Positions | Encoded Values & Semantics | Prov |
|---|---|---|---|---|
| `CC` | `+0x04` | `w1[14], w2[6]` | `0->0x0, 1->0x2, 2->0x1, 3->0x3` | `[V-BIN]` |
| `SF` | `+0x08` | `w0[10], w0[23]` | `0->0x2, 1->0x1, 2->0x0 (NONE)` | `[V-BIN]` |
| `fence_set` | `+0x10` | `b0->w2[7], b1->w2[11], b2->w2[13]` | `0..7` | `[V-BIN]` |
| `fence_wait` | `+0x14` | `b0->w0[11], b1->w0[18]` | `0..3` | `[V-BIN]` |
| `rot` / `sft1_mode` | `+0x48` | `w1[30], w2[2], w3[3], w3[8]` | `0..15` (`bit0<->bit1` swapped: `0,2,1,3,...`) | `[V-BIN]` |
| `mask_mode` | `+0x4c` | `w1[6], w1[28], w2[22], w2[30]` | `0..15` (`0,4,8,c,1,5,9,d,...`; `4` for `BITMASK`) | `[V-BIN]` |
| `mask_en` | `+0x50` | `w1[11]` | `0` = off, `1` = on | `[V-BIN]` |
| `sft2_imm` / `mode` | `+0x54` | `w2[23], w3[4], w3[6], w3[11]` | `0..15` (`0,2,4,6,8,a,c,e,1,3,...`) | `[V-BIN]` |
| `tst_op0` | `+0x58` | `w0[12], w1[27], w3[20]` | `0..7` (`0,4,1,5,2,6,3,7`) | `[V-BIN]` |
| `src_sel` / `mode5c` | `+0x5c` | `w1[18], w2[25]` | `0..3` | `[V-BIN]` |
| `flag60` | `+0x60` | `w2[18]` | `0..1` | `[V-BIN]` |
| `lut16[7:0]` / `[15:8]` | `+0x68` / `+0x69` | `b0->w3[14]`, `b8->w2[24]` (16-bit LUT packed across `w0..w3`) | 16-bit truth table (`p0_lut_en = 0`) | `[V-BIN]` |
| `cnt_en` | `+0x6c` | `w2[26]` | `0 = LUT/shift`, `1 = Bit-Count unit (`CBS`/`FTB`/`CTZ`/`POPCNT`)` | `[V-BIN]` |
| `cnt_op` | `+0x70` | `w2[29], w3[16]` | `0..3` (from `0x2ed88 = [3, 2, 0, 1]` for IR `0x8e..0x91`) | `[V-BIN]` |
| `sh_c` (shift/bfe/rol) | `+0x74` | `w1[13], w2[21], w3[0]` | `0..7` (`0,2,1,3,4,6,5,7`: `2=UBFE, 5=IBFE, 6=ROL`, shifts `LSL/LSR/ASR`) | `[V-BIN]` |
| `flag78` / `flag7c` | `+0x78` / `+0x7c` | `w2[3]` / `w2[5]` | `0..1` | `[V-BIN]` |
| `tst_op1` | `+0x80` | `w3[15], w3[18:17]` | `0..4` (`0..4` identity) | `[V-BIN]` |

#### `BIT_NO_P0_LUT` (`0x38`) Operand Bitmaps (`[V-BIN]`)
| Slot | Width | Bit Layout (`reg_bits` = `b0..bK`; `hi_bits`) | Bank Offsets (`bank: (hi, lo)`) |
|---|---:|---|---|
| `dst0` | 10b | `b0->w0[8], b1->w0[14], b2->w0[19], b3->w1[1], b4->w1[9], b5->w1[10], b6->w1[8], b7->w0[20], b8->w0[16]`; `hi=w0[6]` | `7(SH):(0,0x000)`, `4(TEMP):(1,0x000)`, `5(VI):(1,0x100)`, `2(SLOT):(1,0x180)`, `0(NONE):(1,0x1a0)`, `1(INT):(1,0x1c0)` |
| `dst1` | 5b | `b0->w3[5], b1->w3[12], b2->w2[27], b3->w2[28]`; `hi=w3[9]` | `2(SLOT):(0,0x0)`, `1(INT):(1,0x0)`, `13(PRED p0):(1,0x8)`, `0(NONE):(1,0xc)` |
| `src0` | 11b | `b0->w0[13], b1->w0[17], b2->w1[2], b3->w0[29], b4->w1[3], b5->w1[7], b6->w1[16], b7->w3[2], b8->w0[7]`; `hi=w0[9],w1[20]` | `7(SH):(0,0x000)`, `5(VI):(1,0x000)`, `10(SR):(1,0x100)`, `4(TEMP):(2,0x000)`, `2(SLOT):(2,0x100)`, `8(SC):(3,0x000)`, `1(INT):(3,0x100)` |
| `src1` | 11b | `b0->w0[25], b1->w1[4], b2->w1[12], b3->w1[15], b4->w2[1], b5->w2[10], b6->w3[7], b7->w3[13], b8->w1[5]`; `hi=w0[15],w1[21]` | Same as `src0` |
| `src2` | 10b | `b0->w1[22], b1->w1[19], b2->w1[25], b3->w2[20], b4->w2[16], b5->w2[8], b6->w3[10], b7->w2[0]`; `hi=w1[17],w2[12]` | `4(TEMP):(0,0x00)`, `8(SC):(1,0x00)`, `2(SLOT):(1,0x80)`, `5(VI):(2,0x00)`, `1(INT):(2,0x80)`, `10(SR):(3,0x00)` |
| `src3` | 5b | `b0->w2[19], b1->w3[1], b2->w3[21], b3->w1[29]`; `hi=w2[15]` | `2(SLOT):(0,0x0)`, `1(INT):(1,0x0)`, `8(SC0):(1,0x8)` |

### 2.2 Encoding B — `BIT_NO_DST1_TST_OP1` (`W0[3:0] = 0x06`, 32-bit LUT) `[V-BIN]`
- **Prefix:** `W0[3:0] = 0x6` (8 codes `0x06, 0x16, ..., 0x76`) | **Length:** `1–4` dwords
- **Default Words:** `W1 = 0x78040a61`, `W2 = 0x4060080f`, `W3 = 0x01840301` | **Base (`dst1=NONE, p0_lut_en=1`):** `W0=0x00000086, W1=0x00040060, W2=0x40000000, W3=0x81840301`
- **Header & Control Bits:** `CC=w1[12:11]`, `SF=w0[30],w1[9]` (`0->2,1->1,2->0`), `fence_set=w1[7],w3[27:26]`, `fence_wait=w1[1:0]`, `+0x48=w2[7:4]`, `+0x4c=w0[25],w2[10:8]`, `+0x50=w2[11]`, `+0x54=w2[19:16]`, `+0x58=w0[27:26],w2[20]`, `+0x5c=w2[22:21]`, `+0x60=w0[8]`, `cnt_en (+0x6c)=w1[13]`, `cnt_op (+0x70)=w1[15:14]`, `sh_c (+0x74)=w2[14:12]`, `+0x78=w0[28]`, `+0x7c=w3[25]`.
- **Full 32-Bit Truth-Table LUT (`+0x68..+0x6b`) Bit Map:**
  - `lut[7:0]` (`+0x68`): `b0->w1[19], b1->w1[20], b2->w0[9], b3->w0[10], b4->w1[21], b5->w1[22], b6->w0[11], b7->w0[12]`
  - `lut[15:8]` (`+0x69`): `b0->w1[23], b1->w1[24], b2->w0[13], b3->w0[14], b4->w1[25], b5->w1[26], b6->w0[15], b7->w0[16]`
  - `lut[23:16]` (`+0x6a`): `b0->w0[17], b1->w1[27], b2->w0[18], b3->w1[28], b4->w0[19], b5->w1[29], b6->w0[20], b7->w1[30]`
  - `lut[31:24]` (`+0x6b`): `b0->w0[21], b1->w2[0], b2->w0[22], b3->w2[1], b4->w0[23], b5->w2[2], b6->w0[24], b7->w2[3]`
- **Operands (`0x06`):**
  - `dst0`: `reg(bk=7): b0->w1[4], b1->w1[10], b2->w1[8], b3->w1[16], b4->w1[17], b5->w0[7], b6->w0[6], b7->w1[5], b8->w1[6]`; `hi=w1[18]`
  - `src0`: `reg(bk=7): b0->w1[2], b1..b4->w2[26:23], b5->w1[3], b6..b8->w2[29:27]`; `hi=w2[30],w3[0]`
  - `src1`: `reg(bk=7): b0->w0[4], b1..b4->w3[4:1], b5->w0[5], b6..b8->w3[7:5]`; `hi=w3[9:8]`
  - `src2`: `reg(bk=4): w3[17:10]`; `hi=w3[19:18]`
  - `src3`: `reg(bk=2): w3[23:20]`; `hi=w3[24]`

### 2.3 Bitwise Truth-Table LUT & Operation Recipes `[V-BIN]`
| Operation | Encoding | LUT (`+0x68..+0x6b`) | Auxiliary Control Fields | Prov |
|---|---|---|---|---|
| `and` | `0x38` | `0x0008` (replicated per 4b nibble) | `cnt_en=0` | `[V-BIN]` |
| `xor` | `0x38` | `0x0006` | `cnt_en=0` | `[V-BIN]` |
| `or` | `0x38` | `0x000e` | `cnt_en=0` | `[V-BIN]` |
| `nand` | `0x38` | `0x0007` | `cnt_en=0` | `[V-BIN]` |
| `nor` | `0x38` | `0x0001` | `cnt_en=0` | `[V-BIN]` |
| `xnor` | `0x38` | `0x0009` | `cnt_en=0` | `[V-BIN]` |
| `not` | `0x38` | `0x0005` | `cnt_en=0` | `[V-BIN]` |
| `byp` / `lsl` / `lsr` / `asr` | `0x38` | `0xcccc` (passthrough) | `sh_c (+0x74)` selects shift op (`ConvertBitwiseSHToUseasm`) | `[V-BIN]` |
| `ubfe` / `ibfe` | `0x38` | `0x8888` | `sh_c (+0x74) = 2 (UBFE)` / `5 (IBFE)` (`ConvertBFEToUscasm`) | `[V-BIN]` |
| `bfi` | `0x38` | `0xd8d8` (`(A & M) \| (B & ~M)`) | `ConvertBFIToUscasm` | `[V-BIN]` |
| `bitmask` | `0x38` | `0xaaaa` | `+0x48 = 0`, `+0x4c = 4` (`ConvertBITMASKToUseasm`) | `[V-BIN]` |
| `rol8` / `rol16` | `0x38` | `0xd8d8` | `sh_c (+0x74) = 6 (ROL)`, shift constants `8/24` or `16/16` via `SC` | `[V-BIN]` |
| `cps` (predicated select) | `0x06` | `0xaaaacccc` (`p0 ? A : B`) | `p0_lut_en (+0x64) = 1`, `dst1 = NONE` (`ConvertCPSToUseasm`) | `[V-BIN]` |
| `cbs` / `ftb` / `ctz` / `popcnt` | `0x38` | n/a | `cnt_en (+0x6c) = 1`, `cnt_op (+0x70) = {3, 2, 0, 1}` for IR `0x8e..0x91` (`[OPEN]` exact 1:1 label order) | `[V-BIN]` `[OPEN]` |

---

## 3. Backend Execution Families (`[GAP-ISA-5]` + 40-Bit/64-Bit Global BDA)

### 3.1 `ISA_DMA` — Memory Load, Store, Global Atomics, `IDF`, `MEMBARRIER` `[V-BIN]` `[V-ROUNDTRIP]`

- **Family ID:** `6` | **Prefix:** `W0[5:0] = 0x0e` | **Length:** `1–4` dwords
- **Default Words:** `W1 = 0x00000000`, `W2 = 0x00000000`, `W3 = 0x00000000`
- **Base Form:** `W0=0x0000000e, W1=0x18000000, W2=0x00000000, W3=0x80000000`

#### Header & Control Fields
| Field | Struct Off | Bit Positions | Encoded Values & Semantics | Prov |
|---|---|---|---|---|
| `CC` | `+0x04` | `w0[7:6]` | `0=ALWAYS, 1=EXEC, 2=!p0, 3=p0` | `[V-BIN]` |
| `SF` | `+0x08` | `w0[8], w0[10]` | `0->0x2, 1->0x1, 2->0x0 (NONE)` | `[V-BIN]` |
| `fence_set` | `+0x10` | `w0[13:11]` | `0..7` (replaces Rogue `drc0`/`drc1` tag) | `[V-BIN]` |
| `fence_wait` | `+0x14` | `w0[15:14]` | `0..3` (replaces Rogue `WDF`) | `[V-BIN]` |
| **`op`** | **`+0x18`** | **`w0[19:16]`** | **11 opcodes `0..10`** (table below) | `[V-BIN]` |
| `op_flag` | `+0x1c` | `w0[20]` | `0` = normal, `1` = secondary/sign/burst modifier | `[V-BIN]` |
| **`atomop`** | **`+0x20`** | **`w0[24:21]`** | **11 atomic ops `0..10`** (table below; valid when `op=6` or `7`) | `[V-BIN]` |
| `flag64` / `flag65` | `+0x64` / `+0x65` | `w3[14]` / `w3[15]` | `+0x64` = burst/sign/oob flag (`LD`/`ST` param `+0x18`; `0` for `ATOM`/`IDF`) | `[V-BIN]` |
| `flag70` | `+0x70` | `w3[30]` | 1-bit flag | `[V-BIN]` |
| **`cache_bypass`** | **`+0x74`** | **`w3[26]`** | `0` = cached, `1` = bypass SLC/L1 | `[V-BIN]` |
| **`cache_coherent`** | **`+0x78`** | **`w3[27]`** | `0` = non-coherent, `1` = coherent (MCU/device) | `[V-BIN]` |
| **`persist`** | **`+0x80`** | **`w3[29:28]`** | `0..3` = `LDST_CACHE_PERSIST_PRIORITY_0..3` | `[V-BIN]` |

#### `ISA_DMA` Opcode Table (`+0x18`, `w0[19:16]`)
| `op` | Mnemonic | Semantics | `rgx.sh` Count | Prov |
|---:|---|---|---:|---|
| `0..3` | `membarrier` (4 variants) | Memory barrier / cache flush-invalidate (`ConvertMEMBARRIERToUseasm` selects `0, 1, 2, 3` by scope/invalidate flags; `op=3` is full barrier) | `0` | `[V-BIN]` |
| `4` | `st` | Global/local memory store (`IR 0x5e..0x60`) | `8` | `[V-ROUNDTRIP]` |
| `5` | `ld` | Global/local memory load (`ConvertLDToUseasm`) | `20` | `[V-ROUNDTRIP]` |
| `6` | `atom32` | 32-bit global memory atomic (`IMEMATOM`, `0xe2`) | `0` | `[V-BIN]` |
| `7` | `atom64` | 64-bit global memory atomic (`IMEMATOM64`, `0xe3`) | `0` | `[V-BIN]` |
| `8` | `idf` | Issue Data Fence (wait for in-flight stores/loads; `ConvertIDFToUseasm`) | `0` | `[V-BIN]` |
| `9` | `dma_op9` | Inline DMA (`IR 0x64` / prefetch-cache hint) | `0` | `[V-BIN]` `[OPEN]` |
| `10` | `dma_op10` | Extended DMA (`[OPEN]` label) | `0` | `[V-BIN]` `[OPEN]` |

#### `ISA_DMA` Atomic Operation Table (`+0x20`, `w0[24:21]`) `[V-BIN]`
Verified 100% from `GetAtomicOperation` + `ConvertATOMToUseasm` lookup table `0x3146c = [0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 3]`:

| `atomop` | Hex | Clean-Room Mnemonic | 32-bit (`op=6`) Byte Width (`op54`) | 64-bit (`op=7`) Byte Width (`op54`) | Notes |
|---:|---:|---|---:|---:|---|
| `0` | `0x0` | `atomic_add` | `4` | `8` | Signed/unsigned integer add |
| `1` | `0x1` | `atomic_sub` | `4` | `8` | Integer subtract |
| `2` | `0x2` | `atomic_xchg` | `4` | `8` | Atomic exchange |
| `3` | `0x3` | `atomic_cmpxchg` | `8` (compare+swap pair) | `16` (2×64b pair) | Takes compare + swap regs in consecutive data regs |
| `4` | `0x4` | `atomic_umin` | `4` | `8` | Unsigned minimum |
| `5` | `0x5` | `atomic_imin` | `4` | `8` | Signed minimum |
| `6` | `0x6` | `atomic_umax` | `4` | `8` | Unsigned maximum |
| `7` | `0x7` | `atomic_imax` | `4` | `8` | Signed maximum |
| `8` | `0x8` | `atomic_and` | `4` | `8` | Bitwise AND |
| `9` | `0x9` | `atomic_or` | `4` | `8` | Bitwise OR |
| `10` | `0xa` | `atomic_xor` | `4` | `8` | Bitwise XOR |

- **Cache Mode Matrix (`+0x74 = W3[26]`, `+0x78 = W3[27]`):** `(0,0)` = **Cached** (normal); `(1,0)` = **Bypass**; `(0,1)` = **Coherent**; `(1,1)` = **Bypass + Coherent (Uncached)**.
- **Local / Common-Store Atomics:** Hardware `ISA_DMA` atomics target global memory (`40/64-bit` address). Workgroup shared/local memory (`CS` bank 9) atomics are lowered by the compiler (`EmitRegisterAtomic`, `IR 0x106`) to a `CTRL.MUTEX` lock + `CS` read-modify-write + unlock sequence (and floating-point `FADD`/`FSUB` atomics lower to CAS loops).

#### 40-Bit / 64-Bit Global Addressing (BDA & SSBOs) `[V-BIN]` `[V-ROUNDTRIP]`
- **Address Register Pair (`ADDR_BASE`, `+0x34/+0x38`, `w1[19:10]`):** Holds the base register of a **64-bit little-endian virtual address pair `(reg_lo, reg_lo + 1)`** (Volcanic hardware uses 40-bit physical/device virtual addresses zero-extended in 64-bit pairs).
  - When `ADDR_BASE` uses bank `7 (SH)`, hardware/encoder requires **2-register even alignment** (`(reg_lo & 1) == 0`).
  - `TEMP` (bank 4), `VI` (bank 5), `SLOT` (bank 2), and `DMA_GLOBAL` (bank 14) are also accepted.
- **Dynamic Byte Offset (`ADDR_MEMOFFSET`, `+0x44/+0x48`, `w1[30:29], w2[8:0]`):** Register holding a 32-bit byte offset added to `ADDR_BASE`, or `bank 0 (NONE)` when unindexed.
- **Burst / Width (`op54`, `+0x54/+0x58`, `w2[30:21], w3[1:0]`):** Byte count (`4` = 32-bit, `8` = 64-bit, `16` = 128-bit burst, up to `2048` bytes encoded as `IMM 0`!).
- **Stride / Element Count (`stride_src`, `+0x4c/+0x50`, `w2[20:9]`):** `IMM 1` for 1D contiguous loads/stores/atomics (or stride register/immediate for 2D/strided DMA).
- **Flat-Address Lowering (`ExpandMakeFlatAddress` / `ExpandFlatMemLoadStore`):** Generic pointers tag local-memory pointers in the upper 32-bit word (`0x0000fff3 + 13` / shifted high tag); `ExpandFlatMemLoadStore` tests the high word to predicate global `ISA_DMA` vs local `ISA_MOV` (`CS` bank 9) paths.

#### `ISA_DMA` Operand Bitmaps (`[V-BIN]`)
| Slot | Width | Bit Layout (`reg_bits` = `b0..bK`; `hi_bits`) | Bank Offsets (`bank: (hi, lo)`) |
|---|---:|---|---|
| `data` (`+0x24/+0x28`) | 11b | `b0..b5->w0[30:25], b6..b9->w1[3:0]`; `hi=w1[4]` | `9(CS):(0,0x000)`, `7(SH):(1,0x000)`, `4(TEMP):(1,0x200)`, `5(VI):(1,0x300)`, `0(NONE):(1,0x380)` |
| `index` (`+0x2c/+0x30`) | 5b | `b0..b3->w1[8:5]`; `hi=w1[9]` | `2(SLOT 0..15):(0,0x0)`, `0(NONE):(1,0x0)` |
| **`addr_base`** (`+0x34/+0x38`) | 10b | `b0..b8->w1[18:10]`; `hi=w1[19]` | `7(SH):(0,0x000)`, `4(TEMP):(1,0x000)`, `5(VI):(1,0x100)`, `2(SLOT):(1,0x180)`, `0(NONE):(1,0x1a0)`, `14(DMA_GLOBAL):(1,0x1c0)` |
| `dst` (`+0x3c/+0x40`) | 9b | `b0..b7->w1[27:20]`; `hi=w1[28]` | `4(TEMP):(0,0x00)`, `5(VI):(1,0x00)`, `0(NONE):(1,0x80)` |
| **`addr_off`** (`+0x44/+0x48`) | 11b | `b0..b1->w1[30:29], b2..b9->w2[7:0]`; `hi=w2[8]` | `9(CS):(0,0x000)`, `7(SH):(1,0x000)`, `4(TEMP):(1,0x200)`, `5(VI):(1,0x300)`, `3(POUT):(1,0x380)`, `0(NONE):(1,0x3c0)` |
| `stride_src` (`+0x4c/+0x50`) | 12b | `b0..b10->w2[19:9]`; `hi=w2[20]` | `12(IMM 0..2047):(0,0x000)`, `7(SH):(1,0x000)`, `5(VI):(1,0x200)`, `0(NONE):(1,0x300)`, `4(TEMP):(1,0x400)`, `2(SLOT):(1,0x600)` |
| `op54` (`burst_bytes`) | 12b | `b0..b9->w2[30:21], b10->w3[0]`; `hi=w3[1]` | Same as `stride_src` (`IMM 0` represents `2048` bytes) |
| `op5c` (`y_stride/aux`) | 12b | `b0..b10->w3[12:2]`; `hi=w3[13]` | Same as `stride_src` (`0` for 1D `LD`/`ST`/`ATOM`) |
| `op68` (`aux_state`) | 10b | `b0..b8->w3[24:16]`; `hi=w3[25]` | `7(SH):(0,0x000)`, `2(SLOT):(1,0x000)`, `0(NONE):(1,0x100)` |

---

### 3.2 `ISA_SMP` (Texture Sample / Image Load-Store) & `ISA_TAP` (`GTA`) `[V-BIN]` `[V-ROUNDTRIP]`

#### `ISA_SMP` (`type = 12`, `W0[6:0] = 0x3e`, 1–4 dwords)
- **Default Words:** `W1 = 0x42400008`, `W2 = 0x000c0100`, `W3 = 0x00000000`
- **Base Form:** `W0=0x0300003e, W1=0x00000000, W2=0x00000000, W3=0x80000000`

| Field | Struct Off | Bit Positions | Encoded Values & Semantics | Prov |
|---|---|---|---|---|
| `CC` | `+0x04` | `w1[28], w2[8]` | `0->0x0, 1->0x2, 2->0x1, 3->0x3` | `[V-BIN]` |
| `SF` | `+0x08` | `w0[28], w3[11]` | `0->0x2, 1->0x1, 2->0x0 (NONE)` | `[V-BIN]` |
| `fence_set` | `+0x10` | `b0->w1[5], b1->w1[15], b2->w1[21]` | `0..7` | `[V-BIN]` |
| `fence_wait` | `+0x14` | `b0->w0[12], b1->w0[21]` | `0..3` | `[V-BIN]` |
| **`dmn`** (dimension) | **`+0x58`** | `w0[23], w1[25]` | `0->0x0 (1D)`, `1->0x2 (2D)`, `2->0x1 (3D)`, `3->0x3 (GRAD/CUBE)` (`0x2db30=[3,0,1,2]`; `rgx.sh`: `dmn=1` ×3,076, `dmn=2` ×70) | `[V-ROUNDTRIP]` |
| **`lodm`** (LOD mode) | **`+0x5c`** | `w2[20:18]` | `0..4` (from `0x31440=[3,1,0,2,4]`: `0=normal`, `1=bias`, `2=replace`, `3=grad`, `4=lodm4`; `rgx.sh`: `0`×2020, `1`×464, `2`×44, `3`×618) | `[V-ROUNDTRIP]` |
| **`sbmode`** | **`+0x60`** | `w0[13], w0[29]` | `0..3`: `0=data` (normal), `1=info`, `2=both`, `3=raw/none` | `[V-BIN]` |
| 10 Control Bool Flags | `+0x64..+0x6d` | `w2[14]`, `w1[30]`, `w2[25]`, `w2[12]`, `w2[15]`, `w2[28]`, `w2[1]`, `w1[27]`, `w2[7]`, `w1[3]` | Source `SMP_PARAMS` bytes `(+0x20, +0x04, +0x23, raw_0x8f, +0x21, +0x2c, +0x22, +0x24, +0x50, +0x2d)` covering `proj`, `fcnorm`, `nn`, `pplod`, `tao`, `soo`, `sno`, `wrt`, `array`, `intcoords`/`gather` (`[OPEN]` 1:1 flag permutation) | `[V-BIN]` `[OPEN]` |
| `chan_cnt` / `wrt` | `+0x70` | `w1[23:22]` | `0..2` (`ConvertSMPToUseasm` `sub #1, cmp #3`) | `[V-BIN]` |
| `cache_bypass` / `coherent` | `+0x74` / `+0x78` | `w1[24]` / `w3[8]` | Same 2-bit cache matrix as `ISA_DMA` | `[V-BIN]` |
| `persist` | `+0x80` | `w3[7:6]` | `0..3` (`LDST_CACHE_PERSIST_PRIORITY_0..3`) | `[V-BIN]` |

#### `ISA_SMP` Operand Bitmaps (`[V-BIN]`)
| Slot | Width | Bit Layout (`reg_bits` = `b0..bK`; `hi_bits`) | Bank Offsets (`bank: (hi, lo)`) |
|---|---:|---|---|
| `dst` | 9b | `b0->w1[2], b1->w0[16], b2->w0[18], b3->w1[1], b4->w1[16], b5->w1[29], b6->w2[11], b7->w2[29]`; `hi=w2[9]` | `4(TEMP):(0,0x00)`, `5(VI):(1,0x00)` |
| `img_base` (image state) | 10b | `b0->w0[20], b1->w0[11], b2->w0[7], b3->w0[9], b4->w0[27], b5->w1[7], b6->w1[18], b7->w2[0], b8->w2[5]`; `hi=w2[17]` | `7(SH):(0,0x000)`, `4(TEMP):(1,0x000)`, `5(VI):(1,0x100)`, `2(SLOT):(1,0x180)` |
| `img_off` | 11b | `b0->w3[1], b1->w2[4], b2->w2[23], b3->w2[26], b4->w2[30], b5->w3[3], b6->w3[9], b7->w3[10], b8->w3[12], b9->w3[13]`; `hi=w3[2]` | `12(IMM 0..1023):(0,0x000)`, `4(TEMP):(1,0x000)`, `5(VI):(1,0x200)` |
| `smp_base` (sampler state) | 10b | `b0->w0[19], b1->w0[14], b2->w0[8], b3->w0[10], b4->w0[30], b5->w1[9], b6->w1[19], b7->w2[2], b8->w2[6]`; `hi=w2[21]` | Same as `img_base` |
| `smp_off` | 11b | `b0->w3[5], b1->w2[16], b2->w2[24], b3->w2[27], b4->w3[0], b5->w3[4], b6->w3[14], b7->w3[15], b8->w3[16], b9->w3[17]`; `hi=w3[18]` | Same as `img_off` |
| `lod` | 10b | `b0->w1[10], b1->w1[8], b2->w1[11], b3->w1[6], b4->w1[12], b5->w1[13], b6->w1[20], b7->w2[3], b8->w0[25]`; `hi=w0[24]` | `7(SH):(0,0x000)`, `2(SLOT):(1,0x000)`, `0(NONE):(1,0x100)` |
| `coord` | 9b | `b0->w1[0], b1->w0[22], b2->w0[26], b3->w1[4], b4->w1[17], b5->w1[26], b6->w2[10], b7->w2[22]`; `hi=w2[13]` | `4(TEMP):(0,0x00)`, `5(VI):(1,0x00)` |

#### `ISA_TAP` — GetTexelAddress (`GTA`, Family `16`, `W0[6:0] = 0x3c`, 1–2 dwords) `[V-BIN]`
- **Default Word:** `W1 = 0x00040024` | **Base Form:** `W0=0x0000003c, W1=0x80000000`
- **Header & Flags:** `CC=w1[19:18]`, `SF=w0[13],w1[22]` (`0->2,1->1,2->0`), `fence_set=b0->w0[11],b1->w0[9],b2->w0[20]`, `fence_wait=b0->w0[10],b1->w0[12]`, `oob_pred_en (+0x30)=w1[5]` (writes OOB status to `p0`), `dim_flags (+0x31/+0x32/+0x33)=w0[17], w1[2], w1[21]`.
- **Operands:**
  - `dst` (9b, 64-bit texel address pair): `b0->w0[26], b1->w0[28], b2->w0[23], b3->w0[25], b4->w1[4], b5->w1[14], b6->w1[8], b7->w1[20]`; `hi=w0[14]`; banks `4(TEMP):(0,0x00), 5(VI):(1,0x00)`.
  - `img_base` (10b): `b0->w0[8], b1->w0[7], b2->w0[18], b3->w0[24], b4->w1[1], b5->w1[10], b6->w1[16], b7->w1[9], b8->w1[23]`; `hi=w1[17]`; banks `7(SH):(0,0x000), 4(TEMP):(1,0x000), 5(VI):(1,0x100), 2(SLOT):(1,0x180), 0(NONE):(1,0x1c0)`.
  - `img_off` (11b): `b0->w1[24], b1->w0[30], b2->w1[6], b3->w1[11], b4->w1[12], b5->w1[15], b6->w1[25], b7->w1[26], b8->w1[27], b9->w1[28]`; `hi=w1[29]`; banks `12(IMM):(0,0x000), 4(TEMP):(1,0x000), 0(NONE):(1,0x100), 5(VI):(1,0x200)`.
  - `coord` (9b): `b0->w0[22], b1->w0[21], b2->w0[19], b3->w0[15], b4->w0[27], b5->w1[0], b6->w1[3], b7->w1[13]`; `hi=w0[29]`; banks `4(TEMP):(0,0x00), 5(VI):(1,0x00), 0(NONE):(1,0x80)`.

---

### 3.3 `ISA_ITR` — Fragment Varying Interpolation (`FITR` / `FITRP`) `[V-BIN]` `[V-ROUNDTRIP]`

- **Family ID:** `5` | **Prefix:** `W0[5:0] = 0x0c` | **Length:** `1–3` dwords
- **Default Words:** `W1 = 0x21000080`, `W2 = 0x00002000` (all `629` `ITR` insts in `rgx.sh` elide to **1 dword**!)
- **Base Form:** `W0=0x0000000c, W1=0x21000080, W2=0x80002000`

| Field | Struct Off | Bit Positions | Encoded Values & Semantics | Prov |
|---|---|---|---|---|
| `CC` | `+0x04` | `w0[27], w0[29]` | `0->0x0, 1->0x2, 2->0x1, 3->0x3` | `[V-BIN]` |
| `SF` | `+0x08` | `w0[12], w0[15]` | `0->0x2, 1->0x1, 2->0x0 (NONE)` | `[V-BIN]` |
| `fence_set` | `+0x10` | `b0->w0[9], b1->w0[30], b2->w1[3]` | `0..7` | `[V-BIN]` |
| `fence_wait` | `+0x14` | `b0->w0[19], b1->w0[28]` | `0..3` | `[V-BIN]` |
| `persp_mode` | `+0x38` | `w0[8], w0[16]` | `0->0x0 (linear)`, `1->0x2`, `2->0x1`, `3->0x3` (`FITRP` perspective uses `varying` W coeff) | `[V-BIN]` |
| `sat` / `flag44` | `+0x44` | `w0[7]` (byte) | `psIteration->bSat` (`+0x14`) | `[V-BIN]` |
| `itr_mode` | `+0x48` | `w1[1], w1[8]` | `0->0x0 (PIXEL)`, `1->0x2 (SAMPLE)`, `2->0x1 (CENTROID)` | `[V-BIN]` |
| `flag4c` | `+0x4c` | `w1[4]` (byte) | `psIteration` modifier byte (`+0x04`) | `[V-BIN]` |
| **`count`** | **`+0x54`** | **`w0[6], w0[11:10], w0[23]`** | `1..15` -> `count`; `16` (`RGX_MAX_ITERATION_REPEAT_COUNT`) encodes as **`0`** (`0,2,1,3,4,6,5,7,...`; `rgx.sh`: `cnt=2` ×594, `cnt=3` ×35) | `[V-ROUNDTRIP]` |

- **Operands (`CF` bank 6 indices are divided by `RGX_USC_COEFFICIENT_SET_SIZE = 3`!):**
  - `coeff_src` (`+0x18/+0x1c`, bank 6 `CF` 0..131): `b0->w0[20], b1->w0[18], b2->w0[21], b3->w0[24], b4->w0[26], b5->w1[2], b6->w1[6], b7->w2[18]`.
  - `coeff_idx` (`+0x20/+0x24`, `SLOT`/`NONE`): `b0..b3->w2[17:14]`; `hi=w1[7]` (`0:SLOT, 1:NONE`).
  - `w_coeff` (`+0x28/+0x2c`, bank 6 `CF` 0..131, for `FITRP`): `b0..b2->w1[12:10], b3->w1[30], b4..b7->w2[3:0]`.
  - `w_idx` (`+0x30/+0x34`, `SLOT`/`NONE`): `b0..b3->w1[28:25]`; `hi=w1[29]` (`0:SLOT, 1:NONE`).
  - `dst` (`+0x3c/+0x40`, `TEMP`/`VI`/`NONE`): `b0->w0[14], b1->w0[13], b2->w0[17], b3->w0[25], b4->w1[0], b5->w1[5], b6->w1[9], b7->w1[13]`; `hi=w1[14]` (`4(TEMP):(0,0x00), 5(VI):(1,0x00), 0(NONE):(1,0x80)`).
  - `sample_mask` (`+0x58/+0x5c`, `SH`/`NONE`): `w2[12:4]`, `hi=w2[13]` (`0:SH, 1:NONE`).
  - `fsc` (`+0x60/+0x64`, `SH`/`NONE`): `w1[23:15]`, `hi=w1[24]` (`0:SH, 1:NONE`).

---

### 3.4 `ISA_EMI` — Vertex Emit (`UVSW` / `TESSW`) & ISP Feedback (`ATST` / `DEPTHF` / `ISPREAD`) `[V-BIN]`

- **Family ID:** `7` | **Prefix:** `W0[6:0] = 0x3a` (`W0[6]=0` required!) | **Length:** `1–2` dwords
- **Default Word:** `W1 = 0x0000304a` | **Base Form:** `W0=0x4804403a, W1=0x80003005`

| Field | Struct Off | Bit Positions | Encoded Values & Semantics | Prov |
|---|---|---|---|---|
| `CC` | `+0x04` | `w1[6], w1[10]` | `0..3` | `[V-BIN]` |
| `SF` | `+0x08` | `w0[10], w1[21]` | `0->0x2, 1->0x1, 2->0x0 (NONE)` | `[V-BIN]` |
| `fence_set` | `+0x10` | `b0->w0[21], b1->w0[20], b2->w0[23]` | `0..7` | `[V-BIN]` |
| `fence_wait` | `+0x14` | `b0->w0[16], b1->w0[17]` | `0..3` | `[V-BIN]` |
| **`op`** | **`+0x18`** | **`w0[22], w0[25:24], w1[3], w1[5]`** | **21 opcodes `0..20`** (table below) | `[V-BIN]` |
| `isp_flag` | `+0x1c` | `b0..b3->w1[11:8], b6..b7->w1[15:14]` | `ISPFEEDBACK` predicate/invert flag (`+0x04`) | `[V-BIN]` |
| `stream_id` | `+0x3c` | `w1[23:22]` | `0..3` (Geometry shader vertex stream index) | `[V-BIN]` |

#### `ISA_EMI` Opcode Table (`+0x18`, `0..20`)
| Canonical `op` | Raw Bits `(w0[22],w0[25:24],w1[3],w1[5])` | Clean-Room Mnemonic | Source Converter & Semantics | Prov |
|---:|---:|---|---|---|
| `0..11` | `0x00, 0x10, 0x02, 0x12, 0x08, 0x18, 0x0a, 0x1a, 0x01, 0x11, 0x03, 0x13` | `isp_feedback` (`atst` / `depthf` / `cfb`) | `ConvertISPFEEDBACKToUscasm` (`IISPFEEDBACK` `0xc0` & `IISPCFB` `0xc1`: depth write, alpha/coverage kill, predicate feedback; `[OPEN]` exact 12-entry sub-op matrix) | `[V-BIN]` `[OPEN]` |
| `12` | `0x09` | `isp_read_depth` | `ConvertISPREADToUscasm` (`IISP_READ_DEPTH` `0xc2` -> `dst`) | `[V-BIN]` |
| `13` | `0x19` | `isp_read_stencil` | `ConvertISPREADToUscasm` (`IISP_READ_STENCIL` `0xc3` -> `dst`) | `[V-BIN]` |
| `14` | `0x0b` | `uvsw.write` / `tessw.write` | Write vertex output `src1` (data) to unified output offset `src0` (`IMM`/`reg`) | `[V-BIN]` |
| `15` | `0x1b` | `uvsw.emit` (GS) | `ConvertEMITToUseasm` (Geometry shader emit vertex) | `[V-BIN]` |
| `16` | `0x04` | `uvsw.endtask` | `ConvertEMITToUseasm` (End UVSW vertex task) | `[V-BIN]` |
| `17` | `0x14` | `uvsw.emit_endtask` | `ConvertEMITToUseasm` (Emit + EndTask) | `[V-BIN]` |
| `18` | `0x06` | `uvsw.cut` / `tessw.end` | `ConvertEMITToUseasm` (GS strip cut) & `ConvertTESSWToUseasm` | `[V-BIN]` |
| `19` | `0x16` | `uvsw.emit_cut` | `ConvertEMITToUseasm` (GS emit + cut / single-vertex VS emit) | `[V-BIN]` |
| `20` | `0x0c` | `uvsw.write_emit_endtask` | `ConvertUVSWToUseasm` (Fused final vertex write + emit + endtask!) | `[V-BIN]` |

#### `ISA_EMI` Operand Bitmaps (`[V-BIN]`)
- `src0` (11b — UVSW output slot offset `IMM 0..255` or reg): `b0..b2->w0[9:7], b3->w0[19], b4->w0[28], b5->w1[7], b6->w1[9], b7->w1[11], b8->w1[0]`; `hi=w1[2:1]`. Banks: `7(SH):(0,0x000)`, `12(IMM 0..255):(1,0x000)`, `2(SLOT):(1,0x100)`, `4(TEMP):(2,0x000)`, `0(NONE):(2,0x100)`, `5(VI):(3,0x000)`.
- `src1` (10b — vertex output value / depth value): `b0->w0[12], b1->w0[13], b2->w0[15], b3->w0[26], b4->w0[29], b5->w1[4], b6->w0[30], b7->w0[27], b8->w0[14]`; `hi=w0[18]`. Banks: `7(SH):(0,0x000)`, `4(TEMP):(1,0x000)`, `5(VI):(1,0x100)`, `2(SLOT):(1,0x180)`, `0(NONE):(1,0x1c0)`.
- `dst` (9b — `ISPREAD` dest): `b0->w1[15], b1->w1[16], b2->w1[14], b3->w1[17], b4->w1[18], b5->w1[19], b6->w1[20], b7->w1[12]`; `hi=w1[13]`. Banks: `4(TEMP):(0,0x00)`, `5(VI):(1,0x00)`, `0(NONE):(1,0x80)`.

---

### 3.5 `ISA_PSB_WR` & `ISA_PSB_RD` — Pixel Output Buffer Write (with HW Blend) & Read `[V-BIN]` `[V-ROUNDTRIP]`

Volcanic removes Rogue's `sr32..35` / `sr164..167` pixel output special registers and `EMITPIX`, replacing them with dedicated **Pixel State Buffer (`POUT` bank 3, `pout0..31`)** read/write instructions.

#### `ISA_PSB_WR` (`type = 9`, `W0[5:0] = 0x2a`, 1–3 dwords)
- **Default Words:** `W1 = 0x10000000`, `W2 = 0x00000000` (`1,426` of `1,502` `PSB_WR` insts in `rgx.sh` are **1 dword**!)
- **Base Form:** `W0=0x0000002a, W1=0x10000000, W2=0x80000000`
- **Header:** `CC=w0[8],w1[23]`, `SF=w0[7],w1[28]` (`0->2,1->1,2->0`; `+0x0c`: `w0[7]`), `fence_set=b0->w0[12],b1->w0[11],b2->w0[22]`, `fence_wait=b0->w0[10],b1->w0[16]`.
- **Mode & Blend Control Fields:**
  - `wr_mode` (`+0x18`, `w0[14], w1[24]`, `0..3`): `0 = blended PSB_WR`, `1 = direct POUTWRITE` (`ConvertPOUTWRITEToUscasm`).
  - `is_64bit` / `dual_reg` (`+0x34`, `w0[20]`): `0 = 32-bit (1 reg)`, `1 = 64-bit (2 consecutive regs)`.
  - `end_flag` / `emit` (`+0x35`, `w0[13]`): Final pixel output / emit flag.
  - `fmt_src0` / `fmt_src1` (`+0x38` = `w2[12:11]`, `+0x3c` = `w0[9],w1[27]`): `0..2` (`2` for raw `POUTWRITE`).
  - `write_mask` / `chan` (`+0x40`, `w0[17], w1[16]`): `0..3` (`1` for `POUTWRITE`).
  - **Hardware Blend Unit** (`ConvertPSB_WRToUscasm`): `color_op (+0x44)` = `w1[11],w1[14],w1[21]` (`0..4`), `alpha_op (+0x48)` = `w1[13],w1[15],w1[20]` (`0..4`, from `0x313c4=[0,1,2,3,4]`: `ADD/SUB/REVSUB/MIN/MAX`), `src_cfactor (+0x4c)` = `w0[26],w1[1:0],w1[9]` (`0..14`), `dst_cfactor (+0x50)` = `w0[21],w0[29],w1[8:7]` (`0..14`), `src_afactor (+0x54)` = `w0[28:27],w1[6],w1[19]` (`0..8`), `dst_afactor (+0x58)` = `w0[23],w0[30],w1[10],w1[18]` (`0..8`), `blend_flags (+0x5c/+0x5d)` = `w1[2]`/`w1[29]`.
- **Operands:**
  - `src0` (10b): `b0->w0[24], b1->w0[15], b2->w0[25], b3->w1[3], b4->w1[17], b5->w1[30], b6..b8->w2[2:0]`; `hi=w2[3]`; banks `7(SH):(0,0x000), 4(TEMP):(1,0x000), 5(VI):(1,0x100), 2(SLOT):(1,0x180), 0(NONE):(1,0x1c0)`.
  - `src1` (10b, blend constant): `b0->w2[4], b1->w2[5], b2->w1[22], b3..b7->w2[10:6], b8->w1[4]`; `hi=w1[5]`; banks `7(SH):(0,0x000), 2(SLOT):(1,0x000), 0(NONE):(1,0x100)`.
  - `pout` (5b, bank 3 `pout0..31`): `b0->w0[19], b1->w0[18], b2->w1[12], b3->w1[25], b4->w1[26]`.

#### `ISA_PSB_RD` (`type = 10`, `W0[5:0] = 0x2c`, 1 dword)
- **Default:** 1 dword (all `486` `PSB_RD` insts in `rgx.sh` are **1 dword**!) | **Base Form:** `W0=0xc000002c`
- **Fields:** `CC=w0[22:21]`, `SF=w0[7],w0[30]` (`0->2,1->1,2->0`; `+0x0c`: `w0[7]`), `fence_set=b0->w0[11],b1->w0[9],b2->w0[18]`, `fence_wait=b0->w0[16],b1->w0[19]`, `is_64bit (+0x28)=w0[10]` (1-bit flag `0=32b, 1=64b`).
- **Operands:**
  - `dst` (9b): `b0->w0[12], b1->w0[8], b2->w0[13], b3->w0[17], b4..b7->w0[26:23]`; `hi=w0[27]` (`4(TEMP):0, 5(VI):1`).
  - `pout` (5b, bank 3 `pout0..31`): `b0->w0[15], b1->w0[14], b2->w0[20], b3->w0[28], b4->w0[29]`.

---

## 4. 128-Wide Volcanic Subgroup Hardware Lowering & Active BRNs `[V-BIN]`

### 4.1 128-Wide Hardware Subgroup Architecture (`subgroupSize = 128`)
Volcanic executes **128 instances per slot/subgroup** (vs. 32 on Rogue). Rather than introducing a dedicated subgroup opcode family, Volcanic provides two architectural primitives used by `ConvertSubgroup*`, `ReplaceSubgroupShuffle`, and `ExpandBroadcastSafe`:
1. **`sr14` (`inst_num`, range `0..127`):** Per-lane subgroup invocation ID (moved from Rogue `sr51`).
2. **16 Cross-Lane Hardware `SLOT` Registers (`sl0..sl15`, canonical bank `2`):** Uniform per-slot registers writable by a predicated lane and readable simultaneously by all 128 lanes across `FOP`, `INT`, `BIT`, `MOV`, `DMA`, `SMP`, `EMI`, `PSB`.

#### Verified Compiler Lowering Sequences (`[V-BIN]`)
- **Cross-Lane Broadcast (`subgroupBroadcast(val, lane_id)` / `ExpandBroadcastSafe`):**
  1. Compare lane ID against target: `ISA_INT` (`alu_mode=1, op=4 (EQ), dst1=p0`) comparing `sr14` (`inst_num`) with `lane_id`.
  2. Predicated write to `SLOT`: `ISA_FOP` (`op=8 (fmov), CC=3 (p0)`) writing `slK = val`.
  3. All 128 lanes consume `slK` directly (or copy `rDst = slK` via `ISA_FOP op=8, CC=0`).
- **128-Bit Ballot (`subgroupBallot(pred)` -> `uvec4`, `ConvertSubgroupBallot`):**
  1. Compute per-lane bit mask `1 << (sr14 & 31)` (`ISA_BIT` `LSL`) and zero inactive/false lanes via `MOVC`.
  2. Run **4 × 32-lane sub-loops** (`[0..31]`, `[32..63]`, `[64..95]`, `[96..127]`, `lane += 32`, loop until `lane == 127`).
  3. Each chunk accumulates into a 32-bit ballot dword using cross-lane `SLOT` broadcast + `ISA_BIT` `OR` (`LUT=0x000e`).
- **Shuffle (`subgroupShuffle` / `ReplaceSubgroupShuffle`):**
  - If the lane index is uniform, lowers directly to a single predicated `SLOT` broadcast.
  - If the lane index is divergent across lanes, emits an active-lane loop (`sr14 == iter_lane` -> write `slK` -> lanes matching `req_lane == iter_lane` capture `slK` via predicated `MOVC`, terminated by `EndSubgroupLoop`).
- **Scan / Reduction (`subgroupAdd` / `Mul` / `Min` / `Max` / `And` / `Or` / `Xor`, `ConvertSubgroupScanReduce`):**
  - Uses `GetALUOpcodeForSubgroupOp` (`FOP`/`INT`/`BIT` opcodes for `F32`/`F16`/`I32`/`U32`/`I64`) over `sr14` (`0..127`) with `SLOT` cross-lane exchanges and `128`/`64`/`32`-lane loop/tree steps.

### 4.2 Volcanic Silicon Quirks & Active BRN Audit `[V-BIN]`
- **Explicitly Absent (Rogue / Older Silicon Only):** BRNs **`65104`**, **`69700`**, **`74812`**, and **`77899`** have **zero references** in Pixel 10 `25.3@6908880` (`libusc.so` / `vulkan.powervr.so`). Do **not** port these Rogue workarounds to `pco-volcanic`.
- **Active Volcanic USC BRNs Verified in `libusc.so`:**

| BRN | Compiler Pass / Symbol | Hardware Quirk & Required Mitigation for `pco-volcanic` | Prov |
|---|---|---|---|
| **`BRN71823`** | `FixHWBRN71823` (`validate/brn71823.c`) | **Non-uniform global `ISA_DMA` `ATOM32` (`IMEMATOM`):** When an atomic's address is divergent, split the basic block conditionally on an address bit into two `IMEMATOM` blocks and PHI-merge the result. | `[V-BIN]` |
| **`BRN71237`** | `FixHWBRN71237` | **Pixel shader multi-def output / `PHAS`:** Normalize pixel output writes when multiple instructions define the same `POUT` register across control flow before `PHAS`/exit. | `[V-BIN]` |
| **`BRN73233`** | `FixHWBRN73233` (`aeTrigOps = [72, 73]`) | **`ISA_AF32` `FSIN`/`FCOS` source modifiers:** Do not fold `abs`/`neg` modifiers directly onto `AF32` `FSIN` (`op 8/9`) or `FCOS` (`op 10/11`); materialize the modifier in a prior `FOP` instruction. | `[V-BIN]` |
| **`BRN74247`** | `FixHWBRN74247` | **`ISA_SMP` `lodm=replace`:** Workaround on explicit-LOD sample instructions (LOD offset/clamp adjustment). | `[V-BIN]` |
| **`BRN68994`** | `FixHWBRN68994` (`aeSmpOpcodes = [112..115]`) | **`ISA_SMP` texture sampling:** Affects all four core sample opcodes (`1D`/`2D`/`3D`/`GRAD`). | `[V-BIN]` |
| **`BRN76829`** | `brn76829` validator/fixup | Additional Volcanic USC validation constraint (alongside validator checks `68556`, `69769`, `69936`, `70094`). | `[V-BIN]` |

---

## 5. Live Shipping Binary Verification (`rgx.sh.71.2.2448.1212`) `[V-ROUNDTRIP]`

All **`1,378`** shipping `DXT-48-1536` primary & secondary shaders (**`30,495`** total instructions) were decoded against the exact bitmaps in Sections 1–3 with **`invalid_op = 0`** (100% legal family prefixes, dword lengths, and opcode ranges):

| Family | Total Insts | 1w | 2w | 3w | 4w | Decoded Opcode / Mode Histogram |
|---|---:|---:|---:|---:|---:|---|
| `ISA_FOP` (`0`) | `9,373` | `1,690` | `6,769` | `914` | `0` | `op`: `{0:356, 1:582, 2:880, 7:184, 8:6803, 9:168, 10:352, 11:48}` |
| `ISA_BIT` (`3`) | `6,698` | `136` | `2,685` | `2,267` | `1,610` | `enc`: `BIT_NO_P0_LUT (0x38)` ×`6,698` |
| `ISA_INT` (`2`) | `4,801` | `0` | `3,323` | `1,074` | `404` | `op`: `{0:884, 1:20, 4:708, 5:40, 7:2404, 11:745}` |
| `ISA_CTRL` (`14`) | `3,202` | `2,832` | `370` | `0` | `0` | Verified bit-exact in `VOLCANIC-USC-ISA-MAP.md` |
| `ISA_SMP` (`12`) | `3,146` | `0` | `0` | `3,146` | `0` | `dmn`: `{1(2D):3076, 2(3D):70}`; `lodm`: `{0:2020, 1:464, 2:44, 3:618}` |
| `ISA_PSB_WR` (`9`) | `1,502` | `1,426` | `76` | `0` | `0` | 95% elide to 1 dword |
| `ISA_ITR` (`5`) | `629` | `629` | `0` | `0` | `0` | `count`: `{2:594, 3:35}` (100% 1 dword) |
| `ISA_MOV` (`4`) | `614` | `0` | `304` | `310` | `0` | `tst_op`: `{0:614}` |
| `ISA_PSB_RD` (`10`) | `486` | `486` | `0` | `0` | `0` | 100% 1 dword |
| `ISA_DMA` (`6`) | `28` | `0` | `0` | `0` | `28` | `op`: `{4(ST):8, 5(LD):20}` |
| `ISA_COND` (`15`) | `16` | `16` | `0` | `0` | `0` | 100% 1 dword |

### Representative Decoded `DXT-48-1536` Hex Vectors (`[V-ROUNDTRIP]`)
| Family / Op | Shipping LE Dwords (`W0 .. WN`, EOI bit 31 set on last word) |
|---|---|
| `FOP` `op=0 (fmad)` | `0x0600d641, 0x0000080c, 0x80000408` |
| `FOP` `op=1 (fmul)` | `0x0440069d, 0x0000084b, 0x80001600` |
| `FOP` `op=2 (fadd)` | `0x0414a76b, 0x80408808` |
| `FOP` `op=8 (fmov)` | `0x04838e0f, 0x80400a08` |
| `FOP` `op=9 (fcomp)` | `0x140217ef, 0x80402808` |
| `FOP` `op=10 (fmovc)` | `0x0413a653, 0x80601808` |
| `INT` `op=1 (iadd)` | `0x091023f0, 0x81854008` |
| `INT` `op=4 (eq/max)` | `0x50b06eb0, 0x95902888` |
| `INT` `op=7 (imad/min)` | `0x1a316390, 0x95900808` |
| `BIT` (`0x38`) | `0x00069278, 0xa5b608a0` |
| `MOV` (`0x0a`) | `0x408381ca, 0x80531200` |
| `DMA` `op=5 (ld)` | `0x0005834e, 0x5808021e, 0x01000380, 0x83000004` |
| `DMA` `op=4 (st)` | `0x040445ce, 0x18080218, 0x010003f0, 0x83000004` |
| `SMP` `dmn=1 (2D)` | `0x3380303e, 0x02400020, 0x820d0100` |
| `ITR` (`cnt=2`) | `0xa000804c` |
| `PSB_WR` (1w) | `0x80006e6a` |
| `PSB_RD` (1w) | `0x802000ac` |

---

## 6. Phase 4 `pco-volcanic` Minimal Bringup Emit Recipes & `[OPEN]` List

### 6.1 Minimal Compute Shader Recipe (SSBO / 64-bit BDA `LD` -> ALU -> `ST` / `ATOM32` -> `END`)
1. **Compute byte offset:** Read invocation ID (`sr14` `inst_num` + workgroup ID in `sh` / `cs`) and compute `rOffset = tid * 4` via `ISA_INT` (`op=2`/`7`, `src0_fmt=U32, src1_fmt=U32, res_fmt=2`) or `ISA_BIT` (`0x38`, `LUT=0xcccc`, `LSL #2`).
2. **Global 40/64-bit BDA Load (`ISA_DMA`, `op=5`):**
   - `addr_base = shN` (even-aligned 64-bit BDA pair `shN:shN+1`), `addr_off = rOffset`, `dst = rVal`, `stride_src = IMM 1`, `op54 = IMM 4` (4 bytes; `8` or `16` for 64b/128b), `op5c = IMM 0`, `cache = (0,0)` (or `(0,1)` coherent), `fence_set = 1`.
3. **Compute ALU (with inline wait):**
   - `ISA_FOP` (`op=2` `fadd`, `dst0_fmt=2` `F32`, `fence_wait = 1` waiting on the load fence) or `ISA_INT` (`op=1` `iadd32`, `fence_wait = 1`).
4. **Global Store or Atomic (`ISA_DMA`, `op=4` `ST` or `op=6` `ATOM32`):**
   - For `ST`: `data = rRes`, `addr_base = shM`, `addr_off = rOffset`, `op54 = IMM 4`, `stride_src = IMM 1`.
   - For `ATOM32`: `op=6`, `atomop=0 (ADD)`, `data = rDelta`, `dst = rOld` (or `NONE`), `addr_base = shM`, `addr_off = rOffset`, `op54 = IMM 4` (`8` for `CMPXCHG`). Observe **BRN71823** if address is divergent.
5. **Terminate:** `ISA_CTRL` (`OP = END`, see `VOLCANIC-USC-ISA-MAP.md`).

### 6.2 Minimal Graphics Shader Recipes (VS & FS)
- **Minimal Vertex Shader (`VS`):**
  1. Transform vertex attributes from `VI` (`vi0..viN`) using `ISA_FOP` (`fmad`/`fmul`/`fadd`).
  2. Emit each non-final vertex attribute with `ISA_EMI` (`op=14` `uvsw.write`, `src0 = IMM <out_slot>`, `src1 = rAttr`).
  3. Emit the final vertex attribute with `ISA_EMI` (`op=20` `uvsw.write_emit_endtask`, `src0 = IMM <last_slot>`, `src1 = rLast`) — or `op=14` followed by `op=16`/`17` (`uvsw.endtask` / `uvsw.emit_endtask`).
  4. Terminate with `ISA_CTRL` (`OP = END`).
- **Minimal Fragment Shader (`FS`):**
  1. Interpolate varyings with `ISA_ITR` (`coeff_src = CF (idx/3)`, `dst = rUV`, `count = 2`, `fence_set = 1`).
  2. Sample texture (if needed) with `ISA_SMP` (`dmn = 1 (2D)`, `lodm = 0`, `sbmode = 0`, `img_base = shImg`, `smp_base = shSmp`, `coord = rUV`, `dst = rColor`, `fence_wait = 1`, `fence_set = 2`).
  3. Pack/convert color if needed via `ISA_MOV` (`conv_fmt` from Section 1.5, e.g., `53` for `U8888` normalized pack) or `ISA_FOP`.
  4. Write pixel output with `ISA_PSB_WR` (`wr_mode = 1 (POUTWRITE)`, `pout = pout0`, `src0 = rColor`, `end_flag = 1`, `fence_wait = 2`).
  5. Terminate with `ISA_CTRL` (`OP = END`).

### 6.3 Narrow `[OPEN]` Items (Non-Blocking for Phase 4; Ready for `tools/freeusc/volcanic_bitdiff.py`)
1. `ISA_FOP` rare inline slots `op = 3, 4, 5, 6` (derivative/coarse-fine exact polarity) and `FCOMP` `tst_type` `2` vs `3` (`ORD` vs `UNORD`).
2. `ISA_AF32` auxiliary slots `op = 2, 4, 6, 7` (rare IR `67, 69, 70, 71` variants beyond `FRCP`/`FRSQ`/`FLOG`/`FEXP`/`FSIN`/`FCOS`/`FATAN`).
3. `ISA_INT` unused opcode slots `op = 9, 10` (0 occurrences in `rgx.sh`).
4. `ISA_BIT` `cnt_op` (`+0x70`) 1:1 label order across `0..3` (`CBS`/`FTB`/`CTZ`/`POPCNT` permutation from `0x2ed88 = [3, 2, 0, 1]`).
5. `ISA_SMP` 10 boolean control flags (`+0x64..+0x6d`) exact 1:1 semantic name per bit (`proj`/`fcnorm`/`nn`/`pplod`/`tao`/`soo`/`sno`/`wrt`/`array`/`intcoords`).
6. `ISA_DMA` rare slots `op = 9, 10` (`IR 0x64` prefetch/cache hint).
7. `ISA_EMI` `ISPFEEDBACK` sub-op matrix across `op = 0..11` (exact combinations of `ATST`/`DEPTHF`/`CFB`/discard-feedback).
