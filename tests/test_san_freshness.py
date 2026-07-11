import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import brain.server as server


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_tree(root: Path) -> dict[str, tuple[bytes, int]]:
    """Record every file under root: rel path -> (bytes, st_mtime_ns)."""
    snap: dict[str, tuple[bytes, int]] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            snap[str(p.relative_to(root))] = (p.read_bytes(), st.st_mtime_ns)
    return snap


class SanFreshnessScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.san = self.repo / ".san"
        # Patch repo resolution to our temp repo.
        self._patch_repo = mock.patch.object(
            server, "_resolve_repo_path", return_value=self.repo
        )
        self._patch_repo.start()
        # Also patch _get_repo_paths so any name→path helpers resolve here.
        self._patch_paths = mock.patch.object(
            server, "_get_repo_paths", return_value={"demo": self.repo}
        )
        self._patch_paths.start()

    def tearDown(self):
        self._patch_repo.stop()
        self._patch_paths.stop()
        self._tmp.cleanup()

    def _src(self, rel: str, body: str) -> None:
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)

    def _san_for(self, rel: str, body: str) -> Path:
        p = self.san / (rel + ".san")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        return p

    def _hashes(self, mapping: dict[str, str]) -> None:
        self.san.mkdir(parents=True, exist_ok=True)
        (self.san / ".san_hashes.json").write_text(json.dumps(mapping, indent=2))

    def _valid_san(self, name: str) -> str:
        return f"{name} @module {{\n  src: 1-1\n  purpose: x\n}}\n"

    # --- immutability ------------------------------------------------------

    def test_dry_run_does_not_create_san_directory(self):
        self._src("src/Only.py", "x = 1\n")
        self.assertFalse(self.san.exists())
        server.recompile_san("demo", dry_run=True)
        self.assertFalse(self.san.exists(), "dry-run must not create .san/")

    def test_dry_run_preserves_orphan_hash_index_metrics_and_mtimes(self):
        # Build a populated .san with an orphan, stale hash, and index.
        self._src("src/Fresh.py", "fresh = 1\n")
        fresh_body = self._valid_san("Fresh")
        self._san_for("src/Fresh.py", fresh_body)
        self._san_for("src/Gone.py", self._valid_san("Gone"))  # orphan (no source)
        self._hashes({"src/Fresh.py": _sha(b"fresh = 1\n")})
        (self.san / "_index.json").write_text(json.dumps({"Fresh": {
            "kind": "module", "file": "src/Fresh.py", "tokens_san": 5}}))

        with TemporaryDirectory() as metrics_dir:
            metrics = Path(metrics_dir) / "brain_metrics.jsonl"
            metrics.write_text('{"pre": "existing"}\n')
            with mock.patch.object(server, "METRICS_FILE", metrics):
                before_tree = snapshot_tree(self.san)
                before_metrics = (metrics.read_bytes(), metrics.stat().st_mtime_ns)

                server.recompile_san("demo", dry_run=True)

                after_tree = snapshot_tree(self.san)
                after_metrics = (metrics.read_bytes(), metrics.stat().st_mtime_ns)

        self.assertEqual(before_tree, after_tree, "dry-run mutated .san/ tree")
        self.assertEqual(before_metrics, after_metrics, "dry-run mutated metrics")
        # Orphan SAN must still be present (dry-run never deletes).
        self.assertTrue((self.san / "src/Gone.py.san").exists())

    # --- classification ----------------------------------------------------

    def test_plan_reports_all_states_distinctly_with_digests(self):
        self._src("src/Missing.py", "m = 1\n")               # missing
        self._src("src/Stale.py", "s = 2\n")                 # stale (digest differ)
        self._src("src/Fresh.py", "f = 3\n")                 # fresh (digest equal)
        self._src("src/Broken.py", "b = 4\n")                # malformed SAN
        self._src("src/View.vue", "<template/>\n")           # unsupported (tracked)
        self._san_for("src/Stale.py", self._valid_san("Stale"))
        self._san_for("src/Fresh.py", self._valid_san("Fresh"))
        self._san_for("src/Broken.py", "not a header\n")     # invalid SAN
        self._san_for("src/Gone.py", self._valid_san("Gone"))  # orphaned
        self._hashes({
            "src/Stale.py": _sha(b"OLD DIFFERENT\n"),
            "src/Fresh.py": _sha(b"f = 3\n"),
            "src/View.vue": _sha(b"<template/>\n"),
        })

        plan = server.plan_san_refresh("demo")

        self.assertEqual(plan["status"], "ok")
        counts = plan["counts"]
        self.assertEqual(counts["missing"], 1)
        self.assertEqual(counts["stale"], 1)
        self.assertEqual(counts["fresh"], 1)
        self.assertEqual(counts["orphaned"], 1)
        self.assertEqual(counts["unsupported"], 1)
        self.assertEqual(counts["malformed"], 1)

        self.assertEqual(plan["missing"][0]["source_path"], "src/Missing.py")
        self.assertEqual(
            plan["missing"][0]["source_sha256"], _sha(b"m = 1\n")
        )
        self.assertEqual(plan["stale"][0]["source_path"], "src/Stale.py")
        self.assertEqual(plan["fresh"][0]["source_path"], "src/Fresh.py")
        self.assertEqual(plan["orphaned"][0]["san_path"], ".san/src/Gone.py.san")
        self.assertEqual(plan["unsupported"][0]["source_path"], "src/View.vue")
        self.assertEqual(plan["unsupported"][0]["reason"], "unsupported_extension")
        self.assertEqual(plan["malformed"][0]["source_path"], "src/Broken.py")

    def test_hash_match_is_fresh_without_touching_san(self):
        self._src("src/Fresh.py", "f = 3\n")
        san = self._san_for("src/Fresh.py", self._valid_san("Fresh"))
        self._hashes({"src/Fresh.py": _sha(b"f = 3\n")})
        before = (san.read_bytes(), san.stat().st_mtime_ns)

        plan = server.plan_san_refresh("demo")

        self.assertEqual(plan["counts"]["fresh"], 1)
        self.assertEqual((san.read_bytes(), san.stat().st_mtime_ns), before)

    def test_hash_mismatch_is_stale_even_when_san_mtime_is_newer(self):
        self._src("src/Stale.py", "s = 2\n")
        san = self._san_for("src/Stale.py", self._valid_san("Stale"))
        # Make SAN mtime strictly newer than source.
        src = self.repo / "src/Stale.py"
        src_mtime = src.stat().st_mtime
        os.utime(san, (src_mtime + 100, src_mtime + 100))
        # Stored digest differs from current source content.
        self._hashes({"src/Stale.py": _sha(b"OLD DIFFERENT\n")})

        plan = server.plan_san_refresh("demo")

        self.assertEqual(plan["counts"]["stale"], 1)
        self.assertEqual(plan["counts"]["fresh"], 0)

    def test_invalid_san_is_reported_malformed(self):
        self._src("src/Broken.py", "b = 4\n")
        self._san_for("src/Broken.py", "garbage without a header\n")
        # Even with a matching stored digest, malformed wins over fresh/stale.
        self._hashes({"src/Broken.py": _sha(b"b = 4\n")})

        plan = server.plan_san_refresh("demo")

        self.assertEqual(plan["counts"]["malformed"], 1)
        self.assertEqual(plan["counts"]["fresh"], 0)
        self.assertEqual(plan["counts"]["stale"], 0)
        self.assertEqual(plan["malformed"][0]["source_path"], "src/Broken.py")

    def test_non_utf8_source_and_san_do_not_crash_scan(self):
        # A non-UTF8 source (binary-tainted) or a non-UTF8 existing SAN must
        # NOT crash the read-only scan. read_text() raises UnicodeDecodeError
        # (a ValueError, not OSError), so a naive `except OSError` would let it
        # propagate out of the dry-run tool.
        # Non-UTF8 SOURCE with a valid SAN + matching hash → classified fresh.
        src_bytes = b"c = 1\n\xff\x80\n"
        (self.repo / "src").mkdir(parents=True, exist_ok=True)
        (self.repo / "src/Bin.py").write_bytes(src_bytes)
        self._san_for("src/Bin.py", self._valid_san("Bin"))
        # Non-UTF8 SAN whose corruption lands on the structural src line →
        # fails the strict grammar (→ malformed), never a crash.
        (self.repo / "src/BadSan.py").write_text("x = 2\n")
        bad_san = self.san / "src/BadSan.py.san"
        bad_san.parent.mkdir(parents=True, exist_ok=True)
        bad_san.write_bytes(b"BadSan @module {\n  src: 1-\xff\x80\n}\n")
        self._hashes({"src/Bin.py": _sha(src_bytes)})

        # Must not raise.
        plan = server.plan_san_refresh("demo")
        report = server.check_san_freshness("demo")
        dry = server.recompile_san("demo", dry_run=True)

        self.assertEqual(plan["status"], "ok")
        self.assertIsInstance(report, str)
        self.assertIsInstance(dry, str)
        # Non-UTF8 source with matching hash + valid SAN classifies fresh.
        fresh_paths = {e["source_path"] for e in plan["fresh"]}
        self.assertIn("src/Bin.py", fresh_paths)
        # Non-UTF8 SAN is surfaced (malformed or, if replaced, still handled) —
        # never a crash and never silently fresh.
        malformed_paths = {e["source_path"] for e in plan["malformed"]}
        self.assertNotIn("src/BadSan.py", fresh_paths)
        self.assertIn("src/BadSan.py", malformed_paths)

    def test_invalid_repo_returns_structured_error(self):
        with mock.patch.object(server, "_resolve_repo_path", return_value=None):
            plan = server.plan_san_refresh("nope")
        self.assertEqual(plan["status"], "repo_not_found")

    # --- mutating path still works ----------------------------------------

    def test_non_dry_run_still_removes_orphans_and_rebuilds_metadata(self):
        self._src("src/Fresh.py", "f = 3\n")
        self._san_for("src/Fresh.py", self._valid_san("Fresh"))
        self._san_for("src/Gone.py", self._valid_san("Gone"))  # orphan
        self._hashes({"src/Fresh.py": _sha(b"f = 3\n"),
                      "src/Gone.py": _sha(b"gone\n")})

        with TemporaryDirectory() as metrics_dir:
            with mock.patch.object(
                server, "METRICS_FILE", Path(metrics_dir) / "m.jsonl"
            ):
                out = server.recompile_san("demo", dry_run=False)

        # Orphan removed and index rebuilt by the mutating path.
        self.assertFalse((self.san / "src/Gone.py.san").exists())
        self.assertTrue((self.san / "_index.json").exists())
        self.assertIn("SAN refresh", out)


if __name__ == "__main__":
    unittest.main()
