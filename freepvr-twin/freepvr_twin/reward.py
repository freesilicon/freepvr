# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 FreePVR Contributors
"""Deterministic freesilicon.twin.v1 verdict & GRPO reward contract."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "freesilicon.twin.v1"
VALID_DOMAINS = ("freecs", "freeusc", "freetile", "dgc0")
VALID_TARGETS = (
    "volcanic-dxt-48-1536",
    "volcanic-cxtp-48-1536",
    "buenos-a0",
    "malibu",
)
VALID_SEVERITIES = ("error", "warning", "info")

# Volcanic DXT-48-1536 per-USC on-chip unified/common-store budget (bytes):
# 256 temps * 128 lanes * 4B = 131072B + 1024 common-store dwords * 4B = 4096B + 512 shared * 4B = 2048B
VOLCANIC_USC_DEFAULT_SRAM_LIMIT = 137216


@dataclass
class Diagnostic:
    code: str
    severity: str
    message: str
    offset: Optional[Any] = None
    field: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "offset": self.offset,
            "field": self.field,
            "message": self.message,
        }


def compute_reward(
    *,
    passed: bool,
    fault_severity: float = 1.0,
    nmse: Optional[float] = None,
    sram_bytes: Optional[int] = None,
    sram_limit_bytes: Optional[int] = None,
    est_hw_us: Optional[float] = None,
    ref_hw_us: float = 10.0,
) -> tuple[float, Dict[str, float]]:
    """Compute deterministic scalar GRPO reward in [-1.0, 1.0]."""
    if not passed:
        # Clamp failed hard-gate rewards strictly inside [-1.0, -0.1]
        penalty = max(0.1, min(1.0, float(fault_severity)))
        val = round(-penalty, 6)
        return val, {
            "gate_pass": 0.0,
            "accuracy_score": 0.0,
            "sram_efficiency": 0.0,
            "latency_score": 0.0,
        }

    # Passing kernels score in (0.0, 1.0]
    if nmse is None:
        acc = 1.0
    elif math.isnan(nmse) or math.isinf(nmse):
        acc = 0.0
    else:
        acc = 1.0 / (1.0 + max(0.0, float(nmse)) * 1e4)

    if sram_bytes is not None and sram_limit_bytes and sram_limit_bytes > 0:
        util = max(0.0, min(1.0, float(sram_bytes) / float(sram_limit_bytes)))
        sram_eff = 1.0 - 0.5 * util
    else:
        sram_eff = 1.0

    if est_hw_us is not None and est_hw_us > 0.0:
        lat = 1.0 / (1.0 + float(est_hw_us) / max(1e-3, ref_hw_us))
    else:
        lat = 1.0

    score = 0.50 * 1.0 + 0.30 * acc + 0.10 * sram_eff + 0.10 * lat
    score = max(1e-6, min(1.0, round(score, 6)))
    return score, {
        "gate_pass": 1.0,
        "accuracy_score": round(acc, 6),
        "sram_efficiency": round(sram_eff, 6),
        "latency_score": round(lat, 6),
    }


def make_verdict(
    *,
    domain: str,
    target: str,
    workload: str,
    passed: bool,
    eval_ms: float,
    checks: Dict[str, Any],
    diagnostics: Optional[List[Diagnostic]] = None,
    fault_reason: Optional[str] = None,
    fault_severity: float = 1.0,
    nmse: Optional[float] = None,
    max_abs_err: Optional[float] = None,
    sram_bytes: Optional[int] = None,
    sram_limit_bytes: Optional[int] = None,
    est_hw_us: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if domain not in VALID_DOMAINS:
        raise ValueError(f"invalid domain: {domain}")
    if target not in VALID_TARGETS:
        raise ValueError(f"invalid target: {target}")

    diags = diagnostics or []
    reward, breakdown = compute_reward(
        passed=passed,
        fault_severity=fault_severity,
        nmse=nmse,
        sram_bytes=sram_bytes,
        sram_limit_bytes=sram_limit_bytes,
        est_hw_us=est_hw_us,
    )
    verdict: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "domain": domain,
        "target": target,
        "workload": workload,
        "pass": bool(passed),
        "reward": reward,
        "nmse": None if nmse is None else round(float(nmse), 12),
        "max_abs_err": None if max_abs_err is None else round(float(max_abs_err), 12),
        "sram_bytes": sram_bytes,
        "sram_limit_bytes": sram_limit_bytes,
        "est_hw_us": None if est_hw_us is None else round(float(est_hw_us), 6),
        "eval_ms": round(max(0.0, float(eval_ms)), 6),
        "fault_reason": None if passed else (fault_reason or "VALIDATION_FAILED"),
        "diagnostics": [d.to_dict() for d in diags],
        "checks": checks,
        "reward_breakdown": breakdown,
    }
    if extra:
        verdict.update(extra)
    return verdict


def validate_verdict_schema(verdict: Dict[str, Any]) -> List[str]:
    """Verify a verdict dictionary conforms strictly to REWARD-SCHEMA.json."""
    errs: List[str] = []
    req = (
        "schema_version",
        "domain",
        "target",
        "workload",
        "pass",
        "reward",
        "fault_reason",
        "eval_ms",
        "checks",
    )
    for k in req:
        if k not in verdict:
            errs.append(f"missing required key: {k}")
    if verdict.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"bad schema_version: {verdict.get('schema_version')}")
    if verdict.get("domain") not in VALID_DOMAINS:
        errs.append(f"bad domain: {verdict.get('domain')}")
    if verdict.get("target") not in VALID_TARGETS:
        errs.append(f"bad target: {verdict.get('target')}")
    if not isinstance(verdict.get("workload"), str) or not verdict["workload"]:
        errs.append("workload must be non-empty string")
    if not isinstance(verdict.get("pass"), bool):
        errs.append("pass must be bool")
    rew = verdict.get("reward")
    if not isinstance(rew, (int, float)) or not (-1.0 <= float(rew) <= 1.0):
        errs.append(f"reward out of [-1, 1]: {rew}")
    else:
        if verdict.get("pass") and float(rew) <= 0.0:
            errs.append(f"passing reward must be > 0: {rew}")
        if (not verdict.get("pass")) and not (-1.0 <= float(rew) <= -0.1):
            errs.append(f"failing reward must be in [-1.0, -0.1]: {rew}")
    if verdict.get("pass") and verdict.get("fault_reason") is not None:
        errs.append("fault_reason must be null on pass")
    if (not verdict.get("pass")) and not isinstance(verdict.get("fault_reason"), str):
        errs.append("fault_reason must be string on failure")
    if not isinstance(verdict.get("eval_ms"), (int, float)) or verdict["eval_ms"] < 0:
        errs.append("eval_ms must be >= 0")
    if not isinstance(verdict.get("checks"), dict):
        errs.append("checks must be object")
    for d in verdict.get("diagnostics", []):
        if not isinstance(d, dict):
            errs.append("diagnostic entry must be object")
            continue
        for dk in ("code", "severity", "message"):
            if dk not in d:
                errs.append(f"diagnostic missing {dk}")
        if d.get("severity") not in VALID_SEVERITIES:
            errs.append(f"bad diagnostic severity: {d.get('severity')}")
    return errs
