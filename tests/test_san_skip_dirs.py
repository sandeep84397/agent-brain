import json
import tempfile
import unittest
from pathlib import Path

from brain.server import _is_skipped_source, _rebuild_san_index


class SanGeneratedDirectoryFilterTest(unittest.TestCase):
    def test_skips_wxt_and_wrangler_generated_directories(self):
        generated = (
            ".output/chrome-mv3/background.js",
            ".wxt/types/imports.d.ts",
            "dist-unpacked/chunks/content.js",
            "license-worker/.wrangler/tmp/worker.js",
        )

        for path in generated:
            with self.subTest(path=path):
                self.assertTrue(_is_skipped_source(path))

    def test_keeps_hand_written_jobfill_sources(self):
        for path in (
            "src/application/fillPipeline.ts",
            "entrypoints/content.ts",
            "license-worker/src/index.ts",
            "tests/application/fillPipeline.test.ts",
        ):
            with self.subTest(path=path):
                self.assertFalse(_is_skipped_source(path))

    def test_index_accepts_hyphenated_qualified_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            source = repo / "license-worker/src/index.ts"
            source.parent.mkdir(parents=True)
            source.write_text("export const worker = true;\n")
            san = repo / ".san/license-worker/src/index.ts.san"
            san.parent.mkdir(parents=True)
            san.write_text(
                "license-worker.src.index @route {\n"
                "  src: 1-1\n"
                "  purpose: worker route\n"
                "}\n"
            )

            _rebuild_san_index(repo / ".san", repo_path=repo)

            index = json.loads((repo / ".san/_index.json").read_text())
            self.assertIn("license-worker.src.index", index)
            self.assertEqual(index["license-worker.src.index"]["file"], "license-worker/src/index.ts")


if __name__ == "__main__":
    unittest.main()
