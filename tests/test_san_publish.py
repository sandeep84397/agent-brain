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


if __name__ == "__main__":
    unittest.main()
