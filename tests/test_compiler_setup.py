import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brain.compiler_config import (
    ClaudeCompilerConfig,
    CodexCompilerConfig,
    CompilerConfigError,
    SanCompilerConfig,
    parse_san_compiler_config,
)
from brain.compiler_setup import (
    MANAGED_MARKER,
    ManagedArtifactConflict,
    install_claude_adapter,
    install_codex_adapters,
    install_managed_artifact,
)


ROOT = Path(__file__).resolve().parents[1]
ASSETS_ROOT = ROOT / "san"


def managed_content(body: str, version: int = 1) -> str:
    return f"# {MANAGED_MARKER} version={version}\n{body}\n"


class ManagedArtifactTests(unittest.TestCase):
    def test_missing_artifact_is_created_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "brain-compiler.md"
            rendered = managed_content("new")

            result = install_managed_artifact(path, rendered)

            self.assertEqual(path.read_text(encoding="utf-8"), rendered)
            self.assertEqual(result.path, path)
            self.assertEqual(result.previous_state, "missing")
            self.assertTrue(result.changed)
            self.assertEqual(list(path.parent.glob("*.tmp-*")), [])

    def test_current_artifact_is_byte_and_mtime_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brain-compiler.md"
            rendered = managed_content("current")
            install_managed_artifact(path, rendered)
            before_bytes = path.read_bytes()
            before_mtime = path.stat().st_mtime_ns

            result = install_managed_artifact(path, rendered)

            self.assertEqual(path.read_bytes(), before_bytes)
            self.assertEqual(path.stat().st_mtime_ns, before_mtime)
            self.assertEqual(result.previous_state, "current")
            self.assertFalse(result.changed)

    def test_stale_managed_artifact_is_updated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brain-compiler.md"
            path.write_text(managed_content("old"), encoding="utf-8")
            rendered = managed_content("new")

            result = install_managed_artifact(path, rendered)

            self.assertEqual(path.read_text(encoding="utf-8"), rendered)
            self.assertEqual(result.previous_state, "stale")
            self.assertTrue(result.changed)

    def test_unmarked_conflict_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brain-compiler.md"
            prior = b"user-owned\x00bytes\n"
            path.write_bytes(prior)

            with self.assertRaises(ManagedArtifactConflict) as raised:
                install_managed_artifact(path, managed_content("replacement"))

            self.assertEqual(raised.exception.path, path)
            self.assertEqual(path.read_bytes(), prior)
            self.assertEqual(list(path.parent.glob("*.tmp-*")), [])

    def test_replace_failure_preserves_previous_bytes_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brain-compiler.md"
            prior = managed_content("old").encode()
            path.write_bytes(prior)

            with mock.patch(
                "brain.compiler_setup.os.replace",
                side_effect=OSError("injected replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected replace failure"):
                    install_managed_artifact(path, managed_content("new"))

            self.assertEqual(path.read_bytes(), prior)
            self.assertEqual(list(path.parent.glob("*.tmp-*")), [])


class ProviderInstallTests(unittest.TestCase):
    def setUp(self):
        self.config = parse_san_compiler_config({})

    def test_claude_install_creates_only_claude_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"

            result = install_claude_adapter(
                claude_home=claude_home,
                config=self.config,
                assets_root=ASSETS_ROOT,
            )

            expected = claude_home / "agents" / "brain-compiler.md"
            self.assertEqual(result.path, expected)
            self.assertTrue(expected.is_file())
            self.assertFalse((root / ".codex").exists())

    def test_codex_install_creates_only_codex_agent_and_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"

            agent_result, skill_result = install_codex_adapters(
                codex_home=codex_home,
                config=self.config,
                assets_root=ASSETS_ROOT,
            )

            agent = codex_home / "agents" / "brain-compiler.toml"
            skill = codex_home / "skills" / "brain-compiler" / "SKILL.md"
            self.assertEqual((agent_result.path, skill_result.path), (agent, skill))
            self.assertTrue(agent.is_file())
            self.assertTrue(skill.is_file())
            self.assertFalse((root / ".claude").exists())
            before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in (agent, skill)
            }

            repeated = install_codex_adapters(
                codex_home=codex_home,
                config=self.config,
                assets_root=ASSETS_ROOT,
            )

            self.assertTrue(all(MANAGED_MARKER.encode() in data for data, _ in before.values()))
            self.assertFalse(any(result.changed for result in repeated))
            self.assertEqual(
                {
                    path: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in (agent, skill)
                },
                before,
            )

    def test_rendered_adapters_use_effective_overrides(self):
        config = parse_san_compiler_config({
            "san_compiler": {
                "claude": {"model": "claude-custom"},
                "codex": {
                    "model": "gpt-custom",
                    "reasoning_effort": "low",
                },
            }
        })
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            codex_home = root / ".codex"

            install_claude_adapter(
                claude_home=claude_home,
                config=config,
                assets_root=ASSETS_ROOT,
            )
            install_codex_adapters(
                codex_home=codex_home,
                config=config,
                assets_root=ASSETS_ROOT,
            )

            claude = (claude_home / "agents" / "brain-compiler.md").read_text()
            codex = (codex_home / "agents" / "brain-compiler.toml").read_text()
            self.assertIn("model: claude-custom", claude)
            self.assertIn('model = "gpt-custom"', codex)
            self.assertIn('model_reasoning_effort = "low"', codex)
            self.assertNotIn("{{", claude)
            self.assertNotIn("{{", codex)

    def test_invalid_config_preserves_last_valid_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            codex_home = root / ".codex"
            install_claude_adapter(
                claude_home=claude_home,
                config=self.config,
                assets_root=ASSETS_ROOT,
            )
            install_codex_adapters(
                codex_home=codex_home,
                config=self.config,
                assets_root=ASSETS_ROOT,
            )
            paths = (
                claude_home / "agents" / "brain-compiler.md",
                codex_home / "agents" / "brain-compiler.toml",
                codex_home / "skills" / "brain-compiler" / "SKILL.md",
            )
            before = {path: path.read_bytes() for path in paths}
            invalid = SanCompilerConfig(
                claude=ClaudeCompilerConfig(model=""),
                codex=CodexCompilerConfig(model="", reasoning_effort="ultra"),
                allow_expensive_fallback=False,
            )

            with self.assertRaises(CompilerConfigError):
                install_claude_adapter(
                    claude_home=claude_home,
                    config=invalid,
                    assets_root=ASSETS_ROOT,
                )
            with self.assertRaises(CompilerConfigError):
                install_codex_adapters(
                    codex_home=codex_home,
                    config=invalid,
                    assets_root=ASSETS_ROOT,
                )

            self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_unrelated_agents_and_skills_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            unrelated = {
                codex_home / "agents" / "reviewer.toml": b"reviewer bytes\n",
                codex_home / "skills" / "other" / "SKILL.md": b"other bytes\n",
            }
            for path, content in unrelated.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

            install_codex_adapters(
                codex_home=codex_home,
                config=self.config,
                assets_root=ASSETS_ROOT,
            )

            self.assertEqual(
                {path: path.read_bytes() for path in unrelated},
                unrelated,
            )


if __name__ == "__main__":
    unittest.main()
