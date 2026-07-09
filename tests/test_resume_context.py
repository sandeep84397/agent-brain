import json
import tempfile
import unittest
from pathlib import Path

import networkx as nx

import brain.server as server


class ResumeContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.real = {
            "BRAIN_DIR": server.BRAIN_DIR,
            "GRAPH_FILE": server.GRAPH_FILE,
            "CONFIG_FILE": server.CONFIG_FILE,
            "OFFICE_STATE_FILE": server.OFFICE_STATE_FILE,
            "DECISION_MARKER_FILE": server.DECISION_MARKER_FILE,
            "QUERY_MARKER_FILE": server.QUERY_MARKER_FILE,
            "RECORDS_DIR": server.RECORDS_DIR,
            "ARCHIVE_FILE": server.ARCHIVE_FILE,
            "METRICS_FILE": server.METRICS_FILE,
        }
        server.BRAIN_DIR = self.tmp_path
        server.GRAPH_FILE = self.tmp_path / "decisions.json"
        server.CONFIG_FILE = self.tmp_path / "config.json"
        server.OFFICE_STATE_FILE = self.tmp_path / "office-state.json"
        server.DECISION_MARKER_FILE = self.tmp_path / ".last_decision_marker"
        server.QUERY_MARKER_FILE = self.tmp_path / ".last_query_marker"
        server.RECORDS_DIR = self.tmp_path / "records"
        server.ARCHIVE_FILE = self.tmp_path / "decisions.archive.jsonl"
        server.METRICS_FILE = self.tmp_path / "brain_metrics.jsonl"
        server._GRAPH_CACHE.update({"key": None, "graph": None, "shadow": None})
        server._save_graph(nx.DiGraph())

    def tearDown(self):
        for name, value in self.real.items():
            setattr(server, name, value)
        server._GRAPH_CACHE.update({"key": None, "graph": None, "shadow": None})
        self.tmp.cleanup()

    def test_log_decision_stores_handoff_git_and_validation_metadata(self):
        result = server.log_decision(
            agent="codex",
            repo="agent-brain",
            area="session-context-resume",
            action="COMPLETE resume context work",
            reasoning="structured handoff keeps cross-AI context compact",
            handoff_summary="Added get_resume_context and metadata capture.",
            branch="feature/resume-context",
            base_branch="main",
            commit_before="abc123",
            commit_after="def456",
            commit_range="abc123..def456",
            pr_number="42",
            validation=[
                {
                    "command": "python3 -m unittest tests.test_resume_context -v",
                    "exit_code": 0,
                    "status": "passed",
                    "passed": 3,
                    "failed": 0,
                }
            ],
            blockers=["deploy not run"],
            deferred_work=["wire session-end hook"],
            do_not_touch=["keep existing get_roadmap format"],
            next_action="Run full validation.",
        )

        decision_id = result.split("Decision logged: ")[1].splitlines()[0]
        data = server._load_graph().nodes[decision_id]

        self.assertEqual(data["handoff_summary"], "Added get_resume_context and metadata capture.")
        self.assertEqual(data["git"]["branch"], "feature/resume-context")
        self.assertEqual(data["git"]["base_branch"], "main")
        self.assertEqual(data["git"]["commit_range"], "abc123..def456")
        self.assertEqual(data["git"]["pr_number"], "42")
        self.assertEqual(data["validation"][0]["command"], "python3 -m unittest tests.test_resume_context -v")
        self.assertEqual(data["validation"][0]["status"], "passed")
        self.assertEqual(data["blockers"], ["deploy not run"])
        self.assertEqual(data["deferred_work"], ["wire session-end hook"])
        self.assertEqual(data["do_not_touch"], ["keep existing get_roadmap format"])
        self.assertIn("NUDGE: action looks complete", result)

    def test_get_resume_context_ranks_handoff_open_work_stale_pending_and_san(self):
        repo_root = self.tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".san").mkdir()
        (repo_root / ".san" / "_index.json").write_text(json.dumps({
            "brain.server.get_resume_context": {"file": "brain/server.py"},
        }))
        server.CONFIG_FILE.write_text(json.dumps({
            "repos": {"agent-brain": str(repo_root)},
            "team": [],
        }))

        handoff = server.log_decision(
            "codex",
            "agent-brain",
            "session-context-resume",
            "Add resume context",
            "compact recall",
            handoff_summary="Resume context tool is ready for the next AI.",
            validation=[{"command": "python3 brain/server.py validate", "exit_code": 0, "status": "passed"}],
            next_action="Ship the MCP tool.",
        ).split("Decision logged: ")[1].splitlines()[0]
        roadmap = server.log_decision(
            "claude",
            "agent-brain",
            "session-context-resume/roadmap",
            "Do not remove get_roadmap while adding get_resume_context",
            "backward compatibility",
        ).split("Decision logged: ")[1].splitlines()[0]
        stale = server.log_decision(
            "codex",
            "agent-brain",
            "session-context-resume",
            "COMPLETE old implementation pass",
            "forgot to record outcome",
        ).split("Decision logged: ")[1].splitlines()[0]

        graph = server._load_graph()
        graph.nodes[stale]["timestamp"] = "2026-01-01T00:00:00"
        server._save_graph(graph)

        context = server.get_resume_context(
            repo="agent-brain",
            area="session-context-resume",
            detail="compact",
            limit=5,
        )

        self.assertIn("RESUME CONTEXT: agent-brain | session-context-resume", context)
        self.assertIn(f"[{handoff}]", context)
        self.assertIn("Resume context tool is ready for the next AI.", context)
        self.assertIn(f"[{roadmap}]", context)
        self.assertIn("VALIDATION", context)
        self.assertIn("python3 brain/server.py validate -> passed", context)
        self.assertIn("HYGIENE NUDGES", context)
        self.assertIn(stale, context)
        self.assertIn("SAN", context)
        self.assertIn("1 files compiled", context)


if __name__ == "__main__":
    unittest.main()
