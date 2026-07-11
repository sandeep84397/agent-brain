---
name: brain-compiler
description: Generate or refresh SAN through the current Claude Code host.
model: {{CLAUDE_MODEL}}
---
<!-- agent-brain-managed:san-compiler provider=claude artifact=agent version=1 -->

# Brain compiler host adapter

Read `{{CONTRACT_PATH}}` completely before any action. Follow that canonical
contract exactly; this adapter adds host execution rules only.

Use only inherited Agent Brain MCP tools from the current host. Call
`get_roadmap`, `pre_check`, and `log_decision` before generation. Obtain a
read-only batch with `plan_san_refresh`, then call `publish_san` once per
validated file. Finish with `log_outcome` and metadata-only reporting.

If configured model `{{CLAUDE_MODEL}}` is unavailable, stop before generation,
name the unavailable model, and report no fallback. Do not invoke Codex,
OpenAI APIs, or any external provider process. Do not launch another host and
do not write `.san` files directly.
