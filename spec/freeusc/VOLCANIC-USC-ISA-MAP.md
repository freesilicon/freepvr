# FreePVR / FreeUSC — PowerVR Volcanic USC Shader ISA Core Map

> **Project**: `FreePVR` (umbrella) / `FreeUSC` (Volcanic USC Shader ISA)
> **Targets**: PowerVR Volcanic `DXT-48-1536` (`71.2.2448.1212`) & `CXTP-48-1536 MC1` (`25.3@6908880`)
> **Scope**: Clean-room, bit-exact core ISA specification closing `[GAP-ISA-1]`, `[GAP-ISA-3]`, and `[GAP-ISA-4]` for Mesa Phase 4 `pco-volcanic` (`pco_isa_volcanic.py`, `pco_binary.c`).
> **Companion Spec**: `VOLCANIC-USC-OPCODES.md` (ALU/Bitwise/Backend instruction families: `FOP`, `DOT8`, `INT`, `BIT`, `MOV`, `ITR`, `DMA`, `EMI`, `RSTORE`, `PSB_WR`, `PSB_RD`, `RLOAD`, `SMP`, `AF32`, `TAP`).

## Provenance Legend

- **`[V-BIN]`**: Verified directly from bit-level encoder/decoder/assembler logic in stock Volcanic `25.3@6908880` shader compiler/driver binaries.
- **`[V-ROUNDTRIP]`**: Verified by clean-room Python framing, decode, re-encode round-trip, and branch-target landing checks on real shipping `DXT-48-1536` shader binaries (`1,378` shaders from `rgx.sh.71.2.2448.1212` + `549` embedded `25.3@6908880` shaders = `1,927` total shaders, `38,967` instructions).
- **`[OPEN]`**: Unreferenced in shipping binaries or requires on-device differential probe confirmation (`tools/freeusc/volcanic_bitdiff.py` on Pixel 10 / Pixel 11).

## Gap Closure Scorecard

| Gap ID | Scope | Status | Primary Evidence |
|---|---|---|---|
| **`[GAP-ISA-1]`** | Word framing, bit-31 EOI, 1–4 dword compression, 17-family prefix tree, NOP alignment, inline data fences | **CLOSED** `[V-BIN]` `[V-ROUNDTRIP]` | `1,927 / 1,927` real shaders (`38,967` insts), `0` framing/class errors |
| **`[GAP-ISA-3]`** | 16 Canonical Register Banks, unified linear per-slot bank-offset addressing, `CC` predicate codes, `MOV`/`DMA` indexing, `LIMM` | **CLOSED** `[V-BIN]` `[V-ROUNDTRIP]` | All 95 canonical size/offset tables extracted; zero separate Rogue `S0..S5`/`ISS`/`W0..W1` |
| **`[GAP-ISA-4]`** | Special Registers (`SR 0..60` + Rogue delta), complete 128-entry Special Constants (`SC 0..127`), `ISA_CTRL`, `ISA_COND`, `DMA` sync (`IDF`/`MEMBARRIER`) | **CLOSED** `[V-BIN]` `[V-ROUNDTRIP]` (`CTRL OP=8` & unused SR indices `[OPEN]`) | `4,587 / 4,587` `CTRL` bit-exact round-trips; `139 / 139` valid `BR` landings |

---

## Executive Summary: Rogue vs. Volcanic Paradigm Shift `[V-BIN]`

Volcanic USC is **not** a minor field revision of Rogue USC (`pco_isa.py`). It replaces Rogue's byte-aligned variable-section instruction groups with a **32-bit word-framed, prefix-coded, self-contained instruction encoding**:

| Architectural Property | Rogue USC (Upstream Mesa `pco_isa.py`) | Volcanic USC (`DXT-48-1536` / `CXTP-48-1536`) |
|---|---|---|
| **Instruction unit** | Byte-aligned instruction group (`2–3 B` header + phases + `S0..S5` + `ISS` + `W0..W1` + pad) | **1, 2, 3, or 4 little-endian 32-bit dwords** (`32`–`128` bits) |
| **Length / framing** | Header `length` field (in halfwords) + `oporg` / `opcnt` | **Bit 31 (`0x80000000`) of every dword = End-of-Instruction (EOI)** (`0` = more words, `1` = last word) |
| **Compression** | Omitted optional phase/source/dest byte sections | **Trailing default-word elision**: trailing words equal to per-family 31-bit defaults are omitted unless `min_words` forces length |
| **Alignment padding** | Trailing byte padding (`0xff`, `0xf0\|n`) | **1–4 word `CTRL NOP` (`op=10`, `min_words = min(rem, 4)`)** — no `0xff`/`0xf0` byte pads |
| **Operand encoding** | Separate Lower (`S0..S2`) / Upper (`S3..S5`) source bytes + `ISS` muxes + `W0..W1` dest bytes | **Direct inline unified linear index** per operand slot: `encoded = offsetFromCanonicalMapping[bank] + reg_offset` |
| **Data fences (`WDF`)** | Standalone `WDF drcN` control instruction | **Embedded inline on every instruction**: `fence_wait` (2b), `fence_set` (3b), and `SF` (3b, non-`CTRL`) |
| **Execution mask / `END`** | Header `end` bit on any instruction group | Dedicated **`CTRL END` (`0x8000031c` w/o wait)** instruction; `CC` (2b) embedded on non-`CTRL` instructions |
| **Branch offset units** | Byte offset | **32-bit dword offset** (signed 30-bit relative `[-0x20000000, +0x1fffffff]`, unsigned 30-bit absolute `<= 0x3ffffffe`) |
| **Pixel outputs** | Special Registers `sr32..35`, `sr164..167` | Dedicated **`PSB_RD` / `PSB_WR`** families + `ISA_BANK 3` (`PIXEL_OUTPUT`); tile LD/ST offsets in `SR` |
| **Special Registers (`SR`)** | Sparse `0..167+` | Compact **`0..60`** (`61` = `UNDEF`); **every single Mesa-used SR moved** |
| **Special Constants (`SC`)** | Rogue `sc0..sc127` | Volcanic **128-entry `ISA_C_MAPPING_ARRAY`** (major re-indexing + packed FP16/graphics/sRGB constants) |

---

## 1. `[GAP-ISA-1]` Packet Framing, Prefix Tree, Padding & Inline Fences

### 1.1 Bit-31 Word Framing & Trailing-Default Compression `[V-BIN]` `[V-ROUNDTRIP]`

Every Volcanic USC instruction consists of `N ∈ {1, 2, 3, 4}` little-endian `u32` words `W[0..N-1]`:

```text
  31  30                                                      0
 +---+---------------------------------------------------------+
 | 0 |  Word 0 payload (31 bits) — bit[6:0] = family prefix    |  (if N > 1)
 +---+---------------------------------------------------------+
 | 0 |  Word 1 payload (31 bits)                               |  (if N > 2)
 +---+---------------------------------------------------------+
 | 0 |  Word 2 payload (31 bits)                               |  (if N > 3)
 +---+---------------------------------------------------------+
 | 1 |  Word N-1 payload (31 bits) — EOI = 1 terminates inst   |
 +---+---------------------------------------------------------+
```

- **Framing invariant (`getEncodedInstLength`)**: Scan `W[0]..W[3]` for the first word with bit 31 (`0x80000000`) set. If bit 31 is not set within 4 words (`> 16 B`), the stream is malformed.
- **Trailing-default decompression**: When `N < max_words(family)`, omitted words `W[N..max_words-1]` are reconstructed with the family's exact 31-bit hardware default constants (Section 1.2).
- **Forced minimum length (`min_words`)**: The encoder supports `min_words ∈ {1..4}` (used for label alignment padding and patchable `BR`/`LIMM`/`PHAS` slots). Extra trailing words carry the family's default 31-bit payload (zeros for `CTRL`/`COND` words 2–3), with bit 31 set **only** on `W[N-1]`.

### 1.2 Complete 17-Family `W0[6:0]` Prefix Decode Table `[V-BIN]` `[V-ROUNDTRIP]`

`W0[6:0]` (128 possible values) uniquely identifies the instruction family: `109` valid prefix codes across `17` families (with `BIT` and `CTRL` each having two sub-encodings) and `19` reserved/invalid codes.

| Type ID | Family | `W0[6:0]` Bit Pattern | Hex Codes (`W0 & 0x7f`) | Count | Max Words | Default `W1` (31b) | Default `W2` (31b) | Default `W3` (31b) |
|---:|---|---|---|---:|---:|---|---|---|
| `0` | **`FOP`** | `xxxxxxx1` (`W0[0]=1`) | All odd `0x01..0x7f` | 64 | 3 | `0x00400808` | `0x00000400` | — |
| `1` | **`DOT8`** | `x011000` | `0x18`, `0x58` | 2 | 3 | `0x04048010` | `0x00000000` | — |
| `2` | **`INT`** | `xxx0000` (`W0[3:0]=0`) | `0x00,0x10,0x20,0x30,0x40,0x50,0x60,0x70` | 8 | 4 | `0x01902008` | `0x040c0a98` | `0x00000000` |
| `3a` | **`BIT`** (`NO_DST1_TST_OP1`) | `xxx0110` (`W0[3:0]=0x6`) | `0x06,0x16,0x26,0x36,0x46,0x56,0x66,0x76` | 8 | 4 | `0x78040a61` | `0x4060080f` | `0x01840301` |
| `3b` | **`BIT`** (`NO_P0_LUT`) | `x111000` | `0x38`, `0x78` | 2 | 4 | `0x25b60800` | `0x1a008240` | `0x00000200` |
| `4` | **`MOV`** | `x001010` | `0x0a`, `0x4a` | 2 | 4 | `0x00000800` | `0x00400030` | `0x00000000` |
| `5` | **`ITR`** | `x001100` | `0x0c`, `0x4c` | 2 | 3 | `0x21000080` | `0x00002000` | — |
| `6` | **`DMA`** | `x001110` | `0x0e`, `0x4e` | 2 | 4 | `0x00000000` | `0x00000000` | `0x00000000` |
| `7` | **`EMI`** | `0111010` (`W0[6]=0` req.) | `0x3a` | 1 | 2 | `0x0000304a` | — | — |
| `8` | **`RSTORE`** | `x000010` | `0x02`, `0x42` | 2 | 2 | `0x00000000` | — | — |
| `9` | **`PSB_WR`** | `x101010` | `0x2a`, `0x6a` | 2 | 3 | `0x00010030` | `0x00001008` | — |
| `10` | **`PSB_RD`** | `x101100` | `0x2c`, `0x6c` | 2 | 2 | `0x00000000` | — | — |
| `11` | **`RLOAD`** | `x010010` | `0x12`, `0x52` | 2 | 2 | `0x00000000` | — | — |
| `12` | **`SMP`** | `0111110` (`W0[6]=0` req.) | `0x3e` | 1 | 4 | `0x42400008` | `0x000c0100` | `0x00000000` |
| `13` | **`AF32`** | `x011010` | `0x1a`, `0x5a` | 2 | 2 | `0x00000200` | — | — |
| `14a` | **`CTRL`** (`BREAKPOINT`) | `x001000` | `0x08`, `0x48` | 2 | 4 | `0x00000000` | `0x00000000` | `0x00000000` |
| `14b` | **`CTRL`** (`ENC`) | `x011100` | `0x1c`, `0x5c` | 2 | 4 | `0x00000000` | `0x00000000` | `0x00000000` |
| `15` | **`COND`** | `x011110` | `0x1e`, `0x5e` | 2 | 2 (pad to 4) | `0x00000020` | `0x00000000` | `0x00000000` |
| `16` | **`TAP`** | `0111100` (`W0[6]=0` req.) | `0x3c` | 1 | 2 | `0x00040024` | — | — |
| — | **`INVALID`** | Remaining 19 even codes | `0x04,0x14,0x22,0x24,0x28,0x2e,0x32,0x34,0x44,0x54,0x62,0x64,0x68,0x6e,0x72,0x74,0x7a,0x7c,0x7e` | 19 | — | — | — | — |

> †Note: `BIT` (`3a`) matches `W0[3:0] == 0x6` (`0bxxxx110`, NOT `0bxxxx1110`!). `EMI` (`0x3a`), `SMP` (`0x3e`), and `TAP` (`0x3c`) strictly require `W0[6] == 0` (`0x7a`, `0x7e`, `0x7c` are `INVALID`).

### 1.3 Alignment & Padding Rules `[V-BIN]` `[V-ROUNDTRIP]`

- Volcanic USC instructions are 32-bit dword-aligned at all times (minimum instruction size = `4 B`).
- When a code label requires coarser alignment (e.g., 8B or 16B fetch-line alignment), the assembler inserts one or more **`CTRL NOP` (`op=10`, `W0=0x0000029c`)** instructions with `min_words = min(remaining_dwords, 4)`:
  - 1-dword NOP: `8000029c`
  - 2-dword NOP: `0000029c 80000000`
  - 3-dword NOP: `0000029c 00000000 80000000`
  - 4-dword NOP: `0000029c 00000000 00000000 80000000`
- Confirmed on real `rgx.sh` binaries: `482` `CTRL NOP` instructions observed (`120` x 1-dword, `362` x 2-dword), **zero** Rogue `0xff`/`0xf0` padding bytes anywhere.

### 1.4 Inline Data Fences (`WDF` Replacement) & `SF` Encoding `[V-BIN]`

Every instruction carries inline dependency-counter (data-fence) control instead of Rogue's separate `WDF` opcode (`UscasmUpdateDataFences` / `UscasmGetDataFences`):

| Field | Range | Present On | Semantics |
|---|---:|---|---|
| **`fence_wait`** | `0..3` (2 bits) | All 17 families (`CTRL` & non-`CTRL`) | Fence counter wait mask/index before instruction issues (`0` = no wait; `1, 2, 3` = wait on counter/pipe). Observed in `rgx.sh` `CTRL.END`: `w=0` (1334), `w=2` (26), `w=1` (18). |
| **`fence_set`** | `0..7` (3 bits) | All 17 families (`CTRL` & non-`CTRL`) | Fence counter set/increment slot (`0` = none; `1..7` = counter ID / mask). |
| **`SF` (Sub-Fence / Sync Flag)** | 3 bits (`0..5`, `6..7` invalid) | All **non-`CTRL`** families (`0..13, 15, 16`) | Encoded via `ISA_TAP_ENC_SF_offsetFromCanonicalMapping = [4, 1, 0]` and `sizeFromCanonicalBankMapping = [1, 2, 0]`:<br>• `raw = 0` → canonical `(bank=2, off=0)` (`SF_NONE` / no sub-fence)<br>• `raw = 1..3` → canonical `(bank=1, off=0..2)` (`SF_WAIT_COUNTER 0..2`)<br>• `raw = 4..5` → canonical `(bank=0, off=0..1)` (`SF_SET/MODE_0`, default `raw=4` -> `w1=0x20` in `COND`!) |

> Note on `g_auFenceCount` / `g_auCounterLimit` (`fence.c` `[V-BIN]`): Hardware counter limits per pipe are `[7, 1, 3, 1, 7]` across the 5 async pipe classes (`g_auFenceCount = [3, 2, 4, 5]` for the 4 primary fence groups).

---

## 2. `[GAP-ISA-3]` Canonical Register Banks, Unified Linear Operands, Predicates & Indexing

### 2.1 Canonical Register Bank Enumeration (`ISA_REGISTER_BANK 0..15`) `[V-BIN]`

Volcanic eliminates Rogue's split `S0..S5` / `ISS` / `W0..W1` tables. Instead, the compiler and hardware encoder map every architectural register bank to a canonical bank ID (`0..15`), verified from the `USC_REGTYPE` → `ISA_REGISTER_BANK` lookup table (`0x314bc`) and all 95 `ISA_*_sizeFromCanonicalBankMapping` tables:

| Canonical Bank ID | Name | Hardware Range | Size | Compiler `USC_REGTYPE` | Hardware Purpose & Notes |
|---:|---|---:|---:|---:|---|
| **`0`** | `NONE` | `0` | 1 | `0` (unused) | Sentinel "no register / unused operand" |
| **`1`** | `INTERNAL` (`i0..i7`) | `0..7` | 8 | `8` | Short-lived internal ALU/bitwise scratch registers (analog of Rogue `i0..i2`/`ft*`, expanded to 8) |
| **`2`** | `SLOT` (`sl0..sl15`) | `0..15` | 16 | `9` (`SLOT`) | 16 per-slot scalar/uniform/loop-counter registers; **`CTRL.LIMM` and `CTRL.BRLT` destination bank** |
| **`3`** | `PIXEL_OUTPUT` (`o*`) | `0` | 1 | `4` (`PIXEL_OUTPUT`) | Pixel output target token (used in `DMA_DST_REG` and `PSB_WR` / `PSB_RD`) |
| **`4`** | `TEMP` (`r0..r255`) | `0..255` | 256 | `10` (`HWTEMP`) | Primary per-instance temporary registers (`ISA_INT_SRC2_REG` caps at `0..127`) |
| **`5`** | `VERTEX_INPUT` (`vi0..vi127`) | `0..127` | 128 | `1` (`VERTEX_INPUT`) | Per-vertex / unified input registers (`vi0..vi127`) |
| **`6`** | `COEFFICIENT` (`cf0..cf131`) | `0..131` | 132 | `2` (`COEFFICIENT`) | Varying / plane-equation coefficient registers (`44` triangles × 3 coeffs = `132`; `MOV` stores `reg / 3` + component `reg % 3`) |
| **`7`** | `SHARED` (`sh0..sh511`) | `0..511` | 512 | `3` (`SHARED`) | Shared/constant-calc uniform registers (`sh0..sh511`, expanded vs Rogue `sh0..sh255` default window) |
| **`8`** | `SPECIAL_CONSTANT` (`sc0..sc127`) | `0..127` | 128 | `5` (`SPECIAL_CONSTANT`) | Hardware read-only constant table (`ISA_C_MAPPING_ARRAY`, Section 3.2) |
| **`9`** | `COMMON_STORE` / `LOCAL_MEM` (`cs0..cs1023`) | `0..1023` | 1024 | — (addressed via `MOV`/`DMA`) | Direct common-store / local-memory / tile-buffer register window (`MOV_SRC`, `MOV_DST`, `DMA_DATA`, `DMA_DST`) |
| **`10`** | `SPECIAL_REGISTER` (`sr0..sr63`) | `0..63` | 64 | `6` (`SPECIAL_REGISTER`) | Hardware Special Registers (`0..60` valid, `61` = `UNDEF`, Section 3.1) |
| **`11`** | `EMC` (`emc`) | `0` | 1 | `23` (`EMC`) | Execution Mask Counter register (readable/writable via `MOV_SRC` / `MOV_DST`) |
| **`12`** | `IMM` (inline immediate) | Slot-dependent (`0..63` up to `0..28671`) | varies | `13` (`IMMEDIATE`) | Inline integer immediate in `MOV`, `DMA`, `EMI`, `SMP`, `TAP` slots |
| **`13`** | `BIT_PRED_DST` (`p_tst` / `exec`) | `0` | 1 | — (`BIT_DST1` only) | Predicate/execution-mask destination token in `ISA_BIT_DST1_REG` |
| **`14`** | `DMA_ADDR_GLOBAL` / `NULL` | `0` | 1 | — (`DMA_ADDR_BASE` only) | Zero/unbased address sentinel in `ISA_DMA_ADDR_BASE` |
| **`15`** | `INDEXED_INDIRECT` | Slot-dependent | — | `29` | Indexed/indirect bank marker handled by `MOV`/`DMA`/`FOP`/`INT`/`BIT` converters |

> **Hardware Predicates (`USC_REGTYPE_HWPREDICATE = 11`)**: Unlike Rogue where `p0` is a 1-bit predicate register, Volcanic supports **7 predicate registers (`p0..p6`, value `7` invalid)** in `ISA_COND` (`check_ISA_COND` caps `SRC_PRED_0` and `SRC_PRED_1` at `0..6`), while regular ALU/MOV/DMA instructions use the 2-bit **`CC`** field referencing `p0` + execution mask (Section 2.3).

### 2.2 Unified Linear Per-Slot Bank-Offset Encoding `[V-BIN]`

Every operand slot encodes `(bank, offset)` as a single unsigned integer:

```text
encoded_slot_value = offsetFromCanonicalMapping[bank] + offset
   (valid iff sizeFromCanonicalBankMapping[bank] >= 0 and offset <= sizeFromCanonicalBankMapping[bank])
```

Decoding is a simple range lookup against the slot's non-overlapping bank intervals. Complete verified slot maps across all Volcanic families (`vulkan.powervr.so` `.rodata` `[V-BIN]`):

| Family & Operand Slot | Bit Width | Bank → Encoded Range `[base .. base + max]` |
|---|---:|---|
| **`CTRL.DST`** | 4b (`w1[14],w1[16],w2[1:0]`) | `SLOT(2): [0..15]` (fixed bank `2`) |
| **`AF32.SRC`** | 9b | `TEMP(4): [0..255]`, `VI(5): [256..383]`, `SLOT(2): [384..399]` |
| **`DOT8.DST0`** | 9b | `TEMP(4): [0..255]`, `VI(5): [256..383]`, `SLOT(2): [384..399]`, `NONE(0): [416]`, `INT(1): [448..455]` |
| **`DOT8.DST1`** | 4b | `INT(1): [0..7]`, `NONE(0): [8]` |
| **`DOT8.SRC2`** | 11b | `SH(7): [0..511]`, `VI(5): [512..639]`, `SLOT(2): [768..783]`, `TEMP(4): [1024..1279]`, `INT(1): [1280..1287]`, `SC(8): [1536..1663]` |
| **`INT.SRC2`** | 9b | `TEMP(4): [0..127]`†, `SC(8): [128..255]`, `VI(5): [256..383]`, `SR(10): [384..447]`, `SLOT(2): [448..463]`, `INT(1): [480..487]` |
| **`BIT.DST0`** (`NO_P0_LUT`) | 10b | `SH(7): [0..511]`, `TEMP(4): [512..767]`, `VI(5): [768..895]`, `SLOT(2): [896..911]`, `NONE(0): [928]`, `INT(1): [960..967]` |
| **`BIT.DST1`** (`NO_P0_LUT`) | 5b | `SLOT(2): [0..15]`, `INT(1): [16..23]`, `BIT_PRED(13): [24]`, `NONE(0): [28]` |
| **`BIT.SRC1`** (`NO_P0_LUT`) | 11b | `SH(7): [0..511]`, `VI(5): [512..639]`, `SR(10): [768..831]`, `TEMP(4): [1024..1279]`, `SLOT(2): [1280..1295]`, `SC(8): [1536..1663]`, `INT(1): [1792..1799]` |
| **`BIT.SRC2`** (`NO_P0_LUT`) | 10b | `TEMP(4): [0..255]`, `SC(8): [256..383]`, `SLOT(2): [384..399]`, `VI(5): [512..639]`, `INT(1): [640..647]`, `SR(10): [768..831]` |
| **`BIT.SRC3`** (`NO_P0_LUT`) | 5b | `SLOT(2): [0..15]`, `INT(1): [16..23]`, `SC(8): [24]` (`sc0` only) |
| **`MOV.SRC`** | 12b | `CS(9): [0..1023]`, `TEMP(4): [1024..1279]`, `VI(5): [1536..1663]`, `SH(7): [2048..2559]`, `EMC(11): [2560]`, `CF(6): [3072..3203]` |
| **`MOV.DST`** | 11b | `CS(9): [0..1023]`, `TEMP(4): [1024..1279]`, `VI(5): [1280..1407]`, `EMC(11): [1408]`, `CF(6): [1536..1667]`, `SLOT(2): [1792..1807]`, `NONE(0): [1920]` |
| **`MOV.IDX_ADDR`** | 15b | `IMM(12): [0..28671]`, `TEMP(4): [28672..28927]`, `NONE(0): [30720]` |
| **`MOV.IDX_MOD`** | 11b | `IMM(12): [0..1023]`, `SH(7): [1024..1535]`, `TEMP(4): [1536..1791]`, `VI(5): [1792..1919]`, `SLOT(2): [1920..1935]` |
| **`MOV.BST_LEN`** | 10b | `SH(7): [0..511]`, `IMM(12): [512..575]`, `SLOT(2): [768..783]` |
| **`ITR.VARYING`** | 8b | `CF(6): [0..131]`, `NONE(0): [192]` |
| **`ITR.FSC`** | 10b | `SH(7): [0..511]`, `NONE(0): [512]` |
| **`DMA.DATA_REG`** | 11b | `CS(9): [0..1023]`, `SH(7): [1024..1535]`, `TEMP(4): [1536..1791]`, `VI(5): [1792..1919]`, `NONE(0): [1920]` |
| **`DMA.DST_REG`** | 11b | `CS(9): [0..1023]`, `SH(7): [1024..1535]`, `TEMP(4): [1536..1791]`, `VI(5): [1792..1919]`, `POUT(3): [1920]`, `NONE(0): [1984]` |
| **`DMA.ADDR_BASE`** | 10b | `SH(7): [0..511]`, `TEMP(4): [512..767]`, `VI(5): [768..895]`, `SLOT(2): [896..911]`, `NONE(0): [928]`, `DMA_GLOBAL(14): [960]` |
| **`DMA.INDEX_INDEX`** | 5b | `SLOT(2): [0..15]`, `NONE(0): [16]` |
| **`DMA.STRIDE_SRC`** | 12b | `IMM(12): [0..2047]`, `SH(7): [2048..2559]`, `VI(5): [2560..2687]`, `NONE(0): [2816]`, `TEMP(4): [3072..3327]`, `SLOT(2): [3584..3599]` |
| **`EMI.SRC0`** | 11b | `SH(7): [0..511]`, `IMM(12): [512..767]`, `SLOT(2): [768..783]`, `TEMP(4): [1024..1279]`, `NONE(0): [1280]`, `VI(5): [1536..1663]` |
| **`RLOAD.DST`** | 10b | `SH(7): [0..511]`, `TEMP(4): [512..767]`, `VI(5): [768..895]`, `NONE(0): [896]` |
| **`SMP.STATE_BASE`** | 10b | `SH(7): [0..511]`, `TEMP(4): [512..767]`, `VI(5): [768..895]`, `SLOT(2): [896..911]` |
| **`SMP.SAMPLEROFFSET`** | 11b | `IMM(12): [0..1023]`, `TEMP(4): [1024..1279]`, `VI(5): [1536..1663]` |
| **`SMP.LOD_REG`** | 10b | `SH(7): [0..511]`, `SLOT(2): [512..527]`, `NONE(0): [768]` |
| **`TAP.DST`** | 9b | `TEMP(4): [0..255]`, `VI(5): [256..383]` |
| **`TAP.COORD`** | 9b | `TEMP(4): [0..255]`, `VI(5): [256..383]`, `NONE(0): [384]` |
| **`TAP.STATE_IMAGE`** | 10b | `SH(7): [0..511]`, `TEMP(4): [512..767]`, `VI(5): [768..895]`, `SLOT(2): [896..911]`, `NONE(0): [960]` |
| **`TAP.IMAGEOFFSET`** | 11b | `IMM(12): [0..1023]`, `TEMP(4): [1024..1279]`, `NONE(0): [1280]`, `VI(5): [1536..1663]` |

> †Note: `INT.SRC2` restricts `TEMP` to `r0..r127` (`max=127`). `FOP`/`INT` primary `SRC0`/`SRC1`/`DST0` tables are covered in detail in companion `VOLCANIC-USC-OPCODES.md`.

### 2.3 Universal 2-Bit Execution Condition Code (`CC`) `[V-BIN]`

All non-`CTRL` families (`MOV`, `DMA`, `COND`, `FOP`, `INT`, `BIT`, `ITR`, `SMP`, `TAP`, etc.) share the canonical 2-bit `CC` predicate code (`ISA_TAP_ENC_CC` = identity `[0, 1, 2, 3]`):

| `CC` Value | Meaning | Compiler Condition |
|---:|---|---|
| `0` | **`ALWAYS` (`e0` ignored)** | `INST_ALWAYS_EXECUTE` (`uFlags & 0x100`), no predicate (`DUMMY`) |
| `1` | **`EXEC_MASK` (`e0` active)** | Default active-instance execution, no predicate (`DUMMY`) |
| `2` | **`!p0` (`e0 && !p0`)** | Hardware predicate `p0` with negate flag (`uFlags & 0x2`) |
| `3` | **`p0` (`e0 && p0`)** | Hardware predicate `p0` non-negated |

### 2.4 Indexed Addressing & Immediates (`MOV` / `DMA` / `LIMM`) `[V-BIN]`

1. **Dynamic Register Indexing (`MOV` family, `0x0a`/`0x4a`)**:
   - Volcanic moves dynamic register-file indexing (Rogue's `idx0`/`idx1` bank extensions on `S0..S5`) into dedicated **`MOV` (`LDMOV` / `STMOV` / `IDXRW`)** instructions (`ConvertLDMOVToUscasm`, `ConvertSTMOVToUscasm`, `ConvertIDXRWToUscasm`):
     - `MOV_IDX_ADDR` (15b): dynamic index register (`r0..r255` at `28672..28927`) or static immediate base (`0..28671`, `NONE=30720`).
     - `MOV_IDX_MOD` (11b): stride/modifier (`IMM 0..1023`, `SH 0..511`, `TEMP 0..255`, `VI 0..127`, `SLOT 0..15`).
     - `MOV_BST_LEN` (10b): burst length (`SH 0..511`, `IMM 0..63`, `SLOT 0..15`).
     - `COEFFICIENT` (`bank 6`) special rule (`ConvertMOVUNITArgument`): register number is divided by `3` (`offset = num / 3`) and component selector stored as `num % 3` (`0, 1, 2`).
2. **Indexed Memory Addressing (`DMA` family, `0x0e`/`0x4e`)**:
   - `DMA.ADDR_BASE` (10b at `W1[19:10]`): base address register (`SH`, `TEMP`, `VI`, `SLOT`, `NONE`, or `DMA_GLOBAL`). Note `SH` base must be 2-register aligned (`(reg & 1) == 0`).
   - `DMA.INDEX_INDEX` (5b at `W1[9:5]`): index slot register (`SLOT 0..15` or `NONE=16`).
   - `DMA.STRIDE_SRC` (12b at `W2[20:9]`): inline immediate stride (`0..2047`) or register (`SH`, `VI`, `TEMP`, `SLOT`, `NONE`).
3. **32-Bit Immediates (`CTRL.LIMM`, `op=6`)**:
   - Arbitrary 32-bit constants are loaded via `ISA_CTRL` `OP=6` (`LIMM`) into a `SLOT` register (`sl0..sl15`, `bank=2`) using the exact 32-bit bit-permutation across `W0` and `W1` documented in Section 3.3.2.

---

## 3. `[GAP-ISA-4]` Special Registers (`SR`), Special Constants (`SC`), `CTRL`, `COND` & Sync Opcodes

### 3.1 Volcanic Special Register (`SR`) Numeric Index Table (`bank = 10`, `0..60`, `61 = UNDEF`) `[V-BIN]`

**CRITICAL DELTA VS. ROGUE**: **Every single SR index used by Mesa Rogue (`pco_isa.py`) moved or changed semantics on Volcanic!** Valid SRs span `0..60` (`IsSpecialRegister` returns `61` = `USC_SPECIAL_REGISTER_UNDEF`). Secondary/const-calc-only SRs are enforced by the 64-bit mask `0x000400000000022f` = **`{0, 1, 2, 3, 5, 9, 58}`** (`IsConstCalcSpecialRegister`).

| Volcanic `SR` | Hex | Const-Calc (`SEC`)? | Volcanic Meaning / Hardware Role | Rogue `pco_isa.py` Index | Delta vs. Rogue | Provenance |
|---:|---:|:---:|---|---:|---|---|
| **`sr0`** | `0x00` | Yes | Workgroup / Global ID X (input reg `0`, `GetSource` + `EmitInstanceAddressCalc`) | — | New / Moved | `[V-BIN]` |
| **`sr1`** | `0x01` | Yes | Workgroup / Global ID Y (input reg `1`, `GetSource`) | — | New / Moved | `[V-BIN]` |
| **`sr2`** | `0x02` | Yes | Workgroup / Global ID Z (input reg `2`, `GetSource`) | — | New / Moved | `[V-BIN]` |
| **`sr3`** | `0x03` | Yes | Workgroup / Global ID W / flat (`GetSource`, `< 4` range) | — | New / Moved | `[V-BIN]` |
| **`sr5`** | `0x05` | Yes | Geometry / Ray attribute heap base / buffer ID (`SetupGeometryBufferAddress`, `Ray_GetAttribHeapStride`, `Ray_GetRayStateOffset`) | — | New / Moved | `[V-BIN]` |
| **`sr7`** | `0x07` | No | Vertex shader fixed system-value input (`SetupVertexShaderFixedRegs` — vertex/instance ID) | — | New / Moved | `[V-BIN]` |
| **`sr8`** | `0x08` | No | **Face orientation** (`GetFaceTypeSource`: `front_facing`) | `44` | **MOVED (`44 → 8`)** | `[V-BIN]` |
| **`sr9`** | `0x09` | Yes | **Cluster / USC task / local-memory base ID** (`SetLocalMemoryLocations`, `EmitInstanceAddressCalc`, `EmitGetInstanceOffset`, `Ray_GetRayStateOffset`) | — | New / Moved | `[V-BIN]` |
| **`sr11`** | `0x0b` | No | **Slot number** (`slot_num`, `EmitInstanceAddressCalc`, `EmitGetInstanceOffset`) | — | New / Moved | `[V-BIN]` |
| **`sr14`** | `0x0e` | No | **Instance / Subgroup lane number** (`inst_num`, `0..127` on 128-wide Volcanic! Used by `ConvertSubgroup*`, `ExpandFDSXY_FOP`, `EmitPerInstanceLoop`, `EmitInstanceAddressCalc`, `Ray_GetRayStateOffset`) | `51` | **MOVED (`51 → 14`)** | `[V-BIN]` |
| **`sr16`** | `0x10` | No | **Task / sub-slot offset** (`EmitInstanceAddressCalc`, `EmitGetInstanceOffset`) | — | New / Moved | `[V-BIN]` |
| **`sr17..20`** | `0x11..0x14` | No | **Tile Load Offset `0..3`** (`GetTileOffsetSR.auLDSpecialRegisters[0..3]`, `CopyPSOutputs`) | `32..35` (`pixout0..3`) | **REPLACED** | `[V-BIN]` |
| **`sr21..24`** | `0x15..0x18` | No | **Tile Store Offset `0..3`** (`GetTileOffsetSR.auSTSpecialRegisters[0..3]`, `CopyPSOutputs`) | — | **NEW** | `[V-BIN]` |
| **`sr33`** | `0x21` | No | **Pixel X coordinate** (`ExpandIterationInstructionToALU` mode 0, `ConvertInstToIntermediateF32`) | `49`/`96`/`114` | **MOVED** | `[V-BIN]` |
| **`sr34`** | `0x22` | No | **Centroid / Sample-mode-1 X coordinate** (`ExpandIterationInstructionToALU` mode 1) | — | **NEW** | `[V-BIN]` |
| **`sr35`** | `0x23` | No | **Pixel Y coordinate** (`ExpandIterationInstructionToALU` mode 0, `ConvertInstToIntermediateF32`) | `96` | **MOVED** | `[V-BIN]` |
| **`sr36`** | `0x24` | No | **Centroid / Sample-mode-1 Y coordinate** (`ExpandIterationInstructionToALU` mode 1) | — | **NEW** | `[V-BIN]` |
| **`sr38..41`** | `0x26..0x29` | No | **Tile Load Offset `4..7`** (`GetTileOffsetSR.auLDSpecialRegisters[4..7]`, `CopyPSOutputs`) | `164..167` (`pixout4..7`) | **REPLACED** | `[V-BIN]` |
| **`sr42..45`** | `0x2a..0x2d` | No | **Tile Store Offset `4..7`** (`GetTileOffsetSR.auSTSpecialRegisters[4..7]`, `CopyPSOutputs`) | — | **NEW** | `[V-BIN]` |
| **`sr46`** | `0x2e` | No | **80 ns Timer (low 32 bits)** (`ConvertReadTimer`) | `160` | **MOVED (`160 → 46`)** | `[V-BIN]` |
| **`sr47`** | `0x2f` | No | **80 ns Timer (high 32 bits)** (`ConvertReadTimer`) | — | **NEW (64-bit split)** | `[V-BIN]` |
| **`sr48`** | `0x30` | No | **Valid / live pixel mask (word 0 / mode A)** (`FixupDiscardWithSubgroups`, `CopyPSOutputs`) | — | **NEW** | `[V-BIN]` |
| **`sr49`** | `0x31` | No | **Valid / live pixel mask (word 1 / mode B)** (`FixupDiscardWithSubgroups`, `CopyPSOutputs`) | — | **NEW** | `[V-BIN]` |
| **`sr50`** | `0x32` | No | **Ray query status / proceed register** (`ConvertRayQueryProceed`) | — | **NEW (Volcanic RT)** | `[V-BIN]` |
| **`sr52`** | `0x34` | No | **Per-sample X offset** (`ExpandIterationInstructionToALU` mode 2) | — | **NEW** | `[V-BIN]` |
| **`sr53`** | `0x35` | No | **Per-sample Y offset** (`ExpandIterationInstructionToALU` mode 2) | — | **NEW** | `[V-BIN]` |
| **`sr54`** | `0x36` | No | **Coverage / sample mask word 0** (`ConvertSaveMaskInstruction`, `ExpandIterationInstructionToALU`) | — | **NEW** | `[V-BIN]` |
| **`sr55`** | `0x37` | No | **Coverage / sample mask word 1** (`ConvertSaveMaskInstruction`, `ExpandIterationInstructionToALU`) | — | **NEW** | `[V-BIN]` |
| **`sr56`** | `0x38` | No | **Local-memory address instance offset** (`SetLocalMemoryLocations`) | `112` | **MOVED (`112 → 56`)** | `[V-BIN]` |
| **`sr58`** | `0x3a` | Yes | **Cluster / Core number** (`GetCoreNumber` `[1,1,58,1,1]`, `EmitInstanceAddressCalc`, `EmitGetInstanceOffset`) | `45` | **MOVED (`45 → 58`)** | `[V-BIN]` |
| **`sr59`** | `0x3b` | No | **Ray state / instance slot** (`Ray_GetRayStateOffset`) | — | **NEW (Volcanic RT)** | `[V-BIN]` |
| `sr4,6,10,12,13,15,25..32,37,51,57,60` | — | No | Unreferenced in stock `libusc.so` paths (candidate: sample_num, tile_xy, perf/debug, Tess/Mesh IDs) | — | Unverified | `[OPEN]` |

### 3.2 Complete 128-Entry Volcanic Special Constant (`SC`) Table (`ISA_C_MAPPING_ARRAY`) `[V-BIN]`

Bank `8` (`sc0..sc127`, 128 × `u32`). Note that unlike Rogue (where `sc0..sc63` are simple integers `0..63` and `sc64..` are floats), **Volcanic completely reorganizes `SC`** — placing `0.0` at `sc0`, `1.0f` at `sc1`, packed FP16/graphics pairs in `sc2..sc31`, integer `1` at `sc33`, `0xff` at `sc34`, `0xffffffff` at `sc36`, `2.0f` at `sc40`, `0.5f` at `sc42`, `-1.0f` at `sc58`, sRGB transfer constants in `sc64..sc68`, `pi` at `sc116`, `2*pi` at `sc108`, `pi/2` at `sc105`:

| Idx | Hex (`u32`) | Note (f32 / int) | Idx | Hex (`u32`) | Note (f32 / int) | Idx | Hex (`u32`) | Note (f32 / int) | Idx | Hex (`u32`) | Note (f32 / int) |
|---:|---|---|---:|---|---|---:|---|---|---:|---|---|
| `0` | `0x00000000` | `0` / `0.0f` | `32` | `0x02041001` | bitmask/packed | `64` | `0x3d6147ae` | `0.055f` (sRGB) | `96` | `0x3b46fca9` | `0.0030363f` |
| `1` | `0x3f800000` | `1.0f` | `33` | `0x00000001` | `1` (int) | `65` | `0x3ed55555` | `1/2.4 ≈ 0.41667f` | `97` | `0x3b2dd769` | `0.00265261f` |
| `2` | `0x40003c00` | `f16(2.0,1.0)` | `34` | `0x000000ff` | `255` (`0xff`) | `66` | `0x414eb852` | `12.92f` (sRGB) | `98` | `0x3affffff` | `0.00195312f` |
| `3` | `0x42003800` | `f16(3.0,0.5)` | `35` | `0x00001000` | `4096` | `67` | `0x3f870a3d` | `1.055f` (sRGB) | `99` | `0x7fffffff` | `INT32_MAX` / abs |
| `4` | `0x34000000` | `f16(0.25,0.0)` | `36` | `0xffffffff` | `-1` (`~0u`) | `68` | `0x3b4d2e1c` | `0.0031308f` (sRGB) | `100` | `0x0730601c` | packed |
| `5` | `0x1c000400` | packed f16 | `37` | `0x3f13cd3a` | `1/sqrt(3)` | `69` | `0x41700000` | `15.0f` | `101` | `0x4b80e000` | packed / `1.689e7f` |
| `6` | `0xbc004000` | `f16(-1.0,2.0)` | `38` | `0x38d1b717` | `0.0001f` | `70` | `0x3e800000` | `0.25f` | `102` | `0x00000f60` | `3936` |
| `7` | `0x49004400` | `f16(10.0,4.0)` | `39` | `0x00000010` | `16` | `71` | `0x41800000` | `16.0f` | `103` | `0x400ccccd` | `2.2f` (gamma) |
| `8` | `0x4c005bf8` | `f16(16.0,255)` | `40` | `0x40000000` | `2.0f` | `72` | `0x42a00000` | `80.0f` | `104` | `0x40a00000` | `5.0f` |
| `9` | `0x3e004600` | `f16(1.5,6.0)` | `41` | `0xfd400803` | packed | `73` | `0x501890a0` | `1.02385e10f` | `105` | `0x3fc90fdb` | `pi/2` (`1.5708f`) |
| `10` | `0xbc004500` | `f16(-1.0,5.0)` | `42` | `0x3f000000` | `0.5f` | `74` | `0x3f333333` | `0.7f` | `106` | `0xfe281470` | packed |
| `11` | `0x48004d00` | `f16(8.0,20.0)` | `43` | `0x3f3504f3` | `1/sqrt(2)` | `75` | `0x48804480` | `f16(9.0,4.5)` | `107` | `0x41000000` | `8.0f` |
| `12` | `0x3c000000` | `f16(1.0,0.0)` | `44` | `0x3ed105ec` | `1/sqrt(6)` | `76` | `0x3f19999a` | `0.6f` | `108` | `0x40c90fdb` | `2*pi` (`6.28319f`) |
| `13` | `0x5c002c00` | `f16(256,0.0625)` | `45` | `0x00000002` | `2` (int) | `77` | `0x000000c8` | `200` | `109` | `0x3aa94f9a` | `0.00129174f` |
| `14` | `0x80000000` | `-0.0f` / signbit | `46` | `0x00000008` | `8` (int) | `78` | `0x0000001f` | `31` (`0x1f`) | `110` | `0x477fff00` | `65535.0f` |
| `15` | `0x38003c00` | `f16(0.5,1.0)` | `47` | `0x3dcccccd` | `0.1f` | `79` | `0x00000006` | `6` (int) | `111` | `0x0000000f` | `15` (`0xf`) |
| `16` | `0xc4804900` | `f16(-4.5,10.0)` | `48` | `0x00000018` | `24` (int) | `80` | `0x00000005` | `5` (int) | `112` | `0x3ccccccd` | `0.025f` |
| `17` | `0x30000001` | packed | `49` | `0x3e4ccccd` | `0.2f` | `81` | `0x3fb8aa3b` | `log2(e)` (`1.4427f`) | `113` | `0x3f4ccccd` | `0.8f` |
| `18` | `0x42485800` | packed f16 | `50` | `0x00000003` | `3` (int) | `82` | `0x3fb33333` | `1.4f` | `114` | `0x49800006` | packed |
| `19` | `0x3b461c04` | `0.00302291f` | `51` | `0x3a83126f` | `0.001f` | `83` | `0x40400000` | `3.0f` | `115` | `0x00010000` | `65536` |
| `20` | `0x10002e66` | packed | `52` | `0x0000ffff` | `65535` (`0xffff`) | `84` | `0x3ea8f5c3` | `0.33f` | `116` | `0x40490fdb` | `pi` (`3.14159f`) |
| `21` | `0x3a003266` | packed | `53` | `0x3b808081` | `1/255` | `85` | `0x3ecccccd` | `0.4f` | `117` | `0x358637bd` | `1e-6f` |
| `22` | `0xb8004700` | `f16(-0.5,7.0)` | `54` | `0x3f5105ec` | `sqrt(2/3)` | `86` | `0x4019999a` | `2.4f` (sRGB) | `118` | `0x6424067c` | packed |
| `23` | `0x389e0100` | packed | `55` | `0x20803c0c` | packed | `87` | `0x3e19999a` | `0.15f` | `119` | `0x3cbd9b6c` | `0.0231454f` |
| `24` | `0x54000102` | packed | `56` | `0x3e99999a` | `0.3f` | `88` | `0x3f72a76f` | `0.947867f` | `120` | `0x3c9cf09c` | `0.0191577f` |
| `25` | `0x28005640` | packed | `57` | `0x3c008081` | `2/255` | `89` | `0x3d9e8391` | `0.0773994f` | `121` | `0x3c460b7d` | `0.0120877f` |
| `26` | `0x344a3c00` | packed | `58` | `0xbf800000` | `-1.0f` | `90` | `0x3d55891a` | `0.0521327f` | `122` | `0x3c163b8b` | `0.00916947f` |
| `27` | `0x8000c248` | packed | `59` | `0x00000007` | `7` (int) | `91` | `0x3d25aee6` | `0.04045f` (sRGB) | `123` | `0x3bd3097d` | `0.00644034f` |
| `28` | `0x33f83c00` | packed | `60` | `0x3d4ccccd` | `0.05f` | `92` | `0x3cbc2d0d` | `0.0229707f` | `124` | `0x3bcfdf10` | `0.00634373f` |
| `29` | `0xc0002000` | `f16(-2.0,1/128)` | `61` | `0x00000004` | `4` (int) | `93` | `0x3c37457c` | `0.011186f` | `125` | `0x3b9dafbe` | `0.00481221f` |
| `30` | `0x211f3555` | packed | `62` | `0x3c23d70a` | `0.01f` | `94` | `0x3bee0683` | `0.00726396f` | `126` | `0x3b357a76` | `0.00276914f` |
| `31` | `0x410021b0` | packed | `63` | `0x3eaaaaab` | `1/3` (`0.33333f`) | `95` | `0x3b85da0a` | `0.00408483f` | `127` | `0x3b09aa38` | `0.0021006f` |

### 3.3 `ISA_CTRL` (Type `14`, Prefix `0x1c` / `0x08`) — Bit-Exact Specification `[V-BIN]` `[V-ROUNDTRIP]`

#### 3.3.1 `ISA_CTRL` Opcode Table (`W0[9:6]`, `0..13`; `14..15` Invalid)

| `OP` | Mnemonic | Description & Key Subfields | Provenance |
|---:|---|---|---|
| `0` | **`BR`** (`CALL` if `link=1`) | Relative branch (`signed 30-bit` dword offset in `IMM[29:0]`). Condition in `BCC`. If `link=1` (`W1[13]`), saves return PC into `DST` (`sl0..sl15`). | `[V-BIN]` `[V-ROUNDTRIP]` |
| `1` | **`BA`** | Absolute branch (`unsigned 30-bit` dword address `<= 0x3ffffffe` in `IMM[29:0]`, verified in `EncodeBranch`). | `[V-BIN]` |
| `2` | **`BRLT`** | Loop-counter relative branch (`signed 30-bit` dword offset in `IMM[29:0]`; decrements/tests `DST` `SLOT` counter `sl0..sl15`; `BCC=1`). | `[V-BIN]` |
| `3` | **`LAPC`** (rel) | Load/add PC into `DST` (`SLOT` register, `ConvertLAPCToUscasm` opcode `0x97`). | `[V-BIN]` |
| `4` | **`SAVL` / `LAPC_ABS`** | Save link / absolute PC capture (`ConvertLAPCToUscasm` opcode `0x98`). | `[V-BIN]` |
| `5` | **`BREAKPOINT`** | Debug trap. Uses compact prefix `0x08` (`CTRL_BREAKPOINT`): `W0[7:6]=fence_wait`, `W0[9:8]=0`, `W0[12:10]=fence_set`, `W0[30:13]=bp_imm[17:0]`. | `[V-BIN]` |
| `6` | **`LIMM`** | Load 32-bit immediate (`IMM[31:0]`) into `DST` (`sl0..sl15`, `bank=2`). Sets `BCC=4`. | `[V-BIN]` |
| `7` | **`OLCHK`** | Pixel overlap / ISP feedback check (`ConvertOLCHKToUscasm`; sub-mode `1/2/3` encoded in `IMM`). Observed `1,334` times in `rgx.sh`. | `[V-BIN]` `[V-ROUNDTRIP]` |
| `8` | **`CTRL_OP8`** | Valid in `check_ISA_CTRL` (`OP <= 13`), unreferenced in `libusc.so` paths (candidate: `DITR` / `KILL` / `SLEEP` / `IDLE`). | `[OPEN]` |
| `9` | **`MUTEX`** | Hardware mutex lock/release (`W1[27:24]=mutex_id (0..15)`, `W1[28]=release/lock`, `W1[23]=flag_b`, `BCC=1`). | `[V-BIN]` |
| `10` | **`NOP`** (`TRIGGER` if `W1[29]=1`) | No-op & multi-dword alignment padding (`min_words=1..4`). With `W1[29]=1` & `BCC=1`: `TRIGGER` (`ConvertTRIGGERToUscasm`). | `[V-BIN]` `[V-ROUNDTRIP]` |
| `11` | **`PHAS`** | Next-phase dispatch (`W1[30]=phase_rate` `0=instance/1=sample`, `W1[22]=end_flag`, `DST=slN`). | `[V-BIN]` |
| `12` | **`END`** | End of shader program (`0x8000031c` = 1-dword `END` with `w=0, s=0`). Every real `rgx.sh` & embedded shader ends with `CTRL.END`. | `[V-BIN]` `[V-ROUNDTRIP]` |
| `13` | **`WGBARRIER`** | Workgroup execution barrier (`ConvertWGBarrier`, `BCC=0`). | `[V-BIN]` |

#### 3.3.2 `ISA_CTRL_ENC` (`0x1c`) Complete 32-Bit Word Layouts (`32/32` Bits Accounted Per Word) `[V-BIN]` `[V-ROUNDTRIP]`

- **Word 0 (`W0`)**:
  - `[5:0]` = `0x1c` (prefix)
  - `[9:6]` = `OP` (`0..13`)
  - `[11:10]` = `fence_wait` (`0..3`)
  - `[12]` = `BCC[1]`
  - `[30:13]` = 18 permuted bits of `IMM` (table below)
  - `[31]` = `EOI`
- **Word 1 (`W1`, omitted if `W1[30:0]==0 && W2[30:0]==0`)**:
  - `[9:0]`, `[12:11]`, `[17]`, `[19]` = remaining 14 permuted bits of `IMM`
  - `[10]` = `BCC[0]`, `[15]` = `BCC[2]` (3-bit `BCC ∈ {0..6}`, `7` invalid; `0=uncond/all`, `1=all_exec/active`, `2=any_exec/p0` observed on `139/139` `BR` instructions, `4=LIMM` sentinel; exact full `BCC 3,5,6` predicate senses `[OPEN]` for diff probe)
  - `[13]` = `link` (`CALL` return link enable)
  - `[14]` = `DST[0]`, `[16]` = `DST[1]`
  - `[18]` = `fence_set[0]`, `[21:20]` = `fence_set[2:1]`
  - `[22]` = `phas_end`
  - `[23]` = `flag_b` (mutex/phas modifier)
  - `[27:24]` = `mutex_id` (`0..15`)
  - `[28]` = `flag_a` (mutex lock/release)
  - `[29]` = `trigger` (NOP trigger flag)
  - `[30]` = `phase_rate` (`0..1`)
  - `[31]` = `EOI`
- **Word 2 (`W2`, omitted if `DST < 4` and `min_words <= 2`)**:
  - `[1:0]` = `DST[3:2]` (forming 4-bit `DST` `sl0..sl15`)
  - `[30:2]` = `0` (must be zero; non-zero rejected by `decode_ISA_CTRL`)
  - `[31]` = `EOI`
- **Word 3 (`W3`, only when `min_words == 4`)**:
  - `[30:0]` = `0` (must be zero), `[31]` = `EOI`

#### 3.3.3 Exact 32-Bit `IMM` / Branch-Offset Bit Permutation Table `[V-BIN]` `[V-ROUNDTRIP]`

Verified bit-for-bit from `encode_ISA_CTRL_ENC` / `decode_ISA_CTRL` AND proven by `139/139` real relative `BR` targets landing strictly on instruction boundaries:

| `IMM` Bit | Word:Bit | `IMM` Bit | Word:Bit | `IMM` Bit | Word:Bit | `IMM` Bit | Word:Bit |
|---:|---|---:|---|---:|---|---:|---|
| `0` | `W0[16]` | `8` | `W1[9]` | `16` | `W0[30]` | `24` | `W0[20]` |
| `1` | `W0[14]` | `9` | `W1[12]` | `17` | `W0[29]` | `25` | `W0[27]` |
| `2` | `W0[13]` | `10` | `W1[11]` | `18` | `W0[26]` | `26` | `W0[25]` |
| `3` | `W0[15]` | `11` | `W1[8]` | `19` | `W0[19]` | `27` | `W1[0]` |
| `4` | `W0[17]` | `12` | `W1[6]` | `20` | `W0[21]` | `28` | `W1[2]` |
| `5` | `W0[18]` | `13` | `W1[5]` | `21` | `W0[24]` | `29` | `W1[1]` |
| `6` | `W0[28]` | `14` | `W1[4]` | `22` | `W0[23]` | `30` | `W1[17]` |
| `7` | `W1[7]` | `15` | `W1[3]` | `23` | `W0[22]` | `31` | `W1[19]` |

### 3.4 `ISA_COND` (Type `15`, Prefix `0x1e`) — Execution Predication (`CND*`) `[V-BIN]`

Volcanic maps Rogue's `CNDST` / `CNDEF` / `CNDSM` / `CNDLT` / `CNDEND` execution-mask instructions into the dedicated `ISA_COND` (`0x1e`) family (default `W1 = 0x00000020`, `1..2` words, pad-extensible to 4):

- **Opcode Mapping (`OP ∈ 0..5`, `6..7` invalid)** from `0x31340`, `ConvertINITEMCToUscasm`, `ConvertRESET_EPToUscasm`:
  - `0`: **`CNDST`** (also `INITEMC` with `adj=0, neg0=1` — start conditional / initialize EMC)
  - `1`: **`CNDEF`** (else-flip execution mask)
  - `2`: **`CNDSM`** (also `RESET_EP` / `CNDSMDISCARD` — set mask / discard)
  - `3`: **`CNDLT`** (loop test)
  - `4`: **`CNDEND`** (end conditional block, decrement EMC by `adj`)
  - `5`: **`CNDLTB` / `CND_SWITCH`** (extended loop/switch mask op from `0x31340[7]`)
- **Complete `ISA_COND` Bitfield Layout (`32/32` Bits Accounted Per Word)**:
  - **`W0`**:
    - `[5:0]` = `0x1e` (prefix)
    - `[6]` = `cc2[0]`, `[14]` = `cc2[1]` (2-bit secondary/combine condition mode `0..3`)
    - `[7]` = `OP[2]`, `[10]` = `OP[0]`, `[15]` = `OP[1]` (3-bit `OP ∈ 0..5`)
    - `[8]` = `SF[0]`
    - `[9]` = `fence_wait[0]`, `[11]` = `fence_wait[1]` (2-bit wait fence `0..3`)
    - `[13:12]` = `p0[1:0]`, `[20]` = `p0[2]` (3-bit predicate reg `0..6`; `7` invalid)
    - `[16]` = `neg0` (1-bit negate `p0`)
    - `[17]` = `neg1` (1-bit negate `p1`)
    - `[18]` = `p1[0]`, `[23:22]` = `p1[2:1]` (3-bit predicate reg `0..6`; `7` invalid)
    - `[19]` = `adj[1]`, `[21]` = `adj[0]`, `[28:24]` = `adj[6:2]` (low 7 bits of 11-bit `EMC` adjust `0..2047`)
    - `[30:29]` = `CC` (2-bit execution condition `0..3`)
    - `[31]` = `EOI`
  - **`W1`** (default `0x00000020` when omitted, i.e., `adj[10:7]=0`, `SF=4`, `fence_set=0`):
    - `[3:0]` = `adj[10:7]` (upper 4 bits of 11-bit `adj`)
    - `[5:4]` = `SF[2:1]` (combined with `W0[8]` → 3-bit `SF ∈ 0..5`)
    - `[8:6]` = `fence_set` (3-bit set fence `0..7`)
    - `[30:9]` = `0` (must be zero)
    - `[31]` = `EOI`

### 3.5 Memory Sync (`IDF` & `MEMBARRIER`) via `ISA_DMA` (Type `6`, Prefix `0x0e`) `[V-BIN]`

Volcanic encodes both `IDF` (Issue Data Fence) and `MEMBARRIER` as `ISA_DMA` instructions (`W0[5:0]=0x0e`) with `DMA_OP = 0` and unused data/dst/addr operands (`NONE`), using contiguous word fields verified from `decode_ISA_DMA` & `ConvertIDFToUseasm` / `ConvertMEMBARRIERToUseasm`:

- `W0[5:0]` = `0x0e` (prefix), `W0[7:6]` = `CC`, `W0[10:8]` = `SF`, `W0[13:11]` = `fence_set`, `W0[15:14]` = `fence_wait`
- `W0[19:16]` = `DMA_SUBOP` / `ATOMOP` (`0..10`), `W0[24:21]` = cache/coherency op (`MEMBARRIER`: `0=release/inv`, `1=acquire`, `2=acq_rel`, `3=none`)
- `W3[26]` & `W3[27]` = fence/barrier scope flags (`IDF` sets per-pipe flush bits; `MEMBARRIER` selects device/workgroup/subgroup scope pairs).
- Full `LD`/`ST`/`ATOM` `ISA_DMA` operand layouts are detailed in companion `VOLCANIC-USC-OPCODES.md`.

---

## 4. Live Binary Round-Trip Verification Proof `[V-ROUNDTRIP]`

Clean-room Python decoder/encoder (`freeusc_roundtrip.py`) was executed against **two independent sets of real shipping Volcanic hardware shader binaries**:

### 4.1 Aggregate Verification Results

| Binary Corpus | Shaders Verified | Total Instructions | Framing / Prefix Errors | `ISA_CTRL` Decode→Encode Round-Trip | Relative `BR` Target Landings |
|---|---:|---:|---:|---:|---:|
| **`rgx.sh.71.2.2448.1212`** (`DXT-48-1536`, `1,342` primary + `36` secondary) | **1,378 / 1,378** | **30,495** | **0** | **3,202 / 3,202 PASS (0 FAIL)** | **8 / 8 VALID (0 invalid)** |
| **`25.3@6908880` embedded USC blobs** (`549` hardware shaders) | **549 / 549** | **8,472** | **0** | **1,385 / 1,385 PASS (0 FAIL)** | **131 / 131 VALID (0 invalid)** |
| **Combined Total** | **1,927 / 1,927** | **38,967** | **0** | **4,587 / 4,587 PASS (0 FAIL)** | **139 / 139 VALID (0 invalid)** |

### 4.2 Real `DXT-48-1536` (`rgx.sh.71.2.2448.1212`) Instruction Statistics `[V-ROUNDTRIP]`

- **Packet Word-Length Distribution** (`30,495` instructions):
  - `1 dword (4 B)`: `7,215` (`23.7%`)
  - `2 dwords (8 B)`: `13,527` (`44.4%`)
  - `3 dwords (12 B)`: `7,711` (`25.3%`)
  - `4 dwords (16 B)`: `2,042` (`6.7%`)
- **Family Breakdown**: `BIT`: `9,888` | `FOP`: `9,373` | `INT`: `4,801` | `CTRL`: `3,202` | `PSB_WR`: `1,502` | `ITR`: `629` | `MOV`: `614` | `PSB_RD`: `486` (embedded corpus additionally exercises `TAP`: `50`).
- **`CTRL` Breakdown (`rgx.sh`)**: `END`: `1,378` (100% of shaders terminate with `CTRL.END`; fence wait distribution: `w=0`: `1,334`, `w=2`: `26`, `w=1`: `18`) | `OLCHK`: `1,334` | `NOP`: `482` (`120` × 1w, `362` × 2w) | `BR`: `8` (`bcc=2`).

### 4.3 Annotated Sanitized Decode Excerpt (Real `DXT-48-1536` Shader with Forward `CTRL.BR`) `[V-ROUNDTRIP]`

Excerpt from `rgx.sh.71.2.2448.1212` primary shader `#0530` (`363` dwords, `131` instructions), demonstrating bit-31 framing, multi-word packet boundaries, `CTRL.BR` 32-bit `IMM` unscrambling landing strictly on instruction `@360` (`+336` dwords from `@24`), inline wait fences, and `CTRL.END`:

```text
  @00: [4301e1e0 d091a13a         ]  INT     (2w, bit31 set on W1)
  @02: [40938aca 00001810 80400038]  MOV     (3w, bit31 set on W2)
  @05: [408781ca 00001810 80400038]  MOV     (3w)
  ...
  @23: [8001535e                  ]  BIT     (1w, bit31 set on W0)
  @24: [1002181c 80000200         ]  CTRL.BR w=2 s=0 bcc=2 rel=+336 -> @360 link=0
       # W0=0x1002181c: prefix=0x1c, OP=0(BR), wait=2, BCC[1]=1, IMM bits -> {4, 6} = 0x50
       # W1=0x00000200: IMM bit 8 -> 0x100  ==>  0x100 | 0x50 = +0x150 = +336 dwords!
       # Target = 24 + 336 = dword @360 (exact instruction start!)
  @26: [16602278 a5b63848         ]  BIT     (2w, loop/branch body...)
  ...
  @360: [80000a9c                 ]  CTRL.NOP w=2 s=0   <-- exact BR target!
  @361: [8000109e                 ]  BIT     (1w)
  @362: [8000071c                 ]  CTRL.END w=1 s=0   <-- program terminator
```

---

## 5. Mesa Phase 4 Implementation Blueprint (`pco_isa_volcanic.py` / `pco_binary.c`)

1. **Separate Backend Emitter (`pco_binary_volcanic.c`)**: Do **not** bolt Volcanic onto Rogue's byte-stream `pco_igrp` (`hdr + phases + srcs + iss + dsts + 0xff pad`). Volcanic emits a flat array of `uint32_t` words per instruction (`1..4` dwords):
   - Pack payload into `uint32_t w[4]` initialized to family defaults (Section 1.2).
   - Trim trailing words matching family defaults down to `max(1, min_words)`.
   - Set `w[len - 1] |= 0x80000000u`.
   - Pad aligned labels using `CTRL.NOP` (`0x0000029c`, `min_words = min(rem, 4)`).
2. **Canonical Bank Tables (`pco_isa_volcanic.py`)**: Define `VOLCANIC_BANK` (`0..15`) and codegen per-operand `encode_slot(bank, off)` / `decode_slot(raw)` helpers directly from the Section 2.2 tables.
3. **Fence Allocation**: Replace Rogue `WDF` insertion pass with inline `(fence_wait, fence_set, sf)` tagging on every instruction; emit `ISA_DMA` `IDF`/`MEMBARRIER` for memory/cache ordering and `CTRL.WGBARRIER` (`op=13`) for workgroup barriers.
4. **Remaining `[OPEN]` Items for On-Device `tools/freeusc/volcanic_bitdiff.py` Sweep**:
   - Confirm exact predicate semantics of `ISA_CTRL.BCC` values `3, 5, 6` and `ISA_CTRL.OP=8` via `probes/spirv/` differential compilation on Pixel 10 (`DXT-48-1536`) / Pixel 11 (`CXTP-48-1536 MC1`).
   - Confirm any unreferenced `SR` indices (`sr4,6,10,12,13,15,25..32,37,51,57,60`) via compute/fragment built-in probes (`gl_SampleID`, `gl_SubgroupSize`, `gl_NumWorkGroups`).
   - Run full `COND` (`0x1e`) live-binary round-trip on divergent-branch SPIR-V probes captured via `VK_KHR_pipeline_binary` (since stock `rgx.sh` shaders were branch-light and emitted zero `COND` packets).
