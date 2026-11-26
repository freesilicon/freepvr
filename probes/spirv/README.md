# Clean-Room 1-Op Paired-Twin SPIR-V Probe Corpus (`probes/spirv/`)

This directory contains **33 clean-room GLSL 4.60 compute shader sources (`.comp`)** and their twin-graph manifest ([`corpus_manifest.json`](corpus_manifest.json)).

## Zero-Binary Policy

Compiled `.spv` and `.spvasm` files are intentionally **excluded** from git tracking. Every `.spv` binary can be deterministically regenerated and validated against Vulkan 1.2 (`spirv-val --target-env vulkan1.2`) on any machine with `glslangValidator` and `spirv-val`:

```bash
python3 tools/freeusc/gen_spirv_probes.py --out-dir local-captures/spv --keep-disasm
```

To verify all 33 `.comp` files match the generator without needing Vulkan shader tools installed:

```bash
python3 tools/freeusc/gen_spirv_probes.py --self-test
```
