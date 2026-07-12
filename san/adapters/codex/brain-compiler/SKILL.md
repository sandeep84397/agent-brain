---
name: brain-compiler
description: Generate or refresh SAN using the canonical contract and Agent Brain tools.
---

# Brain compiler

Read `{{CONTRACT_PATH}}` completely before any action. Treat it as normative;
do not redefine SAN semantics in this skill.

Use only Agent Brain MCP tools inherited from the current host:

1. Call `get_roadmap`, `pre_check`, and `log_decision` for one bounded batch.
2. Call `plan_san_refresh`; read only source files returned as missing or stale.
3. Apply all contract parity checks before publication.
4. Call `publish_san` once per validated source and never write `.san` directly.
5. Call `log_outcome`; report paths, counts, and validation metadata only.

If the configured model is unavailable, stop before generation and report no
fallback. Do not invoke Claude, Anthropic APIs, another host, or any external
provider subprocess. Never send source or SAN content outside inherited Agent
Brain publication tools.
