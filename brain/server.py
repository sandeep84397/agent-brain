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
import json
import sqlite3
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
    Check which SAN files are stale (source newer than SAN) or missing.

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
        return f"No .san/ directory in '{repo}'. Run brain-compiler to generate SAN files."

    index = _load_san_index(san_dir)
    if not index:
        return f".san/ exists but _index.json is empty or missing. Run update_san_index first."

    stale = []
    missing = []
    fresh = 0

    for qualified_name, meta in index.items():
        source_rel = meta.get("file", "")
        if not source_rel:
            continue
        source_path = repo_path / source_rel
        san_path = _source_to_san_path(san_dir, source_rel)

        if not san_path.exists():
            missing.append(source_rel)
        elif not source_path.exists():
            # Source deleted but SAN remains — stale
            stale.append(f"{source_rel} (source deleted)")
        else:
            src_mtime = source_path.stat().st_mtime
            san_mtime = san_path.stat().st_mtime
            if src_mtime > san_mtime:
                stale.append(source_rel)
            else:
                fresh += 1

    lines = [f"SAN freshness for '{repo}':", f"  Fresh: {fresh}", f"  Stale: {len(stale)}",
             f"  Missing: {len(missing)}"]
    if stale:
        lines.append("\nStale (need re-compilation):")
        for s in stale[:20]:
            lines.append(f"  - {s}")
        if len(stale) > 20:
            lines.append(f"  ... and {len(stale) - 20} more")
    if missing:
        lines.append("\nMissing SAN files:")
        for m in missing[:20]:
            lines.append(f"  - {m}")
        if len(missing) > 20:
            lines.append(f"  ... and {len(missing) - 20} more")
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
    san_dir = _get_san_dir(repo)
    if not san_dir:
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
    san_dir = _get_san_dir(repo)
    if not san_dir:
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


if __name__ == "__main__":
    mcp.run()
