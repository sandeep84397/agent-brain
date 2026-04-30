# Project Context — Template

Copy this file into your repo as `CLAUDE.md` (or merge into your existing one) so every agent that enters the repo can resolve the same identity, paths, team, and brain conventions.

Bundled agent templates (`backend-engineer.md`, `principal-engineer.md`, etc.) read this on every task — if any required section is missing, the agent should ask the user before proceeding.

---

## Repo Identity

- **Name:** `<repo-name-as-registered-in-brain-config>` (must match the key in `~/.agent-brain/config.json` `repos`)
- **Brain repo tag:** the same name — passed as `repo=<name>` to every brain tool call
- **Root path:** `/absolute/path/to/this/repo`
- **Stack:** e.g. `Python 3.12 + FastAPI`, `Kotlin + Ktor`, `TypeScript + Next.js`
- **Owner:** the lead engineer's name

## Paths

Tell agents where the canonical artifacts live. Use absolute paths or paths relative to the repo root.

- **PRDs:** `docs/prds/`
- **Architecture notes:** `docs/architecture/`
- **Active blockers:** `docs/BLOCKERS.md`
- **Sprint plan:** `docs/sprints/current.md`
- **Test plans:** `docs/test-plans/`

## Team

Canonical agent names and roles for this repo. Names should match `~/.agent-brain/config.json` `team` entries (or the per-repo `teams_per_repo` override). Agents use these to address each other in `heartbeat(... talking_to="...")`.

| Role | Name | Notes |
|---|---|---|
| Principal Engineer | `marcus` | architecture gate |
| Product Owner | `maya` | PRD owner |
| Backend Engineer | `arjun` | |
| Frontend Engineer | `priya` | |
| QA Engineer | `rahul` | |
| Project Manager | `vikram` | |

## Brain Conventions

- **Repo tag:** every `log_decision` / `pre_check` call must use `repo="<name>"` from above.
- **Area prefix:** group decisions by area, e.g. `auth`, `feed`, `schema`, `ui`. Use the same area name across decisions for the same subsystem so `query_decisions(area=...)` finds them.
- **Heartbeat repo:** when this project ships per-repo office state (see config `teams_per_repo`), pass `repo="<name>"` to `heartbeat()` so the agent doesn't appear in other repos' dashboards.
- **Outcome owners:** PRs reviewed by `marcus` (architecture) → `outcome_by="marcus"`. Test failures from CI → `outcome_by="ci"`.

## Code Exploration

- Use `code-review-graph` MCP tools (`query_graph`, `semantic_search_nodes`, `get_impact_radius`) before falling back to `Grep`/`Glob`/`Read`.
- The graph is auto-rebuilt on file changes via project hooks (see `.claude/settings.local.json`).

## Workflow

(Optional — describe the project's release/branching/PR cadence so agents follow it.)

- Branch naming: `feature/<short-desc>`, `bugfix/<short-desc>`
- PR target: `main`
- Reviewers: tag the principal engineer + relevant domain owner

---

> If your project doesn't need every section, drop the ones that don't apply. The required minimum is **Repo Identity** + **Brain Conventions** so agents know which `repo=` tag to use.
