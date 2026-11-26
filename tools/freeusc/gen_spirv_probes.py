#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 FreePVR Contributors
"""
1-Op SPIR-V Paired-Twin Differential Probe Corpus Generator for PowerVR Volcanic GPUs.

Generates minimal GLSL 4.60 compute shaders organized in 1-parameter paired twins
(differing in ONE ALU/bitwise/memory/control-flow/BDA/subgroup op, width, or immediate),
compiles each to SPIR-V 1.5 (Vulkan 1.2) via `glslangValidator`, validates 100% with
`spirv-val --target-env vulkan1.2`, and writes a machine-readable `corpus_manifest.json`
mapping twins and pipeline layout requirements for `vk_pipeline_cache_sweeper` and
`volcanic_bitdiff.py`.

All probes share one compatible VkPipelineLayout:
  - Descriptor Set 0, Binding 0: VK_DESCRIPTOR_TYPE_STORAGE_BUFFER (std430 SSBO)
  - Push Constant Range: 16 bytes @ offset 0 (two uint64_t BDA pointers for 24/25)
  - Local workgroup size: (128, 1, 1) matching Volcanic's 128-wide SIMD subgroup width
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional


@dataclass(frozen=True)
class ProbeSpec:
    name: str
    group: str
    twin_base: Optional[str]
    delta_desc: str
    requires_bda: bool
    requires_int64: bool
    requires_subgroup: bool
    glsl: str


COMMON_HEADER = """#version 460
// SPDX-License-Identifier: Apache-2.0
layout(local_size_x = 128, local_size_y = 1, local_size_z = 1) in;
"""

SSBO_U32_DECL = """layout(std430, set = 0, binding = 0) buffer ProbeBuf {
    uint in_a[256];
    uint in_b[256];
    uint in_c[256];
    uint out_v[256];
} buf;
"""

SSBO_U32_VEC2_DECL = """layout(std430, set = 0, binding = 0) buffer ProbeBuf2 {
    uvec2 in_a[256];
    uvec2 out_v[256];
} buf;
"""

SSBO_U32_VEC4_DECL = """layout(std430, set = 0, binding = 0) buffer ProbeBuf4 {
    uvec4 in_a[256];
    uvec4 out_v[256];
} buf;
"""

SSBO_F32_DECL = """layout(std430, set = 0, binding = 0) buffer ProbeBufF {
    float in_a[256];
    float in_b[256];
    float in_c[256];
    float out_v[256];
} buf;
"""


def _u32_binop(body: str) -> str:
    return (
        COMMON_HEADER
        + SSBO_U32_DECL
        + "void main() {\n"
        + "    uint idx = gl_GlobalInvocationID.x;\n"
        + "    uint a = buf.in_a[idx];\n"
        + "    uint b = buf.in_b[idx];\n"
        + f"    uint r = {body};\n"
        + "    buf.out_v[idx] = r;\n"
        + "}\n"
    )


def _u32_ternop(body: str) -> str:
    return (
        COMMON_HEADER
        + SSBO_U32_DECL
        + "void main() {\n"
        + "    uint idx = gl_GlobalInvocationID.x;\n"
        + "    uint a = buf.in_a[idx];\n"
        + "    uint b = buf.in_b[idx];\n"
        + "    uint c = buf.in_c[idx];\n"
        + f"    uint r = {body};\n"
        + "    buf.out_v[idx] = r;\n"
        + "}\n"
    )


def _f32_binop(body: str) -> str:
    return (
        COMMON_HEADER
        + SSBO_F32_DECL
        + "void main() {\n"
        + "    uint idx = gl_GlobalInvocationID.x;\n"
        + "    float a = buf.in_a[idx];\n"
        + "    float b = buf.in_b[idx];\n"
        + f"    float r = {body};\n"
        + "    buf.out_v[idx] = r;\n"
        + "}\n"
    )


def _f32_ternop(body: str) -> str:
    return (
        COMMON_HEADER
        + SSBO_F32_DECL
        + "void main() {\n"
        + "    uint idx = gl_GlobalInvocationID.x;\n"
        + "    float a = buf.in_a[idx];\n"
        + "    float b = buf.in_b[idx];\n"
        + "    float c = buf.in_c[idx];\n"
        + f"    float r = {body};\n"
        + "    buf.out_v[idx] = r;\n"
        + "}\n"
    )


PROBES: List[ProbeSpec] = [
    # --- Group 0: Baseline / Constant Stores ---
    ProbeSpec(
        name="00_empty_nop",
        group="baseline",
        twin_base=None,
        delta_desc="Empty compute shader (prologue + epilogue/end only)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=COMMON_HEADER + "void main() {\n}\n",
    ),
    ProbeSpec(
        name="01_store_const_0",
        group="baseline",
        twin_base="00_empty_nop",
        delta_desc="Single SSBO u32 store of constant 0u (isolates SSBO address + ST + zero special constant)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=(
            COMMON_HEADER
            + SSBO_U32_DECL
            + "void main() {\n"
            + "    uint idx = gl_GlobalInvocationID.x;\n"
            + "    buf.out_v[idx] = 0u;\n"
            + "}\n"
        ),
    ),
    ProbeSpec(
        name="02_store_const_imm",
        group="baseline",
        twin_base="01_store_const_0",
        delta_desc="Single SSBO u32 store of 32-bit immediate 0x12345678u (isolates imm32 encoding vs zero SC)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=(
            COMMON_HEADER
            + SSBO_U32_DECL
            + "void main() {\n"
            + "    uint idx = gl_GlobalInvocationID.x;\n"
            + "    buf.out_v[idx] = 0x12345678u;\n"
            + "}\n"
        ),
    ),
    ProbeSpec(
        name="02b_store_const_imm_alt",
        group="baseline",
        twin_base="02_store_const_imm",
        delta_desc="Same as 02_store_const_imm with 0x5a5aa5a5u (isolates exact immediate bit positions)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=(
            COMMON_HEADER
            + SSBO_U32_DECL
            + "void main() {\n"
            + "    uint idx = gl_GlobalInvocationID.x;\n"
            + "    buf.out_v[idx] = 0x5a5aa5a5u;\n"
            + "}\n"
        ),
    ),
    # --- Group 1: SSBO Load/Store Burst Width (1dw vs 2dw vs 4dw) ---
    ProbeSpec(
        name="03a_mov_ssbo_1dw",
        group="mem_burst",
        twin_base="01_store_const_0",
        delta_desc="SSBO load + store 1 dword (u32)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=(
            COMMON_HEADER
            + SSBO_U32_DECL
            + "void main() {\n"
            + "    uint idx = gl_GlobalInvocationID.x;\n"
            + "    buf.out_v[idx] = buf.in_a[idx];\n"
            + "}\n"
        ),
    ),
    ProbeSpec(
        name="03b_mov_ssbo_2dw",
        group="mem_burst",
        twin_base="03a_mov_ssbo_1dw",
        delta_desc="SSBO load + store 2-dword burst (uvec2)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=(
            COMMON_HEADER
            + SSBO_U32_VEC2_DECL
            + "void main() {\n"
            + "    uint idx = gl_GlobalInvocationID.x;\n"
            + "    buf.out_v[idx] = buf.in_a[idx];\n"
            + "}\n"
        ),
    ),
    ProbeSpec(
        name="03c_mov_ssbo_4dw",
        group="mem_burst",
        twin_base="03b_mov_ssbo_2dw",
        delta_desc="SSBO load + store 4-dword burst (uvec4)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=(
            COMMON_HEADER
            + SSBO_U32_VEC4_DECL
            + "void main() {\n"
            + "    uint idx = gl_GlobalInvocationID.x;\n"
            + "    buf.out_v[idx] = buf.in_a[idx];\n"
            + "}\n"
        ),
    ),
    # --- Group 2: Integer 32-bit ALU ---
    ProbeSpec(
        name="04_iadd32",
        group="int_alu",
        twin_base="03a_mov_ssbo_1dw",
        delta_desc="32-bit integer add: a + b",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=_u32_binop("a + b"),
    ),
    ProbeSpec(
        name="05_isub32",
        group="int_alu",
        twin_base="04_iadd32",
        delta_desc="32-bit integer sub: a - b (1-op twin of 04_iadd32)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=_u32_binop("a - b"),
    ),
    ProbeSpec(
        name="06_imul32",
        group="int_alu",
        twin_base="04_iadd32",
        delta_desc="32-bit integer mul: a * b (1-op twin of 04_iadd32)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=_u32_binop("a * b"),
    ),
    ProbeSpec(
        name="07_imad32",
        group="int_alu",
        twin_base="06_imul32",
        delta_desc="32-bit integer multiply-add: a * b + c (twin of 06_imul32)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=_u32_ternop("a * b + c"),
    ),
    # --- Group 3: FP32 ALU & Source Modifiers ---
    ProbeSpec(
        name="08_fadd32",
        group="fp_alu",
        twin_base="04_iadd32",
        delta_desc="FP32 add: a + b",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=_f32_binop("a + b"),
    ),
    ProbeSpec(
        name="09_fmul32",
        group="fp_alu",
        twin_base="08_fadd32",
        delta_desc="FP32 mul: a * b (1-op twin of 08_fadd32)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=_f32_binop("a * b"),
    ),
    ProbeSpec(
        name="10_fma32",
        group="fp_alu",
        twin_base="09_fmul32",
        delta_desc="FP32 fused multiply-add: fma(a, b, c) (twin of 09_fmul32)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=_f32_ternop("fma(a, b, c)"),
    ),
    ProbeSpec(
        name="11_fadd32_neg_abs",
        group="fp_alu",
        twin_base="08_fadd32",
        delta_desc="FP32 add with source modifiers: (-a) + abs(b) (isolates neg/abs modifier bits vs 08_fadd32)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=_f32_binop("(-a) + abs(b)"),
    ),
    # --- Group 4: Bitwise & Shift Ops ---
    ProbeSpec(
        name="12_and32",
        group="bitwise",
        twin_base="04_iadd32",
        delta_desc="32-bit bitwise AND: a & b",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=_u32_binop("a & b"),
    ),
    ProbeSpec(
        name="13_or32",
        group="bitwise",
        twin_base="12_and32",
        delta_desc="32-bit bitwise OR: a | b (1-op twin of 12_and32)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=_u32_binop("a | b"),
    ),
    ProbeSpec(
        name="14_xor32",
        group="bitwise",
        twin_base="12_and32",
        delta_desc="32-bit bitwise XOR: a ^ b (1-op twin of 12_and32)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=_u32_binop("a ^ b"),
    ),
    ProbeSpec(
        name="15_shl32",
        group="bitwise",
        twin_base="12_and32",
        delta_desc="32-bit shift left: a << (b & 31u)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=_u32_binop("a << (b & 31u)"),
    ),
    ProbeSpec(
        name="16_shr_u32",
        group="bitwise",
        twin_base="15_shl32",
        delta_desc="32-bit logical shift right: a >> (b & 31u) (1-op twin of 15_shl32)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=_u32_binop("a >> (b & 31u)"),
    ),
    ProbeSpec(
        name="17_shr_s32",
        group="bitwise",
        twin_base="16_shr_u32",
        delta_desc="32-bit arithmetic shift right: uint(int(a) >> (b & 31u)) (1-op twin of 16_shr_u32)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=_u32_binop("uint(int(a) >> (b & 31u))"),
    ),
    # --- Group 5: Compare, Branch, Loop ---
    ProbeSpec(
        name="18_cmp_eq_sel",
        group="control_flow",
        twin_base="04_iadd32",
        delta_desc="Compare-equal + select: (a == b) ? c : a",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=_u32_ternop("(a == b) ? c : a"),
    ),
    ProbeSpec(
        name="19_branch_if_else",
        group="control_flow",
        twin_base="18_cmp_eq_sel",
        delta_desc="Divergent if/else branch with distinct SSBO side effects (forces real CFG branch vs csel)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=(
            COMMON_HEADER
            + SSBO_U32_DECL
            + "void main() {\n"
            + "    uint idx = gl_GlobalInvocationID.x;\n"
            + "    uint a = buf.in_a[idx];\n"
            + "    uint b = buf.in_b[idx];\n"
            + "    if ((a & 1u) != 0u) {\n"
            + "        buf.out_v[idx] = a * b + 3u;\n"
            + "    } else {\n"
            + "        buf.out_v[idx] = (a ^ b) - 7u;\n"
            + "    }\n"
            + "}\n"
        ),
    ),
    ProbeSpec(
        name="20_loop_4",
        group="control_flow",
        twin_base="19_branch_if_else",
        delta_desc="Dynamic-bound loop (runtime count from in_c[idx], prevents unrolling) isolating back-edge branch",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=(
            COMMON_HEADER
            + SSBO_U32_DECL
            + "void main() {\n"
            + "    uint idx = gl_GlobalInvocationID.x;\n"
            + "    uint acc = buf.in_a[idx];\n"
            + "    uint step_v = buf.in_b[idx];\n"
            + "    uint n = (buf.in_c[idx] & 3u) + 1u;\n"
            + "    for (uint i = 0u; i < n; ++i) {\n"
            + "        acc = acc * 1664525u + step_v;\n"
            + "    }\n"
            + "    buf.out_v[idx] = acc;\n"
            + "}\n"
        ),
    ),
    # --- Group 6: Workgroup Shared Memory + Barrier ---
    ProbeSpec(
        name="21_barrier_shared",
        group="sync",
        twin_base="03a_mov_ssbo_1dw",
        delta_desc="Workgroup shared memory store + control/memory barrier() + reversed-lane load",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=(
            COMMON_HEADER
            + SSBO_U32_DECL
            + "shared uint smem[128];\n"
            + "void main() {\n"
            + "    uint lid = gl_LocalInvocationID.x;\n"
            + "    uint gid = gl_GlobalInvocationID.x;\n"
            + "    smem[lid] = buf.in_a[gid];\n"
            + "    barrier();\n"
            + "    buf.out_v[gid] = smem[127u - lid];\n"
            + "}\n"
        ),
    ),
    # --- Group 7: Atomics ---
    ProbeSpec(
        name="22_atomic_add_ssbo",
        group="atomics",
        twin_base="03a_mov_ssbo_1dw",
        delta_desc="SSBO atomicAdd on out_v[0]",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=(
            COMMON_HEADER
            + SSBO_U32_DECL
            + "void main() {\n"
            + "    uint idx = gl_GlobalInvocationID.x;\n"
            + "    uint old_v = atomicAdd(buf.out_v[0], buf.in_a[idx]);\n"
            + "    buf.out_v[idx + 1u] = old_v;\n"
            + "}\n"
        ),
    ),
    ProbeSpec(
        name="23_atomic_max_ssbo",
        group="atomics",
        twin_base="22_atomic_add_ssbo",
        delta_desc="SSBO atomicMax on out_v[0] (1-op twin of 22_atomic_add_ssbo)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=False,
        glsl=(
            COMMON_HEADER
            + SSBO_U32_DECL
            + "void main() {\n"
            + "    uint idx = gl_GlobalInvocationID.x;\n"
            + "    uint old_v = atomicMax(buf.out_v[0], buf.in_a[idx]);\n"
            + "    buf.out_v[idx + 1u] = old_v;\n"
            + "}\n"
        ),
    ),
    # --- Group 8: Buffer Device Address (40-bit Global BDA LD/ST) ---
    ProbeSpec(
        name="24_bda_load_store_u32",
        group="bda",
        twin_base="03a_mov_ssbo_1dw",
        delta_desc="PhysicalStorageBuffer u32 global load + store via push-constant 64-bit BDA",
        requires_bda=True,
        requires_int64=True,
        requires_subgroup=False,
        glsl=(
            "#version 460\n"
            "// SPDX-License-Identifier: Apache-2.0\n"
            "#extension GL_EXT_buffer_reference : require\n"
            "#extension GL_EXT_shader_explicit_arithmetic_types_int64 : require\n"
            "layout(local_size_x = 128, local_size_y = 1, local_size_z = 1) in;\n"
            "layout(buffer_reference, std430, buffer_reference_align = 4) buffer U32Ptr {\n"
            "    uint v[];\n"
            "};\n"
            "layout(push_constant) uniform PC {\n"
            "    uint64_t src_addr;\n"
            "    uint64_t dst_addr;\n"
            "} pc;\n"
            "void main() {\n"
            "    uint idx = gl_GlobalInvocationID.x;\n"
            "    U32Ptr src = U32Ptr(pc.src_addr);\n"
            "    U32Ptr dst = U32Ptr(pc.dst_addr);\n"
            "    dst.v[idx] = src.v[idx];\n"
            "}\n"
        ),
    ),
    ProbeSpec(
        name="25_bda_load_store_u64",
        group="bda",
        twin_base="24_bda_load_store_u32",
        delta_desc="PhysicalStorageBuffer u64 global load + store (1-width twin of 24_bda_load_store_u32)",
        requires_bda=True,
        requires_int64=True,
        requires_subgroup=False,
        glsl=(
            "#version 460\n"
            "// SPDX-License-Identifier: Apache-2.0\n"
            "#extension GL_EXT_buffer_reference : require\n"
            "#extension GL_EXT_shader_explicit_arithmetic_types_int64 : require\n"
            "layout(local_size_x = 128, local_size_y = 1, local_size_z = 1) in;\n"
            "layout(buffer_reference, std430, buffer_reference_align = 8) buffer U64Ptr {\n"
            "    uint64_t v[];\n"
            "};\n"
            "layout(push_constant) uniform PC {\n"
            "    uint64_t src_addr;\n"
            "    uint64_t dst_addr;\n"
            "} pc;\n"
            "void main() {\n"
            "    uint idx = gl_GlobalInvocationID.x;\n"
            "    U64Ptr src = U64Ptr(pc.src_addr);\n"
            "    U64Ptr dst = U64Ptr(pc.dst_addr);\n"
            "    dst.v[idx] = src.v[idx];\n"
            "}\n"
        ),
    ),
    # --- Group 9: 128-Wide Subgroups (Volcanic Native SIMD Width) ---
    ProbeSpec(
        name="26_subgroup_id_128",
        group="subgroup",
        twin_base="01_store_const_0",
        delta_desc="Store gl_SubgroupInvocationID (isolates SR lane-id read on 128-wide Volcanic)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=True,
        glsl=(
            "#version 460\n"
            "// SPDX-License-Identifier: Apache-2.0\n"
            "#extension GL_KHR_shader_subgroup_basic : require\n"
            "layout(local_size_x = 128, local_size_y = 1, local_size_z = 1) in;\n"
            + SSBO_U32_DECL
            + "void main() {\n"
            + "    uint idx = gl_GlobalInvocationID.x;\n"
            + "    buf.out_v[idx] = gl_SubgroupInvocationID;\n"
            + "}\n"
        ),
    ),
    ProbeSpec(
        name="27_subgroup_ballot_128",
        group="subgroup",
        twin_base="26_subgroup_id_128",
        delta_desc="subgroupBallot() -> uvec4 (all 4 dwords folded with XOR to keep 128-bit ballot live)",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=True,
        glsl=(
            "#version 460\n"
            "// SPDX-License-Identifier: Apache-2.0\n"
            "#extension GL_KHR_shader_subgroup_ballot : require\n"
            "layout(local_size_x = 128, local_size_y = 1, local_size_z = 1) in;\n"
            + SSBO_U32_DECL
            + "void main() {\n"
            + "    uint idx = gl_GlobalInvocationID.x;\n"
            + "    uvec4 m = subgroupBallot((buf.in_a[idx] & 1u) != 0u);\n"
            + "    buf.out_v[idx] = (m.x ^ m.y) ^ (m.z ^ m.w);\n"
            + "}\n"
        ),
    ),
    ProbeSpec(
        name="28_subgroup_add_128",
        group="subgroup",
        twin_base="26_subgroup_id_128",
        delta_desc="subgroupAdd(u32) across 128-wide Volcanic subgroup",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=True,
        glsl=(
            "#version 460\n"
            "// SPDX-License-Identifier: Apache-2.0\n"
            "#extension GL_KHR_shader_subgroup_arithmetic : require\n"
            "layout(local_size_x = 128, local_size_y = 1, local_size_z = 1) in;\n"
            + SSBO_U32_DECL
            + "void main() {\n"
            + "    uint idx = gl_GlobalInvocationID.x;\n"
            + "    buf.out_v[idx] = subgroupAdd(buf.in_a[idx]);\n"
            + "}\n"
        ),
    ),
    ProbeSpec(
        name="29_subgroup_shuffle_128",
        group="subgroup",
        twin_base="26_subgroup_id_128",
        delta_desc="subgroupShuffle(u32, lane ^ 1u) across 128-wide Volcanic subgroup",
        requires_bda=False,
        requires_int64=False,
        requires_subgroup=True,
        glsl=(
            "#version 460\n"
            "// SPDX-License-Identifier: Apache-2.0\n"
            "#extension GL_KHR_shader_subgroup_shuffle : require\n"
            "layout(local_size_x = 128, local_size_y = 1, local_size_z = 1) in;\n"
            + SSBO_U32_DECL
            + "void main() {\n"
            + "    uint idx = gl_GlobalInvocationID.x;\n"
            + "    buf.out_v[idx] = subgroupShuffle(buf.in_a[idx], gl_SubgroupInvocationID ^ 1u);\n"
            + "}\n"
        ),
    ),
]


def run_cmd(cmd: List[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"Command failed ({p.returncode}): {' '.join(cmd)}\n"
            f"STDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}"
        )
    return p.stdout


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate & validate 1-op SPIR-V paired-twin probe corpus.")
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "probes" / "spirv",
        help="Output directory for .comp, .spv, .spvasm, and corpus_manifest.json",
    )
    ap.add_argument("--target-env", default="vulkan1.2", help="Target Vulkan env for glslangValidator & spirv-val")
    ap.add_argument("--keep-disasm", action="store_true", help="Also emit .spvasm via spirv-dis")
    ap.add_argument("--emit-glsl-only", action="store_true", help="Write .comp sources without invoking glslangValidator/spirv-val")
    ap.add_argument("--self-test", action="store_true", help="Verify all 33 probe specs and parity with probes/spirv/*.comp offline")
    args = ap.parse_args()

    if args.self_test:
        names = [p.name for p in PROBES]
        assert len(names) == 33, f"Expected 33 probes, got {len(names)}"
        assert len(set(names)) == len(names), "Duplicate probe names"
        name_set = set(names)
        for p in PROBES:
            if p.twin_base is not None:
                assert p.twin_base in name_set, f"Missing twin_base {p.twin_base} for {p.name}"
            assert "local_size_x = 128" in p.glsl, f"Missing 128-wide workgroup in {p.name}"
            assert p.glsl.startswith("#version 460\n// SPDX-License-Identifier: Apache-2.0\n"), p.name
            comp_file = args.out_dir / f"{p.name}.comp"
            if comp_file.exists():
                assert comp_file.read_text() == p.glsl, f"GLSL drift in {comp_file}"
        print(f"[self-test] PASS: {len(PROBES)} paired-twin GLSL probes verified (128-wide, twin graph valid, repo parity OK)")
        return 0

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.emit_glsl_only:
        for spec in PROBES:
            (out_dir / f"{spec.name}.comp").write_text(spec.glsl)
        print(f"[ok] Wrote {len(PROBES)} GLSL .comp probe sources in {out_dir}")
        return 0

    manifest_items = []
    for spec in PROBES:
        comp_path = out_dir / f"{spec.name}.comp"
        spv_path = out_dir / f"{spec.name}.spv"

        comp_path.write_text(spec.glsl)

        # Compile GLSL 4.60 -> SPIR-V 1.5 (Vulkan 1.2)
        run_cmd([
            "glslangValidator",
            "-V",
            "--target-env",
            args.target_env,
            "-o",
            str(spv_path),
            str(comp_path),
        ])

        # Validate with spirv-val
        run_cmd([
            "spirv-val",
            "--target-env",
            args.target_env,
            str(spv_path),
        ])

        if args.keep_disasm:
            spvasm_path = out_dir / f"{spec.name}.spvasm"
            dis = run_cmd(["spirv-dis", str(spv_path)])
            spvasm_path.write_text(dis)

        spv_bytes = spv_path.read_bytes()
        manifest_items.append(
            {
                "name": spec.name,
                "group": spec.group,
                "twin_base": spec.twin_base,
                "delta_desc": spec.delta_desc,
                "requires_bda": spec.requires_bda,
                "requires_int64": spec.requires_int64,
                "requires_subgroup": spec.requires_subgroup,
                "comp_file": comp_path.name,
                "spv_file": spv_path.name,
                "spv_size_bytes": len(spv_bytes),
                "spv_sha256": hashlib.sha256(spv_bytes).hexdigest(),
                "spirv_val_target_env": args.target_env,
                "spirv_val_status": "PASS",
            }
        )

    manifest = {
        "schema_version": 1,
        "target_env": args.target_env,
        "workgroup_size": [128, 1, 1],
        "pipeline_layout": {
            "descriptor_set_0_binding_0": "VK_DESCRIPTOR_TYPE_STORAGE_BUFFER",
            "push_constant_bytes": 16,
        },
        "probe_count": len(manifest_items),
        "probes": manifest_items,
    }
    manifest_path = out_dir / "corpus_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"[ok] Generated and validated {len(manifest_items)}/{len(PROBES)} SPIR-V probes in {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
