"""
Agent Brain MCP Server — v2
Persistent decision memory + code-graph bridge for Claude Code agent teams.

Features:
  - Decision logging with pre_check warnings
  - Code-review-graph bridge (links decisions to code nodes)
  - Fuzzy similarity matching on rejection reasons
  - Agent scorecards with adaptive warnings
  - Team dashboard for project managers

Configuration:
  Set AGENT_BRAIN_DIR env var or defaults to ~/.agent-brain/
  Place config.json in that directory to register repos.
"""

from mcp.server.fastmcp import FastMCP
import networkx as nx
import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
import re
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from collections import defaultdict
import uuid
import threading

try:
    from .san_publish import (
        atomic_write_bytes,
        restore_file,
        snapshot_file,
        validate_san_candidate,
    )
except ImportError:  # pragma: no cover - standalone execution
    from san_publish import (  # type: ignore[no-redef]
        atomic_write_bytes,
        restore_file,
        snapshot_file,
        validate_san_candidate,
    )

try:
    from .compiler_config import CompilerConfigError, parse_san_compiler_config
except ImportError:  # pragma: no cover - standalone execution
    from compiler_config import (  # type: ignore[no-redef]
        CompilerConfigError,
        parse_san_compiler_config,
    )

# ---------------------------------------------------------------------------
# Configuration — no hardcoded paths
# ---------------------------------------------------------------------------

BRAIN_DIR = Path(os.environ.get("AGENT_BRAIN_DIR", str(Path.home() / ".agent-brain")))
GRAPH_FILE = BRAIN_DIR / "decisions.json"
CONFIG_FILE = BRAIN_DIR / "config.json"
# Reentrant: validate_brain holds these while calling tools that re-acquire them.
LOCK = threading.RLock()
OFFICE_LOCK = threading.RLock()
OFFICE_STATE_FILE = BRAIN_DIR / "office-state.json"


def _load_config() -> dict:
    """Load brain config. Returns empty defaults if no config exists."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {"repos": {}, "team": []}
    return {"repos": {}, "team": []}


def _get_repo_paths() -> dict[str, Path]:
    """Get configured repo name → path mapping."""
    config = _load_config()
    repos = config.get("repos", {})
    if not isinstance(repos, dict):
        return {}
    return {name: Path(p) for name, p in repos.items()}


def _get_team_for_repo(repo: str = "") -> list[dict]:
    """
    Resolve the team list for a given repo.

    Resolution order:
      1. config['teams_per_repo'][repo]  — explicit per-repo team override (full list)
      2. Filter config['team']:
         - Entries with no 'repos' key → global, included for every repo
         - Entries with 'repos': [...] → included only when repo matches
      3. If no repo passed → return the full flat global team (legacy behavior)

    Backwards compatible: configs without teams_per_repo or 'repos' field on
    team entries behave exactly like before.
    """
    config = _load_config()
    if not repo:
        return list(config.get("team", []) or [])

    per_repo = config.get("teams_per_repo")
    if isinstance(per_repo, dict) and repo in per_repo:
        explicit = per_repo.get(repo)
        if isinstance(explicit, list):
            return list(explicit)

    result: list[dict] = []
    for t in config.get("team", []) or []:
        if not isinstance(t, dict):
            continue
        scope = t.get("repos")
        if scope is None:
            result.append(t)  # global member, applies to every repo
        elif isinstance(scope, list) and repo in scope:
            result.append(t)
    return result


def _resolve_role(agent: str, repo: str = "") -> str:
    """Look up an agent's role, scoped to repo when provided."""
    team = _get_team_for_repo(repo) if repo else _get_team_for_repo()
    for t in team:
        if isinstance(t, dict) and t.get("name", "").lower() == agent.lower():
            return t.get("role", "unknown")
    # Fallback: any team entry across all scopes (legacy behavior)
    if repo:
        for t in _get_team_for_repo():
            if isinstance(t, dict) and t.get("name", "").lower() == agent.lower():
                return t.get("role", "unknown")
    return "unknown"


# ---------------------------------------------------------------------------
# Office State (for live dashboard)
# ---------------------------------------------------------------------------


def _load_office_state() -> dict:
    """Load office state for dashboard. Returns empty if none exists."""
    if OFFICE_STATE_FILE.exists():
        try:
            return json.loads(OFFICE_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {"agents": {}, "messages": []}
    return {"agents": {}, "messages": []}


_OFFICE_AGENT_TTL_H = 48
_OFFICE_MSG_TTL_H = 12  # Activity-feed messages older than this drop off as stale.


def _save_office_state(state: dict) -> None:
    """Persist office state atomically. Evicts agents idle > 48h and drops
    Activity-feed messages older than 12h (otherwise one-off subagent names
    and stale chatter accumulate forever)."""
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    agents = state.get("agents")
    if isinstance(agents, dict):
        cutoff = datetime.now().timestamp() - _OFFICE_AGENT_TTL_H * 3600
        for name in list(agents.keys()):
            try:
                seen = datetime.fromisoformat(agents[name].get("last_seen", "")).timestamp()
                if seen < cutoff:
                    del agents[name]
            except (ValueError, TypeError, AttributeError):
                pass
    msgs = state.get("messages")
    if isinstance(msgs, list):
        msg_cutoff = datetime.now().timestamp() - _OFFICE_MSG_TTL_H * 3600
        fresh = []
        for m in msgs:
            try:
                if datetime.fromisoformat(m.get("ts", "")).timestamp() >= msg_cutoff:
                    fresh.append(m)
            except (ValueError, TypeError, AttributeError):
                pass  # undated/garbage message -> drop as stale
        state["messages"] = fresh[-50:]
    tmp = OFFICE_STATE_FILE.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(state, separators=(",", ":")))
    tmp.rename(OFFICE_STATE_FILE)


def _cap_text(text: str, limit: int) -> str:
    """Cap stored free-text fields so single nodes can't bloat the graph."""
    if not isinstance(text, str) or len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


DECISION_MARKER_FILE = BRAIN_DIR / ".last_decision_marker"


def _write_decision_marker(agent: str, decision_id: str) -> None:
    """Write a lightweight marker so the enforcement hook knows a decision was logged."""
    try:
        BRAIN_DIR.mkdir(parents=True, exist_ok=True)
        DECISION_MARKER_FILE.write_text(json.dumps({
            "agent": agent,
            "decision_id": decision_id,
            "timestamp": datetime.now().isoformat(),
        }))
    except OSError:
        pass


QUERY_MARKER_FILE = BRAIN_DIR / ".last_query_marker"


def _write_query_marker() -> None:
    """Mark that a brain read happened — satisfies the optional hard research
    gate (remind_brain_before_research.py). Never raises."""
    try:
        BRAIN_DIR.mkdir(parents=True, exist_ok=True)
        QUERY_MARKER_FILE.write_text(json.dumps({
            "timestamp": datetime.now().isoformat(),
        }))
    except OSError:
        pass


# ===========================================================================
# Metrics — is the brain actually earning its keep? (honest instrumentation)
# ===========================================================================
# Answers three questions we've been taking on faith:
#   1. RECALL: of all decisions logged, how many are ever SURFACED by a query?
#      (A decision never returned by any query is write-only dead weight.)
#   2. NET TOKENS: brain COST (payload tokens returned to agents) vs the SAN
#      SAVINGS already tracked — is net positive?
#   3. USAGE: pre_check-before-log_decision ratio + events over time.
# HONEST LIMIT: we log "surfaced" (a fact). We CANNOT observe "the agent then
# changed its behavior" from the server — that gap is reported, not faked.

METRICS_FILE = BRAIN_DIR / "brain_metrics.jsonl"


def _append_metric_strict(event: dict) -> None:
    """Append one metric event as JSONL, PROPAGATING any failure.

    Used by transactional callers (publish_san) that must roll back when the
    metric cannot be persisted. The best-effort :func:`_log_metric` wraps this.
    """
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    event = {"ts": datetime.now().isoformat(), "session": _SESSION_ID, **event}
    with METRICS_FILE.open("a") as f:
        f.write(json.dumps(event, separators=(",", ":")) + "\n")


def _log_metric(event: dict) -> None:
    """Append one metric event as JSONL. Never raises, never blocks a tool."""
    try:
        _append_metric_strict(event)
    except Exception:
        pass


def _record_query(tool: str, surfaced_ids: list, payload: str,
                  repo: str = "", had_result: bool = True) -> None:
    """Record that a query ran, WHICH decision IDs it surfaced, and the token
    cost of the payload it returned to the agent. Drives recall + cost metrics."""
    try:
        _log_metric({
            "kind": "query",
            "tool": tool,
            "repo": repo,
            "surfaced": list(dict.fromkeys(x for x in surfaced_ids if x))[:50],
            "n_surfaced": len({x for x in surfaced_ids if x}),
            "payload_tokens": _tokens_text(payload) if payload else 0,
            "had_result": bool(had_result),
        })
    except Exception:
        pass


def _load_metrics() -> list:
    """Read all metric events. [] on any problem."""
    if not METRICS_FILE.exists():
        return []
    out = []
    try:
        for line in METRICS_FILE.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _metrics_report() -> str:
    """The honest scorecard: recall rate, net token math, usage over time.
    Answers 'is the brain earning its keep?' with facts, and names what it
    cannot measure rather than faking it."""
    events = _load_metrics()
    with LOCK:
        G = _load_graph()
    all_decisions = {n for n, d in G.nodes(data=True) if d.get("type") == "decision"}
    n_decisions = len(all_decisions)

    queries = [e for e in events if e.get("kind") == "query"]
    decisions_ev = [e for e in events if e.get("kind") == "decision"]

    # --- 1. RECALL: how many distinct decisions were ever surfaced by a query ---
    surfaced = set()
    for q in queries:
        surfaced.update(q.get("surfaced", []))
    surfaced_existing = surfaced & all_decisions  # ignore surfaced-then-pruned
    recall_pct = (100.0 * len(surfaced_existing) / n_decisions) if n_decisions else 0.0
    queries_with_hit = sum(1 for q in queries if q.get("had_result"))
    hit_rate = (100.0 * queries_with_hit / len(queries)) if queries else 0.0

    # --- 2. NET TOKENS: savings vs ALL costs (query payloads + SAN generation) ---
    sav = _load_savings_events()
    saved = sum(e.get("raw_tokens", 0) - e.get("san_tokens", 0) for e in sav)
    payload_cost = sum(q.get("payload_tokens", 0) for q in queries)
    gen_events = [e for e in events if e.get("kind") == "san_gen"]
    gen_cost = sum(e.get("gen_cost", 0) for e in gen_events)
    n_reads = len(sav)
    n_gens = len(gen_events)
    net = saved - payload_cost - gen_cost

    # --- 3. USAGE: checked-before-logging ratio + span ---
    checked = sum(1 for d in decisions_ev if d.get("checked_first"))
    checked_pct = (100.0 * checked / len(decisions_ev)) if decisions_ev else 0.0
    span_days = 0
    if events:
        try:
            ts = sorted(e.get("ts", "") for e in events if e.get("ts"))
            span_days = max(1, (datetime.fromisoformat(ts[-1])
                                - datetime.fromisoformat(ts[0])).days)
        except Exception:
            span_days = 1

    L = ["=== BRAIN METRICS (is it earning its keep?) ===", ""]
    L.append(f"Instrumented events: {len(events)} "
             f"({len(queries)} queries, {len(decisions_ev)} decisions) over ~{span_days}d")
    if not events:
        L.append("\nNo metric events yet. Run some pre_check/log_decision calls, then re-check.")
        return "\n".join(L)
    L += ["", "1. RECALL — is it a write-only diary?",
          f"   Decisions in brain: {n_decisions}",
          f"   Ever surfaced by a query: {len(surfaced_existing)} ({recall_pct:.0f}%)",
          f"   Queries that returned a hit: {queries_with_hit}/{len(queries)} ({hit_rate:.0f}%)",
          f"   -> {'Healthy: decisions are being recalled.' if recall_pct >= 20 else 'WEAK: most decisions are never recalled — largely write-only.'}"]
    L += ["", "2. NET TOKENS — does it cost more than it saves?",
          f"   SAN tokens saved on reads ({n_reads} reads):   +{saved:,}",
          f"   SAN generation cost ({n_gens} files compiled):  -{gen_cost:,}",
          f"   Query payload tokens served:                    -{payload_cost:,}",
          f"   NET:                                            {net:+,}",
          f"   -> {'POSITIVE: the brain saves more than it costs.' if net > 0 else 'NEGATIVE so far: costs exceed savings (early phase — needs more reads to amortize generation).'}"]
    if n_gens:
        avg_gen = gen_cost // max(n_gens, 1)
        avg_save = (saved // n_reads) if n_reads else 0
        breakeven = (avg_gen // avg_save) if avg_save else 0
        L.append(f"   Break-even: each SAN costs ~{avg_gen:,} to make, saves ~{avg_save:,}/read "
                 f"→ pays off after ~{breakeven} reads. (Fast-churning files that regen "
                 f"before {breakeven} reads are a NET LOSS.)")
    L.append("   HONEST: generation cost is a token proxy (real LLM output costs more); "
             "hook latency still untracked. True net is somewhat lower.")

    L += ["", "3. USAGE — consulted before writing, or write-only?",
          f"   Decisions preceded by a brain read (<30min): {checked}/{len(decisions_ev)} ({checked_pct:.0f}%)",
          f"   -> {'Used as intended (check-then-decide).' if checked_pct >= 50 else 'Often write-only: decisions logged without consulting the brain first.'}"]

    # --- 4. TREND: the real proof — do new decisions plateau while recall rises? ---
    from collections import defaultdict as _dd
    wk_dec = _dd(int)   # week -> decisions logged
    wk_surf = _dd(set)  # week -> distinct decisions surfaced by queries
    def _week(ts):
        try:
            d = datetime.fromisoformat(ts)
            return d.strftime("%Y-W%W")
        except Exception:
            return None
    for d in decisions_ev:
        w = _week(d.get("ts", ""))
        if w:
            wk_dec[w] += 1
    for q in queries:
        w = _week(q.get("ts", ""))
        if w:
            wk_surf[w].update(q.get("surfaced", []))
    weeks = sorted(set(wk_dec) | set(wk_surf))
    L += ["", "4. TREND — the goal: new decisions plateau while recall rises",
          "   (A useful brain writes less over time and recalls more. A write-only",
          "    diary keeps writing and never recalls.)"]
    if len(weeks) < 2:
        L.append(f"   Only {len(weeks)} week(s) of data — need several weeks to see the trend.")
    else:
        L.append("   week      | decisions logged | distinct recalled")
        for w in weeks[-8:]:
            L.append(f"   {w} | {wk_dec[w]:>16} | {len(wk_surf[w]):>17}")
        first_dec = wk_dec[weeks[0]]; last_dec = wk_dec[weeks[-1]]
        trend = "FLATTENING ✓" if last_dec <= first_dec else "still rising (early phase)"
        L.append(f"   -> decisions/week trend: {trend}")

    L += ["", "CANNOT MEASURE (reported honestly, not faked):",
          "   - Whether a surfaced decision actually CHANGED the agent's action.",
          "     'Surfaced' is observable; 'influenced behavior' is not, from the server.",
          "   - Per-call hook latency; exact LLM billing for SAN generation."]
    return "\n".join(L)


def _auto_heartbeat(agent: str, status: str, task: str = "", talking_to: str = "", repo: str = "") -> None:
    """Silently update office state from brain tool calls. Never raises."""
    try:
        with OFFICE_LOCK:
            state = _load_office_state()
            role = _resolve_role(agent, repo)
            entry = {
                "role": role, "status": status,
                "task": (task or "")[:100],
                "talking_to": talking_to or None,
                "message": None,
                "last_seen": datetime.now().isoformat(),
            }
            if repo:
                entry["repo"] = repo
            state.setdefault("agents", {})[agent] = entry
            _save_office_state(state)
    except Exception:
        pass  # Office state must never break brain functionality


# The instructions field is the universal lifecycle lever every MCP host reads
# (Claude Code, Codex, Cursor, …). Hosts with hooks can also enforce the same
# protocol mechanically; hosts without hooks still get the standing directive.
mcp = FastMCP(
    "agent-brain",
    instructions=(
        "Agent Brain — persistent, cross-session decision memory + cheap code "
        "reading. This protocol is NON-NEGOTIABLE for all agents:\n"
        "1. BEFORE starting any task: call get_roadmap (resume pending work) and "
        "pre_check(agent, area, action) (see past failures). Do NOT re-research "
        "what the brain already holds.\n"
        "2. To READ/EXPLORE code: use get_san(file_path=...) BEFORE raw file "
        "reads — same structure, ~5-11x fewer tokens. Raw-read only to EDIT, "
        "for non-code, or when no .san exists. Use query_san to FIND, grep only "
        "for exact literals SAN drops.\n"
        "3. When you decide an approach: call log_decision(agent, repo, area, "
        "action, reasoning) BEFORE editing code.\n"
        "4. After review/result: call log_outcome(decision_id, outcome, "
        "outcome_by, reason). Use decisions_for to find decisions touching a "
        "symbol/file.\n"
        "On hosts without enforcement hooks (e.g. Codex), following this "
        "protocol is what makes the brain work — treat it as a hard rule."
    ),
)


# ===========================================================================
# Graph I/O
# ===========================================================================


# Persistence model: decisions.json is a periodic full SNAPSHOT; decisions.journal
# is an append-only JSONL of mutations since the snapshot. Each _save_graph diffs
# the graph against its pre-save state and appends only the delta — so a single
# log_* writes a few hundred bytes instead of rewriting the whole (multi-MB)
# snapshot. The journal is replayed on top of the snapshot at load. When the
# journal grows past _JOURNAL_COMPACT_BYTES, the next save compacts: rewrite the
# snapshot and truncate the journal.
#
# Cache: parsing snapshot+journal costs ~140ms at a few MB, paid per tool call
# without this. Keyed on (snapshot stat, journal stat) so it self-invalidates
# when validate_brain swaps GRAPH_FILE to a temp dir or another process writes.
# Invariant: mutate the returned graph only under LOCK and _save_graph after.
_GRAPH_CACHE: dict = {"key": None, "graph": None, "shadow": None}
_JOURNAL_COMPACT_BYTES = 256 * 1024


def _journal_file() -> Path:
    return GRAPH_FILE.with_suffix(".journal")


def _stat_tuple(path: Path):
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _graph_cache_key():
    if not GRAPH_FILE.exists() and not _journal_file().exists():
        return None
    return (str(GRAPH_FILE), _stat_tuple(GRAPH_FILE), _stat_tuple(_journal_file()))


def _apply_journal_line(G: nx.DiGraph, op: dict) -> None:
    """Apply one journal mutation to G. Tolerant of malformed/partial lines."""
    kind = op.get("op")
    if kind == "node":
        nid = op.get("id")
        if nid is not None:
            G.add_node(nid, **op.get("data", {}))
    elif kind == "del_node":
        nid = op.get("id")
        if nid is not None and nid in G:
            G.remove_node(nid)
    elif kind == "edge":
        u, v = op.get("u"), op.get("v")
        if u is not None and v is not None:
            G.add_edge(u, v, **op.get("data", {}))
    elif kind == "del_edge":
        u, v = op.get("u"), op.get("v")
        if u is not None and v is not None and G.has_edge(u, v):
            G.remove_edge(u, v)


def _read_snapshot() -> nx.DiGraph:
    if not GRAPH_FILE.exists():
        return nx.DiGraph()
    try:
        return nx.node_link_graph(json.loads(GRAPH_FILE.read_text()))
    except (json.JSONDecodeError, OSError, KeyError):
        return nx.DiGraph()


def _load_graph() -> nx.DiGraph:
    """Load the decision graph: snapshot + replayed journal (cached)."""
    key = _graph_cache_key()
    if key is None:
        return nx.DiGraph()
    if _GRAPH_CACHE["key"] == key and _GRAPH_CACHE["graph"] is not None:
        return _GRAPH_CACHE["graph"]
    G = _read_snapshot()
    journal = _journal_file()
    if journal.exists():
        try:
            for line in journal.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    _apply_journal_line(G, json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass
    _GRAPH_CACHE["key"] = key
    _GRAPH_CACHE["graph"] = G
    _GRAPH_CACHE["shadow"] = nx.node_link_data(G)
    return G


def _write_snapshot(G: nx.DiGraph) -> None:
    """Write the full snapshot and clear the journal (compaction)."""
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = GRAPH_FILE.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(nx.node_link_data(G), separators=(",", ":")))
    os.replace(tmp, GRAPH_FILE)
    journal = _journal_file()
    if journal.exists():
        try:
            journal.unlink()
        except OSError:
            pass


def _diff_ops(old: dict, new: dict) -> list[dict]:
    """Compute journal ops transforming the old node_link_data into new."""
    ops: list[dict] = []
    old_nodes = {n["id"]: n for n in old.get("nodes", [])}
    new_nodes = {n["id"]: n for n in new.get("nodes", [])}
    for nid, nd in new_nodes.items():
        if old_nodes.get(nid) != nd:
            data = {k: v for k, v in nd.items() if k != "id"}
            ops.append({"op": "node", "id": nid, "data": data})
    for nid in old_nodes:
        if nid not in new_nodes:
            ops.append({"op": "del_node", "id": nid})

    def _edge_key(e):
        return (e.get("source"), e.get("target"))
    old_edges = {_edge_key(e): e for e in old.get("links", [])}
    new_edges = {_edge_key(e): e for e in new.get("links", [])}
    for ek, ed in new_edges.items():
        if old_edges.get(ek) != ed:
            data = {k: v for k, v in ed.items() if k not in ("source", "target")}
            ops.append({"op": "edge", "u": ek[0], "v": ek[1], "data": data})
    for ek in old_edges:
        if ek not in new_edges:
            ops.append({"op": "del_edge", "u": ek[0], "v": ek[1]})
    return ops


def _save_graph(G: nx.DiGraph) -> None:
    """Persist G by appending only the delta to the journal (snapshot stays
    until compaction). Falls back to a full snapshot when there's no prior
    shadow or the journal has grown large."""
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    new_data = nx.node_link_data(G)
    shadow = _GRAPH_CACHE.get("shadow")
    journal = _journal_file()

    # First write of a session, or cache was bypassed: snapshot from scratch.
    if shadow is None or _GRAPH_CACHE.get("graph") is not G:
        _write_snapshot(G)
        _GRAPH_CACHE["key"] = _graph_cache_key()
        _GRAPH_CACHE["graph"] = G
        _GRAPH_CACHE["shadow"] = new_data
        return

    ops = _diff_ops(shadow, new_data)
    if ops:
        try:
            journal_size = journal.stat().st_size if journal.exists() else 0
        except OSError:
            journal_size = 0
        if journal_size >= _JOURNAL_COMPACT_BYTES:
            _write_snapshot(G)  # compaction: fold journal into snapshot
        else:
            with journal.open("a") as f:
                for op in ops:
                    f.write(json.dumps(op, separators=(",", ":")) + "\n")
    _GRAPH_CACHE["key"] = _graph_cache_key()
    _GRAPH_CACHE["graph"] = G
    _GRAPH_CACHE["shadow"] = new_data


# ===========================================================================
# Code-Review-Graph Bridge
# ===========================================================================


def _get_crg_db(repo: str) -> Optional[Path]:
    """Find the code-review-graph SQLite DB for a repo."""
    repo_paths = _get_repo_paths()
    repo_path = repo_paths.get(repo)
    if not repo_path:
        for name, path in repo_paths.items():
            if repo.lower() in name.lower():
                repo_path = path
                break
    if not repo_path:
        return None
    db = repo_path / ".code-review-graph" / "graph.db"
    return db if db.exists() else None


def _resolve_files_to_code_nodes(repo: str, files: list[str]) -> list[dict]:
    """Resolve file paths to code-review-graph qualified_names."""
    db_path = _get_crg_db(repo)
    if not db_path or not files:
        return []

    results = []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            repo_paths = _get_repo_paths()
            for f in files:
                rel = f
                for prefix in [str(p) + "/" for p in repo_paths.values()]:
                    if f.startswith(prefix):
                        rel = f[len(prefix):]
                        break
                rows = conn.execute(
                    "SELECT kind, name, qualified_name, file_path, line_start, line_end "
                    "FROM nodes WHERE file_path LIKE ? ORDER BY kind, name",
                    (f"%{rel}%",),
                ).fetchall()
                for r in rows:
                    results.append(dict(r))
    except Exception:
        pass
    return results


def _get_code_node_details(repo: str, qualified_name: str,
                           conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    """Get details for a specific code node by qualified_name."""
    if conn is not None:
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT kind, name, qualified_name, file_path, line_start, line_end, "
                "parent_name, params, return_type "
                "FROM nodes WHERE qualified_name = ?",
                (qualified_name,),
            ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None
    db_path = _get_crg_db(repo)
    if not db_path:
        return None
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT kind, name, qualified_name, file_path, line_start, line_end, "
                "parent_name, params, return_type "
                "FROM nodes WHERE qualified_name = ?",
                (qualified_name,),
            ).fetchone()
            return dict(row) if row else None
    except Exception:
        return None


def _get_callers_of(repo: str, qualified_name: str,
                    conn: Optional[sqlite3.Connection] = None) -> list[str]:
    """Find what calls a given code node."""
    if conn is not None:
        try:
            rows = conn.execute(
                "SELECT DISTINCT source_qualified FROM edges "
                "WHERE target_qualified = ? AND kind = 'CALLS'",
                (qualified_name,),
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []
    db_path = _get_crg_db(repo)
    if not db_path:
        return []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT DISTINCT source_qualified FROM edges "
                "WHERE target_qualified = ? AND kind = 'CALLS'",
                (qualified_name,),
            ).fetchall()
            return [r[0] for r in rows]
    except Exception:
        return []


# ===========================================================================
# Fuzzy Similarity Matching
# ===========================================================================


def _tokenize(text: str) -> set[str]:
    """Extract meaningful tokens from text, lowercased."""
    # Split camelCase BEFORE lowercasing — once lowered there are no
    # uppercase letters left for the regex to match.
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = text.lower()
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    tokens = set(text.split())
    stopwords = {
        "the", "a", "an", "is", "was", "are", "be", "to", "of", "and", "in",
        "that", "it", "for", "on", "with", "as", "at", "by", "from", "or",
        "not", "but", "this", "should", "must", "also",
        "don", "t", "s", "doesn", "didn", "won", "can",
    }
    return tokens - stopwords


_DOMAIN_TERMS = {
    "interface", "abstract", "concrete", "dip", "solid", "srp", "ocp",
    "dependency", "injection", "di", "koin", "hilt", "module",
    "middleware", "rate", "limiting", "throttl", "bucket", "window",
    "cache", "caching", "firestore", "firebase", "repository", "repo",
    "auth", "token", "jwt", "session", "login", "register",
    "layer", "clean", "architecture", "violation", "infrastructure",
    "service", "handler", "route", "endpoint",
}


def _similarity_sets(tokens_a: set, tokens_b: set) -> float:
    """Jaccard + domain-term bonus over pre-tokenized sets."""
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    jaccard = len(intersection) / len(union)
    domain_shared = intersection & _DOMAIN_TERMS
    domain_boost = len(domain_shared) / len(union) * 0.3 if domain_shared else 0.0
    return min(jaccard + domain_boost, 1.0)


def _similarity(a: str, b: str) -> float:
    """Enhanced similarity: Jaccard + domain-term bonus."""
    return _similarity_sets(_tokenize(a), _tokenize(b))


def _find_similar_rejections(
    G: nx.DiGraph, action_description: str, area: str = "", threshold: float = 0.15,
) -> list[dict]:
    """Find past rejections similar to a proposed action."""
    similar = []
    for node_id, data in G.nodes(data=True):
        if data.get("type") != "decision":
            continue
        if data.get("outcome") not in ("rejected", "failed"):
            continue
        if area and data.get("area") != area:
            continue
        past_action = data.get("action", "")
        past_reason = data.get("outcome_reason", "")
        sim = max(_similarity(action_description, past_action),
                  _similarity(action_description, past_reason))
        if sim >= threshold:
            similar.append({
                "id": node_id, "agent": data.get("agent", "?"),
                "action": past_action[:200], "reason": past_reason[:300],
                "outcome_by": data.get("outcome_by", "?"),
                "area": data.get("area", "?"), "similarity": sim,
                "timestamp": data.get("timestamp", "?")[:10],
                "timestamp_full": data.get("timestamp", ""),
            })
    similar.sort(key=lambda x: -x["similarity"])
    return similar


def _cluster_rejection_reasons(G: nx.DiGraph, area: str = "") -> list[list[dict]]:
    """Cluster rejection reasons by similarity."""
    rejections = []
    for node_id, data in G.nodes(data=True):
        if data.get("type") != "decision":
            continue
        if data.get("outcome") not in ("rejected", "failed"):
            continue
        if area and data.get("area") != area:
            continue
        rejections.append({
            "id": node_id, "reason": data.get("outcome_reason", "unknown"),
            "agent": data.get("agent", "?"), "area": data.get("area", "?"),
        })
    if not rejections:
        return []
    # O(R^2) pairwise clustering: cap input to the most recent 500 rejections
    # and tokenize each reason exactly once (was re-tokenized per pair).
    rejections = rejections[-500:]
    token_sets = [_tokenize(r["reason"]) for r in rejections]
    clusters: list[list[dict]] = []
    used: set[int] = set()
    for i, r in enumerate(rejections):
        if i in used:
            continue
        cluster = [r]
        used.add(i)
        for j, other in enumerate(rejections):
            if j in used:
                continue
            if _similarity_sets(token_sets[i], token_sets[j]) >= 0.20:
                cluster.append(other)
                used.add(j)
        if len(cluster) >= 2:
            clusters.append(cluster)
    return clusters


# ===========================================================================
# Agent Scorecards
# ===========================================================================


def _compute_scorecard(G: nx.DiGraph, agent_name: str = "") -> dict:
    """Compute detailed scorecard for agent(s)."""
    agents: dict[str, dict] = {}
    for node_id, data in G.nodes(data=True):
        if data.get("type") != "decision":
            continue
        a = data.get("agent", "unknown")
        if agent_name and a != agent_name:
            continue
        if a not in agents:
            agents[a] = {
                "total": 0, "accepted": 0, "rejected": 0, "failed": 0,
                "pending": 0, "revised": 0, "rejection_reasons": [],
                "areas": defaultdict(lambda: {"total": 0, "rejected": 0}),
                "timeline": [], "feedback_received": 0, "blocker_count": 0,
            }
        s = agents[a]
        s["total"] += 1
        outcome = data.get("outcome", "pending")
        if outcome in s:
            s[outcome] += 1
        area = data.get("area", "unknown")
        s["areas"][area]["total"] += 1
        if outcome in ("rejected", "failed"):
            s["rejection_reasons"].append(data.get("outcome_reason", "unknown"))
            s["areas"][area]["rejected"] += 1
        s["timeline"].append({"timestamp": data.get("timestamp", ""), "outcome": outcome})

    for node_id, data in G.nodes(data=True):
        if data.get("type") != "feedback":
            continue
        for _, target in G.out_edges(node_id):
            target_agent = G.nodes.get(target, {}).get("agent", "")
            if target_agent in agents:
                agents[target_agent]["feedback_received"] += 1
                if data.get("severity") == "blocker":
                    agents[target_agent]["blocker_count"] += 1

    for a, s in agents.items():
        timeline = sorted(s["timeline"], key=lambda x: x["timestamp"])
        if len(timeline) >= 4:
            mid = len(timeline) // 2
            first_rej = sum(1 for t in timeline[:mid] if t["outcome"] in ("rejected", "failed"))
            second_rej = sum(1 for t in timeline[mid:] if t["outcome"] in ("rejected", "failed"))
            first_rate = first_rej / mid if mid else 0
            second_rate = second_rej / (len(timeline) - mid) if (len(timeline) - mid) else 0
            s["trend"] = "improving" if second_rate < first_rate else (
                "declining" if second_rate > first_rate else "stable")
        else:
            s["trend"] = "insufficient_data"
        if s["rejection_reasons"]:
            reason_groups: dict[str, int] = {}
            for reason in s["rejection_reasons"]:
                matched = False
                for existing in reason_groups:
                    if _similarity(reason, existing) >= 0.20:
                        reason_groups[existing] += 1
                        matched = True
                        break
                if not matched:
                    reason_groups[reason] = 1
            s["top_rejection_categories"] = sorted(reason_groups.items(), key=lambda x: -x[1])[:3]
        else:
            s["top_rejection_categories"] = []
    return agents


def _age_note(timestamp: str, stale_days: int = 90) -> str:
    """Age annotation for old rejections — context may have changed since."""
    try:
        age_days = (datetime.now() - datetime.fromisoformat(timestamp)).days
    except (ValueError, TypeError):
        return ""
    if age_days >= stale_days:
        months = max(1, age_days // 30)
        return f" [{months}mo old — verify the reason still applies]"
    return ""


def _adaptive_warning_level(G: nx.DiGraph, agent: str, area: str) -> str:
    """Determine warning aggressiveness based on agent history."""
    total = 0
    rejected = 0
    for _, data in G.nodes(data=True):
        if data.get("type") != "decision" or data.get("agent") != agent:
            continue
        total += 1
        if data.get("outcome") in ("rejected", "failed"):
            rejected += 1
    if total < 3:
        return "normal"
    rate = rejected / total
    if rate >= 0.5:
        return "strict"
    elif rate >= 0.3:
        return "elevated"
    return "normal"


# ===========================================================================
# MCP Tools — Core
# ===========================================================================


@mcp.tool()
def pre_check(agent: str, area: str, action_description: str,
              repo: str = "") -> str:
    """
    CHECK BEFORE DOING WORK. Returns past failures in this area, similar
    rejections elsewhere, and adaptive warnings from this agent's history.

    repo: optional repo name; when given, also surfaces SAN coverage so you
        read code with get_san (sig/full) instead of raw Read.
    """
    _auto_heartbeat(agent, "planning", action_description)
    _write_query_marker()
    with LOCK:
        G = _load_graph()
    sections = []
    san_line = _san_coverage_line(repo) if repo else ""
    level = _adaptive_warning_level(G, agent, area)
    if level == "strict":
        sections.append(
            f"ALERT: Agent '{agent}' has high rejection rate. "
            f"Extra scrutiny applied. Review ALL warnings carefully.")
    elif level == "elevated":
        sections.append(
            f"NOTE: Agent '{agent}' has elevated rejection rate. "
            f"Pay close attention to past failures below.")

    exact_warnings = []
    surfaced_ids = []  # decision IDs this pre_check actually shows the agent (recall metric)
    my_failures_here = 0
    plan_pointer = None
    for node_id, data in G.nodes(data=True):
        if data.get("type") != "decision" or data.get("area") != area:
            continue
        if data.get("outcome") in ("rejected", "failed"):
            if data.get("agent") == agent:
                my_failures_here += 1
            surfaced_ids.append(node_id)
            exact_warnings.append(
                f"- [{data.get('timestamp', '?')[:10]}]"
                f"{_age_note(data.get('timestamp', ''))} "
                f"{data.get('agent', '?')} tried: {str(data.get('action', '?'))[:200]}\n"
                f"  REJECTED by {data.get('outcome_by', '?')}: "
                f"{str(data.get('outcome_reason', '?'))[:300]}")
        if data.get("plan_file") and data.get("outcome") in ("pending", "accepted"):
            # Most recent plan wins (nodes iterate in insertion order)
            plan_pointer = (data.get("plan_file"), data.get("agent", "?"),
                            data.get("timestamp", "?")[:10])
    if exact_warnings:
        sections.append(
            f"EXACT MATCHES in '{area}' ({len(exact_warnings)}):\n\n"
            + "\n\n".join(exact_warnings[-5:]))

    if plan_pointer:
        sections.append(
            f"PLAN AVAILABLE: {plan_pointer[0]} (by {plan_pointer[1]}, {plan_pointer[2]}). "
            f"Read it before re-deriving the approach — execute against it, don't re-plan.")

    # Two-strikes escalation: this agent failed >=2 times in this area ->
    # recommend re-spawning the work on a stronger model tier.
    if my_failures_here >= 2:
        routing = _load_config().get("model_routing", {})
        escalate_to = routing.get("escalate", "a stronger model (e.g. opus or fable)")
        sections.append(
            f"ESCALATION HINT: '{agent}' has {my_failures_here} rejected/failed "
            f"decisions in '{area}'. Two-strikes rule: do NOT retry on the same "
            f"model tier — re-spawn this task on {escalate_to}.")

    similar = _find_similar_rejections(G, action_description, threshold=0.15)
    exact_ids = {nid for nid, d in G.nodes(data=True)
                 if d.get("area") == area and d.get("outcome") in ("rejected", "failed")}
    similar = [s for s in similar if s["id"] not in exact_ids]
    if similar:
        sim_lines = []
        for s in similar[:5]:
            surfaced_ids.append(s["id"])
            pct = int(s["similarity"] * 100)
            sim_lines.append(
                f"- [{s['timestamp']}]{_age_note(s.get('timestamp_full', ''))} "
                f"{s['agent']} in area={s['area']} "
                f"({pct}% similar): {s['action']}\n"
                f"  REJECTED by {s['outcome_by']}: {s['reason']}")
        sections.append(
            f"SIMILAR REJECTIONS across other areas ({len(similar)}):\n\n"
            + "\n\n".join(sim_lines))

    routing = _load_config().get("model_routing", {})
    routing_line = ("MODEL ROUTING: "
                    + " | ".join(f"{phase}={model}" for phase, model in routing.items())
                    ) if routing else ""

    if not sections:
        base = f"No past failures in '{area}'. Proceed with: {action_description}"
        tail = "\n\n".join(x for x in (san_line, routing_line) if x)
        out = f"{base}\n\n{tail}" if tail else base
        _record_query("pre_check", surfaced_ids, out, repo=repo, had_result=False)
        return out
    if san_line:
        sections.append(san_line)
    if routing_line:
        sections.append(routing_line)

    if level == "strict":
        scorecard = _compute_scorecard(G, agent)
        if agent in scorecard:
            cats = scorecard[agent].get("top_rejection_categories", [])
            if cats:
                top = "; ".join(f'"{c[0][:60]}" ({c[1]}x)' for c in cats[:2])
                sections.append(f"YOUR TOP REJECTION PATTERNS: {top}")

    out = "\n\n---\n\n".join(sections)
    _record_query("pre_check", surfaced_ids, out, repo=repo, had_result=bool(surfaced_ids))
    return out


def _compact_string_list(values, limit: int = 20, chars: int = 200) -> list[str]:
    """Normalize optional list fields without letting one item bloat the graph."""
    if not values:
        return []
    if isinstance(values, (str, bytes)) or not isinstance(values, list):
        values = [values]
    out = []
    for value in values[:limit]:
        out.append(_cap_text(str(value), chars))
    return out


def _sanitize_validation(validation) -> list[dict]:
    """Keep validation evidence structured, bounded, and JSON-friendly."""
    if not validation:
        return []
    if isinstance(validation, dict):
        validation = [validation]
    if not isinstance(validation, list):
        return []
    out = []
    for item in validation[:10]:
        if not isinstance(item, dict):
            continue
        clean = {}
        for key, value in item.items():
            k = _cap_text(str(key), 80)
            if isinstance(value, (int, float, bool)) or value is None:
                clean[k] = value
            elif isinstance(value, list):
                clean[k] = _compact_string_list(value, limit=20, chars=160)
            else:
                clean[k] = _cap_text(str(value), 300)
        if clean:
            out.append(clean)
    return out


def _git_output(repo_path: Path, *args: str) -> Optional[str]:
    """Run a local git metadata command. Never raises or contacts the network."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(repo_path),
            text=True,
            capture_output=True,
            timeout=2,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _collect_git_metadata(repo: str) -> dict:
    """Best-effort repo snapshot for handoff records."""
    repo_path = _resolve_repo_path(repo)
    if not repo_path:
        return {}
    branch = _git_output(repo_path, "branch", "--show-current") or ""
    commit = _git_output(repo_path, "rev-parse", "HEAD") or ""
    origin_head = _git_output(repo_path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or ""
    status = _git_output(repo_path, "status", "--short") or ""
    uncommitted = []
    for line in status.splitlines():
        if len(line) >= 4:
            uncommitted.append(line[3:])
    meta = {
        "branch": branch,
        "base_branch": origin_head.replace("origin/", "", 1) if origin_head else "",
        "commit_before": commit,
        "working_tree_dirty": bool(status),
        "uncommitted_files": uncommitted[:50],
    }
    return {k: v for k, v in meta.items() if v not in ("", [], None)}


def _build_git_metadata(repo: str, branch: Optional[str], base_branch: Optional[str],
                        commit_before: Optional[str], commit_after: Optional[str],
                        commit_range: Optional[str], pr_number: Optional[str],
                        working_tree_dirty: Optional[bool],
                        uncommitted_files: Optional[list[str]]) -> dict:
    """Merge explicit git metadata over best-effort auto-detected fields."""
    git = _collect_git_metadata(repo)
    explicit = {
        "branch": branch,
        "base_branch": base_branch,
        "commit_before": commit_before,
        "commit_after": commit_after,
        "commit_range": commit_range,
        "pr_number": pr_number,
        "working_tree_dirty": working_tree_dirty,
        "uncommitted_files": _compact_string_list(uncommitted_files, limit=50, chars=300),
    }
    for key, value in explicit.items():
        if value not in (None, "", []):
            git[key] = value
    for key in ("branch", "base_branch", "commit_before", "commit_after", "commit_range", "pr_number"):
        if key in git:
            git[key] = _cap_text(str(git[key]), 200)
    if "uncommitted_files" in git:
        git["uncommitted_files"] = _compact_string_list(git["uncommitted_files"], limit=50, chars=300)
    return {k: v for k, v in git.items() if v not in ("", [], None)}


def _looks_complete_but_pending(action: str) -> bool:
    return any(word in action.upper() for word in ("COMPLETE", "PUSHED", "MERGED", "REVIEWED"))


@mcp.tool()
def log_decision(
    agent: str, repo: str, area: str, action: str, reasoning: str,
    files_touched: Optional[list[str]] = None,
    code_symbols: Optional[list[str]] = None,
    plan_file: Optional[str] = None,
    handoff_summary: Optional[str] = None,
    branch: Optional[str] = None,
    base_branch: Optional[str] = None,
    commit_before: Optional[str] = None,
    commit_after: Optional[str] = None,
    commit_range: Optional[str] = None,
    pr_number: Optional[str] = None,
    working_tree_dirty: Optional[bool] = None,
    uncommitted_files: Optional[list[str]] = None,
    validation: Optional[list[dict]] = None,
    blockers: Optional[list[str]] = None,
    deferred_work: Optional[list[str]] = None,
    do_not_touch: Optional[list[str]] = None,
    next_action: Optional[str] = None,
) -> str:
    """
    Log a decision. Call AFTER pre_check, BEFORE doing work — required before
    code edits. area e.g. "auth"; optional files_touched/code_symbols link
    decisions to code; optional handoff/git/validation fields make future
    get_resume_context calls compact and transcript-free.
    """
    # Cap stored text — unbounded fields produced 31KB decision nodes that
    # bloat the graph file and every query/pre_check response echoing them.
    action = _cap_text(action, 1000)
    reasoning = _cap_text(reasoning, 2000)
    git = _build_git_metadata(repo, branch, base_branch, commit_before, commit_after,
                              commit_range, pr_number, working_tree_dirty,
                              uncommitted_files)
    with LOCK:
        G = _load_graph()
        node_id = f"dec_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        resolved_symbols = (code_symbols or [])[:50]
        files = (files_touched or git.get("uncommitted_files") or [])[:50]
        if files and not resolved_symbols:
            code_nodes = _resolve_files_to_code_nodes(repo, files)
            resolved_symbols = list(set(n["qualified_name"] for n in code_nodes))
        node_attrs = dict(type="decision", agent=agent, repo=repo, area=area,
                          action=action, reasoning=reasoning, files=files,
                          code_symbols=resolved_symbols,
                          timestamp=datetime.now().isoformat(), outcome="pending")
        if plan_file:
            node_attrs["plan_file"] = _cap_text(plan_file, 500)
        if handoff_summary:
            node_attrs["handoff_summary"] = _cap_text(handoff_summary, 1200)
        if git:
            node_attrs["git"] = git
        clean_validation = _sanitize_validation(validation)
        if clean_validation:
            node_attrs["validation"] = clean_validation
        for key, values in (
            ("blockers", blockers),
            ("deferred_work", deferred_work),
            ("do_not_touch", do_not_touch),
        ):
            clean = _compact_string_list(values)
            if clean:
                node_attrs[key] = clean
        if next_action:
            node_attrs["next_action"] = _cap_text(next_action, 500)
        G.add_node(node_id, **node_attrs)
        for sym in resolved_symbols:
            sym_node = f"code:{sym}"
            if sym_node not in G:
                G.add_node(sym_node, type="code_ref", qualified_name=sym, repo=repo)
            G.add_edge(node_id, sym_node, relation="touches")
        _save_graph(G)
    _auto_heartbeat(agent, "working", action, repo=repo)
    # Write marker for brain-protocol enforcement hook
    _write_decision_marker(agent, node_id)
    # Usage metric: was this decision preceded by a brain READ (pre_check/query)
    # within 30 min? If not, the brain is being written to but not consulted.
    checked_first = False
    try:
        marker = json.loads(QUERY_MARKER_FILE.read_text())
        age = (datetime.now() - datetime.fromisoformat(marker.get("timestamp", ""))).total_seconds()
        checked_first = age <= 30 * 60
    except Exception:
        pass
    _log_metric({"kind": "decision", "id": node_id, "repo": repo, "area": area,
                 "agent": agent, "checked_first": checked_first})
    result = f"Decision logged: {node_id}"
    if resolved_symbols:
        result += f"\nLinked to {len(resolved_symbols)} code symbol(s)"
    if _looks_complete_but_pending(action):
        result += ("\nNUDGE: action looks complete but outcome is still pending; "
                   "call log_outcome when validation/review is done.")
    return result


@mcp.tool()
def log_outcome(decision_id: str, outcome: str, outcome_by: str, reason: str) -> str:
    """
    Record a decision's outcome after review/execution.
    outcome: accepted | rejected | failed | revised. outcome_by: reviewer name.
    """
    with LOCK:
        G = _load_graph()
        if decision_id not in G:
            return f"ERROR: decision '{decision_id}' not found"
        G.nodes[decision_id]["outcome"] = outcome
        G.nodes[decision_id]["outcome_by"] = outcome_by
        G.nodes[decision_id]["outcome_reason"] = _cap_text(reason, 1000)
        G.nodes[decision_id]["outcome_timestamp"] = datetime.now().isoformat()
        dec_agent = G.nodes[decision_id].get("agent", "")
        dec_repo = G.nodes[decision_id].get("repo", "")
        _save_graph(G)
    _auto_heartbeat(outcome_by, "reviewing", f"outcome: {outcome}", talking_to=dec_agent, repo=dec_repo)
    return f"Outcome recorded: {decision_id} -> {outcome} by {outcome_by}"


@mcp.tool()
def log_feedback(agent: str, decision_id: str, feedback: str, severity: str = "info") -> str:
    """
    Log review feedback on a decision. Used by reviewers (PE, QA, PM).

    Args:
        agent: Who is giving feedback (e.g. "marcus", "qa-1")
        decision_id: The decision being reviewed
        feedback: The feedback content
        severity: One of: info, warning, blocker
    """
    with LOCK:
        G = _load_graph()
        if decision_id not in G:
            return f"ERROR: decision '{decision_id}' not found"
        fb_id = f"fb_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        G.add_node(fb_id, type="feedback", agent=agent, feedback=_cap_text(feedback, 2000),
                    severity=severity, timestamp=datetime.now().isoformat())
        G.add_edge(fb_id, decision_id, relation="feedback_on")
        dec_agent = G.nodes.get(decision_id, {}).get("agent", "")
        dec_repo = G.nodes.get(decision_id, {}).get("repo", "")
        _save_graph(G)
    _auto_heartbeat(agent, "reviewing", feedback[:80], talking_to=dec_agent, repo=dec_repo)
    return f"Feedback logged: {fb_id} -> {decision_id}"


# ===========================================================================
# MCP Tools — Code Bridge
# ===========================================================================


@mcp.tool()
def decisions_for(target: str, repo: str = "", outcome: str = "", limit: int = 10) -> str:
    """
    Find decisions touching a code symbol or file (auto-detected: "/" or an
    extension means file path, else qualified_name). Optional repo/outcome
    filters; limit caps results (default 10).
    """
    # Use the global SOURCE_EXTS (single source of truth) so file-path detection
    # never drifts from generation/freshness/orphan logic.
    looks_like_file = "/" in target or target.lower().endswith(SOURCE_EXTS)
    if looks_like_file:
        return _decisions_for_file(target, repo, limit)
    return _decisions_for_code(target, repo, outcome, limit)


def _decisions_for_code(qualified_name: str, repo: str = "", outcome: str = "", limit: int = 10) -> str:
    """Find all decisions that touched a specific code symbol."""
    with LOCK:
        G = _load_graph()
    sym_node = f"code:{qualified_name}"
    results = []
    if sym_node in G:
        for pred in G.predecessors(sym_node):
            edge = G.edges.get((pred, sym_node), {})
            if edge.get("relation") != "touches":
                continue
            data = G.nodes.get(pred, {})
            if data.get("type") != "decision":
                continue
            if repo and data.get("repo") != repo:
                continue
            if outcome and data.get("outcome") != outcome:
                continue
            results.append(
                f"[{pred}] {data.get('agent','?')} | {str(data.get('action','?'))[:150]} "
                f"-> {data.get('outcome','pending')}")
    for node_id, data in G.nodes(data=True):
        if data.get("type") != "decision":
            continue
        symbols = data.get("code_symbols", [])
        if any(qualified_name in s or s in qualified_name for s in symbols):
            if repo and data.get("repo") != repo:
                continue
            if outcome and data.get("outcome") != outcome:
                continue
            line = (f"[{node_id}] {data.get('agent','?')} | {str(data.get('action','?'))[:150]} "
                    f"-> {data.get('outcome','pending')}")
            if line not in results:
                results.append(line)
    if not results:
        return f"No decisions found touching '{qualified_name}'."
    shown = results[-limit:]
    header = f"{len(results)} decision(s) touching '{qualified_name}'"
    if len(results) > len(shown):
        header += f" (showing last {len(shown)})"
    return header + ":\n\n" + "\n".join(shown)


def _decisions_for_file(file_path: str, repo: str = "", limit: int = 10) -> str:
    """Find all decisions that touched a specific file."""
    with LOCK:
        G = _load_graph()
    rel_path = file_path
    repo_paths = _get_repo_paths()
    for prefix in [str(p) + "/" for p in repo_paths.values()]:
        if file_path.startswith(prefix):
            rel_path = file_path[len(prefix):]
            break
    results = []
    for node_id, data in G.nodes(data=True):
        if data.get("type") != "decision":
            continue
        if repo and data.get("repo") != repo:
            continue
        files = data.get("files", [])
        if any(rel_path in f or f in rel_path for f in files):
            results.append(
                f"[{node_id}] {data.get('agent','?')} | {str(data.get('action','?'))[:150]} "
                f"-> {data.get('outcome','pending')}")
    if not results:
        return f"No decisions found touching '{rel_path}'."
    shown = results[-limit:]
    header = f"{len(results)} decision(s) touching '{rel_path}'"
    if len(results) > len(shown):
        header += f" (showing last {len(shown)})"
    return header + ":\n\n" + "\n".join(shown)


@mcp.tool()
def code_impact(decision_id: str) -> str:
    """
    Show what code symbols a decision touched and their callers.

    Args:
        decision_id: The decision ID to analyze
    """
    with LOCK:
        G = _load_graph()
    if decision_id not in G:
        return f"ERROR: '{decision_id}' not found"
    data = G.nodes[decision_id]
    repo = data.get("repo", "")
    symbols = data.get("code_symbols", [])
    files = data.get("files", [])
    lines = [f"Decision: {decision_id}", f"Action: {str(data.get('action', '?'))[:200]}", ""]
    if files:
        lines.append(f"Files touched ({len(files)}):")
        for f in files[:20]:
            lines.append(f"  - {f}")
        if len(files) > 20:
            lines.append(f"  ... and {len(files) - 20} more")
    if symbols:
        lines.append(f"\nCode symbols ({len(symbols)}):")
        # One connection for all symbol lookups (was 2 connects + 2 config
        # reads per symbol)
        conn = None
        db_path = _get_crg_db(repo)
        if db_path:
            try:
                conn = sqlite3.connect(str(db_path))
            except Exception:
                conn = None
        try:
            for sym in symbols[:20]:
                detail = _get_code_node_details(repo, sym, conn=conn)
                if detail:
                    lines.append(f"  - [{detail['kind']}] {detail['name']} "
                                 f"({detail['file_path']}:{detail.get('line_start', '?')})")
                    callers = _get_callers_of(repo, sym, conn=conn)
                    if callers:
                        lines.append(f"    Called by: {', '.join(callers[:5])}")
                        if len(callers) > 5:
                            lines.append(f"    ... and {len(callers) - 5} more")
                else:
                    lines.append(f"  - {sym} (not in current code graph)")
            if len(symbols) > 20:
                lines.append(f"  ... and {len(symbols) - 20} more symbols")
        finally:
            if conn is not None:
                conn.close()
    if not symbols and not files:
        lines.append("No code symbols or files linked.")
    return "\n".join(lines)


# ===========================================================================
# MCP Tools — Patterns & Similarity
# ===========================================================================


def similar_failures(action_description: str, area: str = "", threshold: float = 0.15) -> str:
    """Find past rejections/failures similar to a proposed action (internal helper)."""
    with LOCK:
        G = _load_graph()
    similar = _find_similar_rejections(G, action_description, area, threshold)
    if not similar:
        return "No similar past failures found."
    lines = [f"{len(similar)} similar past failure(s):\n"]
    for s in similar[:10]:
        pct = int(s["similarity"] * 100)
        lines.append(
            f"- [{s['timestamp']}] {s['agent']} (area={s['area']}, {pct}% match)\n"
            f"  Tried: {s['action']}\n"
            f"  REJECTED by {s['outcome_by']}: {s['reason']}")
    return "\n\n".join(lines)


@mcp.tool()
def get_patterns(area: str = "", min_count: int = 1, action: str = "") -> str:
    """
    Analyze past rejections. With action set: failures similar to that plan.
    Without: clusters of recurring failure patterns (min_count filters size).
    """
    if action:
        return similar_failures(action, area)
    with LOCK:
        G = _load_graph()
    clusters = _cluster_rejection_reasons(G, area)
    if not clusters:
        rejections = []
        for _, data in G.nodes(data=True):
            if data.get("type") != "decision" or data.get("outcome") not in ("rejected", "failed"):
                continue
            if area and data.get("area") != area:
                continue
            rejections.append(f"- {data.get('agent','?')}: {str(data.get('outcome_reason','unknown'))[:150]}")
        if rejections:
            shown = rejections[-20:]
            return (f"No clusters, {len(rejections)} individual rejection(s)"
                    + (f" (last {len(shown)})" if len(rejections) > 20 else "")
                    + ":\n\n" + "\n".join(shown))
        return "No rejection patterns found."
    clusters = [c for c in clusters if len(c) >= min_count][:10]
    if not clusters:
        return f"No rejection clusters with at least {min_count} member(s)."
    lines = [f"{len(clusters)} pattern cluster(s):\n"]
    for i, cluster in enumerate(clusters, 1):
        agents_in = sorted(set(r["agent"] for r in cluster))
        areas_in = sorted(set(r["area"] for r in cluster))
        lines.append(f"Pattern #{i} ({len(cluster)}x, agents: {', '.join(agents_in)}, "
                      f"areas: {', '.join(areas_in)}):")
        lines.append(f"  Core issue: {str(cluster[0]['reason'])[:150]}")
        if len(cluster) > 1:
            lines.append(f"  Also: {str(cluster[1]['reason'])[:150]}")
    return "\n\n".join(lines)


# ===========================================================================
# MCP Tools — Scorecards & Dashboard
# ===========================================================================


def get_agent_stats(agent: str = "") -> str:
    """Compact decision stats for an agent or all agents (internal helper)."""
    with LOCK:
        G = _load_graph()
    stats: dict[str, dict] = {}
    for _, data in G.nodes(data=True):
        if data.get("type") != "decision":
            continue
        a = data.get("agent", "unknown")
        if agent and a != agent:
            continue
        if a not in stats:
            stats[a] = {"total": 0, "accepted": 0, "rejected": 0, "failed": 0, "pending": 0}
        stats[a]["total"] += 1
        outcome = data.get("outcome", "pending")
        if outcome in stats[a]:
            stats[a][outcome] += 1
    if not stats:
        return "No decisions found."
    lines = []
    for a, s in sorted(stats.items()):
        total = s["total"]
        rate = (s["accepted"] / total * 100) if total else 0
        lines.append(f"{a}: {total} decisions | {s['accepted']} accepted ({rate:.0f}%) | "
                      f"{s['rejected']} rejected | {s['failed']} failed | {s['pending']} pending")
    return "\n".join(lines)


@mcp.tool()
def agent_scorecard(agent: str = "", detail: bool = False) -> str:
    """
    Agent performance stats. Compact summary by default (empty agent = all);
    detail=True adds trends, top rejections, area breakdown (needs agent).
    """
    if not detail:
        return get_agent_stats(agent)
    if not agent:
        return "ERROR: detail=True requires an agent name."
    with LOCK:
        G = _load_graph()
    scorecards = _compute_scorecard(G, agent)
    if agent not in scorecards:
        return f"No decisions found for '{agent}'."
    s = scorecards[agent]
    total = s["total"]
    acc_rate = (s["accepted"] / total * 100) if total else 0
    rej_rate = (s["rejected"] / total * 100) if total else 0
    level = _adaptive_warning_level(G, agent, "")

    lines = [
        f"=== SCORECARD: {agent} ===", "",
        f"Total: {total} | Accepted: {s['accepted']} ({acc_rate:.0f}%) | "
        f"Rejected: {s['rejected']} ({rej_rate:.0f}%) | Failed: {s['failed']} | "
        f"Pending: {s['pending']}", f"Feedback: {s['feedback_received']} | "
        f"Blockers: {s['blocker_count']}", f"Trend: {s['trend'].upper()} | "
        f"Warning level: {level.upper()}",
    ]
    if s["areas"]:
        lines.append("\nArea breakdown (top 10 by rejections):")
        ranked_areas = sorted(s["areas"].items(),
                              key=lambda kv: (-kv[1]["rejected"], -kv[1]["total"]))[:10]
        for area_name, area_stats in sorted(ranked_areas):
            ar = (area_stats["rejected"] / area_stats["total"] * 100) if area_stats["total"] else 0
            flag = " !!!" if ar > 50 else ""
            lines.append(f"  {area_name}: {area_stats['total']} dec, "
                          f"{area_stats['rejected']} rej ({ar:.0f}%){flag}")
    if s["top_rejection_categories"]:
        lines.append("\nTop rejection categories:")
        for reason, count in s["top_rejection_categories"]:
            lines.append(f"  - {count}x: {reason[:80]}")
    lines.append("\n--- Advice ---")
    if rej_rate > 50:
        lines.append("HIGH rejection rate. Always pre_check. Discuss with PE before implementing.")
    elif rej_rate > 30:
        lines.append("MODERATE rejection rate. Focus on areas flagged '!!!' above.")
    elif total >= 5 and rej_rate < 15:
        lines.append("STRONG track record.")
    else:
        lines.append("Keep logging decisions. More data = better patterns.")
    return "\n".join(lines)


@mcp.tool()
def team_dashboard(limit: int = 10) -> str:
    """
    Team-wide dashboard: agents' stats, patterns, health.

    Args:
        limit: Max agents shown, by decision volume (default 10)
    """
    with LOCK:
        G = _load_graph()
    decisions = sum(1 for _, d in G.nodes(data=True) if d.get("type") == "decision")
    fb_count = sum(1 for _, d in G.nodes(data=True) if d.get("type") == "feedback")
    code_refs = sum(1 for _, d in G.nodes(data=True) if d.get("type") == "code_ref")
    if decisions == 0:
        return "Brain is empty. No decisions logged yet."
    lines = ["=== TEAM DASHBOARD ===", "",
             f"Decisions: {decisions} | Feedback: {fb_count} | Code refs: {code_refs}",
             "", "--- Agents ---"]
    scorecards = _compute_scorecard(G)
    ranked = sorted(scorecards.items(), key=lambda kv: -kv[1]["total"])
    shown = ranked[:limit]
    for name, s in sorted(shown, key=lambda kv: kv[0]):
        total = s["total"]
        rate = (s["accepted"] / total * 100) if total else 0
        rej_rate = (s["rejected"] + s["failed"]) / total if total else 0
        level = ("strict" if total >= 3 and rej_rate >= 0.5
                 else "elevated" if total >= 3 and rej_rate >= 0.3 else "normal")
        flag = f" [{level.upper()}]" if level != "normal" else ""
        lines.append(f"  {name}: {total} dec, {s['accepted']} ok ({rate:.0f}%), "
                      f"{s['rejected']} rej, trend={s['trend']}{flag}")
    if len(ranked) > limit:
        lines.append(f"  ... and {len(ranked) - limit} more agent(s) "
                     f"(raise limit to see all)")
    clusters = _cluster_rejection_reasons(G)
    if clusters:
        lines.append("\n--- Patterns ---")
        for i, c in enumerate(clusters[:3], 1):
            agents_in = sorted(set(r["agent"] for r in c))
            lines.append(f"  #{i} ({len(c)}x, {', '.join(agents_in)}): {c[0]['reason'][:80]}")
    area_rej: dict[str, int] = defaultdict(int)
    for _, data in G.nodes(data=True):
        if data.get("type") == "decision" and data.get("outcome") in ("rejected", "failed"):
            area_rej[data.get("area", "?")] += 1
    if area_rej:
        lines.append("\n--- Hot Areas ---")
        for a, c in sorted(area_rej.items(), key=lambda x: -x[1])[:5]:
            lines.append(f"  {a}: {c} rejection(s)")
    return "\n".join(lines)


def brain_stats() -> str:
    """Get overall brain statistics. (CLI/internal only — see team_dashboard for MCP.)"""
    with LOCK:
        G = _load_graph()
    decisions = sum(1 for _, d in G.nodes(data=True) if d.get("type") == "decision")
    fb = sum(1 for _, d in G.nodes(data=True) if d.get("type") == "feedback")
    cr = sum(1 for _, d in G.nodes(data=True) if d.get("type") == "code_ref")
    areas = set(d.get("area") for _, d in G.nodes(data=True) if d.get("area"))
    repos = set(d.get("repo") for _, d in G.nodes(data=True) if d.get("repo"))
    agents = set(d.get("agent") for _, d in G.nodes(data=True) if d.get("agent"))
    return (f"Brain Stats:\n  Nodes: {G.number_of_nodes()} | Edges: {G.number_of_edges()}\n"
            f"  Decisions: {decisions} | Feedback: {fb} | Code refs: {cr}\n"
            f"  Areas: {', '.join(sorted(areas)) or 'none'}\n"
            f"  Repos: {', '.join(sorted(repos)) or 'none'}\n"
            f"  Agents: {', '.join(sorted(agents)) or 'none'}")


# ===========================================================================
# MCP Tools — Query
# ===========================================================================


@mcp.tool()
def query_decisions(area: str = "", agent: str = "", repo: str = "",
                    outcome: str = "", limit: int = 10,
                    query: str = "", sort: str = "") -> str:
    """
    Query past decisions, filtered by area/agent/repo/outcome
    (pending|accepted|rejected|failed|revised). limit defaults to 10.

    query: free-text — ranks matches by relevance over action+reasoning+area
        (not just recency). Use this to find decisions about a topic/symbol
        when you don't know the exact area, e.g. "AppStorage pending roadmap".
    sort: "relevance" (default when query is set) or "recency" (default
        otherwise). Relevance uses token + domain-term similarity; recency
        returns newest-first.
    """
    _write_query_marker()
    with LOCK:
        G = _load_graph()
    if not sort:
        sort = "relevance" if query else "recency"
    q_tokens = _tokenize(query) if query else set()

    rows = []
    for node_id, data in G.nodes(data=True):
        if data.get("type") != "decision":
            continue
        if area and data.get("area") != area:
            continue
        if agent and data.get("agent") != agent:
            continue
        if repo and data.get("repo") != repo:
            continue
        if outcome and data.get("outcome") != outcome:
            continue
        ts = data.get("timestamp", "")
        score = 0.0
        if q_tokens:
            haystack = _tokenize(
                f"{data.get('action','')} {data.get('reasoning','')} "
                f"{data.get('area','')}")
            score = _similarity_sets(q_tokens, haystack)
            if score == 0.0:
                continue  # text query given but no overlap — drop
        rows.append((score, ts, node_id, data))

    if not rows:
        _record_query("query_decisions", [], "", repo=repo, had_result=False)
        return "No matching decisions."

    if sort == "relevance" and q_tokens:
        rows.sort(key=lambda r: (r[0], r[1]), reverse=True)  # score, then recency
    else:
        rows.sort(key=lambda r: r[1], reverse=True)  # recency

    shown = rows[:limit]
    lines = []
    for score, ts, node_id, data in shown:
        prefix = f"({int(score*100)}%) " if (q_tokens and sort == "relevance") else ""
        lines.append(
            f"[{node_id}] {prefix}{data.get('agent','?')} @ {data.get('repo','?')} | "
            f"area={data.get('area','?')} | {str(data.get('action','?'))[:150]} "
            f"-> {data.get('outcome','pending')}")
    header = (f"{len(rows)} decision(s), top {len(shown)} by {sort}:"
              if len(rows) > len(shown) else f"{len(rows)} decision(s):")
    out = f"{header}\n\n" + "\n".join(lines)
    _record_query("query_decisions", [r[2] for r in shown], out, repo=repo)
    return out


@mcp.tool()
def get_decision(decision_id: str) -> str:
    """
    Get full details of a decision including feedback and code symbols.

    Args:
        decision_id: The decision ID
    """
    with LOCK:
        G = _load_graph()
    if decision_id not in G:
        return f"ERROR: '{decision_id}' not found"
    data = dict(G.nodes[decision_id])
    lines = [f"Decision: {decision_id}", ""]
    for k, v in sorted(data.items()):
        text = str(v)
        if len(text) > 600:  # legacy uncapped nodes can hold 30KB+ per field
            text = text[:600] + f"…[{len(text) - 600} more chars]"
        lines.append(f"  {k}: {text}")
    feedback = []
    for pred in G.predecessors(decision_id):
        edge = G.edges[pred, decision_id]
        if edge.get("relation") == "feedback_on":
            fb = G.nodes[pred]
            feedback.append((fb.get("timestamp", ""),
                             f"  [{fb.get('severity','info')}] {fb.get('agent','?')}: "
                             f"{str(fb.get('feedback',''))[:300]}"))
    if feedback:
        feedback.sort(key=lambda x: x[0])
        recent = feedback[-10:]
        lines.append("\nFeedback:")
        if len(feedback) > 10:
            lines.append(f"  [+{len(feedback) - 10} older feedback omitted]")
        lines.extend(line for _, line in recent)
    code_syms = []
    for _, succ in G.out_edges(decision_id):
        edge = G.edges[decision_id, succ]
        if edge.get("relation") == "touches":
            sym_data = G.nodes.get(succ, {})
            code_syms.append(sym_data.get("qualified_name", succ))
    if code_syms:
        lines.append("\nCode symbols:")
        for sym in code_syms[:30]:
            lines.append(f"  - {sym}")
        if len(code_syms) > 30:
            lines.append(f"  ... and {len(code_syms) - 30} more")
    return "\n".join(lines)


def _roadmap_rows(G: nx.DiGraph, repo: str = "") -> list[dict]:
    """Open work to resume: pending decisions + roadmap/blocker-tagged ones.

    Ranked so durable roadmap markers float above transient pending edits:
    roadmap/blocker areas first, then newest. Used by both get_roadmap (pull)
    and the SessionStart digest hook (push), so they never diverge.
    """
    rows = []
    for node_id, data in G.nodes(data=True):
        if data.get("type") != "decision":
            continue
        outcome = data.get("outcome", "pending")
        area = str(data.get("area", ""))
        is_roadmap = "roadmap" in area.lower() or "blocker" in area.lower()
        # Resume-worthy = still open, or explicitly tagged roadmap/blocker.
        if outcome not in ("pending", "revised") and not is_roadmap:
            continue
        if repo and data.get("repo") != repo:
            continue
        rows.append({
            "id": node_id,
            "repo": data.get("repo", "?"),
            "area": area or "?",
            "action": str(data.get("action", "?")),
            "outcome": outcome,
            "timestamp": data.get("timestamp", ""),
            "is_roadmap": is_roadmap,
        })
    # roadmap-tagged first, then most recent
    rows.sort(key=lambda r: (r["is_roadmap"], r["timestamp"]), reverse=True)
    return rows


def _format_roadmap_digest(rows: list[dict], limit: int = 15,
                           action_chars: int = 150) -> str:
    """Compact, token-budgeted digest of open work. Shared push/pull format."""
    if not rows:
        return "No open work. Brain has no pending or roadmap decisions."
    shown = rows[:limit]
    lines = []
    for r in shown:
        tag = "★ROADMAP " if r["is_roadmap"] else ""
        lines.append(
            f"[{r['id']}] {tag}{r['repo']} | {r['area']} | "
            f"{r['action'][:action_chars]} -> {r['outcome']}")
    header = f"OPEN WORK ({len(rows)} item(s)"
    if len(rows) > len(shown):
        header += f", top {len(shown)} shown — query_decisions for more"
    header += "):"
    return header + "\n" + "\n".join(lines)


def _area_matches(data_area: str, area: str) -> bool:
    """Exact-or-prefix area filter so area='x' includes x/roadmap."""
    if not area:
        return True
    data_area = data_area or ""
    return data_area == area or data_area.startswith(area.rstrip("/") + "/")


def _decision_timestamp(data: dict) -> str:
    return str(data.get("timestamp", ""))


def _format_git_summary(git: dict) -> str:
    if not isinstance(git, dict) or not git:
        return ""
    parts = []
    for key, label in (
        ("branch", "branch"),
        ("base_branch", "base"),
        ("commit_range", "range"),
        ("pr_number", "PR"),
    ):
        if git.get(key) not in ("", None):
            parts.append(f"{label}={git[key]}")
    if "working_tree_dirty" in git:
        parts.append(f"dirty={bool(git.get('working_tree_dirty'))}")
    return ", ".join(parts)


def _format_validation_entry(item: dict) -> str:
    cmd = str(item.get("command", "?"))
    status = str(item.get("status", "?"))
    exit_code = item.get("exit_code")
    suffix = f" (exit {exit_code})" if exit_code is not None else ""
    counts = []
    if item.get("passed") is not None:
        counts.append(f"passed={item.get('passed')}")
    if item.get("failed") is not None:
        counts.append(f"failed={item.get('failed')}")
    if counts:
        suffix += " [" + ", ".join(counts) + "]"
    return f"{cmd} -> {status}{suffix}"


def _matching_decisions(G: nx.DiGraph, repo: str, area: str = "") -> list[tuple[str, dict]]:
    rows = []
    for node_id, data in G.nodes(data=True):
        if data.get("type") != "decision":
            continue
        if repo and data.get("repo") != repo:
            continue
        if not _area_matches(str(data.get("area", "")), area):
            continue
        rows.append((node_id, dict(data)))
    rows.sort(key=lambda row: _decision_timestamp(row[1]), reverse=True)
    return rows


def _pending_completion_nudges(rows: list[tuple[str, dict]], stale_days: int = 30) -> list[str]:
    cutoff = datetime.now() - timedelta(days=stale_days)
    nudges = []
    for node_id, data in rows:
        if data.get("outcome", "pending") != "pending":
            continue
        action = str(data.get("action", ""))
        reasons = []
        if _looks_complete_but_pending(action):
            reasons.append("action looks complete")
        try:
            ts = datetime.fromisoformat(str(data.get("timestamp", "")))
            if ts <= cutoff:
                reasons.append(f"pending >{stale_days}d")
        except (TypeError, ValueError):
            pass
        if reasons:
            nudges.append(f"[{node_id}] {', '.join(reasons)}: consider log_outcome or resolve_stale_pending")
    return nudges


@mcp.tool()
def get_roadmap(repo: str = "", limit: int = 15) -> str:
    """
    What's left to do — pending decisions + roadmap/blocker-tagged work,
    ranked (roadmap markers first, then newest). One call to resume context
    after a fresh session or compaction without guessing query terms.
    Scope to one repo with `repo`. Tag durable work by putting "roadmap" or
    "blocker" in a decision's `area` (e.g. area="kmp-foundation/roadmap").
    """
    _write_query_marker()
    with LOCK:
        G = _load_graph()
    rows = _roadmap_rows(G, repo)
    out = _format_roadmap_digest(rows, limit=limit)
    _record_query("get_roadmap", [r.get("id") for r in rows[:limit] if r.get("id")],
                  out, repo=repo, had_result=bool(rows))
    return out


@mcp.tool()
def get_resume_context(repo: str, area: str = "", detail: str = "compact",
                       limit: int = 5) -> str:
    """
    Return compact cross-session context for a repo/area: latest handoff,
    open roadmap items, recent decisions, hygiene nudges, validation evidence,
    SAN visibility, and the next recommended action.
    """
    _write_query_marker()
    with LOCK:
        G = _load_graph()

    rows = _matching_decisions(G, repo, area)
    if not rows:
        out = f"RESUME CONTEXT: {repo}{(' | ' + area) if area else ''}\nNo matching decisions."
        _record_query("get_resume_context", [], out, repo=repo, had_result=False)
        return out

    shown_ids = [node_id for node_id, _ in rows[:limit]]
    lines = [f"RESUME CONTEXT: {repo}{(' | ' + area) if area else ''}", ""]

    handoffs = [(node_id, data) for node_id, data in rows if data.get("handoff_summary")]
    if handoffs:
        lines.append("LATEST HANDOFF")
        for node_id, data in handoffs[:max(1, min(limit, 3))]:
            lines.append(f"[{node_id}] {data.get('timestamp', '?')[:19]} {data.get('agent', '?')} | {data.get('area', '?')}")
            lines.append(f"  {data.get('handoff_summary')}")
            git_line = _format_git_summary(data.get("git", {}))
            if git_line:
                lines.append(f"  git: {git_line}")
            if data.get("next_action"):
                lines.append(f"  next: {data.get('next_action')}")
        lines.append("")

    roadmap = [r for r in _roadmap_rows(G, repo) if _area_matches(str(r.get("area", "")), area)]
    if roadmap:
        lines.append("OPEN WORK")
        for r in roadmap[:limit]:
            tag = "ROADMAP " if r.get("is_roadmap") else ""
            lines.append(f"[{r['id']}] {tag}{r['area']} | {r['action'][:150]} -> {r['outcome']}")
        lines.append("")

    blockers = []
    do_not_touch = []
    deferred = []
    validation_lines = []
    next_actions = []
    for node_id, data in rows:
        if data.get("outcome", "pending") in ("pending", "revised"):
            for blocker in data.get("blockers", []) or []:
                blockers.append(f"[{node_id}] {blocker}")
        for item in data.get("do_not_touch", []) or []:
            do_not_touch.append(f"[{node_id}] {item}")
        for item in data.get("deferred_work", []) or []:
            deferred.append(f"[{node_id}] {item}")
        for item in data.get("validation", []) or []:
            if isinstance(item, dict):
                validation_lines.append(f"[{node_id}] {_format_validation_entry(item)}")
        if data.get("next_action"):
            next_actions.append(f"[{node_id}] {data.get('next_action')}")

    if blockers:
        lines.append("BLOCKERS")
        lines.extend(f"- {x}" for x in blockers[:limit])
        lines.append("")
    if deferred and detail != "compact":
        lines.append("DEFERRED")
        lines.extend(f"- {x}" for x in deferred[:limit])
        lines.append("")
    if do_not_touch:
        lines.append("DO NOT TOUCH")
        lines.extend(f"- {x}" for x in do_not_touch[:limit])
        lines.append("")
    if validation_lines:
        lines.append("VALIDATION")
        lines.extend(f"- {x}" for x in validation_lines[:limit])
        lines.append("")

    nudges = _pending_completion_nudges(rows)
    if nudges:
        lines.append("HYGIENE NUDGES")
        lines.extend(f"- {x}" for x in nudges[:limit])
        lines.append("")

    lines.append("RECENT DECISIONS")
    for node_id, data in rows[:limit]:
        lines.append(
            f"[{node_id}] {data.get('timestamp', '?')[:10]} {data.get('agent', '?')} | "
            f"{data.get('area', '?')} | {str(data.get('action', '?'))[:150]} "
            f"-> {data.get('outcome', 'pending')}")
    lines.append("")

    san_line = _san_coverage_line(repo)
    lines.append("SAN")
    lines.append(san_line if san_line else f"No SAN coverage visible for repo '{repo}'.")
    lines.append("")

    if next_actions:
        lines.append("NEXT")
        lines.append(next_actions[0])

    out = "\n".join(lines).rstrip()
    _record_query("get_resume_context", shown_ids, out, repo=repo, had_result=True)
    return out


# ===========================================================================
# Records lifecycle — human-readable export, prune, archive
# ===========================================================================
# The decision graph grows forever otherwise. These give the user a readable
# audit trail (records/YYYY-MM-DD.md) and a safe way to forget: prune archives
# to decisions.archive.jsonl (recoverable) AND removes from the live graph so
# pre_check / get_roadmap stop surfacing pruned work. Dry-run by default.

RECORDS_DIR = BRAIN_DIR / "records"
ARCHIVE_FILE = BRAIN_DIR / "decisions.archive.jsonl"


def _decision_one_liner(node_id: str, data: dict) -> str:
    """Compact single-line record of a decision for the markdown export."""
    ts = str(data.get("timestamp", ""))[:19].replace("T", " ")
    outcome = data.get("outcome", "pending")
    area = data.get("area", "?")
    repo = data.get("repo", "?")
    agent = data.get("agent", "?")
    action = str(data.get("action", "?")).replace("\n", " ")[:200]
    return (f"- `{node_id}` **{outcome}** · {ts} · {repo} / {area} · {agent}\n"
            f"  {action}")


def _export_records(repo: str = "") -> tuple[int, int]:
    """Write records/YYYY-MM-DD.md, one file per day, newest decisions last.
    Returns (days_written, decisions_written). Regenerates from the graph —
    deleting a day file just re-renders on next export, so the graph stays the
    source of truth; to actually forget, prune (which removes from the graph)."""
    with LOCK:
        G = _load_graph()
    by_day: dict[str, list[tuple[str, str, dict]]] = defaultdict(list)
    for node_id, data in G.nodes(data=True):
        if data.get("type") != "decision":
            continue
        if repo and data.get("repo") != repo:
            continue
        ts = str(data.get("timestamp", ""))
        day = ts[:10] if len(ts) >= 10 else "undated"
        by_day[day].append((ts, node_id, data))
    RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    total = 0
    for day, rows in by_day.items():
        rows.sort(key=lambda r: r[0])
        lines = [f"# Decisions — {day}", ""]
        if repo:
            lines.append(f"_repo: {repo}_\n")
        for _, node_id, data in rows:
            lines.append(_decision_one_liner(node_id, data))
            total += 1
        (RECORDS_DIR / f"{day}.md").write_text("\n".join(lines) + "\n")
    # Index file listing all days.
    if by_day:
        idx = ["# Decision records", "",
               "One file per day. To forget a decision permanently, prune it",
               "(archives + removes from the brain) — deleting a file here only",
               "re-renders on the next export.", ""]
        for day in sorted(by_day, reverse=True):
            idx.append(f"- [{day}]({day}.md) — {len(by_day[day])} decision(s)")
        (RECORDS_DIR / "INDEX.md").write_text("\n".join(idx) + "\n")
    return (len(by_day), total)


def _archive_nodes(nodes: list[tuple[str, dict]]) -> None:
    """Append pruned decision nodes to decisions.archive.jsonl (recoverable)."""
    if not nodes:
        return
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    with ARCHIVE_FILE.open("a") as f:
        for node_id, data in nodes:
            rec = dict(data)
            rec["id"] = node_id
            rec["archived_at"] = datetime.now().isoformat()
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def _prune_candidates(G: nx.DiGraph, repo: str, before_days: int,
                      keep_rejections: bool) -> list[tuple[str, dict]]:
    """Decisions eligible to prune: older than before_days, resolved (or stale
    pending), in repo. Rejections/roadmap kept by default — they carry the
    learning. Returns [(node_id, data)]."""
    cutoff = datetime.now() - timedelta(days=before_days)
    out = []
    for node_id, data in G.nodes(data=True):
        if data.get("type") != "decision":
            continue
        if repo and data.get("repo") != repo:
            continue
        ts = data.get("timestamp", "")
        try:
            if datetime.fromisoformat(ts) > cutoff:
                continue  # too recent
        except (ValueError, TypeError):
            continue  # undated -> never auto-prune
        outcome = data.get("outcome", "pending")
        area = str(data.get("area", "")).lower()
        # Keep the learning-valuable: rejections/failures and roadmap markers.
        if keep_rejections and outcome in ("rejected", "failed"):
            continue
        if "roadmap" in area or "blocker" in area:
            continue
        out.append((node_id, data))
    return out


@mcp.tool()
def export_records(repo: str = "") -> str:
    """
    Write a human-readable audit trail to ~/.agent-brain/records/ —
    one markdown file per day (YYYY-MM-DD.md) plus an INDEX.md, each
    decision shown with its date, repo, area, agent, action, and outcome.
    Scope to one repo with `repo`. Regenerated from the graph each call.
    Use this to review what the brain remembers before pruning.
    """
    days, total = _export_records(repo)
    scope = f" for '{repo}'" if repo else ""
    return (f"Exported {total} decision(s) across {days} day(s){scope} to "
            f"{RECORDS_DIR}\nOpen {RECORDS_DIR / 'INDEX.md'} to browse by date.")


@mcp.tool()
def prune_decisions(repo: str = "", before_days: int = 90,
                    keep_rejections: bool = True, dry_run: bool = True) -> str:
    """
    Forget old, resolved decisions to keep the brain lean and relevant.
    Archives to ~/.agent-brain/decisions.archive.jsonl (recoverable) AND
    removes from the live graph so pre_check/get_roadmap stop surfacing them.

    DRY-RUN BY DEFAULT: shows what would be pruned without changing anything;
    pass dry_run=False to apply. Keeps learning-valuable decisions: rejections/
    failures (when keep_rejections=True) and roadmap/blocker-tagged work are
    never pruned. Only decisions older than before_days (default 90) qualify.

    repo: scope to one repo (empty = all). before_days: age cutoff in days.
    """
    with LOCK:
        G = _load_graph()
        candidates = _prune_candidates(G, repo, before_days, keep_rejections)
        if not candidates:
            return (f"Nothing to prune: no resolved decisions older than "
                    f"{before_days} days" + (f" in '{repo}'" if repo else "") + ".")
        if dry_run:
            from collections import Counter
            by_outcome = Counter(d.get("outcome", "pending") for _, d in candidates)
            preview = "\n".join(
                f"  {_decision_one_liner(nid, d).splitlines()[0]}"
                for nid, d in sorted(candidates, key=lambda c: c[1].get("timestamp", ""))[:15])
            more = f"\n  … and {len(candidates) - 15} more" if len(candidates) > 15 else ""
            return (f"DRY RUN — would prune {len(candidates)} decision(s) "
                    f"(by outcome: {dict(by_outcome)}).\n"
                    f"Kept: rejections/failures + roadmap/blocker.\n\n"
                    f"{preview}{more}\n\n"
                    f"Re-run with dry_run=False to archive + remove them.")
        # Apply: archive, then remove from graph.
        _archive_nodes(candidates)
        for node_id, _ in candidates:
            if node_id in G:
                G.remove_node(node_id)
        _save_graph(G)
    # Refresh the readable export so it reflects the prune.
    _export_records(repo)
    return (f"Pruned {len(candidates)} decision(s) — archived to {ARCHIVE_FILE} "
            f"and removed from the brain. Records re-exported to {RECORDS_DIR}.")


@mcp.tool()
def resolve_stale_pending(before_days: int = 30, repo: str = "",
                          dry_run: bool = True) -> str:
    """
    Mark long-abandoned 'pending' decisions as 'superseded' so they stop
    polluting get_roadmap. Pending decisions older than before_days (default
    30) with no outcome are assumed done/abandoned. DRY-RUN BY DEFAULT.
    Does NOT delete — only changes outcome (still archivable later via prune).
    """
    cutoff = datetime.now() - timedelta(days=before_days)
    with LOCK:
        G = _load_graph()
        stale = []
        for node_id, data in G.nodes(data=True):
            if data.get("type") != "decision" or data.get("outcome") != "pending":
                continue
            if repo and data.get("repo") != repo:
                continue
            try:
                if datetime.fromisoformat(data.get("timestamp", "")) <= cutoff:
                    stale.append(node_id)
            except (ValueError, TypeError):
                continue
        if not stale:
            return f"No pending decisions older than {before_days} days."
        if dry_run:
            return (f"DRY RUN — would mark {len(stale)} stale pending decision(s) "
                    f"as 'superseded'. Re-run with dry_run=False to apply.")
        for node_id in stale:
            G.nodes[node_id]["outcome"] = "superseded"
            G.nodes[node_id]["outcome_reason"] = (
                f"auto-superseded: pending >{before_days}d with no outcome")
            G.nodes[node_id]["outcome_timestamp"] = datetime.now().isoformat()
        _save_graph(G)
    return f"Marked {len(stale)} stale pending decision(s) as 'superseded'."


# ===========================================================================
# MCP Tools — Office Dashboard
# ===========================================================================


@mcp.tool()
def heartbeat(agent: str, status: str, task: str = "", talking_to: str = "", message: str = "", repo: str = "") -> str:
    """
    Report status to the office dashboard. Call at task START and END.
    status: working | idle | planning | discussing | reviewing | blocked | waiting.
    Optional: task label, talking_to agent, message (chat), repo scope.
    """
    with OFFICE_LOCK:
        state = _load_office_state()
        role = _resolve_role(agent, repo)
        entry = {
            "role": role, "status": status,
            "task": (task or "")[:100],
            "talking_to": talking_to or None,
            "message": (message or None)[:200] if message else None,
            "last_seen": datetime.now().isoformat(),
        }
        if repo:
            entry["repo"] = repo
        state.setdefault("agents", {})[agent] = entry
        if message and talking_to:
            msg_entry = {
                "from": agent, "to": talking_to,
                "text": (message or "")[:200],
                "ts": datetime.now().isoformat(),
            }
            if repo:
                msg_entry["repo"] = repo
            state.setdefault("messages", []).append(msg_entry)
            state["messages"] = state["messages"][-50:]
        _save_office_state(state)
    return "ok"


def office_state(repo: str = "") -> str:
    """
    Get current office state (CLI/dashboard only — not exposed via MCP).

    Args:
        repo: Optional repo filter. Shows only agents scoped to this repo.
              Agents are included if their last heartbeat carried this repo,
              or if they belong to the resolved team for this repo
              (configured via teams_per_repo / per-entry 'repos').
              Omit to see the full unfiltered office.
    """
    with OFFICE_LOCK:
        state = _load_office_state()
    agents_dict = state.get("agents", {}) or {}
    if not agents_dict:
        return "Office is empty. No agents have checked in."

    if repo:
        scoped_names = {
            t.get("name", "").lower()
            for t in _get_team_for_repo(repo)
            if isinstance(t, dict) and t.get("name")
        }
        filtered = {
            name: info for name, info in agents_dict.items()
            if info.get("repo") == repo or name.lower() in scoped_names
        }
        if not filtered:
            return f"No agents currently scoped to repo '{repo}'."
        agents_dict = filtered

    header = f"=== OFFICE STATE [{repo}] ===\n" if repo else "=== OFFICE STATE ===\n"
    lines = [header]
    for name, info in sorted(agents_dict.items()):
        status = info.get("status", "unknown")
        task = info.get("task", "")
        talking = info.get("talking_to")
        last = info.get("last_seen", "?")[:19]
        repo_tag = f" @ {info['repo']}" if info.get("repo") and not repo else ""
        lines.append(f"  {name} [{info.get('role','')}]{repo_tag}: {status} (seen: {last})")
        if task:
            lines.append(f"    task: {task}")
        if talking:
            lines.append(f"    talking to: {talking}")

    messages = state.get("messages", []) or []
    if repo:
        messages = [m for m in messages if m.get("repo") == repo]
    if messages:
        lines.append(f"\n  {len(messages)} message(s) in log")
    return "\n".join(lines)


@mcp.tool()
def detect_stalls(stall_minutes: int = 5, limit: int = 10) -> str:
    """
    Find agents with open (pending) decisions but no heartbeat within
    stall_minutes (default 5). PM report. limit caps detail rows.
    """
    with OFFICE_LOCK:
        state = _load_office_state()
    agents_info = state.get("agents", {})
    now = datetime.now()

    # Find all open (pending) decisions grouped by agent
    with LOCK:
        G = _load_graph()
    open_by_agent: dict[str, list] = defaultdict(list)
    for node_id, data in G.nodes(data=True):
        if data.get("type") == "decision" and data.get("outcome") == "pending":
            open_by_agent[data.get("agent", "unknown")].append({
                "id": node_id,
                "area": data.get("area", ""),
                "action": data.get("action", "")[:80],
                "timestamp": data.get("timestamp", ""),
            })

    if not open_by_agent:
        return "No open decisions. Nothing to check."

    stalled = []
    active = []
    no_heartbeat = []

    for agent, decisions in open_by_agent.items():
        agent_state = agents_info.get(agent)
        if not agent_state:
            # Agent has open decisions but never sent a heartbeat
            no_heartbeat.append((agent, decisions))
            continue

        last_seen_str = agent_state.get("last_seen", "")
        status = agent_state.get("status", "unknown")

        # If agent reported blocked, that's not a stall — it's a known blocker
        if status == "blocked":
            active.append((agent, status, "blocked — not a stall"))
            continue

        try:
            last_seen = datetime.fromisoformat(last_seen_str)
            idle_minutes = (now - last_seen).total_seconds() / 60.0
        except (ValueError, TypeError):
            no_heartbeat.append((agent, decisions))
            continue

        if idle_minutes >= stall_minutes:
            stalled.append({
                "agent": agent,
                "idle_minutes": round(idle_minutes, 1),
                "last_status": status,
                "open_decisions": decisions,
            })
        else:
            active.append((agent, status, f"seen {round(idle_minutes, 1)}m ago"))

    # Build report
    lines = [f"=== STALL DETECTION (threshold: {stall_minutes}m) ===\n"]

    if stalled:
        stalled.sort(key=lambda s: -s["idle_minutes"])
        lines.append(f"STALLED ({len(stalled)} agent(s)):\n")
        for s in stalled[:limit]:
            lines.append(f"  {s['agent']} — idle {s['idle_minutes']}m, last status: {s['last_status']}")
            for d in s["open_decisions"]:
                lines.append(f"    [{d['id']}] {d['area']}: {d['action']}")
            lines.append("")
        if len(stalled) > limit:
            lines.append(f"  ... and {len(stalled) - limit} more stalled agent(s)\n")
        lines.append("ACTION: Nudge these agents to continue or log_outcome if done.\n")
    else:
        lines.append("No stalled agents.\n")

    if no_heartbeat:
        lines.append(f"NO HEARTBEAT ({len(no_heartbeat)} agent(s) with open decisions but no activity):\n")
        for agent, decisions in no_heartbeat[:limit]:
            lines.append(f"  {agent} — never checked in, {len(decisions)} open decision(s)")
        if len(no_heartbeat) > limit:
            lines.append(f"  ... and {len(no_heartbeat) - limit} more")
        lines.append("")

    if active:
        names = ", ".join(f"{a}({s})" for a, s, _ in active[:15])
        extra = f" +{len(active) - 15} more" if len(active) > 15 else ""
        lines.append(f"ACTIVE ({len(active)}): {names}{extra}")

    return "\n".join(lines)


# ===========================================================================
# MCP Tools — SAN (Structured Associative Notation)
# ===========================================================================


# ---------------------------------------------------------------------------
# SAN refresh: hash-based staleness detection + orphan cleanup (no parser)
# SAN content is generated by brain-compiler (LLM), not by this server.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Token savings tracking — how much SAN saves vs reading raw source.
# Counted conservatively: only get_san reads (raw source tokens minus SAN
# tokens actually served). query_san is not counted. ~4 chars per token.
# ---------------------------------------------------------------------------

SAVINGS_FILE = BRAIN_DIR / "san_savings.jsonl"
_SESSION_ID = f"ses_{uuid.uuid4().hex[:8]}"
_SESSION_SAVINGS = {"reads": 0, "raw_tokens": 0, "san_tokens": 0}


def _tokens(chars: int) -> int:
    """Estimate tokens from a character count (~4 chars/token for code)."""
    return max(0, round(chars / 4))


_TOKEN_ENCODER = None
_TOKEN_ENCODER_TRIED = False


def _tokens_text(text: str) -> int:
    """Count tokens with tiktoken (o200k_base) when installed; else chars/4.

    chars/4 measured ~1.4 points optimistic vs the real tokenizer on code —
    install tiktoken in the brain venv to make token_savings exact.
    """
    global _TOKEN_ENCODER, _TOKEN_ENCODER_TRIED
    if not _TOKEN_ENCODER_TRIED:
        _TOKEN_ENCODER_TRIED = True
        try:
            import tiktoken
            _TOKEN_ENCODER = tiktoken.get_encoding("o200k_base")
        except Exception:
            _TOKEN_ENCODER = None
    if _TOKEN_ENCODER is not None:
        try:
            return len(_TOKEN_ENCODER.encode(text))
        except Exception:
            pass
    return _tokens(len(text))


def _record_san_saving(repo: str, file_rel: str, raw_text: str, san_text: str) -> None:
    """Record one get_san read that replaced a raw source read. Never raises."""
    try:
        raw_t, san_t = _tokens_text(raw_text), _tokens_text(san_text)
        if raw_t <= san_t:
            return  # no saving (tiny source or oversized SAN) — don't inflate stats
        _SESSION_SAVINGS["reads"] += 1
        _SESSION_SAVINGS["raw_tokens"] += raw_t
        _SESSION_SAVINGS["san_tokens"] += san_t
        BRAIN_DIR.mkdir(parents=True, exist_ok=True)
        with SAVINGS_FILE.open("a") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(),
                "session": _SESSION_ID,
                "repo": repo,
                "file": file_rel,
                "raw_tokens": raw_t,
                "san_tokens": san_t,
            }) + "\n")
    except Exception:
        pass  # savings tracking must never break get_san


def _record_san_gen(repo: str, file_rel: str, source_path, san_path) -> None:
    """Record the COST of (re)generating one SAN file, so net-token math is
    honest. The brain-compiler (LLM) reads the raw source (~input tokens) and
    writes the SAN (~output tokens). This is charged ONCE per (re)generation,
    detected when a fresh SAN is first hash-registered. Never raises.

    Honest note: this is a proxy — real LLM billing weights output higher and
    includes prompt overhead. It's a floor on the true generation cost, so the
    net-tokens number it produces is OPTIMISTIC, not inflated."""
    try:
        src = source_path.read_text(errors="replace")
        san = san_path.read_text(errors="replace")
        in_t, out_t = _tokens_text(src), _tokens_text(san)
        _log_metric({
            "kind": "san_gen",
            "repo": repo,
            "file": file_rel,
            "input_tokens": in_t,    # raw source the compiler read
            "output_tokens": out_t,  # SAN it wrote
            "gen_cost": in_t + out_t,
        })
    except Exception:
        pass  # cost tracking must never break the freshness sweep


def _load_savings_events() -> list[dict]:
    """Read all persisted savings events. Returns [] on any problem."""
    if not SAVINGS_FILE.exists():
        return []
    events = []
    try:
        for line in SAVINGS_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return events


def _format_savings_report(session: dict, events: list[dict], today_str: str) -> str:
    """Build the savings report from session counters + persisted events."""
    def block(label: str, reads: int, raw: int, san: int) -> list[str]:
        saved = raw - san
        pct = (saved / raw * 100) if raw else 0.0
        return [f"{label}:",
                f"  SAN reads: {reads}",
                f"  Raw source cost avoided: {raw:,} tokens",
                f"  SAN tokens served: {san:,} tokens",
                f"  SAVED: {saved:,} tokens ({pct:.0f}%)"]

    lines = ["=== SAN TOKEN SAVINGS ===", ""]
    lines += block("This session", session["reads"],
                   session["raw_tokens"], session["san_tokens"])

    today = [e for e in events if str(e.get("ts", "")).startswith(today_str)]
    t_raw = sum(e.get("raw_tokens", 0) for e in today)
    t_san = sum(e.get("san_tokens", 0) for e in today)
    lines += [""] + block(f"Today ({today_str})", len(today), t_raw, t_san)

    a_raw = sum(e.get("raw_tokens", 0) for e in events)
    a_san = sum(e.get("san_tokens", 0) for e in events)
    lines += [""] + block("All time", len(events), a_raw, a_san)

    if session["reads"] == 0 and not events:
        lines += ["", "No SAN reads recorded yet. Savings are counted when",
                  "agents call get_san instead of reading raw source files."]
    lines += ["", "Note: conservative estimate — counts get_san reads only",
              "(query_san and decision-memory benefits not included). ~4 chars/token."]
    return "\n".join(lines)


_SAN_SKIP_DIRS = (
    "build", "bin", "out", "dist", ".gradle", "node_modules", "Pods",
    ".output", ".wxt", "dist-unpacked", ".wrangler",
)

# One extension list for ALL SAN code paths (index build, freshness, orphan
# cleanup). Divergent lists previously made non-JVM repos report wrong
# stale/missing counts and risked deleting valid SAN files as "orphans".
# The single source of truth for "what counts as a source file" across SAN
# generation, freshness sweeps, orphan cleanup, and decisions_for path-detection.
# The brain-compiler is language-agnostic, so this is the full set it handles —
# keep it complete: a missing ext silently breaks stale-detection AND can delete
# valid SANs for that language as orphans.
SOURCE_EXTS = (".kt", ".java", ".py", ".ts", ".tsx", ".js", ".jsx",
               ".swift", ".go", ".rs", ".rb", ".c", ".cpp", ".h", ".cs",
               ".php", ".scala", ".m", ".mm")

# Freshness sweeps stat every indexed file + rglob the .san tree — too heavy
# to repeat per get_san/query_san call. Debounce per repo.
_SAN_FRESH_TTL_S = 60
_SAN_FRESH_CHECKED: dict[str, float] = {}


def _is_skipped_source(rel: str) -> bool:
    """True for build outputs / vendored dirs that should never get SAN files."""
    parts = rel.replace("\\", "/").split("/")
    return any(p in _SAN_SKIP_DIRS for p in parts[:-1])


def _resolve_repo_path(repo: str) -> Optional[Path]:
    """Resolve repo name to path from config."""
    repo_paths = _get_repo_paths()
    repo_path = repo_paths.get(repo)
    if not repo_path:
        for name, path in repo_paths.items():
            if repo.lower() in name.lower():
                repo_path = path
                break
    return repo_path


def _resolve_absolute_path(abs_path: str) -> tuple[Optional[str], Optional[Path]]:
    """Inverse of _resolve_repo_path: given an absolute source path, find which
    configured repo contains it. Returns (repo_name, repo_root) or (None, None).

    Longest-root match so a nested repo wins over its parent; realpath-normalized
    so symlinks don't cause a valid path to silently miss.
    """
    try:
        target = os.path.realpath(abs_path)
    except (OSError, ValueError):
        return (None, None)
    best: tuple[Optional[str], Optional[Path]] = (None, None)
    best_len = -1
    for name, root in _get_repo_paths().items():
        try:
            root_real = os.path.realpath(str(root))
        except (OSError, ValueError):
            continue
        if target == root_real or target.startswith(root_real + os.sep):
            if len(root_real) > best_len:
                best_len = len(root_real)
                best = (name, root)
    return best


def _load_san_hashes(san_dir: Path) -> dict:
    """Load source content hashes from .san_hashes.json."""
    hash_file = san_dir / ".san_hashes.json"
    if hash_file.exists():
        try:
            return json.loads(hash_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_san_hashes(san_dir: Path, hashes: dict) -> None:
    """Persist source content hashes to .san_hashes.json (atomic write)."""
    hash_file = san_dir / ".san_hashes.json"
    atomic_write_bytes(hash_file, json.dumps(hashes, indent=2).encode("utf-8"))


def _hash_source(source_path: Path) -> str:
    """Return sha256 hex digest of a source file's content."""
    return hashlib.sha256(source_path.read_bytes()).hexdigest()


def _refresh_san(repo_path: Path, san_dir: Path, stale_files: list[str],
                  orphan_sans: list[Path], hashes: Optional[dict] = None) -> dict:
    """
    Refresh SAN metadata: delete orphans, track staleness via hashes.
    Does NOT generate SAN content — that's the brain-compiler's job.
    Returns stats dict.
    """
    stats = {"deleted": 0, "errors": 0, "hash_skipped": 0,
             "orphans_removed": 0, "stale_detected": 0}

    if hashes is None:
        hashes = _load_san_hashes(san_dir)
    hashes_changed = False

    # Delete orphans (from caller's mtime-based detection)
    for orphan in orphan_sans:
        try:
            orphan.unlink()
            stats["deleted"] += 1
        except OSError:
            stats["errors"] += 1

    # Cleanup: remove SANs for sources tracked in hashes but now deleted
    for tracked_source in list(hashes.keys()):
        if not (repo_path / tracked_source).exists():
            san_path = _source_to_san_path(san_dir, tracked_source)
            try:
                if san_path.exists():
                    san_path.unlink()
                    stats["orphans_removed"] += 1
            except OSError:
                stats["errors"] += 1
            del hashes[tracked_source]
            hashes_changed = True

    # Check stale files: hash to distinguish real changes from mtime false positives
    for source_rel in stale_files:
        source_path = repo_path / source_rel
        if not source_path.exists():
            continue
        if source_path.suffix not in SOURCE_EXTS:
            continue

        try:
            current_hash = _hash_source(source_path)
        except OSError:
            stats["errors"] += 1
            continue

        stored_hash = hashes.get(source_rel)
        if stored_hash == current_hash:
            # Content identical — mtime changed but source didn't
            stats["hash_skipped"] += 1
            # Touch SAN to reset mtime so it's not flagged stale again
            san_path = _source_to_san_path(san_dir, source_rel)
            if san_path.exists():
                san_path.touch()
        else:
            # Source genuinely changed — SAN is stale
            # Do NOT update hash here — hash should only update when SAN
            # is actually regenerated (via brain-compiler + recompile_san).
            # Updating now would hide staleness on subsequent checks.
            stats["stale_detected"] += 1

    # Persist hashes if anything changed
    if hashes_changed:
        _save_san_hashes(san_dir, hashes)

    return stats


def _ensure_san_fresh(repo: str, force: bool = False) -> Optional[str]:
    """
    Check SAN freshness: detect stale/orphaned SANs, clean up orphans.
    Does NOT generate SAN content — only reports staleness and cleans up.
    Called internally by query_san and get_san before serving results.
    Debounced per repo (_SAN_FRESH_TTL_S) — the sweep stats every indexed
    file, far too heavy to repeat on burst reads. force=True skips the TTL.
    """
    now = time.monotonic()
    if not force:
        last = _SAN_FRESH_CHECKED.get(repo)
        if last is not None and (now - last) < _SAN_FRESH_TTL_S:
            return None
    _SAN_FRESH_CHECKED[repo] = now

    repo_path = _resolve_repo_path(repo)
    if not repo_path:
        return None

    san_dir = repo_path / ".san"
    if not san_dir.exists():
        san_dir.mkdir(parents=True, exist_ok=True)

    index = _load_san_index(san_dir)

    stale = []
    missing_san = []
    orphans = []

    if index:
        # Check indexed files (deduplicate — multiple index entries can point to same file)
        seen_source_rels = set()
        for qualified_name, meta in index.items():
            source_rel = meta.get("file", "")
            if not source_rel or source_rel in seen_source_rels:
                continue
            seen_source_rels.add(source_rel)
            source_path = repo_path / source_rel
            san_path = _source_to_san_path(san_dir, source_rel)

            if not source_path.exists():
                if san_path.exists():
                    orphans.append(san_path)
            elif not san_path.exists():
                missing_san.append(source_rel)
            else:
                if source_path.stat().st_mtime > san_path.stat().st_mtime:
                    stale.append(source_rel)

        # Find .san files with no index entry and no source
        try:
            for san_file in san_dir.rglob("*.san"):
                rel = str(san_file.relative_to(san_dir))
                source_rel = str(Path(rel).with_suffix(""))
                for ext in SOURCE_EXTS:
                    candidate = repo_path / (source_rel + ext) if not source_rel.endswith(ext) else repo_path / source_rel
                    if candidate.exists():
                        break
                else:
                    if san_file not in orphans:
                        source_with_ext = repo_path / source_rel
                        if not source_with_ext.exists():
                            orphans.append(san_file)
        except Exception:
            pass
    else:
        # No index — scan source tree to detect stale SANs
        for ext in ("**/*" + e for e in SOURCE_EXTS):
            for source_path in repo_path.glob(ext):
                rel = str(source_path.relative_to(repo_path))
                if _is_skipped_source(rel):
                    continue
                san_path = _source_to_san_path(san_dir, rel)
                if not san_path.exists():
                    missing_san.append(rel)
                elif source_path.stat().st_mtime > san_path.stat().st_mtime:
                    stale.append(rel)

    # Check for hash-tracked orphans (deleted sources) even if mtime found nothing
    hashes = _load_san_hashes(san_dir)
    has_hash_orphans = any(
        not (repo_path / src).exists() for src in hashes
    )

    # Register hashes for fresh SANs that aren't tracked yet.
    # Without this, hash-based false-positive detection won't work
    # for SANs generated by brain-compiler until recompile_san is called.
    # A source appearing here for the FIRST time (not in hashes, fresh SAN on
    # disk) means the brain-compiler just (re)generated it — so we can charge
    # its generation cost: input ~= raw source tokens read, output ~= SAN tokens.
    hashes_backfilled = False
    if index:
        seen_files = set()
        for qualified_name, meta in index.items():
            source_rel = meta.get("file", "")
            if not source_rel or source_rel in hashes:
                continue
            source_path = repo_path / source_rel
            san_path = _source_to_san_path(san_dir, source_rel)
            if source_path.exists() and san_path.exists():
                if san_path.stat().st_mtime >= source_path.stat().st_mtime:
                    try:
                        hashes[source_rel] = _hash_source(source_path)
                        hashes_backfilled = True
                        if source_rel not in seen_files:
                            seen_files.add(source_rel)
                            _record_san_gen(repo, source_rel, source_path, san_path)
                    except OSError:
                        pass
    if hashes_backfilled:
        _save_san_hashes(san_dir, hashes)

    if not stale and not missing_san and not orphans and not has_hash_orphans:
        return None

    stats = _refresh_san(repo_path, san_dir, stale, orphans, hashes=hashes)

    # Rebuild index after orphan removal
    if stats["deleted"] > 0 or stats.get("orphans_removed", 0) > 0:
        _rebuild_san_index(san_dir, repo_path=repo_path)

    msg = []
    if stats["deleted"]:
        msg.append(f"deleted {stats['deleted']} orphan(s)")
    if stats.get("orphans_removed"):
        msg.append(f"removed {stats['orphans_removed']} orphan(s) via hash tracker")
    if stats.get("stale_detected"):
        msg.append(f"{stats['stale_detected']} SAN(s) stale — run brain-compiler to regenerate")
    if missing_san:
        msg.append(f"{len(missing_san)} source(s) have no SAN — run brain-compiler to generate")
    if stats.get("hash_skipped"):
        msg.append(f"skipped {stats['hash_skipped']} unchanged (hash match)")
    if stats["errors"]:
        msg.append(f"{stats['errors']} error(s)")
    return ", ".join(msg) if msg else None


def _build_san_index(
    san_dir: Path,
    repo_path: Optional[Path] = None,
    *,
    strict: bool = False,
) -> dict:
    """Scan .san files and return the in-memory _index.json mapping.

    Pure Python, no MCP tool call, and no filesystem writes. With
    ``strict=False`` (default) an individual unreadable ``.san`` file is
    skipped so one corrupt file cannot blank the whole index; with
    ``strict=True`` any read/scan error propagates so a publication
    transaction can detect it.
    """
    index: dict = {}
    for san_file in san_dir.rglob("*.san"):
        rel = str(san_file.relative_to(san_dir))
        try:
            content = san_file.read_text()
        except OSError:
            if strict:
                raise
            continue

        # Canonical name form is <source>.san, so stripping .san usually
        # leaves the real source path. Legacy replace-form names need the
        # extension resolved from disk; never fabricate one.
        source_rel = rel[:-4] if rel.endswith(".san") else rel
        if not source_rel.endswith(SOURCE_EXTS) and repo_path:
            for ext in SOURCE_EXTS:
                if (repo_path / (source_rel + ext)).exists():
                    source_rel = source_rel + ext
                    break

        # Extract qualified names from SAN content
        for match in re.finditer(r'^(\S+)\s+@(\w+)\s*\{', content, re.MULTILINE):
            qname = match.group(1)
            kind = match.group(2)
            index[qname] = {
                "kind": kind,
                "file": source_rel,
                "tokens_san": len(content.split()),
            }
    return index


def _rebuild_san_index(
    san_dir: Path,
    repo_path: Optional[Path] = None,
    *,
    strict: bool = False,
):
    """Rebuild _index.json from .san files.

    ``strict=False`` (default) preserves the historical best-effort behavior:
    scan and write failures are swallowed so callers on the hot path never
    raise. ``strict=True`` propagates any read/scan/write failure so a
    publication transaction can detect and roll back a corrupt index.
    """
    if strict:
        index = _build_san_index(san_dir, repo_path, strict=True)
        atomic_write_bytes(
            san_dir / "_index.json",
            json.dumps(index, indent=2).encode("utf-8"),
        )
        return

    try:
        index = _build_san_index(san_dir, repo_path)
    except Exception:
        index = {}

    try:
        atomic_write_bytes(
            san_dir / "_index.json",
            json.dumps(index, indent=2).encode("utf-8"),
        )
    except OSError:
        pass


def _get_san_dir(repo: str) -> Optional[Path]:
    """Get the .san/ directory for a repo."""
    repo_paths = _get_repo_paths()
    repo_path = repo_paths.get(repo)
    if not repo_path:
        for name, path in repo_paths.items():
            if repo.lower() in name.lower():
                repo_path = path
                break
    if not repo_path:
        return None
    san_dir = repo_path / ".san"
    return san_dir if san_dir.exists() else None


def _load_san_index(san_dir: Path) -> dict:
    """Load _index.json from a .san directory."""
    idx_file = san_dir / "_index.json"
    if idx_file.exists():
        try:
            return json.loads(idx_file.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _san_coverage_line(repo: str) -> str:
    """One-line SAN coverage nudge for pre_check. Empty string if no coverage.

    Counts UNIQUE source files (the index maps many symbols per file), so the
    number means 'files you can read via SAN', not raw entry count.
    """
    try:
        san_dir = _get_san_dir(repo)
        if not san_dir:
            return ""
        index = _load_san_index(san_dir)
        files = {e.get("file") for e in index.values()
                 if isinstance(e, dict) and e.get("file")}
        n = len(files)
        if n <= 0:
            return ""
        return (f"SAN AVAILABLE: repo '{repo}' has {n} files compiled. "
                f"Read code with get_san (detail='sig' for what exists, 'full' "
                f"for impl) instead of raw Read — same structure, far fewer tokens.")
    except Exception:
        return ""  # coverage hint must never break pre_check


def _source_to_san_path(san_dir: Path, source_rel: str) -> Path:
    """
    Convert a source file relative path to its .san counterpart.
    Canonical convention (per san/brain-compiler.md): APPEND .san —
    src/.../Auth.kt → src/.../Auth.kt.san. Legacy files used
    extension-replacement (Auth.san); prefer whichever exists on disk,
    defaulting to the canonical append form for new files.
    """
    appended = san_dir / (source_rel + ".san")
    if appended.exists():
        return appended
    legacy = san_dir / Path(source_rel).with_suffix(".san")
    if legacy.exists():
        return legacy
    return appended


def _scan_san_freshness(repo: str) -> dict[str, object]:
    """Classify SAN freshness for a repo using filesystem READS ONLY.

    Never mkdir/write/touch/unlink/log and never calls any mutating helper
    (_ensure_san_fresh, _refresh_san, _save_san_hashes, _record_san_gen,
    _rebuild_san_index). Returns the stable freshness schema.
    """
    repo_path = _resolve_repo_path(repo)
    if not repo_path:
        return {"status": "repo_not_found", "repo": repo}

    san_dir = repo_path / ".san"

    missing: list[dict] = []
    stale: list[dict] = []
    fresh: list[dict] = []
    orphaned: list[dict] = []
    unsupported: list[dict] = []
    malformed: list[dict] = []

    hashes = _load_san_hashes(san_dir) if san_dir.exists() else {}
    seen_sources: set[str] = set()

    # 1. Walk supported source files.
    for ext in ("**/*" + e for e in SOURCE_EXTS):
        for source_path in repo_path.glob(ext):
            if not source_path.is_file():
                continue
            rel = str(source_path.relative_to(repo_path))
            if rel.startswith(".san" + os.sep) or _is_skipped_source(rel):
                continue
            if rel in seen_sources:
                continue
            seen_sources.add(rel)

            try:
                digest = _hash_source(source_path)
            except OSError:
                continue
            san_path = _source_to_san_path(san_dir, rel) if san_dir.exists() else None

            if san_path is None or not san_path.exists():
                missing.append({"source_path": rel, "source_sha256": digest})
                continue

            # Structural validity takes precedence over fresh/stale. Read with
            # errors="replace" so a non-UTF8 SAN or a binary-tainted source
            # never raises UnicodeDecodeError out of this read-only path — a
            # non-UTF8 SAN then fails the strict grammar (→ malformed) and the
            # source is classified from its already-hashed bytes.
            try:
                san_text = san_path.read_text(errors="replace")
                source_line_count = (
                    source_path.read_text(errors="replace").count("\n") + 1
                )
            except OSError:
                malformed.append({
                    "source_path": rel,
                    "san_path": _rel_san_path(repo_path, san_path),
                    "errors": [],
                })
                continue
            validation = validate_san_candidate(san_text, source_line_count)
            if not validation["valid"]:
                malformed.append({
                    "source_path": rel,
                    "san_path": _rel_san_path(repo_path, san_path),
                    "errors": validation["errors"],
                })
                continue

            stored = hashes.get(rel)
            if stored is not None:
                if stored == digest:
                    fresh.append({"source_path": rel, "source_sha256": digest})
                else:
                    stale.append({"source_path": rel, "source_sha256": digest})
            else:
                # Compatibility path: no stored digest → mtime comparison only,
                # with NO backfill/touch.
                try:
                    src_newer = source_path.stat().st_mtime > san_path.stat().st_mtime
                except OSError:
                    src_newer = True
                if src_newer:
                    stale.append({"source_path": rel, "source_sha256": digest})
                else:
                    fresh.append({"source_path": rel, "source_sha256": digest})

    # 2. Orphaned SAN files: a .san whose source no longer exists.
    if san_dir.exists():
        for san_file in sorted(san_dir.rglob("*.san")):
            rel = str(san_file.relative_to(san_dir))
            source_rel_append = rel[:-4] if rel.endswith(".san") else rel
            source_rel_legacy = str(Path(rel).with_suffix(""))
            has_source = False
            for source_rel in {source_rel_append, source_rel_legacy}:
                if (repo_path / source_rel).exists():
                    has_source = True
                    break
                if not source_rel.endswith(SOURCE_EXTS):
                    for e in SOURCE_EXTS:
                        if (repo_path / (source_rel + e)).exists():
                            has_source = True
                            break
                if has_source:
                    break
            if not has_source:
                orphaned.append({"san_path": _rel_san_path(repo_path, san_file)})

    # 3. Hash-tracked sources whose extension is unsupported by SOURCE_EXTS.
    #    Only files explicitly tracked in hash metadata are surfaced — never a
    #    blanket sweep of every non-code repository file.
    for tracked_rel in sorted(hashes.keys()):
        if tracked_rel in seen_sources:
            continue
        if tracked_rel.endswith(SOURCE_EXTS):
            continue
        if (repo_path / tracked_rel).exists():
            unsupported.append({
                "source_path": tracked_rel,
                "reason": "unsupported_extension",
            })

    counts = {
        "missing": len(missing),
        "stale": len(stale),
        "fresh": len(fresh),
        "orphaned": len(orphaned),
        "unsupported": len(unsupported),
        "malformed": len(malformed),
    }
    return {
        "status": "ok",
        "repo": repo,
        "counts": counts,
        "missing": missing,
        "stale": stale,
        "fresh": fresh,
        "orphaned": orphaned,
        "unsupported": unsupported,
        "malformed": malformed,
    }


def _rel_san_path(repo_path: Path, san_file: Path) -> str:
    """Return a `.san/...`-relative display path for a SAN file."""
    try:
        return str(san_file.relative_to(repo_path))
    except ValueError:
        return str(san_file)


@mcp.tool()
def plan_san_refresh(repo: str) -> dict[str, object]:
    """Report SAN freshness WITHOUT mutating anything (filesystem reads only).

    Returns a structured plan: which sources are missing/stale/fresh, which
    SAN files are orphaned, which tracked sources are unsupported, and which
    existing SAN files are structurally malformed. Performs zero filesystem or
    metrics mutation — never creates `.san/`, deletes orphans, or writes
    hashes. Use before invoking the brain-compiler to see what needs work.
    """
    return _scan_san_freshness(repo)


def _format_san_freshness(plan: dict[str, object]) -> str:
    """Render a freshness plan dict as a human-readable report."""
    if plan.get("status") == "repo_not_found":
        return f"ERROR: repo '{plan.get('repo')}' not found in config"

    repo = plan.get("repo")
    counts = plan.get("counts", {})
    lines = [
        f"SAN freshness for '{repo}':",
        f"  Fresh: {counts.get('fresh', 0)}",
        f"  Stale: {counts.get('stale', 0)}",
        f"  Missing: {counts.get('missing', 0)}",
        f"  Orphaned: {counts.get('orphaned', 0)}",
        f"  Unsupported: {counts.get('unsupported', 0)}",
        f"  Malformed: {counts.get('malformed', 0)}",
    ]

    def _emit(title: str, items: list, key: str) -> None:
        if not items:
            return
        lines.append(f"\n{title}:")
        for item in items[:10]:
            lines.append(f"  - {item.get(key)}")
        if len(items) > 10:
            lines.append(f"  ... and {len(items) - 10} more")

    _emit("Stale — run brain-compiler to regenerate", plan.get("stale", []), "source_path")
    _emit("Missing — run brain-compiler to generate", plan.get("missing", []), "source_path")
    _emit("Malformed — regenerate", plan.get("malformed", []), "source_path")
    _emit("Orphaned SAN (source gone)", plan.get("orphaned", []), "san_path")
    return "\n".join(lines)


def check_san_freshness(repo: str) -> str:
    """
    Report SAN freshness (read-only). Exposed via recompile_san(dry_run=True).

    This performs NO mutation — it never cleans orphans, creates `.san/`, or
    updates hashes. The mutating housekeeping lives in recompile_san's
    non-dry-run path.
    """
    return _format_san_freshness(_scan_san_freshness(repo))


# One repo-wide lock: the hash file and index file are shared across all
# sources in a repo, so concurrent publications must serialize their
# shared-file writes. Source-digest compare-and-swap + process-unique temp
# files remain the cross-process guard.
SAN_PUBLISH_LOCK = threading.RLock()

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _publish_failure(status: str, *, retryable: bool, **extra) -> dict:
    return {"status": status, "retryable": retryable, **extra}


def _validate_publish_source_path(repo_path: Path, source_path: str):
    """Return (rel, abs_path) or a failure dict for a publication source path."""
    if not source_path or source_path.startswith("/") or os.path.isabs(source_path):
        return _publish_failure("invalid_source_path", retryable=False)
    parts = Path(source_path).parts
    if ".." in parts:
        return _publish_failure("invalid_source_path", retryable=False)

    candidate = repo_path / source_path
    # Symlink-escape guard: the realpath must remain inside the repo root.
    try:
        real = Path(os.path.realpath(candidate))
        root_real = Path(os.path.realpath(repo_path))
    except (OSError, ValueError):
        return _publish_failure("invalid_source_path", retryable=False)
    if real != root_real and root_real not in real.parents:
        return _publish_failure("invalid_source_path", retryable=False)

    return source_path, candidate


def _publish_san(
    repo: str,
    source_path: str,
    expected_source_sha256: str,
    san_content: str,
    provider: str,
    model: str,
    reasoning_effort,
) -> dict:
    # --- validation (no mutation) -------------------------------------------
    repo_path = _resolve_repo_path(repo)
    if not repo_path:
        return _publish_failure("repo_not_found", retryable=False, repo=repo)
    repo_path = Path(repo_path)

    path_result = _validate_publish_source_path(repo_path, source_path)
    if isinstance(path_result, dict):
        return path_result
    rel, abs_source = path_result

    if _is_skipped_source(rel):
        return _publish_failure("skipped_source", retryable=False,
                                source_path=rel)
    if not rel.endswith(SOURCE_EXTS):
        return _publish_failure("unsupported_extension", retryable=False,
                                source_path=rel)
    if not abs_source.is_file():
        return _publish_failure("source_not_found", retryable=True,
                                source_path=rel)

    if not isinstance(expected_source_sha256, str) or not _SHA256_RE.match(
        expected_source_sha256
    ):
        return _publish_failure("invalid_digest", retryable=False)

    try:
        config = parse_san_compiler_config(_load_config())
    except CompilerConfigError as error:
        return _publish_failure("compiler_config_invalid", retryable=False,
                                detail=str(error))

    if provider not in ("claude", "codex"):
        return _publish_failure("provider_mismatch", retryable=False,
                                provider=provider)
    if provider == "claude":
        expected_model = config.claude.model
        if model != expected_model:
            return _publish_failure("model_mismatch", retryable=False)
        if reasoning_effort not in (None, ""):
            return _publish_failure("reasoning_effort_mismatch", retryable=False)
    else:  # codex
        expected_model = config.codex.model
        if model != expected_model:
            return _publish_failure("model_mismatch", retryable=False)
        if reasoning_effort != config.codex.reasoning_effort:
            return _publish_failure("reasoning_effort_mismatch", retryable=False)

    # Source digest compare (pre-validation read).
    try:
        current_digest = _hash_source(abs_source)
    except OSError:
        return _publish_failure("source_not_found", retryable=True,
                                source_path=rel)
    if current_digest != expected_source_sha256:
        return _publish_failure("source_changed", retryable=True,
                                source_path=rel)

    # Structural candidate validation.
    try:
        source_line_count = abs_source.read_text(errors="replace").count("\n") + 1
    except OSError:
        return _publish_failure("source_not_found", retryable=True,
                                source_path=rel)
    validation = validate_san_candidate(san_content, source_line_count)
    validation_summary = {
        "valid": validation["valid"],
        "block_count": validation["block_count"],
        "bytes": validation["byte_count"],
        "errors": validation["errors"],
    }
    if not validation["valid"]:
        return _publish_failure("invalid_candidate", retryable=False,
                                source_path=rel, validation=validation_summary)

    # --- transaction (serialized; snapshot → replace → rollback on failure) --
    with SAN_PUBLISH_LOCK:
        san_dir = repo_path / ".san"
        dest = san_dir / (rel + ".san")          # canonical append form only
        hash_file = san_dir / ".san_hashes.json"
        index_file = san_dir / "_index.json"

        def _snap(path: Path):
            data = snapshot_file(path)
            mtime = path.stat().st_mtime_ns if data is not None else None
            return data, mtime

        def _restore(path: Path, snap) -> None:
            data, mtime = snap
            restore_file(path, data)
            if data is not None and mtime is not None:
                try:
                    st = path.stat()
                    os.utime(path, ns=(st.st_atime_ns, mtime))
                except OSError:
                    pass

        dest_snap = _snap(dest)
        hash_snap = _snap(hash_file)
        index_snap = _snap(index_file)
        metrics_snap = _snap(METRICS_FILE)

        # Directories that do not yet exist and would be newly created — remove
        # them on a first-publication rollback (deepest first).
        created_dirs = []
        probe = dest.parent
        while not probe.exists():
            created_dirs.append(probe)
            probe = probe.parent

        def _rollback() -> bool:
            try:
                _restore(dest, dest_snap)
                _restore(hash_file, hash_snap)
                _restore(index_file, index_snap)
                _restore(METRICS_FILE, metrics_snap)
                for directory in created_dirs:  # deepest → shallowest
                    try:
                        if directory.exists() and not any(directory.iterdir()):
                            directory.rmdir()
                    except OSError:
                        pass
                return True
            except Exception:
                return False

        try:
            # Re-hash immediately before replacement (compare-and-swap).
            recheck = _hash_source(abs_source)
            if recheck != expected_source_sha256:
                return _publish_failure("source_changed", retryable=True,
                                        source_path=rel)

            # 1. Replace the SAN atomically.
            atomic_write_bytes(dest, san_content.encode("utf-8"))

            # 2. Persist copied hashes with the expected digest.
            hashes = _load_san_hashes(san_dir)
            hashes = dict(hashes)
            hashes[rel] = expected_source_sha256
            atomic_write_bytes(
                hash_file, json.dumps(hashes, indent=2).encode("utf-8")
            )

            # 3. Rebuild index strictly (propagates any failure).
            _rebuild_san_index(san_dir, repo_path=repo_path, strict=True)

            # 4. Append the publication metric strictly.
            source_tokens = _tokens_text(abs_source.read_text(errors="replace"))
            san_tokens = _tokens_text(san_content)
            _append_metric_strict({
                "kind": "san_publish",
                "repo": repo,
                "file": rel,
                "provider": provider,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "input_tokens": source_tokens,
                "output_tokens": san_tokens,
                "gen_cost": source_tokens + san_tokens,
            })
        except Exception:
            if _rollback():
                return _publish_failure("publication_failed", retryable=True,
                                        source_path=rel,
                                        validation=validation_summary)
            return _publish_failure("rollback_failed", retryable=False,
                                    source_path=rel)

        # 5. Invalidate the freshness cache only after a committed publication.
        _SAN_FRESH_CHECKED.pop(repo, None)

        return {
            "status": "published",
            "repo": repo,
            "source_path": rel,
            "san_path": f".san/{rel}.san",
            "source_sha256": expected_source_sha256,
            "provider": provider,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "validation": validation_summary,
        }


@mcp.tool()
def publish_san(
    repo: str,
    source_path: str,
    expected_source_sha256: str,
    san_content: str,
    provider: str,
    model: str,
    reasoning_effort: str | None = None,
) -> dict[str, object]:
    """Atomically publish one validated SAN file, bound to a source digest.

    The brain-compiler (an LLM host) calls this after generating SAN content.
    The Agent Brain server never selects or invokes a model — it validates the
    DECLARED provider/model/effort against the configured compiler settings,
    verifies the source has not changed since generation (SHA-256 compare-and-
    swap), structurally validates the candidate, then atomically replaces the
    SAN, updates the source-hash record and index, and appends a cost metric.
    Any post-replacement failure rolls back SAN, hashes, index, and metrics to
    their prior state. Results and metrics carry only validation summaries —
    never source or SAN content.
    """
    return _publish_san(
        repo,
        source_path,
        expected_source_sha256,
        san_content,
        provider,
        model,
        reasoning_effort,
    )


@mcp.tool()
def recompile_san(repo: str, dry_run: bool = False) -> str:
    """
    Refresh SAN metadata: rebuild index, clean orphans, update hashes.
    Does NOT generate content (use brain-compiler). dry_run=True = report only.
    """
    if dry_run:
        return check_san_freshness(repo)
    repo_path = _resolve_repo_path(repo)
    if not repo_path:
        return f"ERROR: repo '{repo}' not found in config"

    san_dir = repo_path / ".san"
    if not san_dir.exists():
        san_dir.mkdir(parents=True, exist_ok=True)

    hashes = _load_san_hashes(san_dir)
    hashes_changed = False
    errors = 0
    orphans_removed = 0
    stale_count = 0
    missing_count = 0
    hash_updated = 0

    # 1. Clean up orphans: SANs whose source no longer exists
    for tracked_source in list(hashes.keys()):
        if not (repo_path / tracked_source).exists():
            san_path = _source_to_san_path(san_dir, tracked_source)
            try:
                if san_path.exists():
                    san_path.unlink()
                    orphans_removed += 1
            except OSError:
                errors += 1
            del hashes[tracked_source]
            hashes_changed = True

    # Also check .san files not in hash tracker
    try:
        for san_file in list(san_dir.rglob("*.san")):
            rel = str(san_file.relative_to(san_dir))
            source_rel = str(Path(rel).with_suffix(""))
            source_exists = False
            for ext in SOURCE_EXTS:
                candidate = repo_path / (source_rel + ext) if not source_rel.endswith(ext) else repo_path / source_rel
                if candidate.exists():
                    source_exists = True
                    break
            if not source_exists:
                source_with_ext = repo_path / source_rel
                if not source_with_ext.exists():
                    try:
                        san_file.unlink()
                        orphans_removed += 1
                    except OSError:
                        errors += 1
    except Exception:
        errors += 1

    # 2. Check hashes for all source files with existing SANs
    #    Only update hash if SAN mtime >= source mtime (SAN is fresh).
    #    If SAN is stale, do NOT update hash — it would hide staleness.
    for ext in ("**/*" + e for e in SOURCE_EXTS):
        for source_path in repo_path.glob(ext):
            rel = str(source_path.relative_to(repo_path))
            if _is_skipped_source(rel):
                continue
            san_path = _source_to_san_path(san_dir, rel)
            if not san_path.exists():
                missing_count += 1
                continue
            try:
                current_hash = _hash_source(source_path)
                stored_hash = hashes.get(rel)
                if stored_hash != current_hash:
                    # Check if SAN is actually fresh (regenerated after source change)
                    san_is_fresh = san_path.stat().st_mtime >= source_path.stat().st_mtime
                    if san_is_fresh:
                        # SAN was (re)generated by the compiler — safe to update
                        # hash, and charge its generation cost for net-token math.
                        hashes[rel] = current_hash
                        hashes_changed = True
                        hash_updated += 1
                        _record_san_gen(repo, rel, source_path, san_path)
                    else:
                        # SAN is stale — don't update hash, report it
                        stale_count += 1
            except OSError:
                errors += 1

    if hashes_changed:
        _save_san_hashes(san_dir, hashes)

    # 3. Rebuild index from existing .san files
    _rebuild_san_index(san_dir, repo_path=repo_path)

    lines = [f"SAN refresh for '{repo}':"]
    lines.append(f"  Index rebuilt")
    if hash_updated:
        lines.append(f"  Hashes updated: {hash_updated}")
    if orphans_removed:
        lines.append(f"  Orphans removed: {orphans_removed}")
    if stale_count:
        lines.append(f"  Stale SANs detected: {stale_count} — run brain-compiler to regenerate")
    if missing_count:
        lines.append(f"  Sources without SAN: {missing_count} — run brain-compiler to generate")
    if errors:
        lines.append(f"  Errors: {errors}")
    return "\n".join(lines)


@mcp.tool()
def query_san(repo: str, keyword: str, max_results: int = 10) -> str:
    """
    Find code via SAN instead of grepping raw source — same hits, far fewer
    tokens. Use BEFORE Grep/Glob over a configured repo. Searches both the
    index and .san file contents by keyword (function/class name, pattern).

    Args:
        repo: Repository name from config.json
        keyword: Search term (function name, class name, pattern, etc.)
        max_results: Maximum results to return (default 10)
    """
    # Check SAN freshness before searching
    _ensure_san_fresh(repo)

    san_dir = _get_san_dir(repo)
    if not san_dir:
        # _ensure_san_fresh should have created it, but try resolving manually
        repo_path = _resolve_repo_path(repo)
        if repo_path:
            san_dir = repo_path / ".san"
            if not san_dir.exists():
                return f"No .san/ directory for '{repo}'."
        else:
            return f"No .san/ directory for '{repo}'."

    index = _load_san_index(san_dir)
    keyword_lower = keyword.lower()
    results = []

    # Phase 1: Search index (qualified names + metadata)
    for qname, meta in index.items():
        if keyword_lower in qname.lower():
            results.append({
                "qualified_name": qname,
                "kind": meta.get("kind", "?"),
                "file": meta.get("file", "?"),
                "match_type": "index",
                "preview": "",
            })
            if len(results) >= max_results:
                break
    # Read previews only for results that will be shown
    for r in results[:max_results]:
        san_path = _source_to_san_path(san_dir, r["file"])
        if san_path.exists():
            try:
                r["preview"] = san_path.read_text()[:500]
            except OSError:
                r["preview"] = "(read error)"

    # Phase 2: Search .san file contents (if not enough index hits)
    if len(results) < max_results:
        # Compare in .san-path units; r["file"] is the SOURCE path
        seen_files = {str(_source_to_san_path(san_dir, r["file"]).relative_to(san_dir))
                      for r in results}
        try:
            for san_file in san_dir.rglob("*.san"):
                rel = str(san_file.relative_to(san_dir))
                if rel in seen_files:
                    continue
                try:
                    content = san_file.read_text()
                except OSError:
                    continue
                content_lower = content.lower()
                if keyword_lower in content_lower:
                    # Find the matching line for context (single lowercase pass)
                    context_lines = []
                    for line, line_lower in zip(content.split("\n"), content_lower.split("\n")):
                        if keyword_lower in line_lower:
                            context_lines.append(line.strip())
                    results.append({
                        "qualified_name": rel.replace(".san", ""),
                        "kind": "?",
                        "file": rel,
                        "match_type": "content",
                        "preview": "\n".join(context_lines[:5]),
                    })
                if len(results) >= max_results:
                    break
        except Exception:
            pass

    if not results:
        return f"No SAN matches for '{keyword}' in '{repo}'."

    lines = [f"{len(results)} SAN match(es) for '{keyword}':\n"]
    for r in results[:max_results]:
        lines.append(f"[{r['kind']}] {r['qualified_name']} ({r['match_type']} match)")
        lines.append(f"  file: {r['file']}")
        if r["preview"]:
            # Truncate preview
            preview = r["preview"][:300]
            lines.append(f"  preview: {preview}")
        lines.append("")
    return "\n".join(lines)


_SAN_HEADER_RE = re.compile(r"^\S+\s+@\w+\s*\{")


def _san_signatures(content: str) -> str:
    """Reduce a SAN brief to its public API surface.

    Keeps block headers, src ranges, deps, and public fn signatures
    (impl detail in [...] stripped). Drops private members (- prefix),
    @state/@errors/purpose/impl prose. Measured ~2x smaller than full
    SAN, ~11x smaller than raw source (972 files).
    """
    out = []
    for line in content.splitlines():
        s = line.strip()
        if _SAN_HEADER_RE.match(line) or s == "}":
            out.append(line)
        elif s.startswith(("src:", "deps:")):
            out.append(line)
        elif s.startswith("fn:"):
            # Strip impl-step blocks (" [validate → ...]") but never type
            # annotations ("list[User]") — impl blocks always have a space
            # before the bracket, types never do.
            out.append(re.sub(r"\s+\[.*\]\s*$", "", line))
    return "\n".join(out)


@mcp.tool()
def get_san(repo: str, file_path: str, max_chars: int = 4000,
            detail: str = "full") -> str:
    """
    READ existing code via SAN instead of raw Read — same structure (signatures,
    deps, error handling, constraints), far fewer tokens: ~5x cheaper than raw
    for the full brief, ~11x cheaper with detail="sig". Use this BEFORE Read for
    any code file you are EXPLORING. Raw Read stays correct for files you're
    about to EDIT (need exact bytes), non-code files, or when no .san exists.

    Args:
        repo: Repository name from config.json. Optional when file_path is
            absolute — the repo is auto-detected from the path (and this arg is
            then ignored).
        file_path: Source file path — either repo-relative (e.g. "src/.../Auth.kt")
            OR an absolute path (e.g. the one you got from grep/glob); when
            absolute, repo + relative are resolved for you.
        max_chars: Truncate content above this size (default 4000; raise to read more)
        detail: "full" (default) for the complete SAN brief, or "sig" for the
            public API surface only — block headers, src ranges, deps, and
            public function signatures with impl detail stripped (~2x cheaper
            than full, ~11x cheaper than raw; use for "what exists here?")
    """
    # Absolute path: auto-detect repo + relative remainder (cuts call-site
    # friction — the agent passes the same path it got from grep/glob).
    if os.path.isabs(file_path):
        resolved_name, resolved_root = _resolve_absolute_path(file_path)
        if not resolved_name:
            return (f"'{file_path}' is not under any repo in config.json. "
                    f"Add the repo, or pass repo + a relative path.")
        repo = resolved_name
        try:
            file_path = os.path.relpath(os.path.realpath(file_path),
                                        os.path.realpath(str(resolved_root)))
        except (OSError, ValueError):
            return f"Could not resolve '{file_path}' within repo '{repo}'."
        # Abs path == repo root -> relpath returns "." which is not a file.
        if file_path == "." or file_path.startswith(".."):
            return (f"'{repo}' resolves to the repo root, not a source file. "
                    f"Pass a path to a specific code file.")

    # Check SAN freshness before reading
    _ensure_san_fresh(repo)

    san_dir = _get_san_dir(repo)
    if not san_dir:
        repo_path = _resolve_repo_path(repo)
        if repo_path:
            san_dir = repo_path / ".san"
        if not san_dir or not san_dir.exists():
            return f"No .san/ directory for '{repo}'."

    san_path = _source_to_san_path(san_dir, file_path)
    if not san_path.exists():
        # Try fuzzy match — maybe they gave partial path
        matches = []
        try:
            for f in san_dir.rglob("*.san"):
                rel = str(f.relative_to(san_dir))
                if file_path.replace("/", "") in rel.replace("/", "").replace(".san", ""):
                    matches.append(f)
        except Exception:
            pass
        if len(matches) == 1:
            san_path = matches[0]
        elif matches:
            lines = [f"Multiple SAN matches for '{file_path}':"]
            for m in matches[:10]:
                lines.append(f"  - {m.relative_to(san_dir)}")
            return "\n".join(lines)
        else:
            return f"No SAN file for '{file_path}'. Run brain-compiler on this file."

    try:
        content = san_path.read_text()
    except OSError as e:
        return f"ERROR reading SAN file: {e}"

    if detail == "sig":
        content = _san_signatures(content)

    # Cap output to keep token cost bounded
    total_chars = len(content)
    if total_chars > max_chars:
        content = (content[:max_chars]
                   + f"\n[truncated {max_chars} of {total_chars} chars — "
                     f"raise max_chars or use query_san]")

    # Include freshness info
    repo_path = _resolve_repo_path(repo)
    freshness = ""
    if repo_path:
        source = repo_path / file_path
        if not source.exists():
            # Fuzzy-matched SAN — derive source path from the .san file location
            derived = str(san_path.relative_to(san_dir))
            derived = derived[:-4] if derived.endswith(".san") else derived
            source = repo_path / derived
        if source.exists():
            src_mtime = source.stat().st_mtime
            san_mtime = san_path.stat().st_mtime
            if src_mtime > san_mtime:
                freshness = "\n⚠ STALE: source is newer than SAN. Re-run brain-compiler."
            else:
                freshness = "\n✓ Fresh"
            # Track tokens saved vs reading the raw source
            try:
                _record_san_saving(repo, str(source.relative_to(repo_path)),
                                   source.read_text(errors="replace"), content)
            except Exception:
                pass

    rel = san_path.relative_to(san_dir)
    return f"SAN: {rel}{freshness}\n{'=' * 40}\n{content}"


@mcp.tool()
def token_savings() -> str:
    """
    Report tokens saved by SAN this session, today, and all-time —
    raw-source cost avoided vs SAN tokens served, with percentages.
    """
    return _format_savings_report(_SESSION_SAVINGS, _load_savings_events(),
                                  datetime.now().strftime("%Y-%m-%d"))


def update_san_index(repo: str) -> str:
    """
    Rebuild _index.json by scanning all .san files in the repo's .san/ directory.
    (CLI/internal only — recompile_san covers this for MCP callers.)
    Extracts qualified_name, kind, and file mapping from each .san file.

    Args:
        repo: Repository name from config.json
    """
    repo_paths = _get_repo_paths()
    repo_path = repo_paths.get(repo)
    if not repo_path:
        for name, path in repo_paths.items():
            if repo.lower() in name.lower():
                repo_path = path
                break
    if not repo_path:
        return f"ERROR: repo '{repo}' not found in config"

    san_dir = repo_path / ".san"
    if not san_dir.exists():
        return f"No .san/ directory in '{repo}'."

    index: dict[str, dict] = {}
    errors = 0

    try:
        for san_file in san_dir.rglob("*.san"):
            if san_file.name.startswith("_"):
                continue
            rel = str(san_file.relative_to(san_dir))
            source_rel = str(Path(rel).with_suffix(""))  # Remove .san
            # Try to detect suffix from source tree
            for ext in SOURCE_EXTS:
                candidate = repo_path / (source_rel + ext)
                if candidate.exists():
                    source_rel = source_rel + ext
                    break

            try:
                content = san_file.read_text()
            except OSError:
                errors += 1
                continue

            # Parse qualified_name and kind from SAN format:
            # QualifiedName @kind {
            entries_in_file = []
            for line in content.split("\n"):
                line = line.strip()
                match = re.match(r"^(\S+)\s+@(\w+)\s*\{", line)
                if match:
                    qname = match.group(1)
                    kind = match.group(2)
                    entries_in_file.append((qname, kind))

            if entries_in_file:
                for qname, kind in entries_in_file:
                    index[qname] = {
                        "kind": kind,
                        "file": source_rel,
                        "san_file": rel,
                        "tokens_san": len(content.split()),
                    }
            else:
                # No parseable SAN header — index by filename
                name_key = Path(rel).stem
                index[name_key] = {
                    "kind": "unknown",
                    "file": source_rel,
                    "san_file": rel,
                    "tokens_san": len(content.split()),
                }
    except Exception as e:
        return f"ERROR scanning .san/: {e}"

    # Atomic write
    idx_file = san_dir / "_index.json"
    try:
        atomic_write_bytes(idx_file, json.dumps(index, indent=2).encode("utf-8"))
    except OSError as e:
        return f"ERROR writing index: {e}"

    total_tokens = sum(v.get("tokens_san", 0) for v in index.values())
    return (f"SAN index rebuilt for '{repo}':\n"
            f"  Entries: {len(index)}\n"
            f"  Total SAN tokens: {total_tokens}\n"
            f"  Errors: {errors}\n"
            f"  Index: {idx_file}")


def validate_san_system() -> str:
    """
    Run self-tests on the SAN hash/orphan/staleness system.
    (CLI only: `server.py validate-san` — not exposed via MCP.)
    Creates a temporary repo, simulates scenarios, and verifies each one.
    Safe to run anytime — uses an isolated temp directory, touches nothing real.
    """
    results = []
    passed = 0
    failed = 0
    tmp_root = None

    def _test(name: str, condition: bool, detail: str = ""):
        nonlocal passed, failed
        if condition:
            passed += 1
            results.append(f"  PASS: {name}")
        else:
            failed += 1
            results.append(f"  FAIL: {name}" + (f" — {detail}" if detail else ""))

    try:
        tmp_root = Path(tempfile.mkdtemp(prefix="san_validate_"))
        repo_path = tmp_root / "test-repo"
        repo_path.mkdir()
        san_dir = repo_path / ".san"
        san_dir.mkdir()
        src_dir = repo_path / "src"
        src_dir.mkdir(parents=True)

        # --- Test 1: _hash_source produces consistent sha256 ---
        test_file = src_dir / "A.kt"
        test_file.write_text("class A { fun hello() {} }")
        h1 = _hash_source(test_file)
        h2 = _hash_source(test_file)
        _test("hash consistency", h1 == h2, f"{h1} != {h2}")
        _test("hash is sha256 hex (64 chars)", len(h1) == 64 and all(c in "0123456789abcdef" for c in h1))

        # --- Test 2: hash changes when content changes ---
        test_file.write_text("class A { fun hello() {} fun bye() {} }")
        h3 = _hash_source(test_file)
        _test("hash changes on content change", h1 != h3)

        # --- Test 3: _load/_save_san_hashes round-trip ---
        test_hashes = {"src/A.kt": h1, "src/B.kt": "abc123"}
        _save_san_hashes(san_dir, test_hashes)
        loaded = _load_san_hashes(san_dir)
        _test("hash save/load round-trip", loaded == test_hashes, f"got {loaded}")

        # --- Test 4: _load_san_hashes handles missing file ---
        empty_san = tmp_root / "empty_san"
        empty_san.mkdir()
        _test("load hashes from empty dir", _load_san_hashes(empty_san) == {})

        # --- Test 5: _load_san_hashes handles corrupt file ---
        corrupt_dir = tmp_root / "corrupt_san"
        corrupt_dir.mkdir()
        (corrupt_dir / ".san_hashes.json").write_text("not json{{{")
        _test("load hashes from corrupt file", _load_san_hashes(corrupt_dir) == {})

        # --- Test 6: _source_to_san_path correct mapping ---
        sp = _source_to_san_path(san_dir, "src/A.kt")
        _test("source_to_san_path canonical append form",
              sp == san_dir / "src" / "A.kt.san")
        legacy_dir = san_dir / "src"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_f = legacy_dir / "L.san"
        legacy_f.write_text("legacy")
        _test("source_to_san_path honors existing legacy file",
              _source_to_san_path(san_dir, "src/L.kt") == legacy_f)
        legacy_f.unlink()

        # --- Setup: create a proper SAN file for remaining tests ---
        source_a = src_dir / "A.kt"
        source_a.write_text("class A { fun doWork() {} }")
        san_a_dir = san_dir / "src"
        san_a_dir.mkdir(parents=True, exist_ok=True)
        san_a = san_a_dir / "A.san"
        san_a.write_text("com.example.A @svc {\n  fn:doWork() → Unit\n}\n")
        # Make SAN newer than source
        time.sleep(0.05)
        san_a.touch()

        # Build index
        _rebuild_san_index(san_dir, repo_path=repo_path)
        index = _load_san_index(san_dir)
        _test("rebuild index finds SAN entry", "com.example.A" in index)
        _test("index has correct file field", index.get("com.example.A", {}).get("file", "").endswith("A.kt"))

        # --- Test 7: _refresh_san with no stale files ---
        hashes = {"src/A.kt": _hash_source(source_a)}
        _save_san_hashes(san_dir, hashes)
        stats = _refresh_san(repo_path, san_dir, [], [])
        _test("refresh with nothing stale: all zeros",
              stats["deleted"] == 0 and stats["hash_skipped"] == 0 and stats["stale_detected"] == 0)

        # --- Test 8: hash skip — mtime changed, content same ---
        # Touch source to make mtime newer, but content is same
        time.sleep(0.05)
        source_a.write_text("class A { fun doWork() {} }")  # same content
        hashes_before = _load_san_hashes(san_dir)
        stats = _refresh_san(repo_path, san_dir, ["src/A.kt"], [], hashes=dict(hashes_before))
        _test("hash skip on mtime false positive", stats["hash_skipped"] == 1 and stats["stale_detected"] == 0,
              f"hash_skipped={stats['hash_skipped']}, stale={stats['stale_detected']}")

        # --- Test 9: stale detection — content actually changed ---
        time.sleep(0.05)
        source_a.write_text("class A { fun doWork() {} fun newMethod() {} }")
        stats = _refresh_san(repo_path, san_dir, ["src/A.kt"], [], hashes=dict(hashes_before))
        _test("stale detected on real content change", stats["stale_detected"] == 1 and stats["hash_skipped"] == 0,
              f"stale={stats['stale_detected']}, hash_skipped={stats['hash_skipped']}")

        # Verify hash was NOT updated (should remain old)
        hashes_after = _load_san_hashes(san_dir)
        _test("hash NOT updated on stale detection", hashes_after.get("src/A.kt") == hashes_before.get("src/A.kt"),
              "hash was updated prematurely — staleness would be hidden")

        # --- Test 10: orphan cleanup — source deleted, hash tracked ---
        source_b = src_dir / "B.kt"
        source_b.write_text("class B {}")
        san_b = san_a_dir / "B.san"
        san_b.write_text("com.example.B @svc {\n}\n")
        b_hash = _hash_source(source_b)
        hashes_with_b = _load_san_hashes(san_dir)
        hashes_with_b["src/B.kt"] = b_hash
        _save_san_hashes(san_dir, hashes_with_b)

        # Delete source
        source_b.unlink()
        _test("B.san exists before orphan cleanup", san_b.exists())
        stats = _refresh_san(repo_path, san_dir, [], [])
        _test("orphan removed after source deletion", not san_b.exists() and stats["orphans_removed"] == 1,
              f"san_exists={san_b.exists()}, orphans_removed={stats['orphans_removed']}")

        # Verify hash entry removed
        hashes_after_orphan = _load_san_hashes(san_dir)
        _test("hash entry removed for deleted source", "src/B.kt" not in hashes_after_orphan)

        # --- Test 11: orphan cleanup — caller-detected orphan ---
        orphan_san = san_a_dir / "Orphan.san"
        orphan_san.write_text("dead content")
        stats = _refresh_san(repo_path, san_dir, [], [orphan_san])
        _test("caller-detected orphan deleted", not orphan_san.exists() and stats["deleted"] == 1)

        # --- Test 12: recompile_san hash update only for fresh SANs ---
        # Reset: source_a has changed content, SAN is stale (source newer than SAN)
        source_a.write_text("class A { fun v3() {} }")
        time.sleep(0.05)  # ensure mtime difference
        # SAN was last touched earlier, so san_mtime < source_mtime
        old_hashes = _load_san_hashes(san_dir)
        old_a_hash = old_hashes.get("src/A.kt")

        # Simulate what recompile_san step 2 does: check hash, skip stale
        current_hash = _hash_source(source_a)
        san_is_fresh = san_a.stat().st_mtime >= source_a.stat().st_mtime
        _test("recompile_san detects stale SAN (mtime)", not san_is_fresh,
              "SAN should be stale but mtime says fresh")

        # Now simulate brain-compiler regenerating the SAN
        time.sleep(0.05)
        san_a.write_text("com.example.A @svc {\n  fn:v3() → Unit\n}\n")
        san_is_fresh_after = san_a.stat().st_mtime >= source_a.stat().st_mtime
        _test("after brain-compiler regen, SAN is fresh (mtime)", san_is_fresh_after)

        # --- Test 13: backfill hashes for fresh untracked SANs ---
        # Remove A.kt from hashes to simulate untracked
        hashes_no_a = _load_san_hashes(san_dir)
        hashes_no_a.pop("src/A.kt", None)
        _save_san_hashes(san_dir, hashes_no_a)

        # Rebuild index so backfill can find it
        _rebuild_san_index(san_dir, repo_path=repo_path)
        index = _load_san_index(san_dir)

        # Simulate backfill logic from _ensure_san_fresh
        hashes_for_backfill = _load_san_hashes(san_dir)
        backfilled = False
        for qname, meta in index.items():
            src_rel = meta.get("file", "")
            if not src_rel or src_rel in hashes_for_backfill:
                continue
            s_path = repo_path / src_rel
            sn_path = _source_to_san_path(san_dir, src_rel)
            if s_path.exists() and sn_path.exists():
                if sn_path.stat().st_mtime >= s_path.stat().st_mtime:
                    hashes_for_backfill[src_rel] = _hash_source(s_path)
                    backfilled = True
        _test("backfill registers hash for fresh untracked SAN",
              backfilled and "src/A.kt" in hashes_for_backfill,
              f"backfilled={backfilled}, keys={list(hashes_for_backfill.keys())}")

        # --- Test 14: _rebuild_san_index extracts qualified names correctly ---
        multi_san = san_a_dir / "Multi.san"
        multi_san.write_text(
            "com.example.Foo @svc {\n  fn:bar() → Unit\n}\n\n"
            "com.example.Baz @model {\n  fn:qux() → String\n}\n"
        )
        _rebuild_san_index(san_dir, repo_path=repo_path)
        idx = _load_san_index(san_dir)
        _test("index extracts multiple entries from one SAN",
              "com.example.Foo" in idx and "com.example.Baz" in idx,
              f"keys={list(idx.keys())}")
        _test("index kind is correct", idx.get("com.example.Foo", {}).get("kind") == "svc"
              and idx.get("com.example.Baz", {}).get("kind") == "model")

        # Cleanup temp multi san
        multi_san.unlink()

        # --- Test 15: _rebuild_san_index ignores non-.san files ---
        (san_dir / "notes.txt").write_text("not a san file")
        (san_dir / ".san_hashes.json").write_text("{}")  # already exists
        _rebuild_san_index(san_dir, repo_path=repo_path)
        idx = _load_san_index(san_dir)
        _test("index ignores non-.san files", "notes" not in str(idx))

    except Exception as e:
        failed += 1
        results.append(f"  FAIL: unexpected exception — {type(e).__name__}: {e}")
    finally:
        # Clean up temp directory
        if tmp_root and tmp_root.exists():
            try:
                shutil.rmtree(tmp_root)
            except OSError:
                results.append(f"  WARN: could not clean up {tmp_root}")

    header = f"SAN System Validation: {passed} passed, {failed} failed"
    if failed == 0:
        header += " ✓ ALL TESTS PASSED"
    return header + "\n" + "\n".join(results)


def validate_brain() -> str:
    """
    Run comprehensive self-tests on the entire Agent Brain system:
    decision memory, graph persistence, pre_check warnings, scorecards,
    similarity matching, office state, code bridge, and SAN subsystem.
    Safe — uses isolated temp directory, never touches real brain data.
    """
    results = []
    passed = 0
    failed = 0
    tmp_root = None

    # Save real globals, swap in temp
    real_brain_dir = BRAIN_DIR
    real_graph_file = GRAPH_FILE
    real_config_file = CONFIG_FILE
    real_office_file = OFFICE_STATE_FILE
    real_marker_file = DECISION_MARKER_FILE
    real_query_marker_file = QUERY_MARKER_FILE
    real_records_dir = RECORDS_DIR
    real_archive_file = ARCHIVE_FILE
    real_metrics_file = METRICS_FILE

    def _test(name: str, condition: bool, detail: str = ""):
        nonlocal passed, failed
        if condition:
            passed += 1
            results.append(f"  PASS: {name}")
        else:
            failed += 1
            results.append(f"  FAIL: {name}" + (f" — {detail}" if detail else ""))

    # Acquire both locks to prevent concurrent access during global swap
    with LOCK, OFFICE_LOCK:
     try:
        tmp_root = Path(tempfile.mkdtemp(prefix="brain_validate_"))
        # Redirect globals to temp (including marker to avoid polluting real state)
        globals()["BRAIN_DIR"] = tmp_root
        globals()["GRAPH_FILE"] = tmp_root / "decisions.json"
        globals()["CONFIG_FILE"] = tmp_root / "config.json"
        globals()["OFFICE_STATE_FILE"] = tmp_root / "office-state.json"
        globals()["DECISION_MARKER_FILE"] = tmp_root / ".last_decision_marker"
        globals()["QUERY_MARKER_FILE"] = tmp_root / ".last_query_marker"
        globals()["RECORDS_DIR"] = tmp_root / "records"
        globals()["ARCHIVE_FILE"] = tmp_root / "decisions.archive.jsonl"
        globals()["METRICS_FILE"] = tmp_root / "brain_metrics.jsonl"
        # Drop any shadow/cache from the real brain so temp diffs start clean.
        _GRAPH_CACHE.update({"key": None, "graph": None, "shadow": None})

        # ===================================================================
        # SECTION 1: Graph Persistence
        # ===================================================================
        results.append("\n--- Graph Persistence ---")

        # Test 1: Empty graph load
        G = _load_graph()
        _test("empty graph on fresh start", G.number_of_nodes() == 0)

        # Test 2: Save and reload
        G.add_node("test_node", type="decision", agent="tester", area="auth",
                    action="test action", outcome="pending",
                    timestamp=datetime.now().isoformat())
        _save_graph(G)
        G2 = _load_graph()
        _test("graph save/reload preserves nodes", G2.number_of_nodes() == 1)
        _test("graph save/reload preserves data",
              G2.nodes["test_node"]["agent"] == "tester")

        # Test 3: Atomic write (tmp file cleaned up)
        _test("no leftover .tmp file", not (tmp_root / "decisions.tmp").exists())

        # ===================================================================
        # SECTION 2: Decision Memory (log_decision, log_outcome, log_feedback)
        # ===================================================================
        results.append("\n--- Decision Memory ---")

        # Start fresh
        _save_graph(nx.DiGraph())

        # Test 4: log_decision
        result = log_decision(
            agent="alice", repo="test-repo", area="auth",
            action="add JWT validation", reasoning="security requirement",
            files_touched=["src/auth/jwt.kt"]
        )
        _test("log_decision returns ID", result.startswith("Decision logged: dec_"))
        dec_id = result.split("Decision logged: ")[1].split("\n")[0]

        # Test 5: Decision in graph
        G = _load_graph()
        _test("decision node exists in graph", dec_id in G)
        _test("decision has correct agent", G.nodes[dec_id].get("agent") == "alice")
        _test("decision has correct area", G.nodes[dec_id].get("area") == "auth")
        _test("decision outcome is pending", G.nodes[dec_id].get("outcome") == "pending")
        _test("decision has files", G.nodes[dec_id].get("files") == ["src/auth/jwt.kt"])

        # Test 6: log_outcome
        result = log_outcome(dec_id, "rejected", "marcus", "violates DIP")
        _test("log_outcome succeeds", "Outcome recorded" in result)
        G = _load_graph()
        _test("outcome updated to rejected", G.nodes[dec_id].get("outcome") == "rejected")
        _test("outcome_by recorded", G.nodes[dec_id].get("outcome_by") == "marcus")
        _test("outcome_reason recorded", G.nodes[dec_id].get("outcome_reason") == "violates DIP")

        # Test 7: log_outcome on missing decision
        result = log_outcome("nonexistent_id", "accepted", "bob", "ok")
        _test("log_outcome rejects missing ID", "ERROR" in result)

        # Test 8: log_feedback
        result = log_feedback("marcus", dec_id, "needs interface wrapper", "blocker")
        _test("log_feedback returns ID", "Feedback logged:" in result)
        fb_id = result.split("Feedback logged: ")[1].split(" ->")[0]

        G = _load_graph()
        _test("feedback node exists", fb_id in G)
        _test("feedback has correct severity", G.nodes[fb_id].get("severity") == "blocker")
        _test("feedback edge points to decision",
              G.has_edge(fb_id, dec_id) and G.edges[fb_id, dec_id].get("relation") == "feedback_on")

        # Test 9: log_feedback on missing decision
        result = log_feedback("bob", "nonexistent", "text", "info")
        _test("log_feedback rejects missing ID", "ERROR" in result)

        # ===================================================================
        # SECTION 3: pre_check & Adaptive Warnings
        # ===================================================================
        results.append("\n--- Pre-check & Warnings ---")

        # Test 10: pre_check with no history
        _save_graph(nx.DiGraph())
        result = pre_check("newagent", "billing", "add payment flow")
        _test("pre_check clean area: no warnings", "No past failures" in result)

        # Test 11: pre_check with rejection in same area
        G = nx.DiGraph()
        G.add_node("dec_old", type="decision", agent="bob", area="auth",
                    action="hardcode JWT secret", reasoning="quick fix",
                    outcome="rejected", outcome_by="marcus",
                    outcome_reason="security violation",
                    timestamp="2025-01-01T00:00:00")
        _save_graph(G)
        result = pre_check("alice", "auth", "add JWT validation")
        _test("pre_check shows exact match warning", "EXACT MATCHES" in result)
        _test("pre_check shows rejection reason", "security violation" in result)

        # Test 12: Adaptive warning levels
        G = nx.DiGraph()
        for i in range(10):
            G.add_node(f"dec_{i}", type="decision", agent="badagent", area="api",
                        action=f"attempt {i}", outcome="rejected",
                        outcome_by="pe", outcome_reason="bad design",
                        timestamp=f"2025-01-{i+1:02d}T00:00:00")
        _save_graph(G)
        level = _adaptive_warning_level(G, "badagent", "api")
        _test("high rejection rate → STRICT level", level == "strict",
              f"got {level}")

        result = pre_check("badagent", "api", "another attempt")
        _test("strict agent gets ALERT in pre_check", "ALERT" in result)
        _test("strict agent sees rejection patterns", "REJECTION PATTERNS" in result)

        # Test 13: Normal warning level for good agent
        G.add_node("dec_good1", type="decision", agent="goodagent", area="ui",
                    action="add button", outcome="accepted", outcome_by="pe",
                    outcome_reason="good", timestamp="2025-01-01T00:00:00")
        G.add_node("dec_good2", type="decision", agent="goodagent", area="ui",
                    action="add form", outcome="accepted", outcome_by="pe",
                    outcome_reason="good", timestamp="2025-01-02T00:00:00")
        G.add_node("dec_good3", type="decision", agent="goodagent", area="ui",
                    action="add modal", outcome="accepted", outcome_by="pe",
                    outcome_reason="good", timestamp="2025-01-03T00:00:00")
        _save_graph(G)
        level = _adaptive_warning_level(G, "goodagent", "ui")
        _test("good agent → NORMAL level", level == "normal", f"got {level}")

        # ===================================================================
        # SECTION 4: Similarity Matching
        # ===================================================================
        results.append("\n--- Similarity Matching ---")

        # Test 14: Tokenizer (camelCase split before lowercasing)
        tokens = _tokenize("AuthService rate limiting middleware")
        _test("tokenizer splits camelCase",
              "auth" in tokens and "service" in tokens and "authservice" not in tokens,
              f"got {tokens}")
        _test("tokenizer extracts meaningful terms",
              "rate" in tokens and "limiting" in tokens)

        # Test 15: stopwords removed
        tokens = _tokenize("the is and or but for")
        _test("tokenizer removes stopwords", len(tokens) == 0, f"got {tokens}")

        # Test 16: Similarity calculation
        sim = _similarity("rate limiting on login endpoint",
                           "rate limiting on signup endpoint")
        _test("similar actions have high similarity", sim > 0.3, f"sim={sim:.2f}")

        sim_diff = _similarity("rate limiting on login", "database migration for schema")
        _test("different actions have low similarity", sim_diff < 0.2, f"sim={sim_diff:.2f}")

        # Test 17: find_similar_rejections
        G = nx.DiGraph()
        G.add_node("dec_rl", type="decision", agent="bob", area="api",
                    action="rate limiting on login", outcome="rejected",
                    outcome_by="marcus", outcome_reason="use token bucket instead",
                    timestamp="2025-01-01T00:00:00")
        _save_graph(G)
        similar = _find_similar_rejections(G, "rate limiting on signup endpoint")
        _test("similar rejection found", len(similar) >= 1,
              f"found {len(similar)}")

        # Test 18: no false positives
        similar_false = _find_similar_rejections(G, "database schema migration tool")
        _test("unrelated action: no similar rejections", len(similar_false) == 0,
              f"found {len(similar_false)}")

        # ===================================================================
        # SECTION 5: Pattern Clustering
        # ===================================================================
        results.append("\n--- Pattern Clustering ---")

        G = nx.DiGraph()
        G.add_node("d1", type="decision", agent="a", area="api",
                    action="x", outcome="rejected", outcome_reason="violates DIP principle",
                    timestamp="2025-01-01T00:00:00")
        G.add_node("d2", type="decision", agent="b", area="api",
                    action="y", outcome="rejected", outcome_reason="DIP violation in service layer",
                    timestamp="2025-01-02T00:00:00")
        G.add_node("d3", type="decision", agent="c", area="ui",
                    action="z", outcome="rejected", outcome_reason="missing unit tests",
                    timestamp="2025-01-03T00:00:00")
        _save_graph(G)

        clusters = _cluster_rejection_reasons(G)
        _test("DIP rejections cluster together", any(
            len(c) >= 2 and any("DIP" in r["reason"] or "dip" in r["reason"].lower() for r in c)
            for c in clusters
        ), f"clusters={[[r['reason'] for r in c] for c in clusters]}")

        # ===================================================================
        # SECTION 6: Scorecards & Dashboard
        # ===================================================================
        results.append("\n--- Scorecards & Dashboard ---")

        G = nx.DiGraph()
        for i in range(5):
            G.add_node(f"d_acc_{i}", type="decision", agent="star",
                        area="api", action=f"feature {i}", outcome="accepted",
                        outcome_by="pe", outcome_reason="good",
                        timestamp=f"2025-01-{i+1:02d}T00:00:00")
        G.add_node("d_rej_1", type="decision", agent="star", area="api",
                    action="bad feature", outcome="rejected", outcome_by="pe",
                    outcome_reason="wrong approach",
                    timestamp="2025-01-10T00:00:00")
        _save_graph(G)

        scorecards = _compute_scorecard(G, "star")
        _test("scorecard computed for agent", "star" in scorecards)
        s = scorecards["star"]
        _test("scorecard total correct", s["total"] == 6, f"got {s['total']}")
        _test("scorecard accepted correct", s["accepted"] == 5, f"got {s['accepted']}")
        _test("scorecard rejected correct", s["rejected"] == 1, f"got {s['rejected']}")

        # Test brain_stats
        result = brain_stats()
        _test("brain_stats returns stats", "Decisions: 6" in result, f"got: {result}")

        # Test get_agent_stats
        result = get_agent_stats("star")
        _test("get_agent_stats finds agent", "star:" in result)

        # Test agent_scorecard (detail mode)
        result = agent_scorecard("star", detail=True)
        _test("agent_scorecard renders", "SCORECARD: star" in result)
        _test("agent_scorecard shows acceptance rate", "83%" in result, f"got: {result}")
        # Compact mode delegates to get_agent_stats
        _test("agent_scorecard compact mode", "star:" in agent_scorecard("star"))

        # Test team_dashboard
        result = team_dashboard()
        _test("team_dashboard renders", "TEAM DASHBOARD" in result)
        _test("team_dashboard shows agent", "star:" in result)

        # ===================================================================
        # SECTION 7: Query & Retrieval
        # ===================================================================
        results.append("\n--- Query & Retrieval ---")

        # query_decisions
        result = query_decisions(area="api", agent="star")
        _test("query_decisions finds by area+agent", "decision(s)" in result)

        result = query_decisions(outcome="rejected")
        _test("query_decisions filters by outcome", "bad feature" in result)

        result = query_decisions(area="nonexistent")
        _test("query_decisions empty result", "No matching" in result)

        # get_decision
        result = get_decision("d_acc_0")
        _test("get_decision retrieves details", "feature 0" in result)

        result = get_decision("nonexistent")
        _test("get_decision handles missing", "ERROR" in result)

        # decisions_for_file
        G = _load_graph()
        G.nodes["d_acc_0"]["files"] = ["src/api/handler.kt"]
        _save_graph(G)
        result = decisions_for("src/api/handler.kt")
        _test("decisions_for (file mode) finds by path", "decision(s)" in result)

        # ===================================================================
        # SECTION 8: Code Bridge
        # ===================================================================
        results.append("\n--- Code Bridge ---")

        # code_symbols linking
        G = _load_graph()
        G.nodes["d_acc_0"]["code_symbols"] = ["com.example.AuthService.login"]
        code_node = "code:com.example.AuthService.login"
        G.add_node(code_node, type="code_ref", qualified_name="com.example.AuthService.login",
                    repo="test-repo")
        G.add_edge("d_acc_0", code_node, relation="touches")
        _save_graph(G)

        result = decisions_for("com.example.AuthService.login")
        _test("decisions_for (code mode) finds linked decision", "decision(s)" in result)

        result = decisions_for("com.example.NonExistent")
        _test("decisions_for (code mode) empty for unknown symbol", "No decisions" in result)

        result = code_impact("d_acc_0")
        _test("code_impact shows symbols", "AuthService" in result)

        result = code_impact("nonexistent")
        _test("code_impact handles missing ID", "ERROR" in result)

        # ===================================================================
        # SECTION 9: Office State & Heartbeat
        # ===================================================================
        results.append("\n--- Office State ---")

        # Write a minimal config for role lookup
        (tmp_root / "config.json").write_text(json.dumps({
            "repos": {}, "team": [
                {"name": "alice", "role": "backend-engineer"},
                {"name": "marcus", "role": "principal-engineer"},
            ]
        }))

        result = heartbeat("alice", "working", task="implementing auth",
                            talking_to="marcus", message="DIP question")
        _test("heartbeat returns ok", result == "ok")

        state = _load_office_state()
        _test("office state has agent", "alice" in state.get("agents", {}))
        _test("office state status correct", state["agents"]["alice"]["status"] == "working")
        _test("office state role resolved", state["agents"]["alice"]["role"] == "backend-engineer")
        _test("office state task recorded", state["agents"]["alice"]["task"] == "implementing auth")
        _test("office state talking_to recorded", state["agents"]["alice"]["talking_to"] == "marcus")
        _test("office state message recorded", state["agents"]["alice"]["message"] == "DIP question")

        # Messages log
        _test("message logged in messages array",
              len(state.get("messages", [])) == 1 and state["messages"][0]["from"] == "alice")

        # office_state tool
        result = office_state()
        _test("office_state tool renders", "alice" in result and "working" in result)

        # auto_heartbeat from brain tools
        _auto_heartbeat("marcus", "reviewing", "checking auth PR")
        state = _load_office_state()
        _test("auto_heartbeat updates state", "marcus" in state.get("agents", {}))
        _test("auto_heartbeat role resolved", state["agents"]["marcus"]["role"] == "principal-engineer")

        # ===================================================================
        # SECTION 10: Config & Edge Cases
        # ===================================================================
        results.append("\n--- Config & Edge Cases ---")

        # Missing config
        config_backup = (tmp_root / "config.json").read_text()
        (tmp_root / "config.json").unlink()
        config = _load_config()
        _test("missing config returns empty defaults", config == {"repos": {}, "team": []})
        (tmp_root / "config.json").write_text(config_backup)

        # Corrupt config
        (tmp_root / "config.json").write_text("{corrupt json!!!")
        config = _load_config()
        _test("corrupt config returns empty defaults", config == {"repos": {}, "team": []})
        (tmp_root / "config.json").write_text(config_backup)

        # Corrupt graph file (snapshot + journal both unreadable → empty)
        (tmp_root / "decisions.json").write_text("not json")
        jf = (tmp_root / "decisions.journal")
        if jf.exists():
            jf.unlink()
        _GRAPH_CACHE.update({"key": None, "graph": None, "shadow": None})
        G = _load_graph()
        _test("corrupt graph returns empty graph", G.number_of_nodes() == 0)

        # ===================================================================
        # SECTION 11: SAN System (delegate to validate_san_system)
        # ===================================================================
        results.append("\n--- SAN System ---")
        san_result = validate_san_system()
        san_passed = "ALL TESTS PASSED" in san_result
        _test("SAN subsystem all tests pass", san_passed,
              san_result.split("\n")[0] if not san_passed else "")

        # ===================================================================
        # SECTION 12: Integration — Full Workflow
        # ===================================================================
        results.append("\n--- Integration: Full Workflow ---")

        # Clean slate
        _save_graph(nx.DiGraph())

        # Step 1: pre_check on clean brain
        r = pre_check("dev", "payments", "add Stripe integration")
        _test("workflow: pre_check clean", "No past failures" in r)

        # Step 2: log_decision
        r = log_decision("dev", "test-repo", "payments", "add Stripe integration",
                          "business requirement", files_touched=["src/pay/stripe.kt"])
        wf_dec_id = r.split("Decision logged: ")[1].split("\n")[0]
        _test("workflow: decision logged", wf_dec_id.startswith("dec_"))

        # Step 3: PE rejects
        r = log_outcome(wf_dec_id, "rejected", "pe", "wrap behind PaymentGateway interface")
        _test("workflow: outcome logged", "Outcome recorded" in r)

        # Step 4: PE gives feedback
        r = log_feedback("pe", wf_dec_id, "must use interface for DIP compliance", "blocker")
        _test("workflow: feedback logged", "Feedback logged" in r)

        # Step 5: Another agent tries similar action — should see warning
        r = pre_check("dev2", "payments", "integrate payment provider directly")
        _test("workflow: pre_check shows past rejection",
              "EXACT MATCHES" in r or "SIMILAR" in r,
              f"got: {r[:200]}")

        # Step 6: get_decision shows full picture
        r = get_decision(wf_dec_id)
        _test("workflow: get_decision shows outcome", "rejected" in r)
        _test("workflow: get_decision shows feedback", "blocker" in r or "DIP" in r)

        # Step 7: Scorecard reflects rejection
        r = agent_scorecard("dev", detail=True)
        _test("workflow: scorecard shows 100% rejection", "100%" in r and "rejected" in r.lower())

        # Step 8: query finds it
        r = query_decisions(area="payments")
        _test("workflow: query finds decision", wf_dec_id in r)

        # Step 9: similar_failures finds it
        r = similar_failures("add payment processing with Stripe SDK")
        _test("workflow: similar_failures finds rejection",
              "similar past failure" in r.lower() or "payment" in r.lower(),
              f"got: {r[:200]}")

        # --- Amnesia fix: relevance search + roadmap digest ---
        # Seed: a roadmap-tagged decision + a noisy recent one in another area.
        rm = log_decision("dev", "test-repo", "kmp-foundation/roadmap",
                          "Foundation-first: shared models then SharedDB then AppStorage",
                          "user directive: fix platform blockers before screens")
        rm_id = rm.split("Decision logged: ")[1].split("\n")[0]
        for i in range(3):
            log_decision("dev", "test-repo", "ui/buttons", f"tweak button color {i}", "polish")

        # Relevance: a topical query must rank the roadmap decision above the
        # noisier-but-more-recent button decisions (the old recency bug).
        r = query_decisions(repo="test-repo", query="foundation AppStorage roadmap", limit=3)
        _test("amnesia: relevance ranks roadmap over recency", rm_id in r,
              f"got: {r[:300]}")

        # Recency mode still works (no query).
        r = query_decisions(repo="test-repo", sort="recency", limit=2)
        _test("amnesia: recency sort returns newest", "button color 2" in r,
              f"got: {r[:200]}")

        # get_roadmap surfaces pending + roadmap, roadmap-tagged FIRST.
        r = get_roadmap("test-repo", limit=10)
        first_line = r.split("\n")[1] if len(r.split("\n")) > 1 else ""
        _test("amnesia: get_roadmap lists roadmap decision first",
              rm_id in first_line and "ROADMAP" in first_line, f"got: {first_line[:200]}")
        _test("amnesia: get_roadmap includes pending Stripe decision",
              wf_dec_id in r or "OPEN WORK" in r)

        # Roadmap digest is non-empty and token-budgeted (header + items).
        from_rows = _roadmap_rows(_load_graph(), "test-repo")
        digest = _format_roadmap_digest(from_rows, limit=15)
        _test("amnesia: digest non-empty", "OPEN WORK" in digest and rm_id in digest)

        # Query marker is written by read tools (satisfies optional hard gate).
        _test("amnesia: query marker written", QUERY_MARKER_FILE.exists())

        # --- §8 SAN adoption: absolute-path get_san, coverage, lead docstrings ---
        import os as _os
        san_repo = tmp_root / "san-proj"
        (san_repo / ".san" / "src").mkdir(parents=True)
        (san_repo / "src").mkdir(exist_ok=True)
        src_kt = san_repo / "src" / "Auth.kt"
        san_kt = san_repo / ".san" / "src" / "Auth.kt.san"
        src_kt.write_text("class Auth { fun login() {} }")
        san_kt.write_text("com.app.Auth @svc {\n  src: 1-9\n  deps: UserRepo\n"
                          "  fn:login(email) -> Result [validate -> issue]\n}")
        # Index so coverage counts unique files.
        (san_repo / ".san" / "_index.json").write_text(json.dumps({
            "com.app.Auth": {"kind": "svc", "file": "src/Auth.kt", "tokens_san": 40},
        }))
        # Register the repo in temp config so resolution finds it.
        CONFIG_FILE.write_text(json.dumps({"repos": {"san-proj": str(san_repo)},
                                           "team": []}))
        _GRAPH_CACHE  # (no-op ref; config read is uncached)

        # _resolve_absolute_path: inside -> (name, root); outside -> (None, None)
        name, root = _resolve_absolute_path(str(src_kt))
        _test("san8: resolve_absolute finds repo", name == "san-proj" and root is not None)
        n2, _ = _resolve_absolute_path("/tmp/definitely/outside/Foo.kt")
        _test("san8: resolve_absolute returns None outside repos", n2 is None)

        # get_san with an ABSOLUTE path (repo auto-detected, repo arg empty).
        r = get_san(repo="", file_path=str(src_kt), detail="sig")
        _test("san8: get_san accepts absolute path", "com.app.Auth @svc" in r,
              f"got: {r[:150]}")
        # sig strips impl block but keeps signature.
        _test("san8: get_san sig strips impl, keeps fn",
              "fn:login(email)" in r and "[validate" not in r, f"got: {r[:200]}")
        # absolute path outside any repo -> clear message, no crash.
        r = get_san(repo="", file_path="/tmp/nope/Foo.kt")
        _test("san8: get_san abs outside repo -> clear msg",
              "not under any repo" in r, f"got: {r[:120]}")
        # absolute path == repo ROOT -> clear msg, not a crash/fuzzy-match storm.
        r = get_san(repo="", file_path=str(san_repo))
        _test("san8: get_san abs==repo root -> clear msg, no crash",
              "repo root" in r and "Multiple SAN matches" not in r, f"got: {r[:140]}")
        # relative-path path still works (regression).
        r = get_san(repo="san-proj", file_path="src/Auth.kt")
        _test("san8: get_san relative path unchanged", "com.app.Auth @svc" in r)

        # _san_coverage_line counts UNIQUE files (1 here), not index entries.
        cov = _san_coverage_line("san-proj")
        _test("san8: coverage line counts unique files",
              "SAN AVAILABLE" in cov and "1 files compiled" in cov, f"got: {cov}")
        _test("san8: coverage empty for unknown repo", _san_coverage_line("nope") == "")

        # pre_check with repo surfaces SAN coverage; without repo stays clean.
        r = pre_check("dev", "auth", "explore auth code", repo="san-proj")
        _test("san8: pre_check(repo) surfaces SAN coverage", "SAN AVAILABLE" in r)
        r = pre_check("dev", "auth", "explore auth code")
        _test("san8: pre_check() no-repo backward compat", "SAN AVAILABLE" not in r)

        # Docstrings LEAD with the read-instead-of directive (fix #5).
        _test("san8: get_san docstring leads with 'instead of Read'",
              "instead of raw Read" in (get_san.__doc__ or "")[:200])
        _test("san8: query_san docstring leads with 'instead of grep'",
              "instead of grepping" in (query_san.__doc__ or "")[:120])

        # --- SOURCE_EXTS single-source-of-truth (no drift -> no wrong orphan delete) ---
        # decisions_for must use the SAME ext set the orphan/freshness sweep uses,
        # else it accepts files those sweeps skip (silent staleness / deletion).
        _test("exts: extended languages present in global SOURCE_EXTS",
              all(e in SOURCE_EXTS for e in (".rb", ".cs", ".cpp", ".scala", ".php")),
              f"got: {SOURCE_EXTS}")
        # decisions_for now references the global set directly (no private copy).
        import inspect as _inspect
        _df_src = _inspect.getsource(decisions_for)
        _test("exts: decisions_for uses global SOURCE_EXTS (no private _SOURCE_EXTS copy)",
              "_SOURCE_EXTS = (" not in _df_src and "endswith(SOURCE_EXTS)" in _df_src)
        # decisions_for path-detection treats an extended-language file as a path
        # (routes to _decisions_for_file, not _decisions_for_code).
        _test("exts: .rb classified as file path",
              "x/User.rb".lower().endswith(SOURCE_EXTS))
        # Orphan-safety: a fresh legacy-form .san for an extended-language source
        # whose source EXISTS must NOT be flagged orphan (was deletable before).
        orphan_repo = tmp_root / "rb-proj"
        (orphan_repo / ".san").mkdir(parents=True)
        (orphan_repo / "user.rb").write_text("class User; end")
        (orphan_repo / ".san" / "user.san").write_text("User @model {\n  src: 1-1\n}")
        _orphans = []
        for _sf in (orphan_repo / ".san").rglob("*.san"):
            _rel = str(_sf.relative_to(orphan_repo / ".san"))
            _srel = str(Path(_rel).with_suffix(""))
            for _e in SOURCE_EXTS:
                _cand = (orphan_repo / (_srel + _e)) if not _srel.endswith(_e) else (orphan_repo / _srel)
                if _cand.exists():
                    break
            else:
                if not (orphan_repo / _srel).exists():
                    _orphans.append(_sf)
        _test("exts: legacy .san for existing .rb source not flagged orphan",
              len(_orphans) == 0, f"wrongly orphaned: {_orphans}")

        # --- Records lifecycle: export, prune (dry+apply), stale-pending ---
        # Seed: an old accepted decision (prunable), an old rejection (kept),
        # an old roadmap (kept), a recent one (too new), an old pending.
        def _seed(area, action, outcome, age_days):
            r = log_decision("dev", "rec-repo", area, action, "seed")
            did = r.split("Decision logged: ")[1].split("\n")[0]
            if outcome != "pending":
                log_outcome(did, outcome, "pe", "seed outcome")
            with LOCK:
                g = _load_graph()
                g.nodes[did]["timestamp"] = (datetime.now() - timedelta(days=age_days)).isoformat()
                _save_graph(g)
            return did
        old_acc = _seed("billing", "old accepted thing", "accepted", 200)
        old_rej = _seed("billing", "old rejected thing", "rejected", 200)
        old_road = _seed("kmp/roadmap", "old roadmap thing", "pending", 200)
        recent = _seed("billing", "recent thing", "accepted", 5)
        old_pend = _seed("billing", "old pending thing", "pending", 60)

        # export_records writes per-day markdown + INDEX.
        r = export_records("rec-repo")
        _test("records: export reports files written", "Exported" in r and "day" in r)
        _test("records: INDEX.md created", (RECORDS_DIR / "INDEX.md").exists())
        _test("records: a day file contains the decision id",
              any(old_acc in p.read_text() for p in RECORDS_DIR.glob("*.md")))

        # prune dry-run: lists old accepted, NOT rejection/roadmap/recent.
        r = prune_decisions(repo="rec-repo", before_days=90, dry_run=True)
        _test("records: prune dry-run finds old accepted", old_acc in r and "DRY RUN" in r)
        _test("records: prune dry-run keeps rejection", old_rej not in r)
        _test("records: prune dry-run keeps roadmap", old_road not in r)
        _test("records: prune dry-run skips recent", recent not in r)
        # Nothing actually removed yet.
        _test("records: dry-run did not delete", old_acc in _load_graph())

        # prune apply: archives + removes from graph.
        r = prune_decisions(repo="rec-repo", before_days=90, dry_run=False)
        _test("records: prune apply removes from graph", old_acc not in _load_graph())
        _test("records: rejection survives prune", old_rej in _load_graph())
        _test("records: archive file written", ARCHIVE_FILE.exists()
              and old_acc in ARCHIVE_FILE.read_text())

        # stale-pending: old pending -> superseded; roadmap pending kept as-is by intent.
        r = resolve_stale_pending(before_days=30, repo="rec-repo", dry_run=True)
        _test("records: stale-pending dry-run finds old pending", "would mark" in r)
        r = resolve_stale_pending(before_days=30, repo="rec-repo", dry_run=False)
        with LOCK:
            _g = _load_graph()
        _test("records: old pending now superseded",
              _g.nodes.get(old_pend, {}).get("outcome") == "superseded")

     except Exception as e:
        import traceback
        failed += 1
        results.append(f"  FAIL: unexpected exception — {type(e).__name__}: {e}")
        results.append(f"  {traceback.format_exc()[-500:]}")
     finally:
        # Restore real globals
        globals()["BRAIN_DIR"] = real_brain_dir
        globals()["GRAPH_FILE"] = real_graph_file
        globals()["CONFIG_FILE"] = real_config_file
        globals()["OFFICE_STATE_FILE"] = real_office_file
        globals()["DECISION_MARKER_FILE"] = real_marker_file
        globals()["QUERY_MARKER_FILE"] = real_query_marker_file
        globals()["RECORDS_DIR"] = real_records_dir
        globals()["ARCHIVE_FILE"] = real_archive_file
        globals()["METRICS_FILE"] = real_metrics_file
        # Drop temp shadow/cache; next real _load_graph re-reads from disk.
        _GRAPH_CACHE.update({"key": None, "graph": None, "shadow": None})
        # Clean up
        if tmp_root and tmp_root.exists():
            try:
                shutil.rmtree(tmp_root)
            except OSError:
                results.append(f"  WARN: could not clean up {tmp_root}")

    header = f"Agent Brain Validation: {passed} passed, {failed} failed"
    if failed == 0:
        header += " ✓ ALL TESTS PASSED"
    return header + "\n" + "\n".join(results)


def _diagnose(project: str = "") -> int:
    """
    Self-check from the shell. Prints PASS/FAIL per check.

    Verifies (always):
      1. server.py importable + MCP tools registered
      2. config.json valid JSON (or absent — that's OK, server allows empty)
      3. ~/.agent-brain/ writable (decision marker round-trip)
      4. decisions.json readable if present (atomic-write safety)
      5. MCP registration: Claude or Codex config mentions agent-brain
      6. Agent .md frontmatter is subagent-MCP-safe (omits `tools:` OR includes
         ToolSearch as fallback)
      7. Per-repo team scoping: report resolved team for each configured repo

    Verifies (when project=<path> is set):
      8. Claude project MCP files when present
      9. Codex AGENTS.md guidance when present
     10. <project>/.gitignore covers brain artifacts (warning, not fail)

    Exit code 0 if all checks pass, 1 otherwise.
    """
    import sys as _sys

    checks: list[tuple[str, bool, str]] = []

    # 1. tools registered
    try:
        tool_count = len(mcp._tool_manager._tools)
        checks.append(("MCP tools registered", tool_count > 0,
                       f"{tool_count} tools"))
    except Exception as e:
        checks.append(("MCP tools registered", False, str(e)))

    # 2. config.json
    cfg_ok = True
    cfg_msg = "no config.json (empty brain — OK)"
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            cfg_msg = (f"{len(cfg.get('repos', {}))} repo(s), "
                       f"{len(cfg.get('team', []))} team member(s)")
        except Exception as e:
            cfg_ok = False
            cfg_msg = f"INVALID JSON: {e}"
    checks.append(("config.json valid", cfg_ok, cfg_msg))

    # 3. brain dir writable
    try:
        BRAIN_DIR.mkdir(parents=True, exist_ok=True)
        probe = BRAIN_DIR / ".diagnose_probe"
        probe.write_text("ok")
        probe.unlink()
        checks.append(("BRAIN_DIR writable", True, str(BRAIN_DIR)))
    except Exception as e:
        checks.append(("BRAIN_DIR writable", False, f"{BRAIN_DIR}: {e}"))

    # 4. decisions.json readable
    if GRAPH_FILE.exists():
        try:
            data = json.loads(GRAPH_FILE.read_text())
            n = len(data.get("nodes", []))
            checks.append(("decisions.json readable", True, f"{n} node(s)"))
        except Exception as e:
            checks.append(("decisions.json readable", False, str(e)))
    else:
        checks.append(("decisions.json readable", True,
                       "absent (fresh install — OK)"))

    # 5. MCP registration in either Claude or Codex config file
    home = Path.home()
    codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).expanduser()
    locations = [
        ("~/.claude.json", home / ".claude.json"),
        ("~/.claude/settings.json", home / ".claude" / "settings.json"),
    ]
    found_anywhere = False
    location_report = []
    for label, path in locations:
        if not path.exists():
            location_report.append(f"{label}: missing")
            continue
        try:
            data = json.loads(path.read_text())
            if isinstance(data.get("mcpServers"), dict) and "agent-brain" in data["mcpServers"]:
                location_report.append(f"{label}: ✓")
                found_anywhere = True
            else:
                location_report.append(f"{label}: not registered")
        except Exception as e:
            location_report.append(f"{label}: parse error ({e})")
    codex_config = codex_home / "config.toml"
    if codex_config.exists():
        try:
            if "[mcp_servers.agent-brain]" in codex_config.read_text():
                location_report.append(f"{codex_config}: ✓")
                found_anywhere = True
            else:
                location_report.append(f"{codex_config}: not registered")
        except Exception as e:
            location_report.append(f"{codex_config}: read error ({e})")
    else:
        location_report.append(f"{codex_config}: missing")

    codex_hooks = codex_home / "hooks.json"
    if codex_hooks.exists():
        try:
            if "enforce_brain_protocol.py" in codex_hooks.read_text():
                checks.append(("Codex hooks configured", True, str(codex_hooks)))
            else:
                checks.append(("Codex hooks configured", True,
                               f"{codex_hooks} exists, no agent-brain hooks"))
        except Exception as e:
            checks.append(("Codex hooks configured", False, f"read error: {e}"))
    else:
        checks.append(("Codex hooks configured", True,
                       f"{codex_hooks} missing (OK unless using Codex)"))
    checks.append(("MCP registered (main session)", found_anywhere,
                   " | ".join(location_report)))

    # 6. agent .md frontmatter is subagent-MCP-safe.
    #    Two valid configurations:
    #      (a) NO `tools:` field → subagent inherits everything including MCP (preferred)
    #      (b) `tools:` includes ToolSearch → fallback bootstrap path
    #    Anything else silently filters out mcp__* tools.
    agents_dir = home / ".claude" / "agents"
    if agents_dir.is_dir():
        broken: list[str] = []
        scanned = 0
        for md in sorted(agents_dir.glob("*.md")):
            scanned += 1
            try:
                head = md.read_text(errors="replace")[:2000]
            except OSError:
                continue
            # Find a yaml line starting with `tools:` (ignoring comments)
            tools_line = None
            for line in head.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if stripped.startswith("tools:"):
                    tools_line = stripped
                    break
            if tools_line is None:
                continue  # case (a): no tools field, MCP inherits — OK
            # case (b): tools field present — must include ToolSearch
            if "ToolSearch" not in tools_line:
                broken.append(md.name)
        if scanned == 0:
            checks.append(("Subagent frontmatter MCP-safe", True,
                           "no agent .md files (skip)"))
        elif broken:
            checks.append(("Subagent frontmatter MCP-safe", False,
                           f"sets `tools:` without ToolSearch (strips MCP) in: "
                           f"{', '.join(broken)}. Either remove the tools field "
                           f"or add ToolSearch."))
        else:
            checks.append(("Subagent frontmatter MCP-safe", True,
                           f"{scanned} file(s) OK"))
    else:
        checks.append(("Subagent frontmatter MCP-safe", True,
                       f"no {agents_dir} (skip)"))

    # 7. per-repo team scoping report (informational; never fails)
    try:
        config = _load_config()
        repos = list((config.get("repos") or {}).keys())
        if repos:
            details = []
            for r in repos:
                team = _get_team_for_repo(r)
                names = [t.get("name", "?") for t in team if isinstance(t, dict)]
                details.append(f"{r}: {len(names)} ({', '.join(names) or 'empty'})")
            checks.append(("Per-repo team resolution", True, "; ".join(details)))
        else:
            checks.append(("Per-repo team resolution", True,
                           "no repos configured"))
    except Exception as e:
        checks.append(("Per-repo team resolution", False, str(e)))

    # 8-10. Project-layer checks (only when --project specified)
    if project:
        proj = Path(project).expanduser()
        if not proj.is_dir():
            checks.append((f"Project '{project}'", False,
                           "path is not a directory"))
        else:
            # 8. Claude project MCP files (informational unless malformed)
            mcp_path = proj / ".mcp.json"
            claude_project_mcp = False
            if not mcp_path.exists():
                checks.append((f"{mcp_path.name} (Claude project MCP)", True,
                               f"missing (OK unless using Claude subagents)"))
            else:
                try:
                    pdata = json.loads(mcp_path.read_text())
                    if "agent-brain" in (pdata.get("mcpServers") or {}):
                        claude_project_mcp = True
                        checks.append((f"{mcp_path.name} (Claude project MCP)", True,
                                       "agent-brain registered"))
                    else:
                        checks.append((f"{mcp_path.name} (Claude project MCP)", False,
                                       "exists but no agent-brain entry"))
                except Exception as e:
                    checks.append((f"{mcp_path.name} (Claude project MCP)", False,
                                   f"parse error: {e}"))

            # 9. .claude/settings.local.json
            slp = proj / ".claude" / "settings.local.json"
            if not slp.exists():
                checks.append(("Project Claude settings.local.json", True,
                               "missing (OK unless using Claude subagents)"))
            else:
                try:
                    sdata = json.loads(slp.read_text())
                    enable_all = sdata.get("enableAllProjectMcpServers")
                    allowlist = sdata.get("enabledMcpjsonServers") or []
                    in_allow = isinstance(allowlist, list) and "agent-brain" in allowlist
                    if enable_all and in_allow:
                        checks.append(("Project Claude settings.local.json", True,
                                       "MCP enabled + agent-brain allowed"))
                    elif not claude_project_mcp:
                        checks.append(("Project Claude settings.local.json", True,
                                       "present but Claude project MCP is not linked"))
                    else:
                        problems = []
                        if not enable_all:
                            problems.append("enableAllProjectMcpServers != true")
                        if not in_allow:
                            problems.append("agent-brain not in enabledMcpjsonServers")
                        checks.append(("Project Claude settings.local.json", False,
                                       "; ".join(problems)))
                except Exception as e:
                    checks.append(("Project Claude settings.local.json", False,
                                   f"parse error: {e}"))

            agents_md = proj / "AGENTS.md"
            if not agents_md.exists():
                checks.append(("Project Codex AGENTS.md", True,
                               f"missing (run `setup.sh --link-project={proj}` for project guidance)"))
            else:
                try:
                    text = agents_md.read_text(errors="replace")
                    if "agent-brain:codex-protocol" in text or "Agent Brain Protocol" in text:
                        checks.append(("Project Codex AGENTS.md", True,
                                       "agent-brain guidance present"))
                    else:
                        checks.append(("Project Codex AGENTS.md", True,
                                       "exists without agent-brain block"))
                except Exception as e:
                    checks.append(("Project Codex AGENTS.md", False,
                                   f"read error: {e}"))

            # 10. .gitignore (informational)
            gi = proj / ".gitignore"
            wanted = {".mcp.json", ".san/.san_hashes.json", ".san/_cache/"}
            if not gi.exists():
                checks.append(("Project .gitignore", True,
                               "no .gitignore — skipping"))
            else:
                lines = {ln.strip() for ln in gi.read_text().splitlines()}
                missing_gi = sorted(wanted - lines)
                if missing_gi:
                    # Informational — not all repos want .mcp.json gitignored
                    checks.append(("Project .gitignore", True,
                                   f"missing entries (consider adding): "
                                   f"{', '.join(missing_gi)}"))
                else:
                    checks.append(("Project .gitignore", True, "covers brain artifacts"))

    # Render
    print("agent-brain diagnose")
    print("=" * 60)
    failed = 0
    for label, ok, msg in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  [{mark}] {label}: {msg}")
    print("=" * 60)
    if failed:
        print(f"{failed} check(s) failed.")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    import sys as _sys
    cmd = _sys.argv[1] if len(_sys.argv) > 1 else ""

    if cmd == "diagnose":
        proj = ""
        for a in _sys.argv[2:]:
            if a.startswith("--project="):
                proj = a.split("=", 1)[1]
            elif a in ("--help", "-h"):
                print("Usage: server.py diagnose [--project=<path>]")
                _sys.exit(0)
            else:
                print(f"Unknown diagnose flag: {a}")
                _sys.exit(2)
        _sys.exit(_diagnose(project=proj))

    # Admin commands moved off MCP to keep the tool surface lean.
    if cmd == "validate":
        print(validate_brain())
        _sys.exit(0)
    if cmd == "validate-san":
        print(validate_san_system())
        _sys.exit(0)
    if cmd == "san-index":
        if len(_sys.argv) < 3:
            print("Usage: server.py san-index <repo>")
            _sys.exit(2)
        print(update_san_index(_sys.argv[2]))
        _sys.exit(0)
    if cmd == "stats":
        print(brain_stats())
        _sys.exit(0)
    if cmd == "office":
        print(office_state(_sys.argv[2] if len(_sys.argv) > 2 else ""))
        _sys.exit(0)
    if cmd == "roadmap":
        # Open-work digest for the SessionStart/compact hook (and humans).
        _repo = _sys.argv[2] if len(_sys.argv) > 2 else ""
        with LOCK:
            _G = _load_graph()
        print(_format_roadmap_digest(_roadmap_rows(_G, _repo)))
        _sys.exit(0)
    if cmd == "savings":
        # CLI runs in its own process: "this session" counters are empty here,
        # so report the most recent recorded session instead.
        events = _load_savings_events()
        last = {"reads": 0, "raw_tokens": 0, "san_tokens": 0}
        if events:
            last_id = events[-1].get("session")
            ses = [e for e in events if e.get("session") == last_id]
            last = {"reads": len(ses),
                    "raw_tokens": sum(e.get("raw_tokens", 0) for e in ses),
                    "san_tokens": sum(e.get("san_tokens", 0) for e in ses)}
        report = _format_savings_report(last, events,
                                        datetime.now().strftime("%Y-%m-%d"))
        print(report.replace("This session", "Last recorded session", 1))
        _sys.exit(0)
    if cmd == "metrics":
        # The honest scorecard: recall, net tokens, usage.
        print(_metrics_report())
        _sys.exit(0)
    if cmd == "adapter":
        # Emit host-specific setup so the SAME brain works across runtimes.
        # Usage: server.py adapter [codex|claude|show]
        target = _sys.argv[2] if len(_sys.argv) > 2 else "show"
        _pybin = _sys.executable
        _server = str(Path(__file__).resolve())
        if target == "codex":
            print("# Easiest path:")
            print("#   ./setup.sh")
            print("#")
            print("# Manual MCP-only config for ~/.codex/config.toml:\n")
            print("[mcp_servers.agent-brain]")
            print(f'command = "{_pybin}"')
            print(f'args = ["{_server}"]\n')
            print("# Then restart Codex; /mcp should show agent-brain.")
            print("# All brain TOOLS work (pre_check/log_decision/get_san/…) and")
            print("# Codex reads the MCP `instructions` field as its standing protocol.")
            print("#")
            print("# For full Codex support, ./setup.sh also writes")
            print("# ~/.codex/hooks.json for decision gating, roadmap injection,")
            print("# and Read/Bash->SAN nudges. Review/trust hooks with /hooks.")
        elif target == "claude":
            print("# Claude Code: run ./setup.sh (registers MCP + all 5 hooks +")
            print("# the CLAUDE.md tool-ladder). Or register the MCP server only:")
            print(f'claude mcp add --transport stdio --scope user agent-brain -- '
                  f'"{_pybin}" "{_server}"')
        else:
            print("agent-brain adapters — one brain, many runtimes:\n")
            print("  claude  ->  hooks-enforced (setup.sh): decision-gate, amnesia")
            print("              re-inject on compaction, Read/Bash->SAN routing.")
            print("  codex   ->  MCP-native tools + Codex hooks via ./setup.sh.")
            print("\n  What travels across BOTH: the decision graph, SAN, all MCP tools,")
            print("  and the standing protocol (the MCP `instructions` field).")
            print("  What's runtime-specific: install files and hook trust UX. The")
            print("  knowledge and protocol stay portable.")
            print("\nUsage: server.py adapter [codex|claude|show]")
        _sys.exit(0)
    if cmd == "records":
        # Export the human-readable per-day decision records.
        _repo = _sys.argv[2] if len(_sys.argv) > 2 else ""
        print(export_records.fn(_repo) if hasattr(export_records, "fn")
              else export_records(_repo))
        _sys.exit(0)
    if cmd == "clear-activity":
        # Wipe the Activity-feed messages (stale conversations). Agents kept.
        with OFFICE_LOCK:
            _st = _load_office_state()
            _n = len(_st.get("messages", []))
            _st["messages"] = []
            _save_office_state(_st)
        print(f"Cleared {_n} Activity-feed message(s). Agents left intact.")
        _sys.exit(0)
    if cmd == "prune":
        # Forget old resolved decisions. DRY-RUN unless --apply is passed.
        _repo = next((a for a in _sys.argv[2:] if not a.startswith("--")), "")
        _apply = "--apply" in _sys.argv[2:]
        _days = 90
        for a in _sys.argv[2:]:
            if a.startswith("--before-days="):
                try:
                    _days = int(a.split("=", 1)[1])
                except ValueError:
                    pass
        fn = prune_decisions.fn if hasattr(prune_decisions, "fn") else prune_decisions
        print(fn(repo=_repo, before_days=_days, dry_run=not _apply))
        if not _apply:
            print("\n(dry run — add --apply to archive + remove)")
        _sys.exit(0)
    if cmd == "resolve-stale":
        _repo = next((a for a in _sys.argv[2:] if not a.startswith("--")), "")
        _apply = "--apply" in _sys.argv[2:]
        fn = resolve_stale_pending.fn if hasattr(resolve_stale_pending, "fn") else resolve_stale_pending
        print(fn(before_days=30, repo=_repo, dry_run=not _apply))
        _sys.exit(0)
    if cmd in ("--help", "-h", "help"):
        print("Usage: server.py [diagnose|validate|validate-san|san-index <repo>|"
              "stats|office [repo]|savings|metrics|adapter [codex|claude]|"
              "roadmap [repo]|records [repo]|"
              "prune [repo] [--before-days=N] [--apply]|resolve-stale [repo] [--apply]|"
              "clear-activity]")
        print("  (no args)        run the MCP server")
        _sys.exit(0)

    mcp.run()
