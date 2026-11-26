#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 FreePVR Contributors
#
# FreePVR Clean-Room & OSS Boundary Audit
# Enforces:
#   - Zero proprietary/binary blobs (.so, .bin, .fw, .ko, .img, .spv, rgx.sh*, stray .sh)
#   - Zero vendor C code / proprietary copyright banners
#   - Zero private host paths, internal org identifiers, or personal PII/emails/serials/MACs
#   - Required SPDX-License-Identifier headers on all code/scripts/shaders

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

python3 - "$REPO_ROOT" <<'PY'
import os
import re
import subprocess
import sys
from pathlib import Path

repo_root = Path(sys.argv[1]).resolve()

# Gather files: prefer tracked + untracked non-ignored files if git repo, else walk tree
if (repo_root / ".git").exists():
    out = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=repo_root,
    )
    rel_paths = sorted(p for p in out.decode("utf-8").split("\0") if p)
else:
    rel_paths = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [
            d for d in dirs
            if d not in (".git", "__pycache__", ".pytest_cache", "local-captures", "build", "dist")
        ]
        for fn in files:
            rel_paths.append(str((Path(root) / fn).relative_to(repo_root)))
    rel_paths.sort()

forbidden_exts = (
    ".so",
    ".bin",
    ".fw",
    ".ko",
    ".img",
    ".apk",
    ".spv",
    ".spvasm",
    ".o",
    ".a",
    ".dylib",
    ".dll",
    ".exe",
)

forbidden_magics = (
    (b"\x7fELF", "ELF binary"),
    (b"\x03\x02\x23\x07", "SPIR-V little-endian binary"),
    (b"\x07\x23\x02\x03", "SPIR-V big-endian binary"),
    (b"\xfe\xed\xfa\xce", "Mach-O 32-bit binary"),
    (b"\xfe\xed\xfa\xcf", "Mach-O 64-bit binary"),
    (b"\xce\xfa\xed\xfe", "Mach-O 32-bit LE binary"),
    (b"\xcf\xfa\xed\xfe", "Mach-O 64-bit LE binary"),
    (b"\xca\xfe\xba\xbe", "Mach-O universal binary"),
    (b"\x03\x19\x02\x00", "PowerVR rgx.sh binary container"),
)

# Constructed via concatenation so this script does not false-positive on itself
private_patterns = [
    (re.compile(r"/" + r"Users/"), "private macOS home path"),
    (re.compile(r"/" + r"home/[a-zA-Z0-9._-]+/"), "private Linux home path"),
    (re.compile(r"file" + r"://"), "local file URI"),
    (re.compile(r"saari" + r"-co", re.IGNORECASE), "private org reference"),
    (re.compile(r"saar" + r"ius", re.IGNORECASE), "private handle reference"),
    (re.compile(r"bobby" + r"bones", re.IGNORECASE), "local username reference"),
    (re.compile(r"raw-" + r"private/"), "private scratch path"),
    (re.compile(r"device-" + r"scout"), "internal swarm role name"),
    (re.compile(r"3550" + r"8c7d"), "internal session identifier"),
    (re.compile(r"omamobile-" + r"p4-mesa"), "internal build image tag"),
    (re.compile(r"\bre-" + r"gpus\b", re.IGNORECASE), "private RE-factory skill reference"),
    (re.compile(r"\bre-" + r"tpus\b", re.IGNORECASE), "private RE-factory skill reference"),
]

vendor_code_patterns = [
    (re.compile(r"Imagination\s+Technologies\s+Limited", re.IGNORECASE), "vendor copyright banner"),
    (re.compile(r"All\s+Rights\s+Reserved", re.IGNORECASE), "proprietary rights banner"),
    (re.compile(r"confidential\s+and\s+proprietary", re.IGNORECASE), "proprietary confidentiality banner"),
    (re.compile(r"\bIMG_INTERNAL\b|\bIMG_EXPORT\b|\bPVRSRV_ERROR\b|\bRGXFWIF_"), "vendor DDK C macro/type"),
    (re.compile(r'#\s*include\s+[<"](?:rgx|pvr|img_types|services)[^">]*\.h[">]'), "vendor header include"),
]

pii_patterns = [
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "email address"),
    (re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b"), "MAC address"),
    (re.compile(r"\b(?:IMEI|serial)\s*[:=]\s*[A-Za-z0-9]{8,}\b", re.IGNORECASE), "device serial or IMEI"),
]

spdx_required_exts = {".py", ".c", ".h", ".sh", ".comp"}
violations = []

for rel in rel_paths:
    p = repo_root / rel
    if not p.is_file():
        continue

    name = p.name
    lower_name = name.lower()

    # 1. Forbidden binary/container filenames & extensions
    if (
        lower_name == "rgx.sh"
        or lower_name.startswith("rgx.sh.")
        or lower_name.endswith(forbidden_exts)
        or ".so." in lower_name
        or ".fw." in lower_name
        or (lower_name.endswith(".sh") and not rel.startswith("scripts/"))
    ):
        violations.append(f"[BLOB] Forbidden file or extension: {rel}")
        continue

    raw = p.read_bytes()

    # 2. Forbidden binary magic or NUL bytes
    magic_hit = False
    for magic, label in forbidden_magics:
        if raw.startswith(magic):
            violations.append(f"[MAGIC] {label} detected in {rel}")
            magic_hit = True
            break
    if magic_hit:
        continue

    if b"\x00" in raw:
        violations.append(f"[BINARY] NUL byte found in tracked file: {rel}")
        continue

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        violations.append(f"[ENCODING] Non-UTF-8 file detected: {rel}")
        continue

    # 3. Private paths / orgs / internal IDs
    for rx, desc in private_patterns:
        if rx.search(text):
            # Allow gitignore rule excluding local private scratch dirs
            if rel == ".gitignore" and desc == "private scratch path":
                continue
            violations.append(f"[PRIVATE] {desc} in {rel}")

    # 4. Vendor C code / proprietary copyright banners
    if rel != "scripts/clean-room-audit.sh":
        for rx, desc in vendor_code_patterns:
            if rx.search(text):
                violations.append(f"[VENDOR] {desc} in {rel}")

    # 5. PII / emails / MACs / serials
    for rx, desc in pii_patterns:
        for m in rx.finditer(text):
            val = m.group(0)
            if val.endswith("@users.noreply.github.com"):
                continue
            violations.append(f"[PII] {desc} ({val}) in {rel}")

    # 6. SPDX license header on all code/scripts/shaders
    if p.suffix.lower() in spdx_required_exts:
        head = "\n".join(text.splitlines()[:5])
        if "SPDX-License-Identifier: Apache-2.0" not in head:
            violations.append(f"[SPDX] Missing 'SPDX-License-Identifier: Apache-2.0' header in {rel}")

if violations:
    print(f"[audit] FAILED with {len(violations)} violation(s):", file=sys.stderr)
    for v in violations:
        print(f"  - {v}", file=sys.stderr)
    sys.exit(1)

print(f"[audit] PASS: 0 violations across {len(rel_paths)} files.")
PY
