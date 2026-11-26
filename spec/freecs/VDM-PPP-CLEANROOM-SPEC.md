# FreePVR / FreeCS — PowerVR Volcanic (`DXT-48-1536` / `CXTP-48-1536`) Clean-Room Command-Stream & Control-Register Specification

> **Program**: `FreePVR` — Track 1 (`FreeCS`: Command Stream & Control Registers)  
> **Target Silicon**: PowerVR Volcanic `DXT-48-1536` (`BVNC 71.2.2448.1212`, Tensor G5) & `CXTP-48-1536 MC1` (Tensor G6)  
> **Driver Baseline Analyzed**: Stock Volcanic DDK `25.3@6908880` (`vulkan`, `glesv2`, `srv_um`, GPL `pvrsrvkm`) vs. upstream Mesa `pvr` / `pco` (`src/imagination/csbgen/{rogue,volcanic}/`)  
> **Clean-Room Policy**: Architectural specification only — derived from clean-room behavioral & binary interface analysis and GPL kernel interfaces. Contains **zero** vendor source code, proprietary headers, or binary blobs.

---

## Provenance & Delta Legend

| Tag | Meaning |
| --- | --- |
| `[V-GPL]` | Confirmed directly from GPL-licensed kernel driver (`pvrsrvkm`) MMIO/reset/init sequences |
| `[V-BIN]` | Confirmed directly from stock Volcanic `25.3@6908880` command-stream/state emitters and `.rodata` lookup tables |
| `[OPEN]` | Exact bit name or unused sub-bit not individually toggled in observed emitters; hardware bit position/mask bounded where noted |

| Delta Tag | Meaning (vs. Mesa `rogue/*.xml`, or vs. upstream `volcanic/*.xml` where noted) |
| --- | --- |
| `IDENTICAL` | Same bit range, width, shift, and encoding |
| `MOVED` | Same logical field relocated to different bit positions or word |
| `WIDENED` | Field expanded in bit width (commonly for 32K surfaces, 48-bit VA, or 8-bit counts) |
| `NEW` | Field or entire packet/register absent on Rogue (or absent in upstream `volcanic/*.xml`) |
| `CHANGED` | Encoding or semantics materially altered (e.g., indirect shader descriptor table) |
| `REMOVED` | Legacy Rogue field/word no longer emitted on `DXT-48-1536` |

> **Bit Range Convention**: All ranges use Mesa `csbgen` XML inclusive `[end:start]` notation (MSB:LSB within the owning 32-bit dword or 64-bit qword; `length` is in 32-bit dwords).

---

## Gap Closure Scorecard

| Gap ID | Target Surface | Status | Architectural Delta vs. Rogue / Upstream Mesa | Provenance |
| --- | --- | --- | --- | --- |
| `[GAP-CSB-VDM]` | `csbgen/volcanic/vdm.xml` (DCE 64-bit stream) | **CLOSED** | **Complete architectural break**: Rogue 32-bit `VDMCTRL` replaced by **two-level 64-bit QWORD DCE (Draw Call Engine)** (`Handle Buffer` + 10 opcodes `0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0c`) + indirect/indexed argument descriptors | `[V-BIN]` (3 minor `[OPEN]` sub-flags) |
| `[GAP-CSB-PPP]` | `csbgen/volcanic/ppp.xml` (`TA` / `PPP_STATE`) | **CLOSED** | Header layout preserved, but **PDS state split into indirect 16B Pixel Shader Descriptor Table** (`HEADER[7..8]`), **FSR packed in `HEADER[8]`**, `ISPCTL.upass` widened to 8b (`[27:20]`), `ISPA.passtype` moved (`[27:25]`), `tagwritedisable` moved `ISPCTL[21] -> ISPA[24]`, 10-bit 32K tile clips | `[V-BIN]` (2 minor `[OPEN]` bits) |
| `[GAP-CR-SOFT-RESET]` | `csbgen/volcanic/cr.xml` (`SOFT_RESET`) | **CLOSED (boot-ready)** | Exact 64-bit `SOFT_RESET` (`0x0100`) valid mask `0x7fffffe0_007bdeff`, `SOFT_RESET2` (`0x0108`) mask `0xffffffff`, SPU/Jones reset regs (`0x0630/0x0640/0x0650`), bit `37` = FW CPU (`GARTEN_EN`), and exact 6-step `RGXStart` sequence | `[V-GPL]` (individual domain names outside bit 37 `[OPEN]`) |
| `[GAP-CR-ISP-S7]` | `csbgen/volcanic/cr.xml` (`S7_TOP` ISP / ZLS) | **CLOSED** | 32K screen (`15b` coords), `ISP_CTL` tiles-in-flight at `[31:16]`, `ISP_AA`/`TE_AA` unchanged, **ZLS v2 split into 32-bit `ISP_ZLSCTL` + 3×64-bit `ZLS_WORD0..2`** | `[V-BIN]` (1 `[OPEN]` sub-bit) |
| `[GAP-CR-PBE-WORDS]` | `csbgen/volcanic/pbestate.xml` | **CLOSED** | Upstream XML only had `STATE_WORD0` (with wrong `shift=2`!). Full **3×32-bit `STATE_WORD0..2`** (byte-addressed 48-bit VA) + **3×64-bit `REG_WORD0..2`** (16-bit clip rects, 16-bit stride, 5-bit log2 dims) | `[V-BIN]` |
| `[GAP-ISA-6]` | `csbgen/volcanic/texstate.xml` & `pco/usclib/tex.cl` | **CLOSED** | Unblocks `usclib_volcanic_tex_state_samples` (`IMAGE_WORD2[7:6]` = `smpcnt`). **Critical upstream XML bug found**: on `DXT-48-1536`, `IMAGE_WORD0` `width=[41:27]` (15b) & `height=[56:42]` (15b), `IMAGE_WORD1` is raw byte VA (`[44:0]` + `[45]=VA[47]`), plus missing `IMAGE_WORD3` & `SAMPLER_WORD1` | `[V-BIN]` |
| `cdm.xml` / `pds.xml` Delta Check | `csbgen/volcanic/{cdm,pds}.xml` | **CLOSED** | **`pds.xml` matches 100%**. **`cdm.xml` has 5 critical deltas**: `KERNEL0` `block_type=3` (extended/fence), `usc_unified_size` widened to 8b (`[19:12]`), missing `KERNEL0` `[28]/[27]/[21:20]`, missing `KERNEL1[1]` (`usc_common_shared`), missing `KERNEL8` `usc_common_size[10:0]` & `event_type[12:11]`; `LMTerminateWords` is a no-op | `[V-BIN]` |

---

## 1. `[GAP-CSB-VDM]` — Volcanic Geometry Control Stream (64-bit QWORD DCE)

### 1.1 Architectural Shift: Rogue 32-bit `VDMCTRL` → Volcanic 64-bit Two-Level DCE

On PowerVR Volcanic (`DXT-48-1536` / `CXTP-48-1536`), Rogue's 32-bit `VDMCTRL` stream (`PPP_STATE_UPDATE`, `PDS_STATE_UPDATE`, `VDM_STATE_UPDATE`, `INDEX_LIST`, `STREAM_LINK`, `STREAM_RETURN`, `STREAM_TERMINATE`) is **replaced** by the **Draw Call Engine (DCE)**:

1. **Top-Level Handle Buffer (Component Stream Table)**:
   - Command buffers do **not** emit inline `STREAM_LINK` or `STREAM_RETURN` packets.
   - Instead, each contiguous 64-bit QWORD component stream is registered in a resident **Handle Buffer** array (1 QWORD per component stream). Secondary command buffers are linked by appending component-stream handle entries into the primary handle buffer.
2. **Component Control Streams (64-bit QWORD Packets)**:
   - Every DCE command begins on an 8-byte (QWORD) boundary with an **8-bit opcode in `QWORD0[63:56]`**.
   - Stream termination is performed by emitting a `DCE_PPP_STATE_UPDATE` (`0x01`, `word_count=3`) pointing to a 3-dword `PPP_STATE` containing `STATE_HEADER.pres_terminate = 1` (`0x02000000`) followed by `STATE_TERMINATE0/1`.

> **Mesa `csbgen` Recommendation**: Create `src/imagination/csbgen/volcanic/vdm.xml` (or `dce.xml` with prefix `DCE` / `VDMCTRL2`) using 64-bit `length="2"` base structs per QWORD so `csbgen` packs fields natively at `[63:0]`.

### 1.2 Handle Buffer & Residency Descriptors (`[V-BIN]`)

| Struct / Packet | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `DCE_HANDLE_ENTRY` | 2 (1 QW) | `qword_count` | `[63:32]` | `uint` (`dword_count >> 1`; count of 64b QWORDs in component stream) | `NEW` | `[V-BIN]` |
| `DCE_HANDLE_ENTRY` | 2 (1 QW) | `stream_addr` | `[31:0]` *(see note)* | `shift="3"`, `address` (`(dev_va + 7) >> 3`, 8-byte aligned VA) | `NEW` | `[V-BIN]` |
| `DCE_RESIDENT_DESC0` | 2 (1 QW) | `resident_enable` | `[45:45]` | `bool` (set to `1`, `1ULL << 45`) | `NEW` | `[V-BIN]` |
| `DCE_RESIDENT_DESC0` | 2 (1 QW) | `handle_buf_addr` | `[36:0]` | `shift="3"`, `address` (`(addr + 7) >> 3`, 40-bit VA) | `NEW` | `[V-BIN]` |
| `DCE_RESIDENT_DESC1` | 2 (1 QW) | `desc_valid` | `[48:48]` | `bool` (set to `1`, `1ULL << 48`) | `NEW` | `[V-BIN]` |
| `DCE_RESIDENT_DESC1` | 2 (1 QW) | `desc_addr` | `[39:3]` | `shift="0"` (8-byte aligned raw VA, `(addr + 7) & ~7`) | `NEW` | `[V-BIN]` |

> **Address-width note on `DCE_HANDLE_ENTRY`**: The emitter computes `((uint64_t)(dwords >> 1) << 32) | ((dev_va + 7) >> 3)`. Because `dev_va >> 3` is OR'd directly with `qword_count << 32`, component-stream allocations on the VDM/DCE heap must stay within the lower 35-bit VA window (`< 32 GiB`, so `(dev_va >> 3) < 2^32`) to prevent overlap into `[63:32]`.

### 1.3 Complete DCE QWORD Opcode Map (`QWORD0[63:56]`)

| Opcode | Packet Name | Size (QWs / DWs) | Purpose | Rogue Equivalent | Provenance |
| --- | --- | --- | --- | --- | --- |
| `0x01` | `DCE_PPP_STATE_UPDATE` | 1 QW (2 DW) | Emit PPP state buffer (also used for stream terminate) | `VDMCTRL_PPP_STATE0..1` | `[V-BIN]` |
| `0x03` | `DCE_TESS_DDM_CONFIG` | 1 QW (2 DW) | Tessellation domain / partitioning / output topology config | `VDMCTRL_VDM_STATE` (tess) | `[V-BIN]` |
| `0x04` | `DCE_SHADER_STATE_UPDATE` | 3 QW (6 DW) | Stage shader + PDS + CAM + unified/local-mem setup (VS/HS/DS/GS/DDM/Sync) | `VDMCTRL_VDM_STATE0..5` + `PDS_STATE0..2` | `[V-BIN]` |
| `0x05` | `DCE_PREDICATE_WRAPPER` | 2 QW (4 DW) + body | Conditional rendering wrapper around draw commands | `VDMCTRL_CONTROL` (cond) | `[V-BIN]` |
| `0x06` | `DCE_DRAW_COMMAND` | 3–5 QW (6–10 DW) | Direct / indirect / indexed / multi-draw primitive kick | `VDMCTRL_INDEX_LIST0..9` | `[V-BIN]` |
| `0x07` | `DCE_DEPTH_BIAS_UPDATE` | 2 QW (4 DW) | Inline f32 constant, slope, clamp depth bias | `TA_STATE_ISPDBIAS` (in PPP) | `[V-BIN]` |
| `0x08` | `DCE_SAMPLE_MASK_UPDATE` | 1 QW (2 DW) | Inline 32-bit sample mask update | New (dynamic MSAA mask) | `[V-BIN]` |
| `0x09` | `DCE_SCISSOR_ARRAY_UPDATE` | `1 + N` QW | Inline packed 4×u16 scissor or viewport-clip rectangles | `TA_STATE_ISPSCISSOR` (in PPP) | `[V-BIN]` |
| `0x0a` | `DCE_SCISSOR_DBIAS_EMIT` | 1 QW (2 DW) | Commit updated scissor/depth-bias/mask state to ISP | `TA_STATE_ISPDBSC` (in PPP) | `[V-BIN]` |
| `0x0c` | `DCE_BARRIER` | 1 QW (2 DW) | Geometry pipeline barrier / sync point | `VDMCTRL_CONTROL` (barrier) | `[V-BIN]` |

### 1.4 Per-Packet Bitfield Tables (`[V-BIN]`)

#### `0x01` — `DCE_PPP_STATE_UPDATE` (1 QWORD = 2 DWords)

| Struct / Packet | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `DCE_PPP_STATE_UPDATE` | 2 | `opcode` | `[63:56]` | `uint` = `0x01` | `NEW` | `[V-BIN]` |
| `DCE_PPP_STATE_UPDATE` | 2 | `word_count` | `[55:46]` | `uint` (10-bit dword count; terminate = `3`) | `WIDENED` (8b→10b) | `[V-BIN]` |
| `DCE_PPP_STATE_UPDATE` | 2 | `addr` | `[45:0]` | `shift="2"`, `address` (48-bit dword-aligned VA) | `WIDENED` (40b→48b) | `[V-BIN]` |

#### `0x03` — `DCE_TESS_DDM_CONFIG` (1 QWORD = 2 DWords)

| Struct / Packet | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `DCE_TESS_DDM_CONFIG` | 2 | `opcode` | `[63:56]` | `uint` = `0x03` | `NEW` | `[V-BIN]` |
| `DCE_TESS_DDM_CONFIG` | 2 | `tess_mode` | `[5:0]` | `uint` (packed domain + partitioning + output topology) | `NEW` | `[OPEN]` (exact per-bit enum split) |

#### `0x04` — `DCE_SHADER_STATE_UPDATE` (3 QWORDs = 6 DWords)

Used for **Primary geometry stages** (`stage = 0..3`: VS, HS/TCS, DS/TES, GS), **Secondary DDM / Constant-Calc tasks** (`stage = 4`, header high word `0x0484`), and **Barrier / Predicate Sync tasks** (`stage = 4`, header high word `0x0490` / `0x049a`).

| Struct / Packet | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `DCE_SHADER_STATE0` | 2 | `opcode` | `[63:56]` | `uint` = `0x04` | `NEW` | `[V-BIN]` |
| `DCE_SHADER_STATE0` | 2 | `stage` | `[55:53]` | `enum`: `0`=VS, `1`=HS/TCS, `2`=DS/TES, `3`=GS, `4`=DDM_SECONDARY/SYNC | `NEW` | `[V-BIN]` |
| `DCE_SHADER_STATE0` | 2 | `sync_or_pred_flags` | `[52:51]` | `uint` (`bit 52` set in barrier/sync `0x0490`/`0x049a`; `bit 51` set in `0x049a`) | `NEW` | `[OPEN]` |
| `DCE_SHADER_STATE0` | 2 | `stage_aux_flag` | `[50:50]` | `bool` (`1` in GS-with-tess `0x0466` vs `0x0462`, and in SecondaryDDM `0x0484`) | `NEW` | `[V-BIN]` |
| `DCE_SHADER_STATE0` | 2 | `present` | `[49:49]` | `bool` (`1` = stage enabled; `0x0402_0000_0000_0000` for VS) | `NEW` | `[V-BIN]` |
| `DCE_SHADER_STATE0` | 2 | `max_instances_m1` | `[48:41]` | `uint` (`max_instances - 1`, 8-bit) | `NEW` | `[V-BIN]` |
| `DCE_SHADER_STATE0` | 2 | `prim_id_enable` | `[39:39]` | `bool` (`1ULL << 39`) | `NEW` | `[V-BIN]` |
| `DCE_SHADER_STATE0` | 2 | `flatshade_ctrl` | `[38:37]` | `enum`: `0`=default, `1`=first, `2`=last (`0x20`/`0x40 << 32`) | `MOVED` (from `INDEX_LIST0`) | `[V-BIN]` |
| `DCE_SHADER_STATE0` | 2 | `vertex_output_stride` | `[36:27]` | `uint` (10-bit; vertex stride / GS max vertices; `1` = `0x8000000` in SecondaryDDM/Barrier) | `MOVED` | `[V-BIN]` |
| `DCE_SHADER_STATE0` | 2 | `output_size` | `[26:19]` | `uint` (8-bit output dwords) | `MOVED` | `[V-BIN]` |
| `DCE_SHADER_STATE0` | 2 | `input_size_m1` | `[18:14]` | `uint` (`input_size - 1`, 5-bit) | `MOVED` | `[V-BIN]` |
| `DCE_SHADER_STATE0` | 2 | `cam_vertex_count` | `[13:7]` | `uint` (7-bit VS output blocks / CAM vertex count) | `MOVED` | `[V-BIN]` |
| `DCE_SHADER_STATE0` | 2 | `cam_size` | `[6:0]` | `uint` (7-bit CAM size; `0` = 128) | `MOVED` | `[V-BIN]` |
| `DCE_SHADER_STATE1` | 2 | `usc_unified_size` *(Primary `stage 0..3`)* | `[60:54]` | `uint` (`(unified_dwords + 1) >> 1`, 7-bit; mask `0x1fc0_0000_0000_0000`) | `MOVED` | `[V-BIN]` |
| `DCE_SHADER_STATE1` | 2 | `usc_common_size` *(SecondaryDDM `stage 4`)* | `[63:48]` *(active `[53:48]`)* | `uint` (`(shared_bytes + 63) >> 6`, 64-byte chunks; confirmed by shared-alloc patcher) | `MOVED` | `[V-BIN]` |
| `DCE_SHADER_STATE1` | 2 | `local_mem_alloc` | `[47:39]` | `uint` (`(bytes + 8191) >> 13`, 9-bit, 8 KiB units) | `NEW` | `[V-BIN]` |
| `DCE_SHADER_STATE1` | 2 | `pds_data_size` | `[38:33]` *(Secondary `[39:33]`)* | `uint` (`(dwords + 3) >> 2`, 16-byte chunks; 6b primary / 7b secondary) | `MOVED` | `[V-BIN]` |
| `DCE_SHADER_STATE1` | 2 | `pds_temp_size` | `[32:28]` | `uint` (`(dwords + 1) >> 1`, 8-byte chunks, 5-bit) | `MOVED` | `[V-BIN]` |
| `DCE_SHADER_STATE1` | 2 | `pds_code_addr` | `[27:0]` | `shift="4"`, `address` (32-bit offset within PDS execution heap) | `MOVED` | `[V-BIN]` |
| `DCE_SHADER_STATE2` | 2 | `pds_data_addr` | `[31:4]` | `shift="4"`, `address` (32-bit 16B-aligned offset within PDS data heap; `[63:32]=0`) | `MOVED` | `[V-BIN]` |

#### `0x05` — `DCE_PREDICATE_WRAPPER` (2 QWORDs = 4 DWords header)

| Struct / Packet | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `DCE_PREDICATE0` | 2 | `opcode` | `[63:56]` | `uint` = `0x05` | `NEW` | `[V-BIN]` |
| `DCE_PREDICATE0` | 2 | `cond_word_count` | `[55:46]` | `uint` (10-bit dword count of predicate buffer) | `NEW` | `[V-BIN]` |
| `DCE_PREDICATE0` | 2 | `cond_addr` | `[45:0]` | `shift="2"`, `address` (`(va + 3) >> 2`) | `NEW` | `[V-BIN]` |
| `DCE_PREDICATE1` | 2 | `wrapped_qword_count` | `[63:32]` | `uint` (count of following DCE QWORDs skipped if predicate fails) | `NEW` | `[V-BIN]` |
| `DCE_PREDICATE1` | 2 | `flags` | `[31:0]` | `uint` (inversion / predicate mode flags) | `NEW` | `[OPEN]` (exact flag bits) |

#### `0x06` — `DCE_DRAW_COMMAND` (3 to 5 QWORDs = 6 to 10 DWords)

| Struct / Packet | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `DCE_DRAW0` | 2 | `opcode` | `[63:56]` | `uint` = `0x06` | `NEW` | `[V-BIN]` |
| `DCE_DRAW0` | 2 | `cut_index` | `[55:24]` | `uint` (32-bit primitive restart index: `0xff`, `0xffff`, `0xffffffff`) | `MOVED` (was separate `INDEX_LIST` word) | `[V-BIN]` |
| `DCE_DRAW0` | 2 | `cut_index_enable` | `[23:23]` | `bool` (`1ULL << 23`) | `MOVED` | `[V-BIN]` |
| `DCE_DRAW0` | 2 | `primitive_topology` | `[22:19]` | `enum DCE_PRIM_TOPOLOGY` (4-bit, see table below) | `MOVED` | `[V-BIN]` |
| `DCE_DRAW0` | 2 | `patch_count_m1` | `[18:14]` | `uint` (`patch_control_points - 1`, 5-bit) | `MOVED` | `[V-BIN]` |
| `DCE_DRAW0` | 2 | `tess_enable` | `[13:13]` | `bool` (`0x2000`) | `NEW` | `[V-BIN]` |
| `DCE_DRAW0` | 2 | `gs_enable` | `[12:12]` | `bool` (`0x1000`) | `NEW` | `[V-BIN]` |
| `DCE_DRAW0` | 2 | `aux_geom_flag` | `[8:8]` | `bool` (`0x100`, set when HS/GS/streamout active) | `NEW` | `[V-BIN]` |
| `DCE_DRAW0` | 2 | `degen_cull_enable` | `[7:7]` | `bool` (`0x80`, degenerate triangle/line culling) | `MOVED` | `[V-BIN]` |
| `DCE_DRAW0` | 2 | `indirect_count_present` | `[1:1]` | `bool` (`0x2`, enables optional `DCE_DRAW3`) | `NEW` | `[V-BIN]` |
| `DCE_DRAW0` | 2 | `multi_draw_present` | `[0:0]` | `bool` (`0x1`, enables optional `DCE_DRAW4`) | `NEW` | `[V-BIN]` |
| `DCE_DRAW1` | 2 | `arg_desc_count` | `[57:55]` | `uint` (`1` = non-indexed `0x0080...`, `2` = indexed `0x0100...`) | `NEW` | `[V-BIN]` |
| `DCE_DRAW1` | 2 | `arg_desc_addr` | `[36:0]` | `shift="3"`, `address` (`dev_va >> 3`, points to 1 or 2 QWORD draw-argument descriptors) | `NEW` | `[V-BIN]` |
| `DCE_DRAW2` | 2 | `split_count` | `[55:48]` | `uint` (8-bit max vertices per split batch; `0xfc` for `TRI_LIST`, `0xff` for clear/reset) | `MOVED` | `[V-BIN]` |
| `DCE_DRAW2` | 2 | `draw_data_addr` | `[45:0]` | `shift="2"`, `address` (`dev_va >> 2`, points to inline or indirect draw parameter struct) | `NEW` | `[V-BIN]` |
| `DCE_DRAW3` *(opt, `DRAW0[1]==1`)* | 2 | `indirect_count_addr` | `[45:0]` | `shift="2"`, `address` (48-bit dword-aligned VA of `uint32_t drawCount`) | `NEW` | `[V-BIN]` |
| `DCE_DRAW4` *(opt, `DRAW0[0]==1`)* | 2 | `draw_stride` | `[63:34]` | `shift="2"`, `uint` (30-bit stride in dwords, `stride_bytes >> 2`) | `NEW` | `[V-BIN]` |
| `DCE_DRAW4` *(opt, `DRAW0[0]==1`)* | 2 | `max_draw_count` | `[31:0]` | `uint` (32-bit draw count / `maxDrawCount`) | `NEW` | `[V-BIN]` |

**`DCE_PRIM_TOPOLOGY` (`DCE_DRAW0[22:19]`, `[V-BIN]`)**:

| Vulkan Topology | Hardware Value | Encoding (`val << 19`) |
| --- | --- | --- |
| `POINT_LIST` | `0` | `0x000000` |
| `LINE_LIST` | `1` | `0x080000` |
| `LINE_STRIP` | `2` | `0x100000` |
| `TRIANGLE_LIST` | `4` | `0x200000` |
| `TRIANGLE_STRIP` | `5` | `0x280000` |
| `TRIANGLE_FAN` | `6` | `0x300000` |
| `PATCH_LIST` | `7` | `0x380000` |
| `LINE_LIST_WITH_ADJACENCY` | `9` | `0x480000` |
| `LINE_STRIP_WITH_ADJACENCY` | `10` | `0x500000` |
| `TRIANGLE_LIST_WITH_ADJACENCY` | `12` | `0x600000` |
| `TRIANGLE_STRIP_WITH_ADJACENCY` | `13` | `0x680000` |

**Draw Argument Descriptor & Draw Data Memory Layouts (`DCE_DRAW1.arg_desc_addr` & `DCE_DRAW2.draw_data_addr`, `[V-BIN]`)**:
- **Non-Indexed Draw (`arg_desc_count = 1`)**:
  - `arg_desc[0]` (8 B) = `0x1000_0000_0000_0000` (selects standard 16-byte `VkDrawIndirectCommand` layout).
  - `draw_data` (16 B): `{ uint32_t vertex_count; uint32_t instance_count; uint32_t first_vertex; uint32_t first_instance; }`.
- **Indexed Draw (`arg_desc_count = 2`)**:
  - `arg_desc[0..1]` (16 B) = `{ 0x0000_0000_0000_0000, 0x2000_0000_0000_0004 }`.
  - `draw_data` (36 B):
    ```text
    +0x00 (8B): uint64_t index_buffer_va      (raw device VA of bound index buffer)
    +0x08 (4B): uint32_t index_buffer_size    (remaining size in bytes for HW bounds check)
    +0x0c (4B): uint32_t index_type           (0 = UINT16, 1 = UINT32, 2 = UINT8_KHR)
    +0x10 (4B): uint32_t index_count
    +0x14 (4B): uint32_t instance_count
    +0x18 (4B): uint32_t first_index
    +0x1c (4B): int32_t  vertex_offset
    +0x20 (4B): uint32_t first_instance
    ```
  - *(Note: Direct draws place the 16 B / 36 B struct inline in a transient dynamic buffer; `vkCmdDrawIndirect` can point `draw_data_addr` straight at the app's indirect buffer!)*

#### `0x07` — `DCE_DEPTH_BIAS_UPDATE` (2 QWORDs = 4 DWords)

| Struct / Packet | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `DCE_DEPTH_BIAS0` | 2 | `opcode` | `[63:56]` | `uint` = `0x07` | `NEW` (replaces PPP `ISPDBIAS`) | `[V-BIN]` |
| `DCE_DEPTH_BIAS0` | 2 | `constant_factor` | `[31:0]` | `float` (IEEE-754 f32) | `MOVED` | `[V-BIN]` |
| `DCE_DEPTH_BIAS1` | 2 | `clamp` | `[63:32]` | `float` (IEEE-754 f32) | `MOVED` | `[V-BIN]` |
| `DCE_DEPTH_BIAS1` | 2 | `slope_factor` | `[31:0]` | `float` (IEEE-754 f32) | `MOVED` | `[V-BIN]` |

#### `0x08` — `DCE_SAMPLE_MASK_UPDATE` (1 QWORD = 2 DWords)

| Struct / Packet | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `DCE_SAMPLE_MASK` | 2 | `opcode` | `[63:56]` | `uint` = `0x08` | `NEW` | `[V-BIN]` |
| `DCE_SAMPLE_MASK` | 2 | `sample_mask` | `[31:0]` | `uint` (32-bit MSAA coverage mask) | `NEW` | `[V-BIN]` |

#### `0x09` — `DCE_SCISSOR_ARRAY_UPDATE` (`1 + count` QWORDs)

| Struct / Packet | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `DCE_SCISSOR_HDR` | 2 | `opcode` | `[63:56]` | `uint` = `0x09` | `NEW` (replaces PPP `ISPSCISSOR`) | `[V-BIN]` |
| `DCE_SCISSOR_HDR` | 2 | `count_m1` | `[7:4]` *(upper `[15:8]=0`)* | `uint` (`scissor_count - 1`) | `NEW` | `[V-BIN]` |
| `DCE_SCISSOR_HDR` | 2 | `table_select` | `[3:0]` | `enum`: `0` = user scissor array, `1` = viewport-clip scissor array | `NEW` | `[V-BIN]` |
| `DCE_SCISSOR_RECT` (×N) | 2 | `max_y` | `[63:48]` | `uint` (16-bit inclusive pixel Y) | `WIDENED` (14b→16b) | `[V-BIN]` |
| `DCE_SCISSOR_RECT` (×N) | 2 | `min_y` | `[47:32]` | `uint` (16-bit pixel Y) | `WIDENED` (14b→16b) | `[V-BIN]` |
| `DCE_SCISSOR_RECT` (×N) | 2 | `max_x` | `[31:16]` | `uint` (16-bit inclusive pixel X) | `WIDENED` (14b→16b) | `[V-BIN]` |
| `DCE_SCISSOR_RECT` (×N) | 2 | `min_x` | `[15:0]` | `uint` (16-bit pixel X) | `WIDENED` (14b→16b) | `[V-BIN]` |

#### `0x0a` — `DCE_SCISSOR_DBIAS_EMIT` (1 QWORD = 2 DWords) & `0x0c` — `DCE_BARRIER` (1 QWORD = 2 DWords)

| Struct / Packet | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `DCE_SCISSOR_DBIAS_EMIT` | 2 | `opcode` | `[63:56]` | `uint` = `0x0a` | `NEW` | `[V-BIN]` |
| `DCE_SCISSOR_DBIAS_EMIT` | 2 | `viewport_count_m1` | `[7:4]` | `uint` (`viewport_count - 1`) | `NEW` | `[V-BIN]` |
| `DCE_SCISSOR_DBIAS_EMIT` | 2 | `dbias_mask_emit` | `[1:1]` | `bool` (`0x2`: latch depth bias / sample mask) | `NEW` | `[V-BIN]` |
| `DCE_SCISSOR_DBIAS_EMIT` | 2 | `scissor_emit` | `[0:0]` | `bool` (`0x1`: latch scissor tables; `0x3` = both) | `NEW` | `[V-BIN]` |
| `DCE_BARRIER` | 2 | `opcode` | `[63:56]` | `uint` = `0x0c` | `NEW` | `[V-BIN]` |
| `DCE_BARRIER` | 2 | `reserved` | `[55:0]` | `uint` = `0` | `NEW` | `[V-BIN]` |

---

## 2. `[GAP-CSB-PPP]` — Volcanic `PPP_STATE` (`TA_STATE_*`) & Pixel Shader Descriptor Table

### 2.1 `TA_STATE_HEADER` (1 DWord, `[V-BIN]`)

Header bits determine exact presence & emission order of subsequent dwords. On Volcanic `DXT-48-1536`, **PDS inline shader/size words (`pres_pds_state_ptr0` / `ptr3`) are removed** and replaced by a 1-dword PDS data pointer (`[7]`) + a 1-dword **Pixel Shader Descriptor Table index & Fragment Shading Rate word** (`[8]`).

| Struct / Packet | DWords | Field | Bits `[end:start]` | Emitted DWords | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `TA_STATE_HEADER` | 1 | `pres_terminate` | `[25:25]` | 2 (`TERMINATE0..1`) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `pres_stream_out_program` | `[23:23]` | 2 (`STREAM_OUT1..2`) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `pres_stream_out_size` | `[22:22]` | 1 (`STREAM_OUT0`) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `pres_ppp_ctrl` | `[21:21]` | 1 (`PPP_CTRL`) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `pres_varying_word2` | `[20:20]` | 1 (`VARYING2`) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `pres_varying_word1` | `[19:19]` | 1 (`VARYING1`) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `pres_varying_word0` | `[18:18]` | 1 (`VARYING0`) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `pres_outselects` | `[17:17]` | 1 (`OUTPUT_SEL`) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `pres_wclamp` | `[16:16]` | 1 (`WCLAMP`) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `view_port_count` | `[15:12]` | (`count - 1`) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `pres_viewport` | `[11:11]` | `6 * (vp_cnt + 1)` | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `pres_region_clip` | `[10:10]` | 2 (`REGION_CLIP0..1`) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | *(legacy `pres_pds_state_ptr3`)* | `[9:9]` | 0 (unused on Volcanic) | `REMOVED` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `pres_pds_shader_table_fsr` | `[8:8]` | **1** (`PDS_SHADER_TABLE_FSR`) | `CHANGED` (was 2 DW tex/varying bases) | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `pres_pds_data_addr` | `[7:7]` | **1** (`PDS_DATA_ADDR`) | `CHANGED` (was `sizeinfo2` / 1 DW) | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | *(legacy `pres_pds_state_ptr0`)* | `[6:6]` | 0 (unused on Volcanic) | `REMOVED` (was 3 DW inline shader/size) | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | *(legacy `pres_ispctl_dbsc`)* | `[5:5]` | 0 (moved to DCE `0x07`/`0x09`/`0x0a`) | `REMOVED` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `pres_ispctl_bb` | `[4:4]` | 1 (`ISPB` back) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `pres_ispctl_ba` | `[3:3]` | 1 (`ISPA` back) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `pres_ispctl_fb` | `[2:2]` | 1 (`ISPB` front) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `pres_ispctl_fa` | `[1:1]` | 1 (`ISPA` front) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_HEADER` | 1 | `pres_ispctl` | `[0:0]` | 1 (`ISPCTL`) | `IDENTICAL` | `[V-BIN]` |

> **Exact Proof Vector (`GeneratePPPStateForSingleViewport`)**: Header = `0x00ff_0d87` = bits `{0,1,2,7,8,10,11,16,17,18,19,20,21,22,23}` → `1(hdr) + 1(ispctl) + 1(fa) + 1(fb) + 1(pds_data) + 1(ps_tbl_fsr) + 2(rclip) + 6(vp) + 1(wclamp) + 1(outsel) + 3(var0..2) + 1(pppctrl) + 1(so0) + 2(so1..2) = 23 dwords (0x17)` — exact hardware word-count match!

### 2.2 ISP Control & Face Words (`ISPCTL`, `ISPA`, `ISPB`)

| Struct / Packet | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `TA_STATE_ISPCTL` | 1 | `dce_scissor_active` | `[29:29]` | `bool` (`0x2000_0000`, set on non-culled draws with `scenable`) | `NEW` | `[V-BIN]` |
| `TA_STATE_ISPCTL` | 1 | `upass` | `[27:20]` | `uint` (8-bit user pass ID; `0xff` = unused) | `WIDENED` (Rogue `[27:24]` 4b → 8b) | `[V-BIN]` |
| `TA_STATE_ISPCTL` | 1 | `two_sided` | `[19:19]` | `bool` (`0x80000`) | `MOVED` (Rogue `[22]`) | `[V-BIN]` |
| `TA_STATE_ISPCTL` | 1 | `bpres` | `[18:18]` | `bool` (`0x40000`, back-face words present) | `MOVED` (Rogue `[23]`) | `[V-BIN]` |
| `TA_STATE_ISPCTL` | 1 | `dbenable` | `[17:17]` | `bool` (`0x20000`, depth bias enable) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_ISPCTL` | 1 | `scenable` | `[16:16]` | `bool` (`0x10000`, scissor enable) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_ISPCTL` | 1 | `vistest` | `[15:15]` | `bool` (`0x8000`, occlusion query enable) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_ISPCTL` | 1 | `visreg` | `[14:1]` | `uint` (14-bit visibility register index) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_ISPCTL` | 1 | `visbool` | `[0:0]` | `bool` (`0`=count, `1`=boolean) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_ISPA` | 1 | `objtype` | `[31:28]` | `enum`: `0`=TRI, `1`=LINE, `4`=SPRITE_01UV/POINT, `5`=LINE_FILLED_TRI, `6`=POINT_FILLED_TRI | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_ISPA` | 1 | `passtype` | `[27:25]` | `enum`: `0`=OPAQUE, `1`=TRANSLUCENT, `2`=PUNCHTHROUGH, `5`=DEPTH_FEEDBACK | `MOVED` (Rogue `[26:24]`) | `[V-BIN]` |
| `TA_STATE_ISPA` | 1 | `tagwritedisable` | `[24:24]` | `bool` (`0x0100_0000`, set for depth-only / `CULL_FRONT_AND_BACK`) | `MOVED` (from Rogue `ISPCTL[21]`) | `[V-BIN]` |
| `TA_STATE_ISPA` | 1 | `dwritedisable` | `[21:21]` | `bool` (`0x0020_0000`) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_ISPA` | 1 | `dcmpmode` | `[19:17]` | `enum CMPMODE` (`0`=NEVER..`7`=ALWAYS) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_ISPA` | 1 | `pointlinewidth` | `[15:8]` | `ufixed4.4` | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_ISPA` | 1 | `sref` | `[7:0]` | `uint` (8-bit stencil reference) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_ISPB` | 1 | `scmpmode` | `[27:25]` | `enum CMPMODE` | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_ISPB` | 1 | `sop1` | `[24:22]` | `enum STENCILOP` (stencil fail) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_ISPB` | 1 | `sop2` | `[21:19]` | `enum STENCILOP` (depth fail) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_ISPB` | 1 | `sop3` | `[18:16]` | `enum STENCILOP` (depth pass) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_ISPB` | 1 | `scmpmask` | `[15:8]` | `uint` (8-bit compare mask) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_ISPB` | 1 | `swmask` | `[7:0]` | `uint` (8-bit write mask) | `IDENTICAL` | `[V-BIN]` |

### 2.3 Volcanic PDS Pixel State & Indirect 16-Byte Pixel Shader Table (`HEADER[7..8]`, `[V-BIN]`)

| Struct / Packet | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `TA_STATE_PDS_DATA_ADDR` (`HDR[7]`) | 1 | `pds_data_addr` | `[31:4]` | `shift="4"`, `address` (16B-aligned offset in PDS data heap) | `CHANGED` | `[V-BIN]` |
| `TA_STATE_PDS_SHADER_TABLE_FSR` (`HDR[8]`) | 1 | `fsr_width_log2` | `[30:29]` | `enum`: `0`=1px, `1`=2px, `2`=4px | `NEW` | `[V-BIN]` |
| `TA_STATE_PDS_SHADER_TABLE_FSR` (`HDR[8]`) | 1 | `fsr_height_log2` | `[28:27]` | `enum`: `0`=1px, `1`=2px, `2`=4px | `NEW` | `[V-BIN]` |
| `TA_STATE_PDS_SHADER_TABLE_FSR` (`HDR[8]`) | 1 | `fsr_combiner0` | `[26:24]` | `enum` (`0..4`: KEEP/REPLACE/MIN/MAX/MUL) | `NEW` | `[V-BIN]` |
| `TA_STATE_PDS_SHADER_TABLE_FSR` (`HDR[8]`) | 1 | `fsr_combiner1` | `[23:21]` | `enum` (`0..4`: KEEP/REPLACE/MIN/MAX/MUL) | `NEW` | `[V-BIN]` |
| `TA_STATE_PDS_SHADER_TABLE_FSR` (`HDR[8]`) | 1 | `shader_table_index` | `[20:0]` | `uint` (21-bit index into 16-byte-stride Pixel Shader Descriptor Table) | `NEW` | `[V-BIN]` |
| `PIXEL_SHADER_TABLE_ENTRY0` *(in mem)* | 2 (QW0) | `pds_tex_unicode_base` | `[55:28]` | `shift="4"`, `address` (28-bit PDS tex/uniform code offset `>> 4`) | `NEW` (indirect) | `[V-BIN]` |
| `PIXEL_SHADER_TABLE_ENTRY0` *(in mem)* | 2 (QW0) | `pds_shader_base` | `[27:0]` | `shift="4"`, `address` (28-bit PDS pixel-shader code offset `>> 4`) | `NEW` (indirect) | `[V-BIN]` |
| `PIXEL_SHADER_TABLE_ENTRY1` *(in mem)* | 2 (QW1) | `pds_data_size` | `[33:27]` | `uint` (`(dwords + 3) >> 2`, 7-bit, 16-byte chunks) | `NEW` (indirect) | `[V-BIN]` |
| `PIXEL_SHADER_TABLE_ENTRY1` *(in mem)* | 2 (QW1) | `pds_temp_size` | `[25:17]` | `uint` (`(dwords + 1) >> 1`, 9-bit, 8-byte chunks) | `NEW` (indirect) | `[V-BIN]` |
| `PIXEL_SHADER_TABLE_ENTRY1` *(in mem)* | 2 (QW1) | `entry_valid_flag` | `[9:9]` | `bool` (always set to `1` = `0x200`) | `NEW` | `[V-BIN]` |
| `PIXEL_SHADER_TABLE_ENTRY1` *(in mem)* | 2 (QW1) | `usc_unified_size` | `[5:0]` | `uint` (`(unified_bytes + 63) >> 6`, 6-bit, 64-byte chunks) | `NEW` (indirect) | `[V-BIN]` |

### 2.4 Clipping, Viewport, Output Selects, Varyings, PPP Control, Stream-Out & Terminate

| Struct / Packet | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `TA_REGION_CLIP0` | 1 | `mode` | `[31:31]` | `enum`: `0`=INSIDE, `1`=OUTSIDE | `IDENTICAL` | `[V-BIN]` |
| `TA_REGION_CLIP0` | 1 | `left` | `[25:16]` | `uint` (10-bit 32px tile X, max 1023 → 32768px) | `WIDENED` (9b→10b) | `[V-BIN]` |
| `TA_REGION_CLIP0` | 1 | `right` | `[9:0]` | `uint` (10-bit inclusive 32px tile X) | `WIDENED` (9b→10b) | `[V-BIN]` |
| `TA_REGION_CLIP1` | 1 | `top` | `[25:16]` | `uint` (10-bit 32px tile Y) | `WIDENED` (9b→10b) | `[V-BIN]` |
| `TA_REGION_CLIP1` | 1 | `bottom` | `[9:0]` | `uint` (10-bit inclusive 32px tile Y) | `WIDENED` (9b→10b) | `[V-BIN]` |
| `TA_STATE_VIEWPORT` | 6 | `a0..a5` | `[31:0]` × 6 | `float` (`x_center, x_half, y_center, y_half, z_center, z_half`) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_WCLAMP` | 1 | `wclamp` | `[31:0]` | `float` (default `0x29e12e13` = `1e-13f`) | `IDENTICAL` | `[V-BIN]` |
| `TA_OUTPUT_SEL` | 1 | `vtxsize` | `[31:24]` | `uint` (8-bit output vertex size in dwords) | `IDENTICAL` | `[V-BIN]` |
| `TA_OUTPUT_SEL` | 1 | `ext_output_flag` | `[22:22]` | `bool` (`0x0040_0000`) | `NEW` | `[OPEN]` (exact semantic) |
| `TA_OUTPUT_SEL` | 1 | `tsp_unclamped_z_pres` | `[21:21]` | `bool` | `IDENTICAL` | `[V-BIN]` |
| `TA_OUTPUT_SEL` | 1 | `render_tgt_pres` | `[20:20]` | `bool` | `IDENTICAL` | `[V-BIN]` |
| `TA_OUTPUT_SEL` | 1 | `vpt_tgt_pres` | `[19:19]` | `bool` | `IDENTICAL` | `[V-BIN]` |
| `TA_OUTPUT_SEL` | 1 | `psprite_size_pres` | `[18:18]` | `bool` | `IDENTICAL` | `[V-BIN]` |
| `TA_OUTPUT_SEL` | 1 | `rhw_pres` | `[16:16]` | `bool` | `IDENTICAL` | `[V-BIN]` |
| `TA_OUTPUT_SEL` | 1 | `clip_cull_planes` | `[15:0]` | `uint` (`[15:8]` cull / `[7:0]` clip plane masks) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_VARYING0..2` | 3 | `f32_linear`, `f32_npc`, `f32_flat`, `f16_*` | same | Exact same varying-count packing as Rogue | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_PPP_CTRL` | 1 | `winding_ccw` | `[16:16]` | `bool` (`0x10000` when `frontFace == CCW`; table `[0x2,0x1,0x10002,0x10001]`) | `NEW` / `MOVED` | `[V-BIN]` |
| `TA_STATE_PPP_CTRL` | 1 | `line_fill_last_pixel` | `[15:15]` | `bool` (`0x8000`, strict line rasterization) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_PPP_CTRL` | 1 | `gs_output_topology` | `[14:13]` | `enum`: `0`=POINT, `1`=LINE (`0x2000`), `2`=TRI (`0x4000`) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_PPP_CTRL` | 1 | `pres_prim_id_mode` | `[12:12]` | `bool` (`0x1000`, set when no tess/GS) | `MOVED` | `[V-BIN]` |
| `TA_STATE_PPP_CTRL` | 1 | `clip_mode` | `[10:10]` | `bool` (`0x400`, Z-clip enable when `!depthClamp`) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_PPP_CTRL` | 1 | `drawclippededges` | `[9:9]` | `bool` (`0x200`, always set with `wclampen` as `0x220`) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_PPP_CTRL` | 1 | `flatshade_vtx` | `[8:7]` | `enum`: `0`=VTX0, `1`=VTX1, `2`=VTX2, `3`=VTX3 | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_PPP_CTRL` | 1 | `wclampen` | `[5:5]` | `bool` (`0x20`) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_PPP_CTRL` | 1 | `cullmode` | `[1:0]` | `enum`: `0`=NONE, `1`=CULL_CW, `2`=CULL_CCW | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_STREAM_OUT0` | 1 | `stream3_size` / `stream2_size` / `stream1_size` | `[31:24]`/`[23:16]`/`[15:8]` | `uint` (8-bit each) | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_STREAM_OUT0` | 1 | `stream0_mem_output` / `stream0_ta_output` | `[1:1]` / `[0:0]` | `bool` | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_STREAM_OUT1` | 1 | `sd_mode` / `sync` / `pds_data_size` / `pds_temp_size` | `[15:14]`/`[12]`/`[10:5]`/`[3:0]` | `uint` / `bool` | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_STREAM_OUT2` | 1 | `pds_data_addr` | `[31:4]` | `shift="4"`, `address` | `IDENTICAL` | `[V-BIN]` |
| `TA_STATE_TERMINATE0` | 1 | `clip_right` | `[27:18]` | `uint` (`((width + 31) >> 5) - 1`, 10-bit) | `WIDENED` (Rogue `[26:18]` 9b→10b) | `[V-BIN]` |
| `TA_STATE_TERMINATE0` | 1 | `clip_bottom` | `[9:0]` | `uint` (`((height + 31) >> 5) - 1`, 10-bit) | `WIDENED` (Rogue `[8:0]` 9b→10b) | `[V-BIN]` |
| `TA_STATE_TERMINATE1` | 1 | `clip_left` / `render_target` | `[31:22]` / `[10:0]` | `uint` (`0` in single-RT full-screen Vulkan terminate) | `WIDENED` | `[OPEN]` (exact `TERMINATE1` bit bounds when non-zero) |

---

## 3. `[GAP-CR-SOFT-RESET]` — Volcanic `RGX_CR_SOFT_RESET` & Boot Sequence (`[V-GPL]`)

### 3.1 Control Registers & Valid Bit Masks

| Register | Offset | Size | Valid Mask / Fields | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- |
| `RGX_CR_SOFT_RESET` | `0x0100` | 64-bit (`0x0100` lo, `0x0104` hi) | **Valid Mask (`MASKFULL`) = `0x7fffffe0_007bdeff`** (valid bits: `[7:0], [12:9], [14], [17:15], [22:19], [62:37]`; invalid/rejected bits: `[8], [13], [18], [36:23], [63]` = `0x8000001f_ff842100`) | `CHANGED` (completely new Volcanic bit map) | `[V-GPL]` |
| `RGX_CR_SOFT_RESET.GARTEN_EN` | `0x0100` | bit `[37:37]` | `0x00000020_00000000` — **FW CPU (RISC-V / META) / Garten reset** (held across steps 2–5, cleared alone in step 6 to boot FW!) | `MOVED` (Rogue bit `[30]` → Volcanic bit `[37]`) | `[V-GPL]` |
| `RGX_CR_SOFT_RESET.INTERCONNECT_GROUP` | `0x0100` | bits `{45,44,43,38,11}` | `0x00003840_00000800` — upper interconnect / SLC / MMU / AXI / RAC domain group asserted first & released first | `NEW` | `[V-GPL]` (individual domain names `[OPEN]`) |
| `RGX_CR_SOFT_RESET.JONES_CORE_GROUP` | `0x0100` | bits `{15,7,6,5,4,1}` | `0x00000000_000080f2` — Jones / VDM / CDM / GEOM / ISP / PBE / TDM domain group asserted second & released second | `NEW` | `[V-GPL]` (individual domain names `[OPEN]`) |
| `RGX_CR_SOFT_RESET2` | `0x0108` | 64-bit MMIO (low 32b valid) | **Valid Mask = `0x00000000_ffffffff`** | `WIDENED` | `[V-GPL]` |
| `RGX_CR_SOFT_RESET_SPU` / `TPU` / `USC` banks | `0x0630`, `0x0640`, `0x0650` | 64-bit indexed | Per-SPU / TPU / USC sub-unit reset registers (written `6×` at `0x0630`, `4×` at `0x0650`, `2×` at `0x0640` via indirect select) | `NEW` (S7_TOP multi-SPU reset banks) | `[V-GPL]` |

### 3.2 Proven 6-Step Volcanic Cold-Boot Reset Sequence (`RGXStart`, `[V-GPL]`)

1. **Assert Step 1 (SPU / Sub-Unit Banks)**: Write per-unit reset assertions to `0x0630` (6 writes), `0x0650` (4 writes), `0x0640` (2 writes).
2. **Assert Step 2 (`RGX_CR_SOFT_RESET` `0x0100`)**:
   - Write `0x0100 = 0x00003860_00000800` (asserts `GARTEN_EN[37]` + `INTERCONNECT_GROUP{11,38,43,44,45}`).
   - Write `0x0100 = 0x00003860_000088f2` (adds `JONES_CORE_GROUP{1,4,5,6,7,15}`).
3. **De-assert Step 1 (Excluding FW CPU)**:
   - Write `0x0100 = 0x00000020_000080f2` (releases `INTERCONNECT_GROUP`, keeps `GARTEN_EN[37]` + `JONES_CORE_GROUP`).
   - Write `0x0100 = 0x00000020_00000000` (releases `JONES_CORE_GROUP`, holds **only `GARTEN_EN[37]`**).
4. **De-assert Step 2 (SPU / Sub-Unit Banks)**: De-assert `0x0640`, `0x0650`, `0x0630` in reverse order.
5. **Configure FW & MMU**: Program RISC-V/META wrapper, boot remap, `GARTEN_IDLE`, AXI-ACE, and MMUv4 Page Catalogue base.
6. **Release FW CPU**: Write `0x0100 = 0x00000000_00000000` (clears bit `37`, starting firmware execution).

---

## 4. `[GAP-CR-ISP-S7]` — Volcanic `S7_TOP` ISP & Multi-Word ZLS v2 Registers (`[V-BIN]`)

### 4.1 `ISP_CTL`, `ISP_XTP_PIPE_ENABLE`, `ISP_AA`, `TE_AA`, and 32K Screen / Tile Geometry

| Struct / Register | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `CR_ISP_CTL` | 1 | `tiles_in_flight_pipe_ctl` | `[31:16]` | `uint` (16-bit packed tiles-in-flight / pipe enable mask computed from pixel output reg count) | `CHANGED` (`S7_TOP` 16b upper half) | `[V-BIN]` |
| `CR_ISP_CTL` | 1 | `upass_and_mode_ctl` | `[15:0]` | `uint` (base `0x7500`; bit `6` = `0x40` → `0x7540` when overlap-disable / sample-pos mode clear) | `CHANGED` | `[V-BIN]` |
| `CR_ISP_XTP_PIPE_ENABLE` | 1 | `pipe_width_mode` | `[1:0]` | `enum`: `0` (`<=2` dwords/px), `1` (`4` dwords/px), `2` (`8` dwords/px) | `CHANGED` | `[V-BIN]` |
| `CR_ISP_XTP_PIPE_AUX` *(3D cmd +0x44)* | 1 | `xtp_base_cfg` | `[22:0]` | Base `0x0078_4000 \| aux_flags \| (flag << 17)` | `NEW` | `[V-BIN]` |
| `CR_ISP_AA` | 1 | `mode` | `[1:0]` | `enum`: `0`=1x, `1`=2x, `2`=4x, `3`=8x | `IDENTICAL` | `[V-BIN]` |
| `CR_TE_AA` | 1 | `aa_mode` | `[2:0]` | `enum`: `0`=1x1, `1`=1x2 (2x), `5`=2x2 (4x), `7`=2x4 (8x) | `IDENTICAL` | `[V-BIN]` |
| `CR_PPP_SCREEN` | 1 | `pixmaxy` / `pixmaxx` | `[30:16]` / `[14:0]` | `uint` (`height - 1`, `width - 1`; 15-bit coords up to **32768×32768**) | `WIDENED` (14b→15b) | `[V-BIN]` |
| `CR_TE_SCREEN` / `ISP_MTILE_SIZE` | 1 | `xmax` / `ymax` | 32×32 tiles | Tile grid `((w + 31) >> 5)`, `((h + 31) >> 5)` (max 1024×1024 tiles) | `WIDENED` (16K→32K) | `[V-BIN]` |

### 4.2 Volcanic Multi-Word ZLS v2 (`ISP_ZLSCTL` + `ZLS_WORD0..2`, `[V-BIN]`)

Unlike Rogue's single 64-bit `ISP_ZLSCTL`, Volcanic splits ZLS configuration into a **32-bit `ISP_ZLSCTL`** plus **three 64-bit register words (`ZLS_WORD0..2`)** mirroring the new Volcanic PBE clip/stride/dimension layout:

| Struct / Register | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `CR_ISP_ZLSCTL` | 1 | `s_resolve_mode` | `[25:24]` | `enum`: `0`=NONE, `1`=MIN/SAMPLE0 (`0x1000000`), `2`=MAX (`0x2000000`), `3`=AVG (`0x3000000`) | `NEW` | `[V-BIN]` |
| `CR_ISP_ZLSCTL` | 1 | `s_fbc_or_flush` | `[23:23]` | `bool` (`0x800000`) | `NEW` | `[V-BIN]` |
| `CR_ISP_ZLSCTL` | 1 | `z_fbc_or_flush` | `[22:22]` | `bool` (`0x400000`) | `NEW` | `[V-BIN]` |
| `CR_ISP_ZLSCTL` | 1 | `z_resolve_mode` | `[20:19]` | `enum`: `0`=NONE, `1`=`0x80000`, `2`=`0x100000`, `3`=`0x180000` | `NEW` | `[V-BIN]` |
| `CR_ISP_ZLSCTL` | 1 | `zformat` | `[18:17]` | `enum`: `0`=F32Z, `1`=D24S8/U24 (`0x20000`), `2`=U16 (`0x40000`), `3`=ext (`0x60000`) | `MOVED` | `[V-BIN]` |
| `CR_ISP_ZLSCTL` | 1 | `sstoreen` / `sloaden` | `[13:13]` / `[12:12]` | `bool` (`0x2000` / `0x1000`) | `MOVED` | `[V-BIN]` |
| `CR_ISP_ZLSCTL` | 1 | `zstoreen` / `zloaden` | `[9:9]` / `[8:8]` | `bool` (`0x0200` / `0x0100`) | `MOVED` | `[V-BIN]` |
| `CR_ZLS_WORD0` | 2 (64b) | `size_y_log2` | `[33:29]` | `uint` (5-bit `ceil(log2(height))`) | `NEW` | `[V-BIN]` |
| `CR_ZLS_WORD0` | 2 (64b) | `size_x_log2` | `[28:24]` | `uint` (5-bit `ceil(log2(width))`) | `NEW` | `[V-BIN]` |
| `CR_ZLS_WORD0` | 2 (64b) | `twiddled` | `[22:22]` | `bool` (`0x40_0000`) | `NEW` | `[V-BIN]` |
| `CR_ZLS_WORD0` | 2 (64b) | `fbcdc_enable` | `[3:3]` | `bool` (`0x8`) | `NEW` | `[V-BIN]` |
| `CR_ZLS_WORD1` | 2 (64b) | `min_x` | `[63:48]` | `uint` (16-bit) | `NEW` | `[V-BIN]` |
| `CR_ZLS_WORD1` | 2 (64b) | `max_x_m1` | `[47:32]` | `uint` (16-bit, `max_x - 1`) | `NEW` | `[V-BIN]` |
| `CR_ZLS_WORD1` | 2 (64b) | `min_y` | `[31:16]` | `uint` (16-bit) | `NEW` | `[V-BIN]` |
| `CR_ZLS_WORD1` | 2 (64b) | `max_y_m1` | `[15:0]` | `uint` (16-bit, `max_y - 1`) | `NEW` | `[V-BIN]` |
| `CR_ZLS_WORD2` | 2 (64b) | `stride_m1` | `[47:32]` | `uint` (16-bit, `stride_pixels - 1`, linear/strided mode) | `NEW` | `[V-BIN]` |

---

## 5. `[GAP-CR-PBE-WORDS]` — Volcanic `PBESTATE` (`STATE_WORD0..2` + `REG_WORD0..2`, `[V-BIN]`)

Upstream `src/imagination/csbgen/volcanic/pbestate.xml` currently defines **only `STATE_WORD0`** (and incorrectly specifies `shift="2"`). On `DXT-48-1536`, PBE uses **3×32-bit `STATE_WORD0..2`** + **3×64-bit `REG_WORD0..2`** with **byte-addressed 48-bit VAs** (`addr[44:0]` + `addr[47]` sign/top bit).

> **Empty-Tile Fast Path (`[V-BIN]`)**: Setting `STATE_WORD0 = 0`, `STATE_WORD1 = 0x4000` (`emptytile = 1`), `STATE_WORD2 = 0`, `REG_WORD0..2 = 0` emits a valid no-op/empty PBE emit.

| Struct / Packet | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Rogue→Volcanic Delta | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `PBESTATE_STATE_WORD0` | 1 (32b) | `address_low` | `[31:0]` | `shift="0"`, `address` (raw low 32 bits of device VA — **fix upstream `shift="2"`!**) | `CHANGED` | `[V-BIN]` |
| `PBESTATE_STATE_WORD1` | 1 (32b) | `yuv_pack_flags` | `[31:30]` | `uint` (YUV packing flags `0x60000000`/`0x80000000`) | `NEW` | `[V-BIN]` |
| `PBESTATE_STATE_WORD1` | 1 (32b) | `gamma` | `[29:29]` | `bool` (sRGB encoding enable) | `MOVED` | `[V-BIN]` |
| `PBESTATE_STATE_WORD1` | 1 (32b) | `mrt_index` | `[25:23]` | `uint` (3-bit: `0..7`) | `MOVED` | `[V-BIN]` |
| `PBESTATE_STATE_WORD1` | 1 (32b) | `norm` | `[22:22]` | `bool` (normalized conversion enable) | `MOVED` | `[V-BIN]` |
| `PBESTATE_STATE_WORD1` | 1 (32b) | `packmode` | `[21:15]` | `enum PACKMODE` (7-bit; existing upstream `PACKMODE` enum values match) | `MOVED` | `[V-BIN]` |
| `PBESTATE_STATE_WORD1` | 1 (32b) | `emptytile` | `[14:14]` | `bool` (`0x4000`) | `MOVED` | `[V-BIN]` |
| `PBESTATE_STATE_WORD1` | 1 (32b) | `address_top` | `[13:13]` | `uint` (`dev_va[47]`, top bit of 48-bit VA) | `WIDENED` | `[V-BIN]` |
| `PBESTATE_STATE_WORD1` | 1 (32b) | `address_high` | `[12:0]` | `shift="32"`, `address` (`dev_va[44:32]`, 13-bit upper VA) | `WIDENED` | `[V-BIN]` |
| `PBESTATE_STATE_WORD2` | 1 (32b) | `source_pos` | `[5:2]` | `uint` (4-bit pixel output register offset) | `MOVED` / `WIDENED` | `[V-BIN]` |
| `PBESTATE_STATE_WORD2` | 1 (32b) | `source_format` | `[1:0]` | `enum`: `0`=F16/F32, `2`=INT/UINT, `3`=YUV | `MOVED` | `[V-BIN]` |
| `PBESTATE_REG_WORD0` | 2 (64b) | `linestride` | `[54:39]` | `uint` (16-bit stride in pixels/blocks) | `WIDENED` (14b→16b) | `[V-BIN]` |
| `PBESTATE_REG_WORD0` | 2 (64b) | `size_z` | `[38:34]` | `uint` (5-bit `ceil(log2(depth))`) | `MOVED` | `[V-BIN]` |
| `PBESTATE_REG_WORD0` | 2 (64b) | `size_y` | `[33:29]` | `uint` (5-bit `ceil(log2(height))`) | `MOVED` | `[V-BIN]` |
| `PBESTATE_REG_WORD0` | 2 (64b) | `size_x` | `[28:24]` | `uint` (5-bit `ceil(log2(width))`) | `MOVED` | `[V-BIN]` |
| `PBESTATE_REG_WORD0` | 2 (64b) | `memlayout` | `[23:22]` | `enum`: `0`=STRIDE, `1`=TWIDDLED_2D, `2`=TWIDDLED_3D, `3`=PAGETILED | `MOVED` | `[V-BIN]` |
| `PBESTATE_REG_WORD0` | 2 (64b) | `swiz_chan3` | `[21:19]` | `enum SWIZ` | `MOVED` | `[V-BIN]` |
| `PBESTATE_REG_WORD0` | 2 (64b) | `swiz_chan2` | `[18:16]` | `enum SWIZ` | `MOVED` | `[V-BIN]` |
| `PBESTATE_REG_WORD0` | 2 (64b) | `swiz_chan1` | `[15:13]` | `enum SWIZ` | `MOVED` | `[V-BIN]` |
| `PBESTATE_REG_WORD0` | 2 (64b) | `swiz_chan0` | `[12:10]` | `enum SWIZ` | `MOVED` | `[V-BIN]` |
| `PBESTATE_REG_WORD0` | 2 (64b) | `dither` | `[9:9]` | `bool` (`0x200`) | `MOVED` | `[V-BIN]` |
| `PBESTATE_REG_WORD0` | 2 (64b) | `tirt_enable` | `[8:8]` | `bool` (`0x100`, always set on `DXT-48-1536` memlayout table) | `NEW` | `[V-BIN]` |
| `PBESTATE_REG_WORD0` | 2 (64b) | `downscale` | `[6:5]` | `enum`: `0`=NONE, `1`=BOX (`0x20`), `3`=MIN/MAX/AVG (`0x60`) | `MOVED` | `[V-BIN]` |
| `PBESTATE_REG_WORD0` | 2 (64b) | `fbc_mode` | `[4:3]` | `enum`: `0`=NONE, `1`=DIRECT_8x8, `2`=DIRECT_16x4, `3`=DIRECT_32x2 | `MOVED` | `[V-BIN]` |
| `PBESTATE_REG_WORD0` | 2 (64b) | `rotation` | `[2:1]` | `enum`: `0`=0°, `1`=90°, `2`=180°, `3`=270° | `MOVED` | `[V-BIN]` |
| `PBESTATE_REG_WORD0` | 2 (64b) | `y_flip` | `[0:0]` | `bool` | `MOVED` | `[V-BIN]` |
| `PBESTATE_REG_WORD1` | 2 (64b) | `minclip_x` | `[63:48]` | `uint` (16-bit) | `WIDENED` (14b→16b) | `[V-BIN]` |
| `PBESTATE_REG_WORD1` | 2 (64b) | `maxclip_x` | `[47:32]` | `uint` (16-bit, inclusive) | `WIDENED` (14b→16b) | `[V-BIN]` |
| `PBESTATE_REG_WORD1` | 2 (64b) | `minclip_y` | `[31:16]` | `uint` (16-bit) | `WIDENED` (14b→16b) | `[V-BIN]` |
| `PBESTATE_REG_WORD1` | 2 (64b) | `maxclip_y` | `[15:0]` | `uint` (16-bit, inclusive) | `WIDENED` (14b→16b) | `[V-BIN]` |
| `PBESTATE_REG_WORD2` | 2 (64b) | `zslice` | `[47:32]` | `uint` (16-bit `zslice` / `depth - 1`) | `WIDENED` | `[V-BIN]` |

---

## 6. `[GAP-ISA-6]` — Volcanic `TEXSTATE` (`volcanic/texstate.xml` & `pco/usclib/tex.cl`, `[V-BIN]`)

### 6.1 Immediate Unblock for `src/imagination/pco/usclib/tex.cl` (L219)

`usclib_volcanic_tex_state_samples` reads the 2-bit MSAA sample count from **`TEXSTATE_IMAGE_WORD2[7:6]`** (`smpcnt`: `0` = 1x, `1` = 2x, `2` = 4x, `3` = 8x):

```c
/* Clean-room Volcanic tex.cl sample-count helper (IMAGE_WORD2[7:6] = smpcnt): */
/* 0 -> 1 sample, 1 -> 2 samples, 2 -> 4 samples, 3 -> 8 samples */
return 1u << ((tex_word2_lo >> 6u) & 3u);
```

### 6.2 Critical `DXT-48-1536` Deltas vs. Upstream `src/imagination/csbgen/volcanic/texstate.xml`

Upstream `volcanic/texstate.xml` targets an earlier 14-bit (`16K`) Volcanic core and **will silently corrupt every texture width, height, and device address on `DXT-48-1536` (`32K` / 48-bit VA)** unless updated:

| Struct / Packet | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Delta vs. Upstream `volcanic/texstate.xml` | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `IMAGE_WORD0` | 2 (64b) | `height` | **`[56:42]`** | `uint` (`height - 1`, **15-bit**, max 32768) | **`MOVED` & `WIDENED`** (upstream `[62:49]` 14b!) | `[V-BIN]` |
| `IMAGE_WORD0` | 2 (64b) | `width` | **`[41:27]`** | `uint` (`width - 1`, **15-bit**, max 32768) | **`MOVED` & `WIDENED`** (upstream `[48:35]` 14b!) | `[V-BIN]` |
| `IMAGE_WORD0` | 2 (64b) | *(legacy `minlod`)* | *(absent)* | Not in `IMAGE_WORD0[34:23]` on `DXT-48-1536` | **`REMOVED`** from `IMAGE_WORD0` | `[V-BIN]` |
| `IMAGE_WORD0` | 2 (64b) | `texformat` | `[22:15]` | `enum FORMAT` (8-bit) | `IDENTICAL` | `[V-BIN]` |
| `IMAGE_WORD0` | 2 (64b) | `swiz0..swiz3` | `[14:12]`/`[11:9]`/`[8:6]`/`[5:3]` | `enum SWIZ` | `IDENTICAL` | `[V-BIN]` |
| `IMAGE_WORD0` | 2 (64b) | `textype` | `[2:0]` | `enum TEXTYPE` (`0`=1D, `1`=2D, `2`=3D, `3`=CUBE, `4`=STRIDE, `5`=BUFFER) | `IDENTICAL` | `[V-BIN]` |
| `IMAGE_WORD1` | 2 (64b) | `alpha_msb` | `[62:62]` | `bool` | `IDENTICAL` | `[V-BIN]` |
| `IMAGE_WORD1` | 2 (64b) | `mipmaps_present` | `[61:61]` | `bool` (`num_mip_levels >= 2`) | `IDENTICAL` | `[V-BIN]` |
| `IMAGE_WORD1` *(twiddled/3D)* | 2 (64b) | `depth` | `[58:48]` | `uint` (`depth - 1`, 11-bit) | `IDENTICAL` | `[V-BIN]` |
| `IMAGE_WORD1` *(strided `type=4`)* | 2 (64b) | `stride` | `[60:46]` | `uint` (`stride - 1`, 15-bit) | `IDENTICAL` | `[V-BIN]` |
| `IMAGE_WORD1` | 2 (64b) | `srgb_or_aux_flag` | `[47:47]` | `bool` (`1ULL << 47`) | `NEW` | `[V-BIN]` |
| `IMAGE_WORD1` | 2 (64b) | `border` | `[46:46]` | `bool` (`1ULL << 46`) | `IDENTICAL` | `[V-BIN]` |
| `IMAGE_WORD1` | 2 (64b) | `texaddr_top` | **`[45:45]`** | `uint` (`dev_va[47]`, top bit of 48-bit VA) | **`CHANGED`** (upstream used `shift="2"`!) | `[V-BIN]` |
| `IMAGE_WORD1` | 2 (64b) | `texaddr_low` | **`[44:0]`** | `shift="0"`, `address` (`dev_va[44:0]`, raw byte VA!) | **`CHANGED`** (upstream used `shift="2"`!) | `[V-BIN]` |
| `IMAGE_WORD2` *(buffer `type=5`)* | 2 (64b) | `buffer_width_m1` | `[62:31]` | `uint` (32-bit texel count - 1) | `NEW` | `[V-BIN]` |
| `IMAGE_WORD2` *(buffer `type=5`)* | 2 (64b) | `buffer_offset` | `[30:0]` | `uint` (31-bit buffer offset) | `NEW` | `[V-BIN]` |
| `IMAGE_WORD2` *(planar YUV)* | 2 (64b) | `csc_coeff_index` | `[63:60]` | `uint` (4-bit CSC table index) | `NEW` | `[V-BIN]` |
| `IMAGE_WORD2` *(planar YUV)* | 2 (64b) | `plane2_addr_top` / `low` | `[59:59]` / `[58:14]` | `dev_va[47]` / `dev_va[44:0]` | `NEW` | `[V-BIN]` |
| `IMAGE_WORD2` | 2 (64b) | `num_mip_levels` | `[13:10]` | `uint` (`levels - 1`, 4-bit) | `IDENTICAL` | `[V-BIN]` |
| `IMAGE_WORD2` | 2 (64b) | `fmt_flags` | `[9:8]` | `uint` (2-bit format table flags) | `NEW` | `[V-BIN]` |
| `IMAGE_WORD2` | 2 (64b) | **`smpcnt`** | **`[7:6]`** | `enum`: `0`=1x, `1`=2x, `2`=4x, `3`=8x (**unblocks `tex.cl`!**) | **`NEW` (missing upstream)** | `[V-BIN]` |
| `IMAGE_WORD2` | 2 (64b) | `twocomp_gamma` | `[5:4]` | `enum`: `0`=OFF, `1`=R/RGB, `3`=RG/RGBA | `IDENTICAL` | `[V-BIN]` |
| `IMAGE_WORD2` | 2 (64b) | `baselevel` | `[3:0]` | `uint` (4-bit base mip level) | `IDENTICAL` | `[V-BIN]` |
| `IMAGE_WORD3` | 2 (64b) | `fbc_mode` | `[50:49]` | `enum`: `0`=NONE, `1`=8x8, `2`=16x4, `3`=32x2 | **`NEW` (entire struct missing upstream)** | `[V-BIN]` |
| `IMAGE_WORD3` | 2 (64b) | `astc_flag` | `[48:48]` | `bool` | `NEW` | `[V-BIN]` |
| `IMAGE_WORD3` | 2 (64b) | `yuv_chroma_flags` | `[47:46]` | `uint` (chroma siting flags) | `NEW` | `[V-BIN]` |
| `IMAGE_WORD3` | 2 (64b) | `plane3_addr_top` / `low` | `[45:45]` / `[44:0]` | `dev_va[47]` / `dev_va[44:0]` (3-plane YUV) | `NEW` | `[V-BIN]` |
| `SAMPLER_WORD0` | 2 (64b) | *(all fields `[63:0]`)* | `[63:0]` | `dadjust[12:0]`, `minlod[24:13]`, `maxlod[36:25]`, `anisoctl[39:37]`, `mag/min/mipfilter[44:40]`, `addrmode_u/v/w[53:45]`, `non_norm[56]`, `cmp_mode[59:57]`, `cemedge[63]` | **`IDENTICAL`** | `[V-BIN]` |
| `SAMPLER_WORD1` | 2 (64b) | `bordercolor_index` | `[11:0]` | `uint` (12-bit index into border-color table, `0..4095`) | **`NEW` (entire struct missing upstream)** | `[V-BIN]` |

---

## 7. Upstream `volcanic/cdm.xml` & `volcanic/pds.xml` Delta Check (`[V-BIN]`)

### 7.1 `volcanic/cdm.xml` Deltas (`25.3@6908880` vs. Upstream XML)

| Struct | DWords | Field | Bits `[end:start]` | Shift / Type / Enum | Delta vs. Upstream `volcanic/cdm.xml` | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| `KERNEL0` | 1 | `block_type` | `[31:30]` | `enum`: `0`=COMPUTE_KERNEL, `1`=STREAM_LINK, `2`=STREAM_TERMINATE, **`3`=COMPUTE_KERNEL_EXT / FENCE** (`0xc000_0000`, emitted by Vulkan compute dispatches & GLES fences!) | **`NEW` enum value `3`** | `[V-BIN]` |
| `KERNEL0` | 1 | `indirect_present` | `[29:29]` | `bool` | `IDENTICAL` | `[V-BIN]` |
| `KERNEL0` | 1 | `event_object_present` | `[28:28]` | `bool` (`0x1000_0000`, global offsets / event object present) | **`NEW` (missing upstream)** | `[V-BIN]` |
| `KERNEL0` | 1 | `pds_seq_dep` | `[27:27]` | `bool` (`0x0800_0000`, PDS sequential dependency) | **`NEW` (missing upstream)** | `[V-BIN]` |
| `KERNEL0` | 1 | `wg_mode` | `[21:20]` | `uint` (2-bit workgroup / local-mem dispatch mode) | **`NEW` (missing upstream)** | `[V-BIN]` |
| `KERNEL0` | 1 | `usc_unified_size` | **`[19:12]`** | `uint` (**8-bit**, `(dwords + 1) >> 1`) | **`WIDENED`** (upstream `[18:12]` 7-bit!) | `[V-BIN]` |
| `KERNEL0` | 1 | `pds_temp_size` | `[11:7]` | `uint` (5-bit; 0 in const-load kernels) | `IDENTICAL` | `[V-BIN]` |
| `KERNEL0` | 1 | `pds_data_size` | `[6:1]` | `uint` (6-bit, `(dwords + 3) >> 2`; emitters mask `& 0x1fe` when `pds_temp_size==0`) | `IDENTICAL` | `[V-BIN]` |
| `KERNEL0` | 1 | `usc_target` | `[0:0]` | `enum`: `0`=ALL, `1`=ANY | `IDENTICAL` | `[V-BIN]` |
| `KERNEL1` | 1 | `data_addr` / `sd_type` | `[31:4]` / `[3:2]` | `shift="4"` / `enum SD_TYPE` | `IDENTICAL` | `[V-BIN]` |
| `KERNEL1` | 1 | `usc_common_shared` | **`[1:1]`** | `bool` (`0x2`, shared/constant-load USC common store enable) | **`NEW` (missing upstream)** | `[V-BIN]` |
| `KERNEL2..7` | 1 ea | `code_addr`, `wg_x/y/z`, `indirect_addr` | same | Exact match | `IDENTICAL` | `[V-BIN]` |
| `KERNEL8` | 1 | `max_instances` | `[31:25]` | `uint` (7-bit, max 127) | `IDENTICAL` | `[V-BIN]` |
| `KERNEL8` | 1 | `event_type` | **`[12:11]`** | `uint` (2-bit barrier/event type) | **`NEW` (missing upstream)** | `[V-BIN]` |
| `KERNEL8` | 1 | `usc_common_size` | **`[10:0]`** | `uint` (`(bytes + 15) >> 4`, 16-byte chunks; emitters mask low 16b when `event_type==0`) | **`NEW` (missing upstream)** | `[V-BIN]` |
| `KERNEL9` | 1 | `workgroup_size_x/y/z` | `[29:20]`/`[19:10]`/`[9:0]` | `uint` (`size - 1`, 10-bit each) | `IDENTICAL` | `[V-BIN]` |
| `STREAM_LINK0..1` | 2 | `block_type=1`, `addr[39:2]` | same | Exact match | `IDENTICAL` | `[V-BIN]` |
| `LM_TERMINATE` | 0 | *(none)* | — | **No-op** on `DXT-48-1536` (emitter returns immediately without writing words) | `REMOVED` | `[V-BIN]` |

### 7.2 `volcanic/pds.xml` Verification (`25.3@6908880` vs. Upstream XML)

- **100% MATCH — ZERO DELTAS REQUIRED** on `DOUT` instruction opcodes and `DOUTU` 64-bit execution descriptor:
  - `DOUTD` = `0xf0000000`, `DOUTU` = `0xf0000002`, `DOUTI` = `0xf0000004`; `[27]`=`cc`, `[26]`=`end`, `[19:11]`=`src0` (9-bit), `[10:3]`=`src1` (8-bit).
  - `DOUTU` 64-bit data descriptor: `exe_addr >> 2` at `[29:0]`, `mode[30]`, `sample_rate[31]`, `instance_rate[32]`, `temps >> 1` at `[39:33]`, task flags `[47:41]`/`[51:48]`/`[62:56]` match upstream `volcanic/pds.xml`.

---

## 8. Remaining `[OPEN]` Items & Phase 4 `mesa-volcanic` Action Checklist

### 8.1 Consolidated `[OPEN]` Register (Narrow, Non-Blocking for Phase 4 Brings-Up)

1. **`DCE_TESS_DDM_CONFIG` (`0x03`) `[5:0]`**: Exact per-bit split of domain/partitioning/output-topology inside `[5:0]` (only needed for tessellation shaders).
2. **`DCE_SHADER_STATE0` `[52:51]`** and **`DCE_PREDICATE1` `[31:0]`**: Exact semantics of auxiliary barrier/predicate flags (standard VS/FS draws use `0`).
3. **`TA_OUTPUT_SEL[22]`** & **`TA_STATE_TERMINATE1`** non-zero fields: Single-RT full-screen draws set `TERMINATE1 = 0`.
4. **`RGX_CR_SOFT_RESET` (`0x0100`) individual bit names** inside `INTERCONNECT_GROUP{11,38,43,44,45}`, `JONES_CORE_GROUP{1,4,5,6,7,15}`, and unused valid bits in `0x7fffffe0_007bdeff` (the proven 6-step `RGXStart` masks are already complete and sufficient for full cold-boot and reset in `drm/imagination`).

### 8.2 Phase 4 `mesa-volcanic` Immediate Implementation Checklist

1. **Author `csbgen/volcanic/vdm.xml`** using Section 1 (64-bit QWORD DCE structs `0x01..0x0c` + `DCE_HANDLE_ENTRY` + draw-arg descriptors).
2. **Author `csbgen/volcanic/ppp.xml`** using Section 2 (`TA_STATE_HEADER`, shifted `ISPCTL`/`ISPA`, 16B indirect Pixel Shader Descriptor Table, FSR word, 10-bit 32K region/terminate clips).
3. **Update `csbgen/volcanic/texstate.xml` & `pco/usclib/tex.cl` L219**:
   - Fix `IMAGE_WORD0` `width=[41:27]` (15b) & `height=[56:42]` (15b) for `DXT-48-1536` (guard behind core feature flag if older 14b Volcanic cores remain supported).
   - Fix `IMAGE_WORD1` `texaddr` to byte-addressed `[44:0]` + `[45]=VA[47]`.
   - Add `IMAGE_WORD2.smpcnt[7:6]`, `IMAGE_WORD3`, and `SAMPLER_WORD1.bordercolor_index[11:0]`.
   - Implement `usclib_volcanic_tex_state_samples` using `1u << ((word2_lo >> 6u) & 3u)`.
4. **Complete `csbgen/volcanic/pbestate.xml`**: Fix `STATE_WORD0` to `shift="0"`, add `STATE_WORD1..2` and 64-bit `REG_WORD0..2` from Section 5.
5. **Update `csbgen/volcanic/cdm.xml`**: Add `BLOCK_TYPE=3`, widen `KERNEL0.usc_unified_size` to `[19:12]`, add `KERNEL0[28]/[27]/[21:20]`, `KERNEL1[1]`, and `KERNEL8` `usc_common_size[10:0]` / `event_type[12:11]`.
6. **Update `csbgen/volcanic/cr.xml` & `drm/imagination` reset path**: Add `RGX_CR_SOFT_RESET` mask `0x7fffffe0_007bdeff` (bit `37` = `GARTEN_EN`), 6-step reset sequence, `S7_TOP` `ISP_CTL`/`ISP_XTP_PIPE_ENABLE`, and 4-word ZLS v2 (`ISP_ZLSCTL` + `ZLS_WORD0..2`).
