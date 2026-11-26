#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 FreePVR Contributors
"""
Offline unit tests for FreePVR (`FreeUSC` + `FreeCS` + paired-twin probe corpus).

Runs cleanly on any laptop without a connected phone or proprietary vendor binaries:
  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools" / "freeusc"))
sys.path.insert(0, str(REPO_ROOT / "tools" / "freecs"))

import freecs_packet_validator as freecs  # noqa: E402
import gen_spirv_probes as gsp  # noqa: E402
import rgx_sh_parser as rgx  # noqa: E402
import volcanic_bitdiff as vbd  # noqa: E402


class TestFreeUscTools(unittest.TestCase):
    def test_volcanic_bitdiff_self_test(self) -> None:
        self.assertEqual(vbd.run_self_test(), 0)

    def test_documented_dxt48_hex_vectors_frame_cleanly(self) -> None:
        # Representative clean-room hex vectors from spec/freeusc/VOLCANIC-USC-OPCODES.md Section 5
        vectors = [
            ("FOP", [0x0600D641, 0x0000080C, 0x80000408]),
            ("FOP", [0x0414A76B, 0x80408808]),
            ("INT", [0x091023F0, 0x81854008]),
            ("BIT_NO_P0_LUT", [0x00069278, 0xA5B608A0]),
            ("MOV", [0x408381CA, 0x80531200]),
            ("DMA", [0x0005834E, 0x5808021E, 0x01000380, 0x83000004]),
            ("SMP", [0x3380303E, 0x02400020, 0x820D0100]),
            ("ITR", [0xA000804C]),
            ("PSB_WR", [0x80006E6A]),
            ("PSB_RD", [0x802000AC]),
            ("CTRL_LONG", [0x8000031C]),  # CTRL.END
        ]
        stream = [w for _, words in vectors for w in words]
        raw = struct.pack(f"<{len(stream)}I", *stream)
        framed = vbd.frame_volcanic_instructions(raw)

        self.assertTrue(framed["valid"], framed)
        self.assertTrue(framed["ends_with_ctrl_end"])
        self.assertEqual(framed["instruction_count"], len(vectors))
        for expected, decoded in zip(vectors, framed["instructions"]):
            self.assertEqual(decoded["family"], expected[0])
            self.assertEqual(decoded["word_count"], len(expected[1]))

    def test_rgx_sh_parser_self_test(self) -> None:
        self.assertEqual(rgx.run_self_test(), 0)

    def test_rgx_sh_parser_roundtrip_and_framing(self) -> None:
        pri = struct.pack("<3I", 0x0414A76B, 0x80408808, 0x8000031C)
        sec = struct.pack("<2I", 0x8000029C, 0x8000031C)
        blob = rgx.build_synthetic_rgx_sh([pri], [sec])

        with tempfile.TemporaryDirectory(prefix="freepvr_test_") as td:
            sh_path = Path(td) / "rgx.sh.71.2.2448.1212"
            sh_path.write_bytes(blob)
            info, entries, data = rgx.parse_rgx_sh(sh_path)
            self.assertEqual(info.unaccounted_usc_bytes, 0)
            self.assertEqual(info.total_usc_chunks, 2)
            out_dir = Path(td) / "extracted"
            manifest_path = rgx.extract_chunks(info, entries, data, out_dir)
            analysis = vbd.analyze_rgx_sh_chunks(manifest_path)
            self.assertEqual(analysis["framing_errors"], 0)
            self.assertEqual(analysis["ctrl_end_ok"], 2)


class TestProbeCorpus(unittest.TestCase):
    def test_probe_corpus_33_glsl_sources_and_manifest_parity(self) -> None:
        spirv_dir = REPO_ROOT / "probes" / "spirv"
        manifest = json.loads((spirv_dir / "corpus_manifest.json").read_text())

        self.assertEqual(manifest["probe_count"], 33)
        self.assertEqual(len(manifest["probes"]), 33)
        self.assertEqual(len(gsp.PROBES), 33)
        self.assertEqual(manifest["workgroup_size"], [128, 1, 1])

        # Ensure zero compiled .spv binary blobs exist in probes/spirv/
        self.assertEqual(list(spirv_dir.glob("*.spv")), [])

        manifest_names = [item["name"] for item in manifest["probes"]]
        spec_names = [p.name for p in gsp.PROBES]
        self.assertEqual(manifest_names, spec_names)

        for spec in gsp.PROBES:
            comp = spirv_dir / f"{spec.name}.comp"
            self.assertTrue(comp.exists(), f"Missing {comp}")
            self.assertEqual(comp.read_text(), spec.glsl, f"Mismatch in {comp.name}")


class TestFreeCsValidator(unittest.TestCase):
    def test_freecs_self_test(self) -> None:
        self.assertEqual(freecs.run_self_test(), 0)

    def test_ppp_single_viewport_proof_vector(self) -> None:
        self.assertEqual(freecs.ppp_state_header_dword_count(0x00FF_0D87), 23)
        self.assertEqual(freecs.ppp_state_header_dword_count(0x0200_0000), 3)

    def test_pbe_and_texstate_silicon_fixes(self) -> None:
        va = 0x0000_0ABC_DEF0_1235
        packed = freecs.pack_pbe_state_va(va)
        self.assertEqual(packed["state_word0"], 0xDEF0_1235)
        self.assertEqual(freecs.unpack_pbe_state_va(packed["state_word0"], packed["state_word1_addr_bits"]), va)
        self.assertEqual(freecs.decode_texstate_sample_count(3 << 6), 8)


if __name__ == "__main__":
    unittest.main()
