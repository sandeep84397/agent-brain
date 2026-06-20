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
