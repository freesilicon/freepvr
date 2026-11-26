# FreePVR / FreeUSC — `rgx.sh` Container Structural Inventory (`rgx.sh.71.2.2448.1212`)

Clean-room structural inventory of the PowerVR Volcanic Transfer-Queue (TQ) precompiled shader container.
Zero proprietary code bytes are embedded in this document; extracted private binary slices reside locally under
`local-captures/rgx_sh_chunks` for offline Volcanic USC decoder validation.

## 1. Container Metadata

| Field | Value | Notes |
| --- | --- | --- |
| Filename | `rgx.sh.71.2.2448.1212` | Shipping Pixel 10 `DXT-48-1536` TQ shader bundle |
| Target BVNC | `71.2.2448.1212` | Branch `71`, Version `2`, Number of scalable units `2448`, Config `1212` |
| File Size | `380,392` B (`0x5cde8`) | `client_mem_size + usc_mem_size == file_size` (0 trailing bytes) |
| File SHA-256 | `f4b2994f2e74fe9851c978b2c9bd33c0baedac7acf18b3d32c895dede3b1c04a` | Exact input fingerprint |
| Header Version (`+0x00`) | `0x00021903` | Checked by kernel `PVRSRVTQLoadShaders` |
| Shader Entry Count (`+0x04`) | `1342` (`0x53e`) | Indexed via binary search over sorted `u32` key hashes |
| USC Code Pool Size (`+0x08`) | `262,280` B (`0x40088`) | Mapped to GPU USC heap (`<= 2 MiB` kernel limit) |
| Client Metadata Size (`+0x0c`) | `118,112` B (`0x1cd60`) | Returned by `RGXShaderCLIMemSize`; `16 + N * 88` |
| Hash Table Monotonicity | `True` | Strictly ascending `u32` hashes (`0x00000000` .. `0x01320008`) |
| Unaccounted USC Bytes | `0` B | 100.00% of USC pool covered by primary + secondary slices |

## 2. Binary Section Map (Little-Endian)

| Section | File Offset | Size (Bytes) | Stride / Count | Description |
| --- | --- | --- | --- | --- |
| Container Header | `0x000000` | `16` (`0x10`) | `4 x u32` | `version`, `num_shaders`, `usc_mem_size`, `client_mem_size` |
| Shader Properties Table | `0x000010` | `101,992` (`0x18e68`) | `76 B (0x4c) x 1342` | Per-shader TQ descriptor, reg hints (`+0x04`), primary/secondary entry deltas (`+0x08`/`+0x0a`), constants |
| USC Offset Pairs Table | `0x018e78` | `10,736` (`0x29f0`) | `8 B (2 x u32) x 1342` | `(primary_usc_offset, secondary_or_end_offset)` relative to `0x1cd60` |
| Sorted Key Hash Table | `0x01b868` | `5,368` (`0x14f8`) | `4 B (u32) x 1342` | Sorted `TQ_HashShader` keys for `O(log N)` lookup |
| Volcanic USC Code Pool | `0x01cd60` | `262,280` (`0x40088`) | `1378` chunks | `1342` primary USC chunks + `36` secondary (SA/const-calc) USC chunks |

### USC vs. PDS Chunk Classification

- **USC Chunks**: `1378` total (`1342` primary pixel/TQ USC shaders + `36` secondary constant-calculation / SA-update USC shaders).
- **PDS Chunks**: **`0`** in `rgx.sh`. Transfer Queue PDS programs are emitted dynamically at runtime by userspace (`PDSGENShaderSACode` / `PDSGENTDMShaderSACode`), not bundled inside `rgx.sh`.
- **Secondary Shader Rule**: Every entry with `prop[+0x0a] != 0xffff` (`36/36` exact match) owns a secondary USC program spanning `[ranges[i].secondary_offset, next_primary_offset)`; all other `1306/1306` entries have `prop[+0x0a] == 0xffff` and zero gap before `ranges[i+1].primary_offset`.

## 3. Chunk Size, Uniqueness & Alignment Statistics

| Class | Chunk Count | Unique SHA-256 | Total Bytes | Min | Median | Mean | Max | 4-Byte Aligned Offsets & Sizes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Primary USC | 1342 | 1044 | 249,776 B | 32 B | 116 B | 186.1 B | 2976 B | `True` (719/1342 are 8B-aligned) |
| Secondary USC (SA/Const-Calc) | 36 | 8 | 12,504 B | 76 B | 136 B | 347.3 B | 924 B | `True` |

- **Temp-reg hint distribution (`prop[+0x04]` low u16, top values)**: `2`: 100, `3`: 220, `4`: 316, `5`: 46, `6`: 100, `7`: 36, `8`: 92, `9`: 12, `10`: 44, `12`: 104, `13`: 20, `14`: 12
- **Primary/Secondary entry deltas**: All `1342` primary deltas and all `36` active secondary deltas are `0x0000`.

### Primary USC Chunk Size Distribution

| Size Bucket | Count | Total Bytes |
| --- | --- | --- |
| 32 - 64 B | 317 | 15,660 B |
| 68 - 128 B | 490 | 47,536 B |
| 132 - 256 B | 279 | 49,140 B |
| 260 - 512 B | 184 | 65,012 B |
| 516 - 1024 B | 36 | 20,672 B |
| 1028 - 2048 B | 32 | 40,252 B |
| 2052 - 4096 B | 4 | 11,504 B |

## 4. Recommended Starter Targets for Volcanic USC Decoder Bring-Up

### Smallest Primary USC Shaders (Best for Initial Framing & Op Validation)

| Index | Key Hash | USC Offset | File Offset | Size (B) | Reg Hint | SHA-256 (prefix) |
| --- | --- | --- | --- | --- | --- | --- |
| `333` | `0x001f0008` | `0x0c190` | `0x28ef0` | `32` | `3` | `620aea2422e62cd1` |
| `366` | `0x00220008` | `0x0d208` | `0x29f68` | `32` | `3` | `3e6927e26680f2bf` |
| `967` | `0x005f0008` | `0x29934` | `0x46694` | `32` | `3` | `620aea2422e62cd1` |
| `1000` | `0x00620008` | `0x2a9ac` | `0x4770c` | `32` | `3` | `3e6927e26680f2bf` |
| `117` | `0x00100008` | `0x02e24` | `0x1fb84` | `36` | `2` | `13c37db8b2cb1684` |
| `181` | `0x00140008` | `0x05adc` | `0x2283c` | `36` | `2` | `617c16987787a45d` |
| `196` | `0x00150008` | `0x0636c` | `0x230cc` | `36` | `2` | `786b19df3fe490c1` |
| `223` | `0x00160008` | `0x07404` | `0x24164` | `36` | `2` | `7127d25f7ab78ff0` |
| `242` | `0x00170008` | `0x07e68` | `0x24bc8` | `36` | `2` | `2b39854a66744314` |
| `272` | `0x00190008` | `0x08d48` | `0x25aa8` | `36` | `2` | `5efeb66873effd3d` |

### Largest Primary USC Shaders (Complex Multi-Sample / Filter / Conversion Kernels)

| Index | Key Hash | USC Offset | Size (B) | Has Secondary | Secondary Size (B) | SHA-256 (prefix) |
| --- | --- | --- | --- | --- | --- | --- |
| `532` | `0x00300000` | `0x16f10` | `1456` | `True` | `852` | `bd0cf6f5f1ad5d5e` |
| `533` | `0x00300008` | `0x17814` | `1460` | `True` | `852` | `6552ba0329c5956d` |
| `1336` | `0x01300000` | `0x3c0bc` | `2776` | `True` | `720` | `cf07f1ab15db9316` |
| `1334` | `0x012f0000` | `0x3a4a0` | `2780` | `True` | `720` | `5717e2e4b205785f` |
| `1337` | `0x01300008` | `0x3ce64` | `2972` | `True` | `720` | `7f30dc2b12aa1f0c` |
| `1335` | `0x012f0008` | `0x3b24c` | `2976` | `True` | `720` | `01c1c72539202fbf` |

### Secondary (Constant-Calculation / SA-Update) USC Shaders (First 10 of 36)

| Index | Key Hash | Secondary USC Offset | Secondary File Offset | Secondary Size (B) | SHA-256 (prefix) |
| --- | --- | --- | --- | --- | --- |
| `169` | `0x00130048` | `0x05270` | `0x21fd0` | `136` | `a0c469f4da0b295a` |
| `184` | `0x00140048` | `0x05cd0` | `0x22a30` | `136` | `a0c469f4da0b295a` |
| `199` | `0x00150048` | `0x064c8` | `0x23228` | `136` | `a0c469f4da0b295a` |
| `327` | `0x001c0048` | `0x0bb0c` | `0x2886c` | `136` | `a0c469f4da0b295a` |
| `329` | `0x001d0048` | `0x0be2c` | `0x28b8c` | `136` | `a0c469f4da0b295a` |
| `331` | `0x001e0048` | `0x0c0e4` | `0x28e44` | `136` | `a0c469f4da0b295a` |
| `526` | `0x002d0000` | `0x15a80` | `0x327e0` | `76` | `5ae5b01b7e40d03c` |
| `527` | `0x002d0008` | `0x15b48` | `0x328a8` | `76` | `5ae5b01b7e40d03c` |
| `528` | `0x002e0000` | `0x15c08` | `0x32968` | `76` | `5ae5b01b7e40d03c` |
| `529` | `0x002e0008` | `0x15cc4` | `0x32a24` | `76` | `5ae5b01b7e40d03c` |

## 5. Reproduction

```bash
python3 tools/freeusc/rgx_sh_parser.py \
  local-captures/rgx.sh.71.2.2448.1212 \
  --extract-dir local-captures/rgx_sh_chunks \
  --inventory-md spec/freeusc/RGX-SH-INVENTORY.md
```
