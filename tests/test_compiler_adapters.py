import json
import re
import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
SAN_ROOT = ROOT / "san"
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "san"


class CompilerAdapterTests(unittest.TestCase):
    def read_required(self, path: Path) -> str:
        self.assertTrue(
            path.is_file(),
            f"missing required artifact: {path.relative_to(ROOT)}",
        )
        return path.read_text(encoding="utf-8")

    def test_canonical_contract_is_provider_neutral_and_complete(self):
        contract = self.read_required(SAN_ROOT / "compiler-contract.md")

        required_facts = (
            "Supported source extensions",
            "Skipped directories",
            "src/A.py -> .san/src/A.py.san",
            r"^(\S+)\s+@(\w+)\s*\{",
            "src: <line_start>-<line_end>",
            "Canonical field order",
            "Source order",
            "Exact identifiers and signatures",
            "Public surfaces",
            "dependencies",
            "@state",
            "@errors",
            "@constraint",
            "@threading",
            "patterns",
            "risk",
            "Semantic parity checks",
            "get_roadmap",
            "pre_check",
            "log_decision",
            "log_outcome",
            "plan_san_refresh",
            "publish_san",
            "Retryable failures",
            "Terminal failures",
            "Never log source or SAN contents",
        )
        for fact in required_facts:
            with self.subTest(fact=fact):
                self.assertIn(fact, contract)

        self.assertFalse(contract.startswith("---\n"))
        self.assertNotRegex(
            contract,
            r"claude-sonnet|gpt-|codex exec|ToolSearch|model_reasoning_effort",
        )
        self.assertFalse((SAN_ROOT / "brain-compiler.md").exists())

    def test_codex_template_renders_valid_custom_agent(self):
        template = self.read_required(
            SAN_ROOT / "adapters" / "codex" / "brain-compiler.toml"
        )
        rendered = (
            template.replace("{{CODEX_MODEL}}", "gpt-5.4-mini")
            .replace("{{CODEX_REASONING_EFFORT}}", "medium")
            .replace(
                "{{CODEX_SKILL_PATH}}",
                "/tmp/brain-compiler/SKILL.md",
            )
            .replace("{{CONTRACT_PATH}}", "/tmp/compiler-contract.md")
        )
        parsed = tomllib.loads(rendered)

        for field in (
            "name",
            "description",
            "developer_instructions",
            "model",
            "model_reasoning_effort",
            "skills",
        ):
            with self.subTest(field=field):
                self.assertIn(field, parsed)
        self.assertEqual(parsed["name"], "brain-compiler")
        self.assertEqual(
            parsed["description"],
            "Generate or refresh SAN through the current Codex host.",
        )
        self.assertEqual(parsed["model"], "gpt-5.4-mini")
        self.assertEqual(parsed["model_reasoning_effort"], "medium")
        self.assertEqual(
            parsed["skills"]["config"][0]["path"],
            "/tmp/brain-compiler/SKILL.md",
        )
        self.assertTrue(parsed["skills"]["config"][0]["enabled"])

    def test_adapters_are_isolated_and_use_publication_protocol(self):
        claude = self.read_required(
            SAN_ROOT / "adapters" / "claude" / "brain-compiler.md"
        )
        codex_template = self.read_required(
            SAN_ROOT / "adapters" / "codex" / "brain-compiler.toml"
        )
        codex_skill = self.read_required(
            SAN_ROOT / "adapters" / "codex" / "brain-compiler" / "SKILL.md"
        )
        codex = f"{codex_template}\n{codex_skill}"

        self.assertTrue(claude.startswith("---\nname: brain-compiler\n"))
        self.assertIn("model: {{CLAUDE_MODEL}}", claude)
        self.assertNotIn("\ntools:", claude)
        self.assertNotRegex(
            claude,
            r"gpt-|codex exec|model_reasoning_effort",
        )
        self.assertIn("Do not invoke Codex", claude)

        self.assertNotRegex(codex, r"claude-sonnet|ToolSearch|claude\s")
        self.assertIn("Do not invoke Claude", codex)

        for provider, content in (("claude", claude), ("codex", codex)):
            with self.subTest(provider=provider, tool="plan_san_refresh"):
                self.assertIn("plan_san_refresh", content)
            with self.subTest(provider=provider, tool="publish_san"):
                self.assertIn("publish_san", content)
            with self.subTest(provider=provider, placeholder="contract"):
                self.assertIn("{{CONTRACT_PATH}}", content)

    def test_provider_fixtures_preserve_identical_canonical_facts(self):
        inventory_text = self.read_required(
            FIXTURE_ROOT / "compiler_sample.inventory.json"
        )
        inventory = json.loads(inventory_text)
        fixture_contents = {
            provider: self.read_required(
                FIXTURE_ROOT / provider / "compiler_sample.py.san"
            )
            for provider in ("claude", "codex")
        }

        for provider, content in fixture_contents.items():
            for category in (
                "names",
                "signatures",
                "dependency_facts",
                "public_surfaces",
                "state_facts",
                "error_facts",
            ):
                for fact in inventory[category]:
                    with self.subTest(
                        provider=provider,
                        category=category,
                        fact=fact,
                    ):
                        self.assertIn(fact, content)

            block_inventory = re.findall(
                r"^(\S+\s+@\w+)\s*\{",
                content,
                flags=re.MULTILINE,
            )
            self.assertEqual(block_inventory, inventory["block_inventory"])

        claude_blocks = re.findall(
            r"^(\S+\s+@\w+)\s*\{",
            fixture_contents["claude"],
            flags=re.MULTILINE,
        )
        codex_blocks = re.findall(
            r"^(\S+\s+@\w+)\s*\{",
            fixture_contents["codex"],
            flags=re.MULTILINE,
        )
        self.assertEqual(claude_blocks, codex_blocks)


if __name__ == "__main__":
    unittest.main()
