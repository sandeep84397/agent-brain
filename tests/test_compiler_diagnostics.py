import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from brain.compiler_config import parse_san_compiler_config
from brain.compiler_setup import (
    CompilerArtifactDiagnostic,
    diagnose_compiler_artifacts,
    install_claude_adapter,
    install_codex_adapters,
)


ROOT = Path(__file__).resolve().parents[1]
ASSETS_ROOT = ROOT / "san"


class CompilerDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.config = parse_san_compiler_config({})
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.claude_home = self.base / ".claude"
        self.codex_home = self.base / ".codex"

    def tearDown(self):
        self._tmp.cleanup()

    def _diagnose(self, *, claude=True, codex=True, assets_root=ASSETS_ROOT):
        return diagnose_compiler_artifacts(
            home=self.claude_home,
            codex_home=self.codex_home,
            config=self.config,
            assets_root=assets_root,
            claude_detected=claude,
            codex_detected=codex,
        )

    def _by(self, diags, provider, artifact):
        for d in diags:
            if d.provider == provider and d.artifact == artifact:
                return d
        return None

    def test_reports_effective_models_versions_and_currentness(self):
        # Install both providers, then diagnose → all current with models.
        install_claude_adapter(
            claude_home=self.claude_home, config=self.config, assets_root=ASSETS_ROOT
        )
        install_codex_adapters(
            codex_home=self.codex_home, config=self.config, assets_root=ASSETS_ROOT
        )

        diags = self._diagnose()
        claude = self._by(diags, "claude", "agent")
        self.assertEqual(claude.state, "current")
        self.assertEqual(claude.model, "claude-sonnet-4-6")
        self.assertEqual(claude.expected_version, 1)
        self.assertIn("current; version=1; model=claude-sonnet-4-6", claude.detail)

        codex_agent = self._by(diags, "codex", "agent")
        self.assertEqual(codex_agent.state, "current")
        self.assertEqual(codex_agent.model, "gpt-5.4-mini")
        self.assertEqual(codex_agent.reasoning_effort, "medium")
        self.assertIn("model=gpt-5.4-mini", codex_agent.detail)
        self.assertIn("reasoning_effort=medium", codex_agent.detail)

    def test_reports_missing_claude_adapter_with_exact_setup_command(self):
        diags = self._diagnose(codex=False)
        claude = self._by(diags, "claude", "agent")
        self.assertEqual(claude.state, "missing")
        self.assertEqual(claude.detail, "missing; run ./setup.sh --claude")

    def test_reports_unmarked_codex_conflict(self):
        agent_path = self.codex_home / "agents" / "brain-compiler.toml"
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        agent_path.write_text('name = "brain-compiler"\n# hand-written\n')

        diags = self._diagnose(claude=False)
        codex_agent = self._by(diags, "codex", "agent")
        self.assertEqual(codex_agent.state, "conflict")
        self.assertIn("conflict: unmanaged file preserved at", codex_agent.detail)
        self.assertIn(str(agent_path), codex_agent.detail)

    def test_reports_stale_managed_artifact(self):
        # Install, then corrupt the managed agent's body while keeping the
        # marker → stale (managed but not byte-current).
        install_claude_adapter(
            claude_home=self.claude_home, config=self.config, assets_root=ASSETS_ROOT
        )
        agent_path = self.claude_home / "agents" / "brain-compiler.md"
        text = agent_path.read_text()
        agent_path.write_text(text + "\n# drift\n")

        diags = self._diagnose(codex=False)
        claude = self._by(diags, "claude", "agent")
        self.assertEqual(claude.state, "stale")
        self.assertEqual(claude.detail, "stale managed artifact; rerun the provider setup")

    def test_skips_undetected_host_and_never_invokes_provider(self):
        # Neither host detected → empty diagnostics, no filesystem writes.
        diags = self._diagnose(claude=False, codex=False)
        self.assertEqual(diags, ())
        # No adapter files were created by diagnosis.
        self.assertFalse((self.claude_home / "agents" / "brain-compiler.md").exists())
        self.assertFalse((self.codex_home / "agents" / "brain-compiler.toml").exists())

    def test_diagnosis_never_writes_artifacts(self):
        # Diagnosing a missing/absent install must not create anything.
        before = sorted(self.base.rglob("*"))
        self._diagnose()
        after = sorted(self.base.rglob("*"))
        self.assertEqual(before, after)

    def test_missing_canonical_contract_fails(self):
        # Point assets_root at a tree with no compiler-contract.md.
        empty = self.base / "empty_assets"
        empty.mkdir(parents=True, exist_ok=True)
        diags = self._diagnose(codex=False, assets_root=empty)
        claude = self._by(diags, "claude", "agent")
        self.assertIn("canonical compiler contract missing at", claude.detail)

    def test_all_entries_are_diagnostic_records(self):
        diags = self._diagnose()
        self.assertTrue(all(isinstance(d, CompilerArtifactDiagnostic) for d in diags))


class ServerDiagnoseTests(unittest.TestCase):
    """Exercise the server `diagnose` self-check wiring (read-only)."""

    def setUp(self):
        import contextlib
        import io
        import brain.server as server
        self.server = server
        self.contextlib = contextlib
        self.io = io
        self._tmp = TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run_diagnose(self, project="", config=None):
        import unittest.mock as mock
        patches = []
        if config is not None:
            patches.append(mock.patch.object(self.server, "_load_config", return_value=config))
        buf = self.io.StringIO()
        for p in patches:
            p.start()
        try:
            with self.contextlib.redirect_stdout(buf):
                code = self.server._diagnose(project=project)
        finally:
            for p in reversed(patches):
                p.stop()
        return code, buf.getvalue()

    def test_invalid_compiler_config_fails_without_mutation(self):
        before = sorted(self.base.rglob("*"))
        code, out = self._run_diagnose(
            config={"san_compiler": {"allow_expensive_fallback": True}}
        )
        self.assertIn("SAN compiler config", out)
        self.assertIn("invalid san_compiler config", out)
        # Diagnosis is read-only: our temp base is untouched.
        self.assertEqual(sorted(self.base.rglob("*")), before)

    def test_project_diagnose_reports_missing_dot_san_ignore(self):
        project = self.base / "proj"
        project.mkdir(parents=True, exist_ok=True)
        gitignore = project / ".gitignore"
        gitignore.write_text("node_modules/\n")
        before = gitignore.read_bytes()

        code, out = self._run_diagnose(project=str(project))

        self.assertIn(".san/", out)
        self.assertIn("consider adding", out)
        # Diagnosis must not edit the project's .gitignore.
        self.assertEqual(gitignore.read_bytes(), before)

    def test_project_diagnose_accepts_anchored_dot_san_ignore(self):
        project = self.base / "proj2"
        project.mkdir(parents=True, exist_ok=True)
        (project / ".gitignore").write_text(".mcp.json\n/.san/\n")

        code, out = self._run_diagnose(project=str(project))
        # The anchored /.san/ form satisfies the .san/ requirement.
        self.assertIn("covers brain artifacts", out)


if __name__ == "__main__":
    unittest.main()
