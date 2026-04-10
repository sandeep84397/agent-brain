#!/usr/bin/env python3
"""
Agent Brain Office Dashboard
Real-time pixel art visualization of agent team activity.

Usage:
    python dashboard/server.py [--port PORT]

Opens http://localhost:3333 in your browser.
Reads state from ~/.agent-brain/office-state.json (written by heartbeat MCP tool).
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


class DashboardHandler(SimpleHTTPRequestHandler):
    """Serves static files + SSE endpoint for real-time updates."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_GET(self):
        if self.path == "/events":
            self._handle_sse()
        elif self.path == "/api/state":
            state = load_state()
            body = json.dumps(state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def _handle_sse(self):
        """Server-Sent Events: push state changes to connected dashboards."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        last_data = ""
        try:
            while True:
                state = load_state()
                data = json.dumps(state, separators=(",", ":"))
                if data != last_data:
                    self.wfile.write(f"data: {data}\n\n".encode())
                    self.wfile.flush()
                    last_data = data
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
    print(f"\n\U0001f3e2 Agent Brain Office")
    print(f"   Dashboard:  http://localhost:{PORT}")
    print(f"   State file: {STATE_FILE}")
    print(f"   Ctrl+C to stop\n")

    threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\U0001f44b Office closed.")
        server.shutdown()


if __name__ == "__main__":
    main()
