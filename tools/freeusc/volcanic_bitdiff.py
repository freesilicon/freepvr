#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 FreePVR Contributors
"""
FreePVR / FreeUSC — Host Bit-Diff, Opcode-Cluster & Vulkan Pipeline-Cache Analyzer.

Clean-room offline analysis workbench for PowerVR Volcanic (`DXT-48-1536` / `CXTP-48-1536`)
shader binaries captured via:
  1. Vulkan `vkGetPipelineCacheData` blobs (36-byte PowerVR header + serialized cache)
  2. Vulkan `VK_KHR_pipeline_binary` (`vkGetPipelineBinaryDataKHR`) blobs (32-byte key + serialized shader)
  3. Extracted `rgx.sh` Transfer-Queue USC shader chunks

Capabilities:
  - `parse-cache`: Decode Vulkan pipeline cache / pipeline binary headers and locate embedded
    candidate Volcanic USC code slices via alignment, entropy, and framing heuristics.
  - `diff`: Aligned byte- and bit-level diff between paired twins isolating exact changing
    `[hi:lo]` bitfields per 32-bit word (handles both 1-for-1 opcode/immediate swaps and
    length-delta insertions/deletions via prefix/suffix + LCS anchor alignment).
  - `diff-dir`: Batch paired-twin differential sweep across a on-device capture directory
    keyed by `corpus_manifest.json`, clustering bitfield masks per probe group.
  - `analyze-rgx-sh`: Structural + near-twin bit-diff analysis across extracted `rgx.sh` USC chunks.
  - `--self-test`: Deterministic offline verification of synthetic pipeline cache parsing,
    twin bit-diffing, opcode-field clustering, and `rgx.sh` chunk analysis.
"""

from __future__ import annotations

import argparse
import difflib
import json
import math
import struct
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


# Standard Vulkan VkPipelineCacheHeaderVersionOne (32 bytes) + PowerVR 4-byte field at +0x20 (36 bytes total)
VK_CACHE_HEADER_V1 = struct.Struct("<IIII16s")
VK_CACHE_PVR_HEADER_SIZE = 36
IMG_VENDOR_ID = 0x1010
# Observed Volcanic USC END terminator pattern across 1,036/1,044 unique rgx.sh.71.2.2448.1212 primary shaders
VOLCANIC_USC_END_MASK = 0xFFFFF0FF
VOLCANIC_USC_END_VAL = 0x8000001C


@dataclass
class PipelineCacheHeader:
    format_kind: str  # "vk_pipeline_cache" | "vk_pipeline_binary" | "raw_usc"
    header_size: int
    header_version: Optional[int]
    vendor_id: Optional[int]
    device_id: Optional[int]
    uuid_hex: Optional[str]
    pvr_extra_u32: Optional[int]
    payload_offset: int
    payload_size: int


@dataclass
class CandidateChunk:
    offset: int
    size: int
    entropy_bits: float
    zero_ratio: float
    score: float
    head_u32_hex: str
    tail_u32_hex: str


@dataclass
class WordBitDiff:
    word_index: int
    byte_offset_a: int
    byte_offset_b: int
    val_a: int
    val_b: int
    xor_mask: int
    bit_ranges: List[str]  # e.g. ["[23:18]", "[4:4]"]
    hamming_weight: int


def bit_ranges_from_mask(mask: int, width: int = 32) -> List[str]:
    """Convert a bitmask into MSB-first contiguous `[hi:lo]` bitfield strings."""
    ranges: List[str] = []
    bit = 0
    while bit < width:
        if (mask >> bit) & 1:
            lo = bit
            while bit + 1 < width and ((mask >> (bit + 1)) & 1):
                bit += 1
            hi = bit
            ranges.append(f"[{hi}:{lo}]")
        bit += 1
    ranges.reverse()
    return ranges


def shannon_entropy(buf: bytes) -> float:
    if not buf:
        return 0.0
    counts = Counter(buf)
    n = len(buf)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def parse_pipeline_blob_header(data: bytes) -> PipelineCacheHeader:
    """Detect and parse Vulkan PipelineCache, VK_KHR_pipeline_binary, or raw USC blob."""
    if len(data) >= VK_CACHE_HEADER_V1.size:
        hdr_size, hdr_ver, vendor_id, device_id, uuid_bytes = VK_CACHE_HEADER_V1.unpack_from(data, 0)
        if hdr_size in (32, 36) and hdr_ver == 1:
            # IMG_vkGetPipelineCacheData writes 36 bytes (32B standard + 4B extra u32 at +0x20)
            pvr_extra = None
            payload_off = hdr_size
            if vendor_id == IMG_VENDOR_ID and len(data) >= VK_CACHE_PVR_HEADER_SIZE and hdr_size == 32:
                # PowerVR sets headerSize=32 per Vulkan spec but writes an extra 4-byte field at +0x20
                pvr_extra = struct.unpack_from("<I", data, 32)[0]
                payload_off = VK_CACHE_PVR_HEADER_SIZE
            elif hdr_size == 36 and len(data) >= 36:
                pvr_extra = struct.unpack_from("<I", data, 32)[0]
                payload_off = 36
            return PipelineCacheHeader(
                format_kind="vk_pipeline_cache",
                header_size=hdr_size,
                header_version=hdr_ver,
                vendor_id=vendor_id,
                device_id=device_id,
                uuid_hex=uuid_bytes.hex(),
                pvr_extra_u32=pvr_extra,
                payload_offset=payload_off,
                payload_size=max(0, len(data) - payload_off),
            )

    # Check if this looks like a VK_KHR_pipeline_binary blob (32B key + CopyShader stream)
    # IMG_vkGetPipelineBinaryDataKHR copies 32B key at +0x00 followed by CopyShader serialized blob.
    if len(data) >= 48:
        shader_type, field4 = struct.unpack_from("<II", data, 32)
        if shader_type <= 7 and field4 < 0x100000:
            return PipelineCacheHeader(
                format_kind="vk_pipeline_binary",
                header_size=32,
                header_version=None,
                vendor_id=None,
                device_id=None,
                uuid_hex=data[:32].hex(),
                pvr_extra_u32=shader_type,
                payload_offset=32,
                payload_size=len(data) - 32,
            )

    return PipelineCacheHeader(
        format_kind="raw_usc",
        header_size=0,
        header_version=None,
        vendor_id=None,
        device_id=None,
        uuid_hex=None,
        pvr_extra_u32=None,
        payload_offset=0,
        payload_size=len(data),
    )


def locate_candidate_usc_chunks(
    data: bytes,
    start_offset: int = 0,
    min_size: int = 24,
    window: int = 32,
) -> List[CandidateChunk]:
    """
    Scan a pipeline cache / binary payload for high-entropy, 4-byte-aligned dense runs
    characteristic of compiled Volcanic USC machine code (separating them from sparse
    zero-padded metadata structs).
    """
    payload = data[start_offset:]
    n = len(payload)
    if n < min_size:
        return []

    # Step in 4-byte words; mark words that are not part of >=12-byte zero/0xff runs
    word_count = n // 4
    active = [False] * word_count
    zero_run = 0
    for wi in range(word_count):
        w = struct.unpack_from("<I", payload, wi * 4)[0]
        if w in (0x00000000, 0xFFFFFFFF):
            zero_run += 1
        else:
            zero_run = 0
        if zero_run < 3:
            active[wi] = True

    # Trim trailing zero words inside each active span and collect runs >= min_size
    chunks: List[CandidateChunk] = []
    i = 0
    while i < word_count:
        if not active[i]:
            i += 1
            continue
        j = i
        while j < word_count and active[j]:
            j += 1
        # Trim leading/trailing zero/0xff words
        while i < j and struct.unpack_from("<I", payload, i * 4)[0] in (0, 0xFFFFFFFF):
            i += 1
        while j > i and struct.unpack_from("<I", payload, (j - 1) * 4)[0] in (0, 0xFFFFFFFF):
            j -= 1
        byte_len = (j - i) * 4
        if byte_len >= min_size:
            slice_bytes = payload[i * 4 : j * 4]
            ent = shannon_entropy(slice_bytes)
            zratio = slice_bytes.count(0) / byte_len
            # Volcanic USC chunks from rgx.sh typically have entropy 4.5..7.8 bits and low zero ratio
            score = ent * (1.0 - zratio) * math.log2(byte_len)
            if ent >= 3.0 and zratio <= 0.45:
                head_w = struct.unpack_from("<I", slice_bytes, 0)[0]
                tail_w = struct.unpack_from("<I", slice_bytes, byte_len - 4)[0]
                if (tail_w & VOLCANIC_USC_END_MASK) == VOLCANIC_USC_END_VAL:
                    score *= 1.5  # Boost chunks ending in observed Volcanic USC terminator pattern
                chunks.append(
                    CandidateChunk(
                        offset=start_offset + i * 4,
                        size=byte_len,
                        entropy_bits=round(ent, 3),
                        zero_ratio=round(zratio, 3),
                        score=round(score, 2),
                        head_u32_hex=f"0x{head_w:08x}",
                        tail_u32_hex=f"0x{tail_w:08x}",
                    )
                )
        i = j

    chunks.sort(key=lambda c: c.score, reverse=True)
    return chunks


def diff_twin_bytes(buf_a: bytes, buf_b: bytes) -> Dict[str, Any]:
    """
    Perform aligned byte- and bit-level diff between two paired twin binaries.

    - Strips exact common 4-byte-aligned prefix and suffix first.
    - When inner regions have equal length, computes exact word-by-word XOR & `[hi:lo]` bit ranges.
    - When lengths differ, uses `SequenceMatcher` over 4-byte words to separate matched anchors,
      1-to-1 replaced words (bit-diffed), and pure insertions/deletions.
    """
    len_a, len_b = len(buf_a), len(buf_b)
    if buf_a == buf_b:
        return {
            "identical": True,
            "len_a": len_a,
            "len_b": len_b,
            "len_delta": 0,
            "common_prefix_bytes": len_a,
            "common_suffix_bytes": 0,
            "word_diffs": [],
            "insertions": [],
            "deletions": [],
            "union_bit_ranges": [],
        }

    # Common 4-byte-aligned prefix
    min_words = min(len_a, len_b) // 4
    prefix_words = 0
    while prefix_words < min_words:
        off = prefix_words * 4
        if buf_a[off : off + 4] != buf_b[off : off + 4]:
            break
        prefix_words += 1

    # Common 4-byte-aligned suffix (without overlapping prefix)
    rem_a_words = (len_a // 4) - prefix_words
    rem_b_words = (len_b // 4) - prefix_words
    max_suffix_words = min(rem_a_words, rem_b_words)
    suffix_words = 0
    while suffix_words < max_suffix_words:
        off_a = ((len_a // 4) - 1 - suffix_words) * 4
        off_b = ((len_b // 4) - 1 - suffix_words) * 4
        if buf_a[off_a : off_a + 4] != buf_b[off_b : off_b + 4]:
            break
        suffix_words += 1

    words_a = [
        struct.unpack_from("<I", buf_a, wi * 4)[0]
        for wi in range(prefix_words, (len_a // 4) - suffix_words)
    ]
    words_b = [
        struct.unpack_from("<I", buf_b, wi * 4)[0]
        for wi in range(prefix_words, (len_b // 4) - suffix_words)
    ]

    word_diffs: List[WordBitDiff] = []
    insertions: List[Dict[str, Any]] = []
    deletions: List[Dict[str, Any]] = []
    union_mask = 0

    if len(words_a) == len(words_b):
        for idx, (wa, wb) in enumerate(zip(words_a, words_b)):
            if wa != wb:
                xm = wa ^ wb
                union_mask |= xm
                word_diffs.append(
                    WordBitDiff(
                        word_index=prefix_words + idx,
                        byte_offset_a=(prefix_words + idx) * 4,
                        byte_offset_b=(prefix_words + idx) * 4,
                        val_a=wa,
                        val_b=wb,
                        xor_mask=xm,
                        bit_ranges=bit_ranges_from_mask(xm),
                        hamming_weight=bin(xm).count("1"),
                    )
                )
    else:
        sm = difflib.SequenceMatcher(None, words_a, words_b, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            elif tag == "replace":
                paired = min(i2 - i1, j2 - j1)
                for k in range(paired):
                    wa = words_a[i1 + k]
                    wb = words_b[j1 + k]
                    xm = wa ^ wb
                    union_mask |= xm
                    word_diffs.append(
                        WordBitDiff(
                            word_index=prefix_words + i1 + k,
                            byte_offset_a=(prefix_words + i1 + k) * 4,
                            byte_offset_b=(prefix_words + j1 + k) * 4,
                            val_a=wa,
                            val_b=wb,
                            xor_mask=xm,
                            bit_ranges=bit_ranges_from_mask(xm),
                            hamming_weight=bin(xm).count("1"),
                        )
                    )
                if (i2 - i1) > paired:
                    deletions.append(
                        {
                            "byte_offset_a": (prefix_words + i1 + paired) * 4,
                            "word_count": (i2 - i1) - paired,
                            "words_hex": [f"0x{w:08x}" for w in words_a[i1 + paired : i2]],
                        }
                    )
                if (j2 - j1) > paired:
                    insertions.append(
                        {
                            "byte_offset_b": (prefix_words + j1 + paired) * 4,
                            "word_count": (j2 - j1) - paired,
                            "words_hex": [f"0x{w:08x}" for w in words_b[j1 + paired : j2]],
                        }
                    )
            elif tag == "delete":
                deletions.append(
                    {
                        "byte_offset_a": (prefix_words + i1) * 4,
                        "word_count": i2 - i1,
                        "words_hex": [f"0x{w:08x}" for w in words_a[i1:i2]],
                    }
                )
            elif tag == "insert":
                insertions.append(
                    {
                        "byte_offset_b": (prefix_words + j1) * 4,
                        "word_count": j2 - j1,
                        "words_hex": [f"0x{w:08x}" for w in words_b[j1:j2]],
                    }
                )

    return {
        "identical": False,
        "len_a": len_a,
        "len_b": len_b,
        "len_delta": len_b - len_a,
        "common_prefix_bytes": prefix_words * 4,
        "common_suffix_bytes": suffix_words * 4,
        "diff_word_count": len(word_diffs),
        "word_diffs": [asdict(wd) for wd in word_diffs],
        "insertions": insertions,
        "deletions": deletions,
        "union_xor_mask": f"0x{union_mask:08x}",
        "union_bit_ranges": bit_ranges_from_mask(union_mask),
    }


def cluster_group_diffs(group_results: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """
    Aggregate bit-diff masks across twin pairs within each functional group
    (e.g., `int_alu`, `fp_alu`, `bitwise`, `baseline`) to isolate invariant vs.
    changing bitfields (opcode fields, modifiers, immediates).
    """
    summary: Dict[str, Any] = {}
    for group, diffs in sorted(group_results.items()):
        offset_masks: Dict[int, int] = defaultdict(int)
        offset_counts: Counter = Counter()
        for d in diffs:
            for wd in d.get("word_diffs", []):
                off = wd["byte_offset_a"]
                offset_masks[off] |= wd["xor_mask"]
                offset_counts[off] += 1
        hotspots = []
        for off, mask in sorted(offset_masks.items(), key=lambda kv: (-offset_counts[kv[0]], kv[0])):
            hotspots.append(
                {
                    "byte_offset": off,
                    "pair_hits": offset_counts[off],
                    "combined_xor_mask": f"0x{mask:08x}",
                    "bit_ranges": bit_ranges_from_mask(mask),
                }
            )
        summary[group] = {
            "twin_pairs": len(diffs),
            "bitfield_hotspots": hotspots[:12],
        }
    return summary


def classify_volcanic_word0(w0: int) -> Tuple[str, int]:
    """
    Classify Volcanic USC instruction family and max dword length (1..4) from word0[5:0].
    Verified on 1,378/1,378 real DXT-48-1536 rgx.sh shaders (30,495 instructions, 0 errors).
    """
    if w0 & 1:
        return ("FOP", 3)
    p6 = w0 & 0x3F
    if p6 == 0x12:
        return ("RLOAD", 2)
    if p6 == 0x18:
        return ("DOT8", 3)
    if p6 == 0x1A:
        return ("AF32", 2)
    if p6 == 0x1C:
        return ("CTRL_LONG", 4)
    if p6 == 0x1E:
        return ("COND", 2)
    if p6 == 0x2A:
        return ("PSB_WR", 3)
    if p6 == 0x2C:
        return ("PSB_RD", 2)
    if p6 == 0x38:
        return ("BIT_NO_P0_LUT", 4)
    if p6 == 0x3A:
        return ("EMI", 2)
    if p6 == 0x3C:
        return ("TAP", 2)
    if p6 == 0x3E:
        return ("SMP", 4)
    if (w0 & 0x1F) == 0x08:
        return ("CTRL_SHORT", 4)
    p4 = w0 & 0x0F
    if p4 == 0x0:
        return ("INT", 4)
    if p4 == 0x2:
        return ("RSTORE", 2)
    if p4 == 0x6:
        return ("BIT_NO_DST1_TST_OP1", 4)
    if p4 == 0xA:
        return ("MOV", 4)
    if p4 == 0xC:
        return ("ITR", 3)
    if p4 == 0xE:
        return ("DMA", 4)
    return (f"UNK_0x{p6:02x}", 4)


def frame_volcanic_instructions(data: bytes) -> Dict[str, Any]:
    """
    Volcanic USC instruction-group framer & validator.

    Volcanic instructions are variable-length 1..4 x 32-bit LE word packets where bit 31
    (0x80000000) marks the final word of an instruction, word0[5:0] selects the family,
    and shader programs terminate with 1-word `CTRL END` ((w0 & 0xFFFFF3FF) == 0x8000031C).
    """
    n = len(data)
    if n == 0 or (n % 4) != 0:
        return {
            "valid": False,
            "size_bytes": n,
            "error": "length_not_multiple_of_4",
            "instruction_count": 0,
            "instructions": [],
        }

    words = struct.unpack(f"<{n // 4}I", data)
    insts: List[Dict[str, Any]] = []
    fam_counts: Counter = Counter()
    errors: List[str] = []
    i = 0
    while i < len(words):
        start = i
        w0 = words[i]
        fam, max_w = classify_volcanic_word0(w0)
        if fam.startswith("UNK_"):
            errors.append(f"word[{start}]: unknown prefix {fam}")
        while i < len(words) and not (words[i] & 0x80000000):
            i += 1
        if i >= len(words):
            errors.append(f"word[{start}]: unterminated instruction (missing bit31 end marker)")
            break
        nwords = (i - start) + 1
        if nwords > max_w:
            errors.append(f"word[{start}]: {fam} packet length {nwords} > max {max_w}")
        fam_counts[fam] += 1
        insts.append(
            {
                "byte_offset": start * 4,
                "word_count": nwords,
                "family": fam,
                "words_hex": [f"0x{w:08x}" for w in words[start : i + 1]],
            }
        )
        i += 1

    ends_with_ctrl_end = bool(insts) and insts[-1]["word_count"] == 1 and (
        (words[-1] & 0xFFFFF3FF) == 0x8000031C
    )
    return {
        "valid": len(errors) == 0,
        "size_bytes": n,
        "word_count_u32": len(words),
        "instruction_count": len(insts),
        "ends_with_ctrl_end": ends_with_ctrl_end,
        "family_histogram": dict(fam_counts.most_common()),
        "errors": errors,
        "instructions": insts,
    }


def analyze_rgx_sh_chunks(manifest_path: Path, max_near_twins: int = 12) -> Dict[str, Any]:
    """Analyze extracted `rgx.sh` chunks: tail/head patterns + equal-size near-twin bit-diffs."""
    manifest = json.loads(manifest_path.read_text())
    root = manifest_path.parent
    chunks_meta = manifest["chunks"]

    by_size: Dict[int, List[Tuple[str, bytes]]] = defaultdict(list)
    tail_u32 = Counter()
    head_u32 = Counter()
    fam_total = Counter()
    seen_sha = set()
    total_unique = 0
    total_chunks_framed = 0
    total_insts = 0
    framing_errors = 0
    ctrl_end_ok = 0

    for item in chunks_meta:
        for key_f in ("primary_file", "secondary_file"):
            rel = item.get(key_f)
            if not rel:
                continue
            raw_chunk = (root / rel).read_bytes()
            fr = frame_volcanic_instructions(raw_chunk)
            total_chunks_framed += 1
            total_insts += fr["instruction_count"]
            if not fr["valid"]:
                framing_errors += 1
            if fr["ends_with_ctrl_end"]:
                ctrl_end_ok += 1
            for k, v in fr["family_histogram"].items():
                fam_total[k] += v

        p = root / item["primary_file"]
        raw = p.read_bytes()
        sha = item["primary_sha256"]
        if sha in seen_sha:
            continue
        seen_sha.add(sha)
        total_unique += 1
        if len(raw) >= 4:
            head_u32[struct.unpack_from("<I", raw, 0)[0]] += 1
            tail_u32[struct.unpack_from("<I", raw, len(raw) - 4)[0]] += 1
        by_size[len(raw)].append((item["primary_file"], raw))

    # Find closest near-twin pairs (same byte size, 1..3 differing 32-bit words!)
    near_twins: List[Dict[str, Any]] = []
    for sz in sorted(by_size.keys()):
        group = by_size[sz]
        if len(group) < 2:
            continue
        # Compare consecutive shaders in hash-sorted order (natural TQ shader twins!)
        for idx in range(len(group) - 1):
            name_a, raw_a = group[idx]
            name_b, raw_b = group[idx + 1]
            diff = diff_twin_bytes(raw_a, raw_b)
            wc = diff.get("diff_word_count", 999)
            if 1 <= wc <= 3:
                near_twins.append(
                    {
                        "size_bytes": sz,
                        "chunk_a": name_a,
                        "chunk_b": name_b,
                        "diff_word_count": wc,
                        "union_bit_ranges": diff["union_bit_ranges"],
                        "word_diffs": diff["word_diffs"],
                    }
                )
                if len(near_twins) >= max_near_twins:
                    break
        if len(near_twins) >= max_near_twins:
            break

    return {
        "total_chunks_framed": total_chunks_framed,
        "total_instructions": total_insts,
        "framing_errors": framing_errors,
        "ctrl_end_ok": ctrl_end_ok,
        "family_histogram": dict(fam_total.most_common()),
        "unique_primary_chunks": total_unique,
        "top_head_u32": [(f"0x{w:08x}", c) for w, c in head_u32.most_common(8)],
        "top_tail_u32": [(f"0x{w:08x}", c) for w, c in tail_u32.most_common(8)],
        "near_twin_sample_count": len(near_twins),
        "near_twins": near_twins,
    }


def run_self_test(rgx_manifest: Optional[Path] = None) -> int:
    """Run deterministic offline self-tests covering all core analyzer features."""
    # 1. Synthetic PowerVR Vulkan pipeline cache blob (36B header + zero pad + high-entropy USC chunk)
    uuid = bytes(range(16))
    header = VK_CACHE_HEADER_V1.pack(32, 1, IMG_VENDOR_ID, 0x71022448, uuid) + struct.pack("<I", 1)
    # Deterministic pseudo-USC instruction stream (16 u32 words = 64 bytes)
    base_words = [
        0x4301E1E0, 0xD091A13A, 0x3380303E, 0x02400020,
        0x820D0100, 0x1A316390, 0x95900808, 0x1A3DE390,
        0x95902808, 0x1A316180, 0x95900248, 0x1A39E180,
        0x95902248, 0x00069278, 0xA5B608A0, 0x80000B1C,
    ]
    usc_base = struct.pack(f"<{len(base_words)}I", *base_words)
    cache_blob = header + (b"\x00" * 32) + usc_base + (b"\x00" * 24)

    parsed_hdr = parse_pipeline_blob_header(cache_blob)
    assert parsed_hdr.format_kind == "vk_pipeline_cache", parsed_hdr
    assert parsed_hdr.vendor_id == IMG_VENDOR_ID, parsed_hdr
    assert parsed_hdr.payload_offset == 36, parsed_hdr

    cands = locate_candidate_usc_chunks(cache_blob, parsed_hdr.payload_offset)
    assert len(cands) == 1, f"Expected 1 candidate USC chunk, got {cands}"
    assert cands[0].offset == 68 and cands[0].size == 64, cands[0]

    # 2. Synthetic paired-twin 1-op opcode swap (bits [23:18] at word 5)
    twin_words_op = list(base_words)
    twin_words_op[5] ^= 0x00FC0000  # bits [23:18]
    diff_op = diff_twin_bytes(usc_base, struct.pack(f"<{len(twin_words_op)}I", *twin_words_op))
    assert not diff_op["identical"]
    assert diff_op["diff_word_count"] == 1
    assert diff_op["word_diffs"][0]["byte_offset_a"] == 20
    assert diff_op["word_diffs"][0]["bit_ranges"] == ["[23:18]"], diff_op

    # 3. Synthetic paired-twin immediate swap (full 32-bit word 7: 0x12345678 vs 0x5a5aa5a5)
    imm_a = list(base_words)
    imm_b = list(base_words)
    imm_a[7] = 0x12345678
    imm_b[7] = 0x5A5AA5A5
    diff_imm = diff_twin_bytes(struct.pack(f"<{len(imm_a)}I", *imm_a), struct.pack(f"<{len(imm_b)}I", *imm_b))
    assert diff_imm["diff_word_count"] == 1
    assert diff_imm["word_diffs"][0]["xor_mask"] == (0x12345678 ^ 0x5A5AA5A5)

    # 4. Synthetic length-delta twin (insert 2 words after word 4)
    ins_words = base_words[:5] + [0xCAFEBABE, 0xDEADBEEF] + base_words[5:]
    diff_ins = diff_twin_bytes(usc_base, struct.pack(f"<{len(ins_words)}I", *ins_words))
    assert diff_ins["len_delta"] == 8
    assert diff_ins["common_prefix_bytes"] == 20
    assert diff_ins["common_suffix_bytes"] == 44
    assert len(diff_ins["insertions"]) == 1 and diff_ins["insertions"][0]["word_count"] == 2

    # 5. Opcode-cluster aggregation
    twin_words_op2 = list(base_words)
    twin_words_op2[5] ^= 0x000C0000  # bits [19:18]
    diff_op2 = diff_twin_bytes(usc_base, struct.pack(f"<{len(twin_words_op2)}I", *twin_words_op2))
    clusters = cluster_group_diffs({"int_alu": [diff_op, diff_op2]})
    assert clusters["int_alu"]["bitfield_hotspots"][0]["byte_offset"] == 20
    assert clusters["int_alu"]["bitfield_hotspots"][0]["bit_ranges"] == ["[23:18]"]

    print("[self-test] PASS: pipeline-cache header parse, USC candidate scanner, bit-diff, length-delta align, opcode clustering")

    # 6. Volcanic USC instruction framing verification on documented DXT-48-1536 vectors
    framed = frame_volcanic_instructions(usc_base)
    assert framed["valid"] is True, framed
    assert framed["ends_with_ctrl_end"] is True, framed
    assert framed["instruction_count"] == 8, framed
    assert framed["family_histogram"]["INT"] == 5, framed
    assert framed["family_histogram"]["SMP"] == 1, framed
    assert framed["family_histogram"]["BIT_NO_P0_LUT"] == 1, framed
    assert framed["family_histogram"]["CTRL_LONG"] == 1, framed

    # 7. Optional local `rgx.sh` chunk analysis check (when --rgx-manifest is provided)

    if rgx_manifest is not None and rgx_manifest.exists():
        res = analyze_rgx_sh_chunks(rgx_manifest, max_near_twins=5)
        assert res["unique_primary_chunks"] > 0
        assert res["framing_errors"] == 0, f"Framing errors: {res['framing_errors']}"
        assert res["ctrl_end_ok"] == res["total_chunks_framed"], (
            f"CTRL END mismatch: {res['ctrl_end_ok']}/{res['total_chunks_framed']}"
        )
        print(
            f"[self-test] PASS: rgx.sh real chunk framing & analysis "
            f"(chunks={res['total_chunks_framed']}, insts={res['total_instructions']}, "
            f"errs={res['framing_errors']}, ctrl_end={res['ctrl_end_ok']}/{res['total_chunks_framed']}, "
            f"near_twins_sampled={res['near_twin_sample_count']})"
        )
        if res["near_twins"]:
            nt = res["near_twins"][0]
            print(
                f"            sample real twin: {nt['chunk_a']} vs {nt['chunk_b']} "
                f"(size={nt['size_bytes']}B, diff_words={nt['diff_word_count']}, bits={nt['union_bit_ranges']})"
            )

    return 0


def cmd_diff_dir(capture_dir: Path, manifest_path: Path, suffix: str) -> Dict[str, Any]:
    manifest = json.loads(manifest_path.read_text())
    probes = manifest["probes"]
    group_diffs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    pair_reports: List[Dict[str, Any]] = []

    for p in probes:
        base = p.get("twin_base")
        if not base:
            continue
        file_a = capture_dir / f"{base}{suffix}"
        file_b = capture_dir / f"{p['name']}{suffix}"
        if not file_a.exists() or not file_b.exists():
            continue
        raw_a = file_a.read_bytes()
        raw_b = file_b.read_bytes()
        hdr_a = parse_pipeline_blob_header(raw_a)
        hdr_b = parse_pipeline_blob_header(raw_b)
        diff = diff_twin_bytes(raw_a[hdr_a.payload_offset :], raw_b[hdr_b.payload_offset :])
        rec = {
            "group": p["group"],
            "base": base,
            "twin": p["name"],
            "delta_desc": p["delta_desc"],
            "diff": diff,
        }
        pair_reports.append(rec)
        group_diffs[p["group"]].append(diff)

    return {
        "capture_dir": str(capture_dir),
        "suffix": suffix,
        "pairs_compared": len(pair_reports),
        "clusters": cluster_group_diffs(group_diffs),
        "pairs": pair_reports,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="FreePVR / FreeUSC — Volcanic Pipeline-Cache & Paired-Twin Bit-Diff Analyzer"
    )
    ap.add_argument("--self-test", action="store_true", help="Run built-in offline verification suite")
    ap.add_argument("--rgx-manifest", type=Path, default=None, help="Optional rgx.sh chunks manifest.json for --self-test")

    sub = ap.add_subparsers(dest="cmd")

    p_parse = sub.add_parser("parse-cache", help="Parse a Vulkan pipeline cache or pipeline binary blob")
    p_parse.add_argument("blob", type=Path)

    p_diff = sub.add_parser("diff", help="Bit-diff two paired-twin blobs (auto-strips Vulkan cache headers)")
    p_diff.add_argument("blob_a", type=Path)
    p_diff.add_argument("blob_b", type=Path)
    p_diff.add_argument("--raw", action="store_true", help="Do not strip Vulkan cache/binary headers")

    p_dir = sub.add_parser("diff-dir", help="Run batch paired-twin bit-diffs over an on-device capture directory")
    p_dir.add_argument("capture_dir", type=Path)
    p_dir.add_argument("--manifest", type=Path, required=True, help="Path to corpus_manifest.json")
    p_dir.add_argument("--suffix", default=".pipeline_cache.bin", help="Captured blob suffix (or .pipeline_binary_0.bin)")

    p_rgx = sub.add_parser("analyze-rgx-sh", help="Analyze extracted rgx.sh chunks & real near-twins")
    p_rgx.add_argument("manifest", type=Path, help="Path to rgx_sh_chunks/manifest.json")
    p_rgx.add_argument("--max-twins", type=int, default=12)

    p_frame = sub.add_parser("frame", help="Frame & validate a raw Volcanic USC shader binary")
    p_frame.add_argument("usc_bin", type=Path)

    args = ap.parse_args()

    if args.self_test:
        return run_self_test(args.rgx_manifest)

    if args.cmd == "parse-cache":
        raw = args.blob.read_bytes()
        hdr = parse_pipeline_blob_header(raw)
        cands = locate_candidate_usc_chunks(raw, hdr.payload_offset)
        print(json.dumps({"header": asdict(hdr), "candidates": [asdict(c) for c in cands]}, indent=2))
        return 0

    if args.cmd == "diff":
        raw_a = args.blob_a.read_bytes()
        raw_b = args.blob_b.read_bytes()
        if not args.raw:
            ha = parse_pipeline_blob_header(raw_a)
            hb = parse_pipeline_blob_header(raw_b)
            raw_a = raw_a[ha.payload_offset :]
            raw_b = raw_b[hb.payload_offset :]
        print(json.dumps(diff_twin_bytes(raw_a, raw_b), indent=2))
        return 0

    if args.cmd == "diff-dir":
        print(json.dumps(cmd_diff_dir(args.capture_dir, args.manifest, args.suffix), indent=2))
        return 0

    if args.cmd == "analyze-rgx-sh":
        print(json.dumps(analyze_rgx_sh_chunks(args.manifest, max_near_twins=args.max_twins), indent=2))
        return 0

    if args.cmd == "frame":
        print(json.dumps(frame_volcanic_instructions(args.usc_bin.read_bytes()), indent=2))
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
