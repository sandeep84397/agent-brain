import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from brain.san_publish import (
    SAN_MAX_BLOCKS,
    SAN_MAX_CANDIDATE_BYTES,
    atomic_write_bytes,
    canonical_san_path,
    restore_file,
    snapshot_file,
    validate_san_candidate,
)


def _codes(result: dict) -> set[str]:
    return {e["code"] for e in result["errors"]}


class SanCandidateValidationTests(unittest.TestCase):
    def test_accepts_valid_multi_block_candidate(self):
        candidate = (
            "pkg.Auth @svc {\n"
            "  src: 1-3\n"
            "  purpose: auth\n"
            "}\n"
            "pkg.Auth.login @fn {\n"
            "  src: 2-3\n"
            "  purpose: sign in\n"
            "}\n"
        )
        result = validate_san_candidate(candidate, source_line_count=3)
        self.assertTrue(result["valid"])
        self.assertEqual(result["block_count"], 2)
        self.assertEqual(result["errors"], [])
        # Result must carry only metadata — never source/SAN body text.
        serialized = json.dumps(result)
        self.assertNotIn("purpose: auth", serialized)
        self.assertNotIn("sign in", serialized)
        # Block metadata is structural only.
        blocks = result["blocks"]
        self.assertEqual(blocks[0]["qualified_name"], "pkg.Auth")
        self.assertEqual(blocks[0]["kind"], "svc")
        self.assertEqual(blocks[0]["src_start"], 1)
        self.assertEqual(blocks[0]["src_end"], 3)

    def test_valid_result_hides_source_and_san_content(self):
        candidate = (
            "pkg.Auth @svc {\n"
            "  src: 1-3\n"
            "  purpose: auth\n"
            "}\n"
        )
        result = validate_san_candidate(candidate, source_line_count=3)
        self.assertTrue(result["valid"])
        self.assertEqual(result["block_count"], 1)
        self.assertNotIn("purpose: auth", json.dumps(result))

    def test_rejects_empty_candidate(self):
        for candidate in ("", "   ", "\n\n", "   \n \t\n"):
            result = validate_san_candidate(candidate, source_line_count=3)
            self.assertFalse(result["valid"])
            self.assertIn("empty_candidate", _codes(result))

    def test_rejects_invalid_header_and_text_outside_blocks(self):
        # A line that is clearly meant to be a header (has the `@kind {`
        # signature) but is malformed — comment-prefixed or indented — is
        # invalid_header, not generic stray text.
        comment_header = "# pkg.Auth @svc {\n  src: 1-1\n}\n"
        result = validate_san_candidate(comment_header, source_line_count=1)
        self.assertFalse(result["valid"])
        self.assertIn("invalid_header", _codes(result))

        indented_header = "  pkg.Auth @svc {\n  src: 1-1\n}\n"
        result = validate_san_candidate(indented_header, source_line_count=1)
        self.assertFalse(result["valid"])
        self.assertIn("invalid_header", _codes(result))

        # Trailing junk after the opening brace is also an invalid header.
        trailing = "pkg.Auth @svc { extra\n  src: 1-1\n}\n"
        result = validate_san_candidate(trailing, source_line_count=1)
        self.assertFalse(result["valid"])
        self.assertIn("invalid_header", _codes(result))

        # Arbitrary prose with no header signature is stray text, not a header.
        stray = (
            "pkg.Auth @svc {\n  src: 1-1\n}\n"
            "not a header\n"
            "pkg.B @fn {\n  src: 1-1\n}\n"
        )
        result = validate_san_candidate(stray, source_line_count=1)
        self.assertFalse(result["valid"])
        self.assertIn("text_outside_block", _codes(result))
        self.assertNotIn("invalid_header", _codes(result))

    def test_requires_src_as_first_block_line(self):
        candidate = (
            "pkg.Auth @svc {\n"
            "  purpose: auth\n"
            "  src: 1-1\n"
            "}\n"
        )
        result = validate_san_candidate(candidate, source_line_count=1)
        self.assertFalse(result["valid"])
        self.assertIn("missing_src", _codes(result))

    def test_rejects_zero_reversed_and_past_eof_ranges(self):
        cases = [
            ("pkg.A @svc {\n  src: 0-2\n}\n", 5),   # zero start
            ("pkg.A @svc {\n  src: 3-1\n}\n", 5),   # reversed
            ("pkg.A @svc {\n  src: 1-6\n}\n", 5),   # past EOF
        ]
        for candidate, line_count in cases:
            result = validate_san_candidate(candidate, source_line_count=line_count)
            self.assertFalse(result["valid"], candidate)
            self.assertIn("invalid_src_range", _codes(result), candidate)

    def test_rejects_stray_nested_and_unclosed_braces(self):
        nested = (
            "pkg.A @svc {\n"
            "  src: 1-1\n"
            "pkg.B @fn {\n"
            "  src: 1-1\n"
            "}\n"
            "}\n"
        )
        result = validate_san_candidate(nested, source_line_count=1)
        self.assertFalse(result["valid"])
        self.assertIn("nested_block", _codes(result))

        stray_close = "}\n"
        result = validate_san_candidate(stray_close, source_line_count=1)
        self.assertFalse(result["valid"])
        self.assertIn("stray_closing_brace", _codes(result))

        unclosed = "pkg.A @svc {\n  src: 1-1\n  purpose: x\n"
        result = validate_san_candidate(unclosed, source_line_count=1)
        self.assertFalse(result["valid"])
        self.assertIn("unclosed_block", _codes(result))

    def test_rejects_duplicate_qualified_name_kind(self):
        candidate = (
            "pkg.A @svc {\n  src: 1-1\n}\n"
            "pkg.A @svc {\n  src: 1-1\n}\n"
        )
        result = validate_san_candidate(candidate, source_line_count=1)
        self.assertFalse(result["valid"])
        self.assertIn("duplicate_block", _codes(result))

    def test_allows_same_name_with_different_kind(self):
        candidate = (
            "pkg.A @svc {\n  src: 1-1\n}\n"
            "pkg.A @fn {\n  src: 1-1\n}\n"
        )
        result = validate_san_candidate(candidate, source_line_count=1)
        self.assertTrue(result["valid"])
        self.assertEqual(result["block_count"], 2)

    def test_rejects_placeholder_markers(self):
        cases = [
            "pkg.A @svc {\n  src: 1-1\n  purpose: TODO\n}\n",
            "pkg.A @svc {\n  src: 1-1\n  impl: {{FILL_ME}}\n}\n",
            "pkg.A @svc {\n  src: 1-1\n  purpose: <placeholder>\n}\n",
            "TBD\npkg.A @svc {\n  src: 1-1\n}\n",
        ]
        for candidate in cases:
            result = validate_san_candidate(candidate, source_line_count=1)
            self.assertFalse(result["valid"], candidate)
            self.assertIn("placeholder_marker", _codes(result), candidate)

    def test_placeholder_rule_allows_legitimate_facts(self):
        # A real fact mentioning TODO, or a language-level ellipsis, is valid.
        legit = [
            "pkg.A @svc {\n  src: 1-1\n  risk: source contains TODO comments\n}\n",
            "pkg.A @fn {\n  src: 1-1\n  fn:overload(self, ...) -> None\n}\n",
        ]
        for candidate in legit:
            result = validate_san_candidate(candidate, source_line_count=1)
            self.assertTrue(result["valid"], candidate)
            self.assertNotIn("placeholder_marker", _codes(result), candidate)

    def test_long_stray_line_does_not_catastrophically_backtrack(self):
        # A long single stray line (the exact malformed input the validator
        # exists to reject) must classify in near-linear time. A greedy,
        # unanchored header-like regex backtracks O(n^2) and hangs the MCP
        # server on untrusted compiler output well under the byte cap.
        import signal
        import time

        candidate = "x" * 200_000 + "\n"

        class _Timeout(Exception):
            pass

        def _raise(signum, frame):
            raise _Timeout()

        prev = signal.signal(signal.SIGALRM, _raise)
        try:
            signal.setitimer(signal.ITIMER_REAL, 3)
            start = time.perf_counter()
            result = validate_san_candidate(candidate, source_line_count=1)
            elapsed = time.perf_counter() - start
        except _Timeout:
            self.fail("validate_san_candidate hung on a long stray line (ReDoS)")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prev)

        self.assertFalse(result["valid"])
        self.assertIn("text_outside_block", _codes(result))
        self.assertLess(elapsed, 1.0)

    def test_enforces_byte_and_block_limits(self):
        big = "x" * (SAN_MAX_CANDIDATE_BYTES + 1)
        result = validate_san_candidate(big, source_line_count=1)
        self.assertFalse(result["valid"])
        self.assertIn("candidate_too_large", _codes(result))

        blocks = "".join(
            f"pkg.n{i} @fn {{\n  src: 1-1\n}}\n"
            for i in range(SAN_MAX_BLOCKS + 1)
        )
        result = validate_san_candidate(blocks, source_line_count=1)
        self.assertFalse(result["valid"])
        self.assertIn("too_many_blocks", _codes(result))


class SanAtomicPrimitiveTests(unittest.TestCase):
    def test_atomic_write_replaces_and_cleans_up(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub" / "file.bin"
            atomic_write_bytes(target, b"first")
            self.assertEqual(target.read_bytes(), b"first")
            atomic_write_bytes(target, b"second")
            self.assertEqual(target.read_bytes(), b"second")
            # No temp files linger.
            self.assertEqual(list(target.parent.glob(".*.tmp-*")), [])

    def test_replace_failure_preserves_destination_and_cleans_temp(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.bin"
            target.write_bytes(b"original")
            with mock.patch(
                "brain.san_publish.os.replace",
                side_effect=OSError("boom"),
            ):
                with self.assertRaises(OSError):
                    atomic_write_bytes(target, b"new-content")
            # Destination unchanged; temp cleaned.
            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(list(target.parent.glob(".*.tmp-*")), [])

    def test_cleanup_failure_does_not_mask_primary_error(self):
        # If os.replace fails AND the finally-block temp cleanup also fails, the
        # ORIGINAL replace error must still propagate — the cleanup error must
        # not shadow it (destination stays byte-exact regardless).
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.bin"
            target.write_bytes(b"original")

            real_unlink = Path.unlink

            def failing_unlink(self, *a, **k):
                # Fail only for the temp file, mimicking a read-only dir.
                if ".tmp-" in self.name:
                    raise PermissionError("unlink denied")
                return real_unlink(self, *a, **k)

            with mock.patch(
                "brain.san_publish.os.replace",
                side_effect=OSError("REAL-REPLACE-ERROR"),
            ):
                with mock.patch.object(Path, "unlink", failing_unlink):
                    with self.assertRaises(OSError) as ctx:
                        atomic_write_bytes(target, b"new-content")
            self.assertIn("REAL-REPLACE-ERROR", str(ctx.exception))
            self.assertEqual(target.read_bytes(), b"original")

    def test_temporary_names_are_process_unique(self):
        seen = []
        real_replace = os.replace

        def capture(src, dst):
            seen.append(Path(src).name)
            real_replace(src, dst)

        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.bin"
            with mock.patch("brain.san_publish.os.replace", side_effect=capture):
                atomic_write_bytes(target, b"a")
                atomic_write_bytes(target, b"b")
            self.assertEqual(len(seen), 2)
            self.assertNotEqual(seen[0], seen[1])
            for name in seen:
                self.assertIn(f".tmp-{os.getpid()}-", name)

    def test_snapshot_and_restore_roundtrip(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.bin"
            target.write_bytes(b"before")
            snap = snapshot_file(target)
            self.assertEqual(snap, b"before")
            atomic_write_bytes(target, b"after")
            restore_file(target, snap)
            self.assertEqual(target.read_bytes(), b"before")

    def test_snapshot_none_for_missing_and_restore_unlinks(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "absent.bin"
            self.assertIsNone(snapshot_file(target))
            # Simulate a transaction that created the file, then restore to absent.
            atomic_write_bytes(target, b"created")
            self.assertTrue(target.exists())
            restore_file(target, None)
            self.assertFalse(target.exists())

    def test_canonical_san_path(self):
        san_dir = Path("/repo/.san")
        self.assertEqual(
            canonical_san_path(san_dir, "brain/server.py"),
            san_dir / "brain/server.py.san",
        )


class SanStrictIndexTests(unittest.TestCase):
    """Strict vs best-effort index build in server.py."""

    def _server(self):
        import brain.server as server
        return server

    def _write_san(self, san_dir: Path, rel: str, body: str) -> None:
        p = san_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)

    def test_build_san_index_returns_dict_without_writing(self):
        server = self._server()
        with TemporaryDirectory() as tmp:
            san_dir = Path(tmp)
            self._write_san(
                san_dir, "a.py.san", "pkg.A @svc {\n  src: 1-1\n}\n"
            )
            index = server._build_san_index(san_dir)
            self.assertIn("pkg.A", index)
            self.assertEqual(index["pkg.A"]["kind"], "svc")
            # Pure build must not persist _index.json.
            self.assertFalse((san_dir / "_index.json").exists())

    def test_rebuild_non_strict_swallows_write_failure(self):
        server = self._server()
        with TemporaryDirectory() as tmp:
            san_dir = Path(tmp)
            self._write_san(
                san_dir, "a.py.san", "pkg.A @svc {\n  src: 1-1\n}\n"
            )
            with mock.patch(
                "brain.server.atomic_write_bytes",
                side_effect=OSError("disk full"),
            ):
                # Best-effort: no exception propagates.
                server._rebuild_san_index(san_dir)

    def test_rebuild_strict_propagates_write_failure(self):
        server = self._server()
        with TemporaryDirectory() as tmp:
            san_dir = Path(tmp)
            self._write_san(
                san_dir, "a.py.san", "pkg.A @svc {\n  src: 1-1\n}\n"
            )
            with mock.patch(
                "brain.server.atomic_write_bytes",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaises(OSError):
                    server._rebuild_san_index(san_dir, strict=True)

    def test_rebuild_strict_propagates_scan_failure(self):
        server = self._server()
        with TemporaryDirectory() as tmp:
            san_dir = Path(tmp)
            self._write_san(
                san_dir, "a.py.san", "pkg.A @svc {\n  src: 1-1\n}\n"
            )

            def boom(*a, **k):
                raise OSError("cannot read")

            with mock.patch.object(Path, "read_text", boom):
                with self.assertRaises(OSError):
                    server._rebuild_san_index(san_dir, strict=True)


import hashlib
import threading


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PublishSanTests(unittest.TestCase):
    CLAUDE_MODEL = "claude-sonnet-4-6"
    CODEX_MODEL = "gpt-5.4-mini"
    CODEX_EFFORT = "medium"

    def setUp(self):
        import brain.server as server
        self.server = server
        self._tmp = TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        self.san = self.repo / ".san"
        self._metrics_dir = TemporaryDirectory()
        self.metrics = Path(self._metrics_dir.name) / "brain_metrics.jsonl"

        self._patches = [
            mock.patch.object(server, "_resolve_repo_path", return_value=self.repo),
            mock.patch.object(server, "_get_repo_paths", return_value={"demo": self.repo}),
            mock.patch.object(server, "METRICS_FILE", self.metrics),
            mock.patch.object(server, "_load_config", return_value={}),
            mock.patch.dict(server._SAN_FRESH_CHECKED, {}, clear=True),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()
        self._metrics_dir.cleanup()

    # helpers ---------------------------------------------------------------

    def _src(self, rel: str, body: str) -> str:
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        return _sha(body.encode("utf-8"))

    def _san_text(self, name: str) -> str:
        return f"{name} @module {{\n  src: 1-1\n  purpose: x\n}}\n"

    def _publish(self, **over):
        args = dict(
            repo="demo",
            source_path="src/A.py",
            expected_source_sha256=over.pop("digest", None),
            san_content=over.pop("san", self._san_text("A")),
            provider="claude",
            model=self.CLAUDE_MODEL,
            reasoning_effort=None,
        )
        args.update(over)
        if args["expected_source_sha256"] is None:
            args["expected_source_sha256"] = _sha(b"a = 1\n")
        return self.server.publish_san(**args)

    def _snapshot(self):
        tree = {}
        if self.san.exists():
            for p in sorted(self.san.rglob("*")):
                if p.is_file():
                    st = p.stat()
                    tree[str(p.relative_to(self.san))] = (p.read_bytes(), st.st_mtime_ns)
        metrics = self.metrics.read_bytes() if self.metrics.exists() else None
        return tree, metrics

    def _temp_leftovers(self):
        return [p for p in self.repo.rglob(".*.tmp-*")]

    # validation / rejection ------------------------------------------------

    def test_rejects_absolute_traversal_and_symlink_escape(self):
        self._src("src/A.py", "a = 1\n")
        # Absolute path.
        r = self._publish(source_path="/etc/passwd")
        self.assertEqual(r["status"], "invalid_source_path")
        # Parent traversal.
        r = self._publish(source_path="../outside.py")
        self.assertEqual(r["status"], "invalid_source_path")
        # Symlink escaping the repo.
        outside_dir = Path(self._metrics_dir.name) / "outside"
        outside_dir.mkdir(exist_ok=True)
        (outside_dir / "secret.py").write_text("secret = 1\n")
        link = self.repo / "src" / "link.py"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside_dir / "secret.py")
        r = self._publish(source_path="src/link.py",
                          digest=_sha(b"secret = 1\n"))
        self.assertEqual(r["status"], "invalid_source_path")

    def test_rejects_unsupported_and_skipped_source(self):
        self._src("src/View.vue", "<template/>\n")
        r = self._publish(source_path="src/View.vue",
                          digest=_sha(b"<template/>\n"))
        self.assertEqual(r["status"], "unsupported_extension")

        self._src(".wxt/types/x.ts", "export const x = 1\n")
        r = self._publish(source_path=".wxt/types/x.ts",
                          digest=_sha(b"export const x = 1\n"))
        self.assertEqual(r["status"], "skipped_source")

    def test_source_not_found_is_reported(self):
        r = self._publish(source_path="src/Nope.py", digest=_sha(b"x"))
        self.assertEqual(r["status"], "source_not_found")

    def test_rejects_invalid_or_mismatched_provider_model_effort(self):
        self._src("src/A.py", "a = 1\n")
        # Unknown provider.
        r = self._publish(provider="kimi")
        self.assertEqual(r["status"], "provider_mismatch")
        # Wrong model.
        r = self._publish(model="claude-3-opus")
        self.assertEqual(r["status"], "model_mismatch")
        # Claude with a non-empty reasoning effort.
        r = self._publish(reasoning_effort="high")
        self.assertEqual(r["status"], "reasoning_effort_mismatch")
        # Codex with wrong effort.
        r = self._publish(provider="codex", model=self.CODEX_MODEL,
                          reasoning_effort="high")
        self.assertEqual(r["status"], "reasoning_effort_mismatch")
        # Codex with correct model + effort publishes.
        r = self._publish(provider="codex", model=self.CODEX_MODEL,
                          reasoning_effort=self.CODEX_EFFORT)
        self.assertEqual(r["status"], "published")

    def test_rejects_invalid_digest_format(self):
        self._src("src/A.py", "a = 1\n")
        r = self._publish(digest="NOTAHASH")
        self.assertEqual(r["status"], "invalid_digest")
        r = self._publish(digest=_sha(b"a = 1\n").upper())
        self.assertEqual(r["status"], "invalid_digest")

    def test_rejects_changed_source_digest_and_preserves_state(self):
        self._src("src/A.py", "a = 1\n")
        before = self._snapshot()
        r = self._publish(digest=_sha(b"DIFFERENT\n"))
        self.assertEqual(r["status"], "source_changed")
        self.assertEqual(self._snapshot(), before)

    def test_rejects_invalid_candidate_and_preserves_state(self):
        self._src("src/A.py", "a = 1\n")
        before = self._snapshot()
        r = self._publish(san="garbage without a header\n")
        self.assertEqual(r["status"], "invalid_candidate")
        self.assertIn("validation", r)
        self.assertEqual(self._snapshot(), before)

    def test_rejects_compiler_config_invalid(self):
        self._src("src/A.py", "a = 1\n")
        with mock.patch.object(
            self.server, "_load_config",
            return_value={"san_compiler": {"allow_expensive_fallback": True}},
        ):
            r = self._publish()
        self.assertEqual(r["status"], "compiler_config_invalid")

    def test_rejects_repo_not_found(self):
        with mock.patch.object(self.server, "_resolve_repo_path", return_value=None):
            r = self._publish()
        self.assertEqual(r["status"], "repo_not_found")

    # success ---------------------------------------------------------------

    def test_publishes_to_canonical_append_path(self):
        self._src("src/A.py", "a = 1\n")
        r = self._publish()
        self.assertEqual(r["status"], "published")
        self.assertEqual(r["san_path"], ".san/src/A.py.san")
        # Canonical append form on disk (NOT legacy A.san).
        self.assertTrue((self.san / "src/A.py.san").exists())
        self.assertFalse((self.san / "src/A.san").exists())

    def test_updates_hash_index_and_metric_after_replace(self):
        digest = self._src("src/A.py", "a = 1\n")
        r = self._publish(digest=digest)
        self.assertEqual(r["status"], "published")
        # Hash recorded.
        hashes = json.loads((self.san / ".san_hashes.json").read_text())
        self.assertEqual(hashes["src/A.py"], digest)
        # Index rebuilt with the block.
        index = json.loads((self.san / "_index.json").read_text())
        self.assertIn("A", index)
        # Metric appended with san_publish kind + token fields.
        lines = [json.loads(x) for x in self.metrics.read_text().splitlines() if x.strip()]
        pub = [m for m in lines if m.get("kind") == "san_publish"]
        self.assertEqual(len(pub), 1)
        self.assertEqual(pub[0]["provider"], "claude")
        self.assertEqual(pub[0]["model"], self.CLAUDE_MODEL)
        self.assertIn("input_tokens", pub[0])
        self.assertIn("output_tokens", pub[0])
        self.assertIn("gen_cost", pub[0])

    # transactional rollback ------------------------------------------------

    def test_write_failure_preserves_all_prior_state(self):
        self._src("src/A.py", "a = 1\n")
        # Prior published SAN state.
        self._publish()
        before = self._snapshot()
        with mock.patch.object(
            self.server, "atomic_write_bytes", side_effect=OSError("disk full"),
        ):
            r = self._publish()
        self.assertIn(r["status"], ("publication_failed", "rollback_failed"))
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._temp_leftovers(), [])

    def test_hash_failure_rolls_back_all_prior_state(self):
        self._src("src/A.py", "a = 1\n")
        self._publish()  # establish prior state
        before = self._snapshot()

        real = self.server.atomic_write_bytes
        calls = {"n": 0}

        def flaky(path, data):
            calls["n"] += 1
            # 1st write = SAN (allow); 2nd = hashes (fail).
            if calls["n"] == 2:
                raise OSError("hash write failed")
            return real(path, data)

        with mock.patch.object(self.server, "atomic_write_bytes", side_effect=flaky):
            r = self._publish(san=self._san_text("A2"))
        self.assertIn(r["status"], ("publication_failed", "rollback_failed"))
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._temp_leftovers(), [])

    def test_index_failure_rolls_back_all_prior_state(self):
        self._src("src/A.py", "a = 1\n")
        self._publish()
        before = self._snapshot()
        with mock.patch.object(
            self.server, "_rebuild_san_index", side_effect=OSError("index boom"),
        ):
            r = self._publish(san=self._san_text("A2"))
        self.assertIn(r["status"], ("publication_failed", "rollback_failed"))
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._temp_leftovers(), [])

    def test_metric_failure_rolls_back_all_prior_state(self):
        self._src("src/A.py", "a = 1\n")
        self._publish()
        before = self._snapshot()
        with mock.patch.object(
            self.server, "_append_metric_strict", side_effect=OSError("metric boom"),
        ):
            r = self._publish(san=self._san_text("A2"))
        self.assertIn(r["status"], ("publication_failed", "rollback_failed"))
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._temp_leftovers(), [])

    def test_first_publication_failure_removes_created_empty_directories(self):
        # No .san/ yet. A deep source path forces new nested dirs; a failure
        # must remove the newly created empty directories.
        self._src("pkg/deep/A.py", "a = 1\n")
        self.assertFalse(self.san.exists())
        with mock.patch.object(
            self.server, "_rebuild_san_index", side_effect=OSError("boom"),
        ):
            r = self._publish(source_path="pkg/deep/A.py", digest=_sha(b"a = 1\n"))
        self.assertIn(r["status"], ("publication_failed", "rollback_failed"))
        # Newly created SAN dirs removed (best-effort): the SAN file is gone.
        self.assertFalse((self.san / "pkg/deep/A.py.san").exists())
        self.assertEqual(self._temp_leftovers(), [])

    def test_removes_process_unique_temp_files_after_failure(self):
        self._src("src/A.py", "a = 1\n")
        with mock.patch.object(
            self.server, "_rebuild_san_index", side_effect=OSError("boom"),
        ):
            self._publish()
        self.assertEqual(self._temp_leftovers(), [])

    def test_rechecks_digest_immediately_before_replace(self):
        # A source that changes AFTER validation but BEFORE the atomic replace
        # must be caught by the pre-replace re-hash (compare-and-swap).
        digest = self._src("src/A.py", "a = 1\n")
        before = self._snapshot()

        real_replace = self.server.atomic_write_bytes

        def mutate_then_write(path, data):
            # Simulate a concurrent source edit landing between validation and
            # the SAN write.
            (self.repo / "src/A.py").write_text("a = 999\n")
            return real_replace(path, data)

        # Only mutate on the first (SAN) write attempt.
        with mock.patch.object(self.server, "_hash_source", wraps=self.server._hash_source):
            (self.repo / "src/A.py").write_text("a = 1\n")
            # Re-hash guard: change the file so the pre-replace hash differs.
            def pre_replace_hash(p):
                (self.repo / "src/A.py").write_text("a = 999\n")
                return _sha(b"a = 999\n")
            with mock.patch.object(self.server, "_hash_source", side_effect=pre_replace_hash):
                r = self._publish(digest=digest)
        self.assertEqual(r["status"], "source_changed")
        self.assertEqual(self._snapshot(), before)

    def test_serializes_concurrent_publications(self):
        # Two concurrent publications of different sources must both succeed and
        # leave a consistent hash+index (repo-wide lock serializes shared-file
        # writes).
        self._src("src/A.py", "a = 1\n")
        self._src("src/B.py", "b = 2\n")
        results = {}

        def pub(name, digest):
            results[name] = self.server.publish_san(
                repo="demo", source_path=f"src/{name}.py",
                expected_source_sha256=digest,
                san_content=self._san_text(name),
                provider="claude", model=self.CLAUDE_MODEL,
            )

        t1 = threading.Thread(target=pub, args=("A", _sha(b"a = 1\n")))
        t2 = threading.Thread(target=pub, args=("B", _sha(b"b = 2\n")))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(results["A"]["status"], "published")
        self.assertEqual(results["B"]["status"], "published")
        hashes = json.loads((self.san / ".san_hashes.json").read_text())
        self.assertEqual(hashes["src/A.py"], _sha(b"a = 1\n"))
        self.assertEqual(hashes["src/B.py"], _sha(b"b = 2\n"))
        index = json.loads((self.san / "_index.json").read_text())
        self.assertIn("A", index)
        self.assertIn("B", index)

    def test_result_and_metric_never_contain_source_or_san_content(self):
        self._src("src/A.py", "SECRET_SOURCE_TOKEN = 1\n")
        digest = _sha(b"SECRET_SOURCE_TOKEN = 1\n")
        san = "A @module {\n  src: 1-1\n  purpose: SECRET_SAN_TOKEN\n}\n"
        r = self._publish(digest=digest, san=san)
        self.assertEqual(r["status"], "published")
        blob = json.dumps(r)
        self.assertNotIn("SECRET_SOURCE_TOKEN", blob)
        self.assertNotIn("SECRET_SAN_TOKEN", blob)
        metrics_blob = self.metrics.read_text()
        self.assertNotIn("SECRET_SOURCE_TOKEN", metrics_blob)
        self.assertNotIn("SECRET_SAN_TOKEN", metrics_blob)


if __name__ == "__main__":
    unittest.main()
