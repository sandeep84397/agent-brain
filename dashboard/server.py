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
        elif path in ("/decisions", "/decisions/"):
            self.path = "/decisions.html"
            super().do_GET()
        elif path in ("/3d", "/3d/"):
            # Internal redirect to the 3D dashboard HTML
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
    print(f"   Pixel-art view: http://localhost:{PORT}")
    print(f"   3D view:        http://localhost:{PORT}/3d")
    print(f"   Decisions view: http://localhost:{PORT}/decisions  (live feed)")
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
