"""Structural validation and strict atomic primitives for SAN publication.

This module is provider-neutral and never invokes an LLM or provider CLI. It
only inspects candidate SAN *text* structurally and provides reusable atomic
filesystem operations. Validation results carry structural metadata only —
never source or SAN body content — so the Agent Brain server can validate a
compiler's output without ever handling or persisting model-authored prose
beyond what it writes to the canonical ``.san`` file.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path


# --- SAN v2 grammar --------------------------------------------------------

# Column-zero header: `<qualified_name> @<kind> {`
SAN_HEADER_RE = re.compile(r"^(\S+)\s+@(\w+)\s*\{$")
# First field of every block: `  src: <start>-<end>` (both >= 1).
SAN_SRC_RE = re.compile(r"^  src: ([1-9]\d*)-([1-9]\d*)$")
# Lenient prefix used only to distinguish "attempted src line" from "no src".
_SRC_PREFIX_RE = re.compile(r"^  src:")
# Unfinished compiler/template output. Targets a placeholder marker that is a
# whole line, or a field whose ENTIRE value is a bare marker (`purpose: TODO`),
# plus `{{...}}` template holes and `<placeholder>`-style stubs. It must NOT
# reject a legitimate fact that merely mentions TODO in prose
# (`risk: source contains TODO comments`) nor a language-level ellipsis.
SAN_PLACEHOLDER_RE = re.compile(
    r"(?im)^\s*(?:TODO|TBD|PLACEHOLDER|STUB)\s*[:\-]?\s*$|"
    r"^\s*\w[\w.:()\[\], >-]*:\s*(?:TODO|TBD|PLACEHOLDER|STUB)\s*$|"
    r"\{\{[^{}\n]+\}\}|<\s*(?:placeholder|stub|fill[-_ ]?me)\s*>"
)

SAN_MAX_CANDIDATE_BYTES = 1_048_576
SAN_MAX_BLOCKS = 2_000


def validate_san_candidate(
    san_content: str,
    source_line_count: int,
) -> dict[str, object]:
    """Validate candidate SAN text against the SAN v2 structural grammar.

    Returns a dict with only structural metadata:
    ``valid`` (bool), ``errors`` (list of ``{"code", "line"}``), ``block_count``
    (int), ``byte_count`` (int), and ``blocks`` (list of
    ``{"qualified_name", "kind", "src_start", "src_end"}``). No source or SAN
    body text is ever returned.
    """
    errors: list[dict[str, object]] = []

    def add(code: str, line: int) -> None:
        errors.append({"code": code, "line": line})

    byte_count = len(san_content.encode("utf-8"))

    if byte_count > SAN_MAX_CANDIDATE_BYTES:
        add("candidate_too_large", 0)
        return _result(False, errors, 0, byte_count, [])

    if not san_content.strip():
        add("empty_candidate", 0)
        return _result(False, errors, 0, byte_count, [])

    placeholder = SAN_PLACEHOLDER_RE.search(san_content)
    if placeholder:
        line_no = san_content.count("\n", 0, placeholder.start()) + 1
        add("placeholder_marker", line_no)

    # Line-state machine. Headers and closing braces are column-zero.
    lines = san_content.split("\n")
    # A trailing newline yields a final "" element; ignore it for iteration.
    if lines and lines[-1] == "":
        lines = lines[:-1]

    blocks: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    in_block = False
    expecting_src = False
    current: dict[str, object] | None = None

    for idx, line in enumerate(lines):
        line_no = idx + 1
        header = SAN_HEADER_RE.match(line)

        if not in_block:
            if line.strip() == "":
                continue
            if header:
                in_block = True
                expecting_src = True
                current = {
                    "qualified_name": header.group(1),
                    "kind": header.group(2),
                    "src_start": None,
                    "src_end": None,
                    "line": line_no,
                }
                continue
            if line == "}":
                add("stray_closing_brace", line_no)
                continue
            # Anything else outside a block (including comment-style or
            # otherwise malformed header lines) is stray text.
            add("text_outside_block", line_no)
            continue

        # Inside a block.
        if header:
            add("nested_block", line_no)
            continue
        if line == "}":
            if expecting_src:
                add("missing_src", line_no)
            _finish_block(current, blocks, seen, add, line_no)
            in_block = False
            expecting_src = False
            current = None
            continue
        if expecting_src:
            expecting_src = False
            src = SAN_SRC_RE.match(line)
            if src:
                start, end = int(src.group(1)), int(src.group(2))
                if start > end or end > source_line_count:
                    add("invalid_src_range", line_no)
                else:
                    current["src_start"] = start  # type: ignore[index]
                    current["src_end"] = end  # type: ignore[index]
            elif _SRC_PREFIX_RE.match(line):
                # Looks like a src line but out of range / malformed numbers.
                add("invalid_src_range", line_no)
            else:
                add("missing_src", line_no)
            continue
        # Subsequent body lines are not structurally validated here.

    if in_block:
        add("unclosed_block", len(lines))

    if len(blocks) > SAN_MAX_BLOCKS:
        add("too_many_blocks", 0)

    valid = len(errors) == 0
    public_blocks = [
        {
            "qualified_name": b["qualified_name"],
            "kind": b["kind"],
            "src_start": b["src_start"],
            "src_end": b["src_end"],
        }
        for b in blocks
    ]
    return _result(valid, errors, len(blocks), byte_count, public_blocks)


def _finish_block(current, blocks, seen, add, line_no) -> None:
    if current is None:
        return
    key = (current["qualified_name"], current["kind"])
    if key in seen:
        add("duplicate_block", current["line"])
    else:
        seen.add(key)
    blocks.append(current)


def _result(
    valid: bool,
    errors: list[dict[str, object]],
    block_count: int,
    byte_count: int,
    blocks: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "valid": valid,
        "errors": errors,
        "block_count": block_count,
        "byte_count": byte_count,
        "blocks": blocks,
    }


# --- Reusable atomic filesystem operations ---------------------------------


def _temp_path(path: Path) -> Path:
    """Process-unique sibling temp path for an atomic write."""
    return path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` via a same-dir temp + os.replace.

    Flushes and fsyncs the temp before the rename, and always removes the temp
    on failure so a raised error leaves the destination byte-exact.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_path(path)
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def snapshot_file(path: Path) -> bytes | None:
    """Return the current bytes of ``path``, or ``None`` if it does not exist.

    ``None`` marks "file absent before the transaction" so a later
    :func:`restore_file` can unlink rather than recreate it.
    """
    path = Path(path)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def restore_file(path: Path, snapshot: bytes | None) -> None:
    """Restore ``path`` to a prior snapshot.

    A ``None`` snapshot means the file did not exist before the transaction, so
    it is unlinked. Otherwise its exact prior bytes are rewritten atomically.
    """
    path = Path(path)
    if snapshot is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    atomic_write_bytes(path, snapshot)


def canonical_san_path(san_dir: Path, source_rel: str) -> Path:
    """Canonical ``.san`` path for a repo-relative source file."""
    return san_dir / f"{source_rel}.san"
