#!/usr/bin/env python3
"""
Agent Brain Dashboard
- Pixel/3D office: real-time agent team activity (office-state.json).
- Decisions view (/decisions): browse + live-stream decisions as they're
  logged (decisions.json + decisions.journal).

Usage:
    python dashboard/server.py [--port PORT]

Opens http://localhost:3333 in your browser.
Reads ~/.agent-brain/office-state.json and ~/.agent-brain/decisions.json.
"""

import json
import time
import os
import sys
import webbrowser
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

BRAIN_DIR = Path(os.environ.get("AGENT_BRAIN_DIR", str(Path.home() / ".agent-brain")))
STATE_FILE = BRAIN_DIR / "office-state.json"
GRAPH_FILE = BRAIN_DIR / "decisions.json"
JOURNAL_FILE = BRAIN_DIR / "decisions.journal"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Parse --port flag or env var
PORT = int(os.environ.get("OFFICE_PORT", "3333"))
if "--port" in sys.argv:
    try:
        PORT = int(sys.argv[sys.argv.index("--port") + 1])
    except (IndexError, ValueError):
        pass


def load_state():
    """Load current office state from disk."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {"agents": {}, "messages": []}
    return {"agents": {}, "messages": []}


def _graph_sig():
    """mtime+size signature of the decision store (snapshot + journal).
    Changes whenever a decision is logged/outcome'd, so SSE knows to push."""
    sig = []
    for f in (GRAPH_FILE, JOURNAL_FILE):
        try:
            st = f.stat()
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((0, 0))
    return tuple(sig)


def _load_decisions():
    """Read the decision graph the same way the MCP server persists it:
    JSON snapshot + replayed JSONL journal deltas. Self-contained (no MCP
    import — the dashboard is a separate process). Returns a list of decision
    dicts, newest first, each flattened for the UI."""
    nodes = {}
    # 1. snapshot
    if GRAPH_FILE.exists():
        try:
            data = json.loads(GRAPH_FILE.read_text())
            for n in data.get("nodes", []):
                nid = n.get("id")
                if nid:
                    nodes[nid] = {k: v for k, v in n.items() if k != "id"}
        except (json.JSONDecodeError, OSError):
            pass
    # 2. journal deltas (node / del_node ops; edges don't affect decisions list)
    if JOURNAL_FILE.exists():
        try:
            for line in JOURNAL_FILE.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    op = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = op.get("op")
                if kind == "node":
                    nodes[op["id"]] = op.get("data", {})
                elif kind == "del_node":
                    nodes.pop(op.get("id"), None)
        except OSError:
            pass
    # 3. flatten decisions for the UI
    out = []
    for nid, d in nodes.items():
        if d.get("type") != "decision":
            continue
        out.append({
            "id": nid,
            "agent": d.get("agent", "?"),
            "repo": d.get("repo", "?"),
            "area": d.get("area", "?"),
            "action": str(d.get("action", "")),
            "reasoning": str(d.get("reasoning", "")),
            "outcome": d.get("outcome", "pending"),
            "outcome_by": d.get("outcome_by", ""),
            "outcome_reason": str(d.get("outcome_reason", "")),
            "timestamp": d.get("timestamp", ""),
            "outcome_timestamp": d.get("outcome_timestamp", ""),
            "files": d.get("files", []),
            "plan_file": d.get("plan_file", ""),
        })
    out.sort(key=lambda r: r["timestamp"], reverse=True)
    return out


import re
from urllib.parse import urlparse, parse_qs

CONFIG_FILE = BRAIN_DIR / "config.json"
_SAN_HEADER_RE = re.compile(r"^(\S+)\s+@(\w+)\s*\{")
_SAN_SRC_RE = re.compile(r"^\s*src:\s*(\d+)\s*-\s*(\d+)")


def _san_repos():
    """{repo_name: repo_path} from config.json that actually have a .san dir."""
    out = {}
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
        for name, p in (cfg.get("repos") or {}).items():
            if isinstance(p, str) and (Path(p) / ".san").is_dir():
                out[name] = Path(p)
    except (json.JSONDecodeError, OSError):
        pass
    return out


def _load_san_repos():
    """Repo list + index size, for the SAN view's repo picker."""
    repos = []
    for name, root in _san_repos().items():
        idx_file = root / ".san" / "_index.json"
        n_symbols = n_files = 0
        try:
            idx = json.loads(idx_file.read_text())
            n_symbols = len(idx)
            n_files = len({v.get("file") for v in idx.values()
                           if isinstance(v, dict) and v.get("file")})
        except (json.JSONDecodeError, OSError):
            pass
        repos.append({"repo": name, "symbols": n_symbols, "files": n_files})
    return {"repos": sorted(repos, key=lambda r: r["repo"])}


def _san_block_for(san_path, keyword):
    """Parse a .san file's blocks; return blocks whose header/body matches
    keyword, each with its src: line range. Mirrors how get_san resolves a hit."""
    blocks = []
    try:
        lines = san_path.read_text(errors="replace").splitlines()
    except OSError:
        return blocks
    cur = None
    for ln in lines:
        m = _SAN_HEADER_RE.match(ln)
        if m:
            if cur:
                blocks.append(cur)
            cur = {"name": m.group(1), "kind": m.group(2),
                   "src": "", "snippet": [ln]}
        elif cur is not None:
            if len(cur["snippet"]) < 8:
                cur["snippet"].append(ln)
            sm = _SAN_SRC_RE.match(ln)
            if sm and not cur["src"]:
                cur["src"] = f"{sm.group(1)}-{sm.group(2)}"
            if ln.strip() == "}":
                blocks.append(cur)
                cur = None
    if cur:
        blocks.append(cur)
    kw = keyword.lower()
    hits = [b for b in blocks
            if kw in b["name"].lower() or kw in "\n".join(b["snippet"]).lower()]
    return hits


def _parse_san_block(lines, start_idx):
    """Parse one SAN block starting at its header line. Returns
    (block_dict, end_idx). Mirrors the field structure agents read from SAN:
    header (name/kind), src, purpose, impl, deps, fn:/-fn:, @state, @errors,
    @constraint, @threading, patterns, risk."""
    header = _SAN_HEADER_RE.match(lines[start_idx])
    block = {"name": header.group(1), "kind": header.group(2), "src": "",
             "purpose": "", "impl": "", "deps": [], "fns": [], "state": "",
             "errors": "", "constraint": "", "threading": "", "patterns": "",
             "risk": "", "raw": [lines[start_idx]]}
    i = start_idx + 1
    cur_fn = None
    while i < len(lines):
        ln = lines[i]
        if _SAN_HEADER_RE.match(ln):  # next block started
            break
        block["raw"].append(ln)
        s = ln.strip()
        if s == "}":
            i += 1
            break
        # continuation of a fn's impl notes ([...] on the next line)
        if cur_fn is not None and (s.startswith("[") or (s and not s.endswith(":")
                                   and not s.startswith(("fn:", "-fn:", "@"))
                                   and ":" not in s.split(" ")[0])):
            cur_fn["impl"] = (cur_fn.get("impl", "") + " " + s).strip()
            i += 1
            continue
        cur_fn = None
        sm = _SAN_SRC_RE.match(ln)
        if sm:
            block["src"] = f"{sm.group(1)}-{sm.group(2)}"
        elif s.startswith("purpose:"):
            block["purpose"] = s[len("purpose:"):].strip()
        elif s.startswith("impl:"):
            block["impl"] = s[len("impl:"):].strip()
        elif s.startswith("deps:"):
            block["deps"] = [d.strip() for d in
                             s[len("deps:"):].replace("+", ",").split(",") if d.strip()]
        elif s.startswith("fn:") or s.startswith("-fn:"):
            priv = s.startswith("-fn:")
            sig = s[(4 if priv else 3):].strip()
            cur_fn = {"sig": sig, "private": priv, "impl": ""}
            block["fns"].append(cur_fn)
        elif s.startswith("@state:"):
            block["state"] = s[len("@state:"):].strip()
        elif s.startswith("@errors:"):
            block["errors"] = s[len("@errors:"):].strip()
        elif s.startswith("@constraint:"):
            block["constraint"] = s[len("@constraint:"):].strip()
        elif s.startswith("@threading:"):
            block["threading"] = s[len("@threading:"):].strip()
        elif s.startswith("patterns:"):
            block["patterns"] = s[len("patterns:"):].strip()
        elif s.startswith("risk:"):
            block["risk"] = s[len("risk:"):].strip()
        i += 1
    return block, i


def _all_blocks(san_dir):
    """Map every SAN block by qualified name across the repo: {name: block}."""
    blocks = {}
    try:
        for sf in san_dir.rglob("*.san"):
            if sf.name.startswith("_"):
                continue
            rel = str(sf.relative_to(san_dir))
            src_rel = rel[:-4] if rel.endswith(".san") else rel
            try:
                lines = sf.read_text(errors="replace").splitlines()
            except OSError:
                continue
            i = 0
            while i < len(lines):
                if _SAN_HEADER_RE.match(lines[i]):
                    b, i = _parse_san_block(lines, i)
                    b["file"] = src_rel
                    blocks[b["name"]] = b
                else:
                    i += 1
    except OSError:
        pass
    return blocks


def _short_name(qname):
    """Last path segment of a qualified name, for matching deps to symbols."""
    return qname.rsplit(".", 1)[-1].rsplit("/", 1)[-1]


def _resolve_dep(dep, blocks, by_short):
    """Resolve a dep/type token to a known SAN block name, if any. Strips
    generics (List<User> -> User) and matches by short name."""
    token = re.sub(r"[<>\[\](),?]", " ", dep).split()
    for t in token:
        t = t.strip()
        if t in blocks:
            return t
        if t in by_short:
            return by_short[t]
    return None


def _narrate(block, blocks):
    """Deterministic reasoning narrative from a SAN block's own fields —
    the same facts an agent reads. No LLM; this is SAN made explicit."""
    name = _short_name(block["name"])
    parts = []
    kind_word = {"svc": "service", "repo": "repository", "iface": "interface",
                 "model": "data model", "route": "route handler", "vm": "view-model",
                 "usecase": "use case", "util": "utility", "config": "configuration",
                 "fragment": "UI fragment", "activity": "activity", "module": "module",
                 "test": "test", "fn": "function"}.get(block["kind"], block["kind"])
    lead = f"**{name}** is a `@{block['kind']}` ({kind_word})"
    if block["purpose"]:
        lead += f" — {block['purpose']}"
    parts.append(lead + ".")
    if block["impl"]:
        parts.append(f"Implementation: {block['impl']}.")
    if block["deps"]:
        named = ", ".join(f"`{d}`" for d in block["deps"])
        parts.append(f"It depends on {named} — the collaborators it needs to do its job.")
    pub = [f for f in block["fns"] if not f["private"]]
    if pub:
        parts.append(f"It exposes {len(pub)} public function(s):")
        for f in pub[:12]:
            line = f"  • `{f['sig']}`"
            if f["impl"]:
                line += f" — {f['impl'].strip('[]')}"
            parts.append(line)
    priv = [f for f in block["fns"] if f["private"]]
    if priv:
        parts.append(f"Plus {len(priv)} private helper(s): "
                     + ", ".join(f"`{_short_name(f['sig'].split('(')[0])}`" for f in priv[:8]) + ".")
    if block["state"]:
        parts.append(f"State it holds: {block['state']}.")
    if block["errors"]:
        parts.append(f"Failure behavior: {block['errors']}.")
    if block["constraint"]:
        parts.append(f"Constraint: {block['constraint']}.")
    if block["threading"]:
        parts.append(f"Threading: {block['threading']}.")
    if block["patterns"]:
        parts.append(f"Patterns: {block['patterns']}.")
    if block["risk"]:
        parts.append(f"Risk: {block['risk']}.")
    parts.append(f"All of the above comes straight from the SAN block (`src: {block['src']}` "
                 f"in `{block.get('file','?')}`) — this is what the agent reads instead of the raw file.")
    return "\n".join(parts)


def _symbol_tree(qname, blocks, by_short, depth=0, seen=None):
    """Build the expandable understanding tree for a symbol, following deps
    across files up to a small depth. Each node IS the SAN-derived view."""
    if seen is None:
        seen = set()
    block = blocks.get(qname)
    if not block or qname in seen or depth > 2:
        return None
    seen.add(qname)
    node = {
        "name": qname, "short": _short_name(qname), "kind": block["kind"],
        "file": block.get("file", ""), "src": block["src"],
        "purpose": block["purpose"],
        "fns": [{"sig": f["sig"], "private": f["private"], "impl": f["impl"]}
                for f in block["fns"]],
        "state": block["state"], "errors": block["errors"],
        "constraint": block["constraint"], "deps": [],
    }
    for dep in block["deps"]:
        resolved = _resolve_dep(dep, blocks, by_short)
        child = _symbol_tree(resolved, blocks, by_short, depth + 1, seen) if resolved else None
        node["deps"].append({"label": dep, "resolved": bool(child), "child": child})
    return node


def _san_symbol(raw_path):
    """Full understanding view for one symbol: SAN-derived tree + narrative."""
    qs = parse_qs(urlparse(raw_path).query)
    repo = (qs.get("repo") or [""])[0]
    name = (qs.get("name") or [""])[0]
    out = {"repo": repo, "name": name, "tree": None, "narrative": "", "raw": ""}
    repos = _san_repos()
    root = repos.get(repo)
    if not root and repos:
        for n, p in repos.items():
            if repo.lower() in n.lower():
                root, repo = p, n
                break
    if not root:
        return out
    blocks = _all_blocks(root / ".san")
    by_short = {}
    for qn in blocks:
        by_short.setdefault(_short_name(qn), qn)
    # resolve name -> qualified name (exact, then short-name)
    qname = name if name in blocks else by_short.get(_short_name(name))
    if not qname:
        return out
    out["name"] = qname
    out["tree"] = _symbol_tree(qname, blocks, by_short)
    out["narrative"] = _narrate(blocks[qname], blocks)
    out["raw"] = "\n".join(blocks[qname]["raw"])
    return out


def _san_search(raw_path):
    """Trace a SAN query the way query_san does: index lookup (phase 1) then
    content scan (phase 2). Returns a structured trace for the view to render."""
    qs = parse_qs(urlparse(raw_path).query)
    repo = (qs.get("repo") or [""])[0]
    keyword = (qs.get("q") or [""])[0].strip()
    trace = {"repo": repo, "keyword": keyword, "steps": [], "results": []}
    if not keyword:
        return trace
    repos = _san_repos()
    root = repos.get(repo)
    if not root and repos:  # fuzzy: pick first whose name contains the arg
        for n, p in repos.items():
            if repo.lower() in n.lower():
                root, repo = p, n
                break
    if not root:
        trace["steps"].append({"stage": "resolve", "ok": False,
                               "detail": f"no .san for repo '{repo}'"})
        return trace
    san_dir = root / ".san"
    trace["repo"] = repo
    trace["steps"].append({"stage": "resolve", "ok": True,
                           "detail": f"{repo} → {san_dir}"})

    # Phase 1: index lookup (qualified-name match)
    idx = {}
    try:
        idx = json.loads((san_dir / "_index.json").read_text())
    except (json.JSONDecodeError, OSError):
        pass
    kw = keyword.lower()
    index_hits = []
    for qname, meta in idx.items():
        if kw in qname.lower() and isinstance(meta, dict):
            index_hits.append({"qname": qname, "kind": meta.get("kind", "?"),
                               "file": meta.get("file", "?"),
                               "tokens_san": meta.get("tokens_san", 0)})
    trace["steps"].append({"stage": "index", "ok": True,
                           "detail": f"{len(idx)} symbols indexed → "
                           f"{len(index_hits)} name match(es)",
                           "hits": index_hits[:25]})

    # Phase 2: content scan of .san files (for matches the name didn't catch)
    content_hits = []
    seen_files = {h["file"] for h in index_hits}
    try:
        for sf in sorted(san_dir.rglob("*.san")):
            if sf.name.startswith("_"):
                continue
            rel = str(sf.relative_to(san_dir))
            src_rel = rel[:-4] if rel.endswith(".san") else rel
            if src_rel in seen_files:
                continue
            for b in _san_block_for(sf, keyword):
                content_hits.append({
                    "qname": b["name"], "kind": b["kind"], "src": b["src"],
                    "file": src_rel, "snippet": "\n".join(b["snippet"])})
    except OSError:
        pass
    trace["steps"].append({"stage": "content", "ok": True,
                           "detail": f"scanned .san bodies → "
                           f"{len(content_hits)} extra block match(es)",
                           "hits": content_hits[:25]})

    # Final landing points: file → block → src: line range
    for h in index_hits[:25]:
        # enrich index hits with their src: range by reading the block
        sp = san_dir / (h["file"] + ".san")
        src = ""
        if sp.exists():
            for b in _san_block_for(sp, h["qname"]):
                if b["name"] == h["qname"]:
                    src = b["src"]
                    break
        trace["results"].append({**h, "src": src, "via": "index"})
    for h in content_hits[:25]:
        trace["results"].append({**h, "via": "content"})
    return trace


class DashboardHandler(SimpleHTTPRequestHandler):
    """Serves static files + SSE endpoint for real-time updates."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/events":
            self._handle_sse()
        elif path == "/decision-events":
            self._handle_decision_sse()
        elif path == "/api/state":
            self._send_json(load_state())
        elif path == "/api/decisions":
            self._send_json({"decisions": _load_decisions()})
        elif path == "/api/san":
            self._send_json(_load_san_repos())
        elif path == "/api/san/search":
            self._send_json(_san_search(self.path))
        elif path == "/api/san/symbol":
            self._send_json(_san_symbol(self.path))
        elif path in ("/decisions", "/decisions/"):
            self.path = "/decisions.html"
            super().do_GET()
        elif path in ("/san", "/san/"):
            self.path = "/san.html"
            super().do_GET()
        elif path in ("/", "/3d", "/3d/", "/index.html"):
            # The 3D office is the only office view now.
            self.path = "/office3d.html"
            super().do_GET()
        else:
            super().do_GET()

    def _send_json(self, obj):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_sse(self):
        """Server-Sent Events: push state changes to connected dashboards."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_mtime = None
        try:
            while True:
                # stat() before read: skip parse+serialize when nothing changed
                try:
                    mtime = STATE_FILE.stat().st_mtime_ns
                except OSError:
                    mtime = None
                if mtime != last_mtime:
                    last_mtime = mtime
                    state = load_state()
                    data = json.dumps(state, separators=(",", ":"))
                    self.wfile.write(f"data: {data}\n\n".encode())
                    self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    def _handle_decision_sse(self):
        """SSE: push the full decisions list whenever a decision is logged or
        outcome'd (detected by snapshot+journal mtime/size change)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_sig = None
        try:
            while True:
                sig = _graph_sig()
                if sig != last_sig:
                    last_sig = sig
                    payload = json.dumps({"decisions": _load_decisions()},
                                         separators=(",", ":"))
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    def log_message(self, format, *args):
        pass  # Suppress access logs


def main():
    if not STATIC_DIR.exists():
        print(f"ERROR: Static files not found at {STATIC_DIR}")
        print("Run from the agent-brain repo: python dashboard/server.py")
        sys.exit(1)

    server = ThreadingHTTPServer(("", PORT), DashboardHandler)
    print(f"\n\U0001f3e2 Agent Brain Dashboard")
    print(f"   3D office:      http://localhost:{PORT}")
    print(f"   Decisions view: http://localhost:{PORT}/decisions  (live feed)")
    print(f"   SAN search:     http://localhost:{PORT}/san         (search trace)")
    print(f"   State file:     {STATE_FILE}")
    print(f"   Ctrl+C to stop\n")

    threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\U0001f44b Office closed.")
        server.shutdown()


if __name__ == "__main__":
    main()
