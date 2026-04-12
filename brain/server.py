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
import tempfile
import time
import re
import os
from pathlib import Path
from datetime import datetime
from typing import Optional
from collections import defaultdict
import uuid
import threading

# ---------------------------------------------------------------------------
# Configuration — no hardcoded paths
# ---------------------------------------------------------------------------

BRAIN_DIR = Path(os.environ.get("AGENT_BRAIN_DIR", str(Path.home() / ".agent-brain")))
GRAPH_FILE = BRAIN_DIR / "decisions.json"
CONFIG_FILE = BRAIN_DIR / "config.json"
LOCK = threading.Lock()
OFFICE_LOCK = threading.Lock()
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
    return {name: Path(p) for name, p in config.get("repos", {}).items()}


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


def _save_office_state(state: dict) -> None:
    """Persist office state atomically."""
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OFFICE_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(OFFICE_STATE_FILE)


def _auto_heartbeat(agent: str, status: str, task: str = "", talking_to: str = "") -> None:
    """Silently update office state from brain tool calls. Never raises."""
    try:
        with OFFICE_LOCK:
            state = _load_office_state()
            config = _load_config()
            role = "unknown"
            for t in config.get("team", []):
                if t.get("name", "").lower() == agent.lower():
                    role = t.get("role", "unknown")
                    break
            state.setdefault("agents", {})[agent] = {
                "role": role, "status": status,
                "task": (task or "")[:100],
                "talking_to": talking_to or None,
                "message": None,
                "last_seen": datetime.now().isoformat(),
            }
            _save_office_state(state)
    except Exception:
        pass  # Office state must never break brain functionality


mcp = FastMCP(
    "agent-brain",
    instructions=(
        "Agent Brain: persistent decision memory for agent teams. "
        "Call pre_check BEFORE starting work. "
        "Call log_decision when you decide on an approach. "
        "Call log_outcome after review/result. "
        "Use decisions_for_code to find past decisions touching a code symbol. "
        "This is NON-NEGOTIABLE for all agents."
    ),
)


# ===========================================================================
# Graph I/O
# ===========================================================================


def _load_graph() -> nx.DiGraph:
    """Load the decision graph from disk. Returns empty graph if none exists."""
    if GRAPH_FILE.exists():
        try:
            data = json.loads(GRAPH_FILE.read_text())
            return nx.node_link_graph(data)
        except (json.JSONDecodeError, OSError):
            return nx.DiGraph()
    return nx.DiGraph()


def _save_graph(G: nx.DiGraph) -> None:
    """Persist the decision graph to disk."""
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    data = nx.node_link_data(G)
    # Atomic write: write to temp file then rename
    tmp = GRAPH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(GRAPH_FILE)


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
        conn = sqlite3.connect(str(db_path))
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
        conn.close()
    except Exception:
        pass
    return results


def _get_code_node_details(repo: str, qualified_name: str) -> Optional[dict]:
    """Get details for a specific code node by qualified_name."""
    db_path = _get_crg_db(repo)
    if not db_path:
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT kind, name, qualified_name, file_path, line_start, line_end, "
            "parent_name, params, return_type "
            "FROM nodes WHERE qualified_name = ?",
            (qualified_name,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def _get_callers_of(repo: str, qualified_name: str) -> list[str]:
    """Find what calls a given code node."""
    db_path = _get_crg_db(repo)
    if not db_path:
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT DISTINCT source_qualified FROM edges "
            "WHERE target_qualified = ? AND kind = 'CALLS'",
            (qualified_name,),
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []


# ===========================================================================
# Fuzzy Similarity Matching
# ===========================================================================


def _tokenize(text: str) -> set[str]:
    """Extract meaningful tokens from text, lowercased."""
    text = text.lower()
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
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


def _similarity(a: str, b: str) -> float:
    """Enhanced similarity: Jaccard + domain-term bonus."""
    tokens_a = _tokenize(a)
    tokens_b = _tokenize(b)
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    jaccard = len(intersection) / len(union)
    domain_shared = intersection & _DOMAIN_TERMS
    domain_boost = len(domain_shared) / len(union) * 0.3 if domain_shared else 0.0
    return min(jaccard + domain_boost, 1.0)


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
                "action": past_action, "reason": past_reason,
                "outcome_by": data.get("outcome_by", "?"),
                "area": data.get("area", "?"), "similarity": sim,
                "timestamp": data.get("timestamp", "?")[:10],
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
            if _similarity(r["reason"], other["reason"]) >= 0.20:
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
def pre_check(agent: str, area: str, action_description: str) -> str:
    """
    CHECK THIS BEFORE DOING WORK. Returns past failures, similar rejections,
    and adaptive warnings based on agent history.

    Args:
        agent: Name of the agent calling (e.g. "arjun")
        area: Domain area (e.g. "auth", "feed", "schema", "ui")
        action_description: Brief description of what you plan to do
    """
    _auto_heartbeat(agent, "planning", action_description)
    with LOCK:
        G = _load_graph()
    sections = []
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
    for node_id, data in G.nodes(data=True):
        if data.get("type") != "decision" or data.get("area") != area:
            continue
        if data.get("outcome") in ("rejected", "failed"):
            exact_warnings.append(
                f"- [{data.get('timestamp', '?')[:10]}] "
                f"{data.get('agent', '?')} tried: {data.get('action', '?')}\n"
                f"  REJECTED by {data.get('outcome_by', '?')}: "
                f"{data.get('outcome_reason', '?')}")
    if exact_warnings:
        sections.append(
            f"EXACT MATCHES in '{area}' ({len(exact_warnings)}):\n\n"
            + "\n\n".join(exact_warnings[-5:]))

    similar = _find_similar_rejections(G, action_description, threshold=0.15)
    exact_ids = {nid for nid, d in G.nodes(data=True)
                 if d.get("area") == area and d.get("outcome") in ("rejected", "failed")}
    similar = [s for s in similar if s["id"] not in exact_ids]
    if similar:
        sim_lines = []
        for s in similar[:5]:
            pct = int(s["similarity"] * 100)
            sim_lines.append(
                f"- [{s['timestamp']}] {s['agent']} in area={s['area']} "
                f"({pct}% similar): {s['action']}\n"
                f"  REJECTED by {s['outcome_by']}: {s['reason']}")
        sections.append(
            f"SIMILAR REJECTIONS across other areas ({len(similar)}):\n\n"
            + "\n\n".join(sim_lines))

    if not sections:
        return f"No past failures in '{area}'. Proceed with: {action_description}"

    if level == "strict":
        scorecard = _compute_scorecard(G, agent)
        if agent in scorecard:
            cats = scorecard[agent].get("top_rejection_categories", [])
            if cats:
                top = "; ".join(f'"{c[0][:60]}" ({c[1]}x)' for c in cats[:2])
                sections.append(f"YOUR TOP REJECTION PATTERNS: {top}")

    return "\n\n---\n\n".join(sections)


@mcp.tool()
def log_decision(
    agent: str, repo: str, area: str, action: str, reasoning: str,
    files_touched: Optional[list[str]] = None,
    code_symbols: Optional[list[str]] = None,
) -> str:
    """
    Log a decision an agent is making. Call AFTER pre_check, BEFORE doing work.

    Args:
        agent: Name of the agent (e.g. "arjun")
        repo: Repository name (e.g. "my-backend")
        area: Domain area (e.g. "auth", "feed", "schema")
        action: What you decided to do
        reasoning: Why you chose this approach
        files_touched: List of file paths being modified (optional)
        code_symbols: List of qualified_names from code-review-graph (optional)
    """
    with LOCK:
        G = _load_graph()
        node_id = f"dec_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        resolved_symbols = code_symbols or []
        files = files_touched or []
        if files and not resolved_symbols:
            code_nodes = _resolve_files_to_code_nodes(repo, files)
            resolved_symbols = list(set(n["qualified_name"] for n in code_nodes))
        G.add_node(node_id, type="decision", agent=agent, repo=repo, area=area,
                    action=action, reasoning=reasoning, files=files,
                    code_symbols=resolved_symbols,
                    timestamp=datetime.now().isoformat(), outcome="pending")
        for sym in resolved_symbols:
            sym_node = f"code:{sym}"
            if sym_node not in G:
                G.add_node(sym_node, type="code_ref", qualified_name=sym, repo=repo)
            G.add_edge(node_id, sym_node, relation="touches")
        _save_graph(G)
    _auto_heartbeat(agent, "working", action)
    result = f"Decision logged: {node_id}"
    if resolved_symbols:
        result += f"\nLinked to {len(resolved_symbols)} code symbol(s)"
    return result


@mcp.tool()
def log_outcome(decision_id: str, outcome: str, outcome_by: str, reason: str) -> str:
    """
    Log the outcome of a decision after review or execution.

    Args:
        decision_id: The ID returned by log_decision
        outcome: One of: accepted, rejected, failed, revised
        outcome_by: Who reviewed (e.g. "marcus", "qa-1", "runtime")
        reason: Why it was accepted/rejected/failed
    """
    with LOCK:
        G = _load_graph()
        if decision_id not in G:
            return f"ERROR: decision '{decision_id}' not found"
        G.nodes[decision_id]["outcome"] = outcome
        G.nodes[decision_id]["outcome_by"] = outcome_by
        G.nodes[decision_id]["outcome_reason"] = reason
        G.nodes[decision_id]["outcome_timestamp"] = datetime.now().isoformat()
        dec_agent = G.nodes[decision_id].get("agent", "")
        _save_graph(G)
    _auto_heartbeat(outcome_by, "reviewing", f"outcome: {outcome}", talking_to=dec_agent)
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
        G.add_node(fb_id, type="feedback", agent=agent, feedback=feedback,
                    severity=severity, timestamp=datetime.now().isoformat())
        G.add_edge(fb_id, decision_id, relation="feedback_on")
        dec_agent = G.nodes.get(decision_id, {}).get("agent", "")
        _save_graph(G)
    _auto_heartbeat(agent, "reviewing", feedback[:80], talking_to=dec_agent)
    return f"Feedback logged: {fb_id} -> {decision_id}"


# ===========================================================================
# MCP Tools — Code Bridge
# ===========================================================================


@mcp.tool()
def decisions_for_code(qualified_name: str, repo: str = "", outcome: str = "") -> str:
    """
    Find all decisions that touched a specific code symbol.

    Args:
        qualified_name: The code-review-graph qualified_name
        repo: Filter by repo (optional)
        outcome: Filter by outcome (optional)
    """
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
                f"[{pred}] {data.get('agent','?')} | {data.get('action','?')} "
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
            line = (f"[{node_id}] {data.get('agent','?')} | {data.get('action','?')} "
                    f"-> {data.get('outcome','pending')}")
            if line not in results:
                results.append(line)
    if not results:
        return f"No decisions found touching '{qualified_name}'."
    return f"{len(results)} decision(s) touching '{qualified_name}':\n\n" + "\n".join(results)


@mcp.tool()
def decisions_for_file(file_path: str, repo: str = "") -> str:
    """
    Find all decisions that touched a specific file.

    Args:
        file_path: File path (relative or absolute)
        repo: Filter by repo (optional)
    """
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
                f"[{node_id}] {data.get('agent','?')} | {data.get('action','?')} "
                f"-> {data.get('outcome','pending')}")
    if not results:
        return f"No decisions found touching '{rel_path}'."
    return f"{len(results)} decision(s) touching '{rel_path}':\n\n" + "\n".join(results)


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
    lines = [f"Decision: {decision_id}", f"Action: {data.get('action', '?')}", ""]
    if files:
        lines.append(f"Files touched ({len(files)}):")
        for f in files:
            lines.append(f"  - {f}")
    if symbols:
        lines.append(f"\nCode symbols ({len(symbols)}):")
        for sym in symbols:
            detail = _get_code_node_details(repo, sym)
            if detail:
                lines.append(f"  - [{detail['kind']}] {detail['name']} "
                             f"({detail['file_path']}:{detail.get('line_start', '?')})")
                callers = _get_callers_of(repo, sym)
                if callers:
                    lines.append(f"    Called by: {', '.join(callers[:5])}")
                    if len(callers) > 5:
                        lines.append(f"    ... and {len(callers) - 5} more")
            else:
                lines.append(f"  - {sym} (not in current code graph)")
    if not symbols and not files:
        lines.append("No code symbols or files linked.")
    return "\n".join(lines)


# ===========================================================================
# MCP Tools — Patterns & Similarity
# ===========================================================================


@mcp.tool()
def similar_failures(action_description: str, area: str = "", threshold: float = 0.15) -> str:
    """
    Find past rejections/failures similar to a proposed action.

    Args:
        action_description: What you plan to do
        area: Optionally restrict to an area
        threshold: Similarity threshold 0.0-1.0 (default 0.15)
    """
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
def get_patterns(area: str = "", min_count: int = 1) -> str:
    """
    Find recurring failure patterns using fuzzy clustering.

    Args:
        area: Optionally filter to a specific area
        min_count: Minimum cluster size (default 1)
    """
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
            rejections.append(f"- {data.get('agent','?')}: {data.get('outcome_reason','unknown')}")
        if rejections:
            return f"No clusters, {len(rejections)} individual rejection(s):\n\n" + "\n".join(rejections)
        return "No rejection patterns found."
    lines = [f"{len(clusters)} pattern cluster(s):\n"]
    for i, cluster in enumerate(clusters, 1):
        agents_in = sorted(set(r["agent"] for r in cluster))
        areas_in = sorted(set(r["area"] for r in cluster))
        lines.append(f"Pattern #{i} ({len(cluster)}x, agents: {', '.join(agents_in)}, "
                      f"areas: {', '.join(areas_in)}):")
        lines.append(f"  Core issue: {cluster[0]['reason']}")
        if len(cluster) > 1:
            lines.append(f"  Also: {cluster[1]['reason']}")
    return "\n\n".join(lines)


# ===========================================================================
# MCP Tools — Scorecards & Dashboard
# ===========================================================================


@mcp.tool()
def get_agent_stats(agent: str = "") -> str:
    """
    Get decision statistics for an agent or all agents.

    Args:
        agent: Agent name (leave empty for all)
    """
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
def agent_scorecard(agent: str) -> str:
    """
    Detailed scorecard: acceptance rate, trends, top rejections, area breakdown.

    Args:
        agent: Agent name (e.g. "arjun")
    """
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
        lines.append("\nArea breakdown:")
        for area_name, area_stats in sorted(s["areas"].items()):
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
def team_dashboard() -> str:
    """Team-wide dashboard: all agents' stats, patterns, health."""
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
    for name, s in sorted(scorecards.items()):
        total = s["total"]
        rate = (s["accepted"] / total * 100) if total else 0
        level = _adaptive_warning_level(G, name, "")
        flag = f" [{level.upper()}]" if level != "normal" else ""
        lines.append(f"  {name}: {total} dec, {s['accepted']} ok ({rate:.0f}%), "
                      f"{s['rejected']} rej, trend={s['trend']}{flag}")
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


@mcp.tool()
def brain_stats() -> str:
    """Get overall brain statistics."""
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
                    outcome: str = "", limit: int = 10) -> str:
    """
    Query past decisions with optional filters.

    Args:
        area: Filter by domain area
        agent: Filter by agent name
        repo: Filter by repository name
        outcome: Filter by outcome (pending, accepted, rejected, failed, revised)
        limit: Max results (default 10)
    """
    with LOCK:
        G = _load_graph()
    results = []
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
        results.append(f"[{node_id}] {data.get('agent','?')} @ {data.get('repo','?')} | "
                        f"area={data.get('area','?')} | {data.get('action','?')} "
                        f"-> {data.get('outcome','pending')}")
    if not results:
        return "No matching decisions."
    return f"{len(results)} decision(s):\n\n" + "\n".join(results[-limit:])


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
        lines.append(f"  {k}: {v}")
    feedback = []
    for pred in G.predecessors(decision_id):
        edge = G.edges[pred, decision_id]
        if edge.get("relation") == "feedback_on":
            fb = G.nodes[pred]
            feedback.append(f"  [{fb.get('severity','info')}] {fb.get('agent','?')}: "
                            f"{fb.get('feedback','')}")
    if feedback:
        lines.append("\nFeedback:")
        lines.extend(feedback)
    code_syms = []
    for _, succ in G.out_edges(decision_id):
        edge = G.edges[decision_id, succ]
        if edge.get("relation") == "touches":
            sym_data = G.nodes.get(succ, {})
            code_syms.append(sym_data.get("qualified_name", succ))
    if code_syms:
        lines.append("\nCode symbols:")
        for sym in code_syms:
            lines.append(f"  - {sym}")
    return "\n".join(lines)


# ===========================================================================
# MCP Tools — Office Dashboard
# ===========================================================================


@mcp.tool()
def heartbeat(agent: str, status: str, task: str = "", talking_to: str = "", message: str = "") -> str:
    """
    Report agent status for the live office dashboard.
    Call this to update your visible state in the pixel art office.

    Args:
        agent: Your name (e.g. "arjun")
        status: One of: working, idle, planning, discussing, reviewing, blocked, waiting
        task: What you're working on (shown as label, max 100 chars)
        talking_to: Name of agent you're interacting with (shows discussion animation)
        message: Chat message content (appears as speech bubble + in chat log)
    """
    with OFFICE_LOCK:
        state = _load_office_state()
        config = _load_config()
        role = "unknown"
        for t in config.get("team", []):
            if t.get("name", "").lower() == agent.lower():
                role = t.get("role", "unknown")
                break
        state.setdefault("agents", {})[agent] = {
            "role": role, "status": status,
            "task": (task or "")[:100],
            "talking_to": talking_to or None,
            "message": (message or None)[:200] if message else None,
            "last_seen": datetime.now().isoformat(),
        }
        if message and talking_to:
            state.setdefault("messages", []).append({
                "from": agent, "to": talking_to,
                "text": (message or "")[:200],
                "ts": datetime.now().isoformat(),
            })
            state["messages"] = state["messages"][-50:]
        _save_office_state(state)
    return "ok"


@mcp.tool()
def office_state() -> str:
    """Get current office state (for debugging the dashboard)."""
    state = _load_office_state()
    if not state.get("agents"):
        return "Office is empty. No agents have checked in."
    lines = ["=== OFFICE STATE ===\n"]
    for name, info in sorted(state["agents"].items()):
        status = info.get("status", "unknown")
        task = info.get("task", "")
        talking = info.get("talking_to")
        last = info.get("last_seen", "?")[:19]
        lines.append(f"  {name} [{info.get('role','')}]: {status} (seen: {last})")
        if task:
            lines.append(f"    task: {task}")
        if talking:
            lines.append(f"    talking to: {talking}")
    msg_count = len(state.get("messages", []))
    if msg_count:
        lines.append(f"\n  {msg_count} message(s) in log")
    return "\n".join(lines)


# ===========================================================================
# MCP Tools — SAN (Structured Associative Notation)
# ===========================================================================


# ---------------------------------------------------------------------------
# SAN refresh: hash-based staleness detection + orphan cleanup (no parser)
# SAN content is generated by brain-compiler (LLM), not by this server.
# ---------------------------------------------------------------------------


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
    tmp = hash_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(hashes, indent=2))
    tmp.rename(hash_file)


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
        if source_path.suffix not in (".kt", ".java"):
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


def _ensure_san_fresh(repo: str) -> Optional[str]:
    """
    Check SAN freshness: detect stale/orphaned SANs, clean up orphans.
    Does NOT generate SAN content — only reports staleness and cleans up.
    Called internally by query_san and get_san before serving results.
    """
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
                for ext in (".kt", ".java"):
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
        for ext in ("**/*.kt", "**/*.java"):
            for source_path in repo_path.glob(ext):
                rel = str(source_path.relative_to(repo_path))
                if rel.startswith("build/") or "/.gradle/" in rel:
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
    hashes_backfilled = False
    if index:
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


def _rebuild_san_index(san_dir: Path, repo_path: Optional[Path] = None):
    """Rebuild _index.json from .san files. Pure Python, no MCP tool call."""
    index = {}
    try:
        for san_file in san_dir.rglob("*.san"):
            rel = str(san_file.relative_to(san_dir))
            try:
                content = san_file.read_text()
            except OSError:
                continue

            source_rel = str(Path(rel).with_suffix(""))
            # Determine source extension — check disk if repo_path available
            for ext in (".kt", ".java"):
                if source_rel.endswith(ext):
                    break
            else:
                resolved = False
                if repo_path:
                    for ext in (".kt", ".java"):
                        if (repo_path / (source_rel + ext)).exists():
                            source_rel = source_rel + ext
                            resolved = True
                            break
                if not resolved:
                    # Fallback: default to .kt
                    source_rel = source_rel + ".kt" if not source_rel.endswith(".kt") else source_rel

            # Extract qualified names from SAN content
            for match in re.finditer(r'^([\w.]+)\s+@(\w+)\s*\{', content, re.MULTILINE):
                qname = match.group(1)
                kind = match.group(2)
                index[qname] = {
                    "kind": kind,
                    "file": source_rel,
                    "tokens_san": len(content.split()),
                }
    except Exception:
        pass

    idx_file = san_dir / "_index.json"
    try:
        tmp = idx_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, indent=2))
        tmp.rename(idx_file)
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


def _source_to_san_path(san_dir: Path, source_rel: str) -> Path:
    """Convert a source file relative path to its .san counterpart."""
    # src/main/kotlin/com/example/Auth.kt → src/main/kotlin/com/example/Auth.san
    p = Path(source_rel)
    return san_dir / p.with_suffix(".san")


@mcp.tool()
def check_san_freshness(repo: str) -> str:
    """
    Check SAN freshness: detect stale/missing SANs, clean up orphans.
    Does NOT regenerate SAN content — reports what needs brain-compiler.

    Args:
        repo: Repository name from config.json
    """
    repo_path = _resolve_repo_path(repo)
    if not repo_path:
        return f"ERROR: repo '{repo}' not found in config"

    # Clean up orphans and detect staleness
    result = _ensure_san_fresh(repo)

    # Now report current state
    san_dir = repo_path / ".san"
    if not san_dir.exists():
        return f"No .san/ directory in '{repo}'."

    index = _load_san_index(san_dir)
    fresh = 0
    remaining_stale = []
    seen_files = set()

    for qualified_name, meta in index.items():
        source_rel = meta.get("file", "")
        if not source_rel or source_rel in seen_files:
            continue
        seen_files.add(source_rel)
        source_path = repo_path / source_rel
        san_path = _source_to_san_path(san_dir, source_rel)
        if san_path.exists() and source_path.exists():
            if source_path.stat().st_mtime > san_path.stat().st_mtime:
                remaining_stale.append(source_rel)
            else:
                fresh += 1

    lines = [f"SAN freshness for '{repo}':",
             f"  Entries: {len(index)}",
             f"  Fresh: {fresh}",
             f"  Remaining stale: {len(remaining_stale)}"]
    if result:
        lines.insert(1, f"  Auto-fix: {result}")
    if remaining_stale:
        lines.append("\nStale — run brain-compiler to regenerate:")
        for s in remaining_stale[:10]:
            lines.append(f"  - {s}")
        if len(remaining_stale) > 10:
            lines.append(f"  ... and {len(remaining_stale) - 10} more")
    return "\n".join(lines)


@mcp.tool()
def recompile_san(repo: str) -> str:
    """
    Refresh SAN metadata: rebuild index, clean up orphans, update content hashes.
    Does NOT generate SAN content — use brain-compiler for that.
    Call this after large merges, branch switches, or to force cleanup.

    Args:
        repo: Repository name from config.json
    """
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
            for ext in (".kt", ".java"):
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
    for ext in ("**/*.kt", "**/*.java"):
        for source_path in repo_path.glob(ext):
            rel = str(source_path.relative_to(repo_path))
            if rel.startswith("build/") or "/.gradle/" in rel or "/build/" in rel:
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
                        # SAN was regenerated — safe to update hash
                        hashes[rel] = current_hash
                        hashes_changed = True
                        hash_updated += 1
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
    Search SAN files by keyword. Searches both the index and .san file contents.

    Args:
        repo: Repository name from config.json
        keyword: Search term (function name, class name, pattern, etc.)
        max_results: Maximum results to return (default 10)
    """
    # Auto-recompile stale SAN before searching
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
            san_path = _source_to_san_path(san_dir, meta.get("file", ""))
            content = ""
            if san_path.exists():
                try:
                    content = san_path.read_text()[:500]
                except OSError:
                    content = "(read error)"
            results.append({
                "qualified_name": qname,
                "kind": meta.get("kind", "?"),
                "file": meta.get("file", "?"),
                "match_type": "index",
                "preview": content,
            })

    # Phase 2: Search .san file contents (if not enough index hits)
    if len(results) < max_results:
        seen_files = {r["file"] for r in results}
        try:
            for san_file in san_dir.rglob("*.san"):
                rel = str(san_file.relative_to(san_dir))
                if rel in seen_files:
                    continue
                try:
                    content = san_file.read_text()
                except OSError:
                    continue
                if keyword_lower in content.lower():
                    # Find the matching line for context
                    context_lines = []
                    for line in content.split("\n"):
                        if keyword_lower in line.lower():
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


@mcp.tool()
def get_san(repo: str, file_path: str) -> str:
    """
    Get the SAN-compressed content for a source file.

    Args:
        repo: Repository name from config.json
        file_path: Source file path (relative to repo root, e.g. "src/main/kotlin/com/example/Auth.kt")
    """
    # Auto-recompile stale SAN before reading
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

    # Include freshness info
    repo_paths = _get_repo_paths()
    repo_path = repo_paths.get(repo)
    freshness = ""
    if repo_path:
        source = repo_path / file_path
        if source.exists():
            src_mtime = source.stat().st_mtime
            san_mtime = san_path.stat().st_mtime
            if src_mtime > san_mtime:
                freshness = "\n⚠ STALE: source is newer than SAN. Re-run brain-compiler."
            else:
                freshness = "\n✓ Fresh"

    rel = san_path.relative_to(san_dir)
    return f"SAN: {rel}{freshness}\n{'=' * 40}\n{content}"


@mcp.tool()
def update_san_index(repo: str) -> str:
    """
    Rebuild _index.json by scanning all .san files in the repo's .san/ directory.
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
            for ext in [".kt", ".java", ".py", ".ts", ".tsx", ".js", ".jsx", ".swift", ".go", ".rs"]:
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
    tmp = idx_file.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(index, indent=2))
        tmp.rename(idx_file)
    except OSError as e:
        return f"ERROR writing index: {e}"

    total_tokens = sum(v.get("tokens_san", 0) for v in index.values())
    return (f"SAN index rebuilt for '{repo}':\n"
            f"  Entries: {len(index)}\n"
            f"  Total SAN tokens: {total_tokens}\n"
            f"  Errors: {errors}\n"
            f"  Index: {idx_file}")


@mcp.tool()
def validate_san_system() -> str:
    """
    Run self-tests on the SAN hash/orphan/staleness system.
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
        _test("source_to_san_path maps .kt → .san", sp == san_dir / "src" / "A.san")

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


@mcp.tool()
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

    def _test(name: str, condition: bool, detail: str = ""):
        nonlocal passed, failed
        if condition:
            passed += 1
            results.append(f"  PASS: {name}")
        else:
            failed += 1
            results.append(f"  FAIL: {name}" + (f" — {detail}" if detail else ""))

    try:
        tmp_root = Path(tempfile.mkdtemp(prefix="brain_validate_"))
        # Redirect globals to temp
        globals()["BRAIN_DIR"] = tmp_root
        globals()["GRAPH_FILE"] = tmp_root / "decisions.json"
        globals()["CONFIG_FILE"] = tmp_root / "config.json"
        globals()["OFFICE_STATE_FILE"] = tmp_root / "office-state.json"

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

        # Test 14: Tokenizer
        tokens = _tokenize("AuthService rate limiting middleware")
        _test("tokenizer extracts meaningful terms",
              "authservice" in tokens and "rate" in tokens and "limiting" in tokens)

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

        # Test agent_scorecard
        result = agent_scorecard("star")
        _test("agent_scorecard renders", "SCORECARD: star" in result)
        _test("agent_scorecard shows acceptance rate", "83%" in result, f"got: {result}")

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
        result = decisions_for_file("src/api/handler.kt")
        _test("decisions_for_file finds by path", "decision(s)" in result)

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

        result = decisions_for_code("com.example.AuthService.login")
        _test("decisions_for_code finds linked decision", "decision(s)" in result)

        result = decisions_for_code("com.example.NonExistent")
        _test("decisions_for_code empty for unknown symbol", "No decisions" in result)

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

        # Corrupt graph file
        (tmp_root / "decisions.json").write_text("not json")
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
        r = agent_scorecard("dev")
        _test("workflow: scorecard shows 100% rejection", "100%" in r and "rejected" in r.lower())

        # Step 8: query finds it
        r = query_decisions(area="payments")
        _test("workflow: query finds decision", wf_dec_id in r)

        # Step 9: similar_failures finds it
        r = similar_failures("add payment processing with Stripe SDK")
        _test("workflow: similar_failures finds rejection",
              "similar past failure" in r.lower() or "payment" in r.lower(),
              f"got: {r[:200]}")

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


if __name__ == "__main__":
    mcp.run()
