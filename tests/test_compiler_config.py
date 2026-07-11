import json
import tempfile
import unittest
from pathlib import Path

from brain.compiler_config import (
    CompilerConfigError,
    load_san_compiler_config,
    parse_san_compiler_config,
)


class CompilerConfigTests(unittest.TestCase):
    def test_missing_section_uses_release_defaults(self):
        cfg = parse_san_compiler_config({})
        self.assertEqual(cfg.claude.model, "claude-sonnet-4-6")
        self.assertEqual(cfg.codex.model, "gpt-5.4-mini")
        self.assertEqual(cfg.codex.reasoning_effort, "medium")
        self.assertFalse(cfg.allow_expensive_fallback)

    def test_valid_provider_overrides_are_preserved(self):
        cfg = parse_san_compiler_config({
            "san_compiler": {
                "claude": {"model": "claude-custom"},
                "codex": {"model": "gpt-custom", "reasoning_effort": "low"},
                "allow_expensive_fallback": False,
            }
        })
        self.assertEqual(cfg.claude.model, "claude-custom")
        self.assertEqual(cfg.codex.model, "gpt-custom")
        self.assertEqual(cfg.codex.reasoning_effort, "low")

    def test_supported_codex_efforts_are_accepted(self):
        for effort in ("none", "low", "medium", "high", "xhigh"):
            with self.subTest(effort=effort):
                cfg = parse_san_compiler_config({
                    "san_compiler": {"codex": {"reasoning_effort": effort}}
                })
                self.assertEqual(cfg.codex.reasoning_effort, effort)

    def test_invalid_fields_have_exact_paths(self):
        bad = (
            ({"san_compiler": {"claude": {"model": " "}}},
             "san_compiler.claude.model: expected non-empty string"),
            ({"san_compiler": {"codex": {"model": 7}}},
             "san_compiler.codex.model: expected non-empty string"),
            ({"san_compiler": {"codex": {"reasoning_effort": "ultra"}}},
             "san_compiler.codex.reasoning_effort: expected one of"),
            ({"san_compiler": {"allow_expensive_fallback": True}},
             "san_compiler.allow_expensive_fallback: must be false"),
        )
        for payload, message in bad:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(CompilerConfigError, message):
                    parse_san_compiler_config(payload)

    def test_file_loader_rejects_invalid_json_and_non_object_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{")
            with self.assertRaisesRegex(CompilerConfigError, "config.json: invalid JSON"):
                load_san_compiler_config(path)
            path.write_text(json.dumps([]))
            with self.assertRaisesRegex(CompilerConfigError, "config.json: expected JSON object"):
                load_san_compiler_config(path)
