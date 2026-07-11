import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, cast


CodexReasoningEffort = Literal["none", "low", "medium", "high", "xhigh"]

DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"
DEFAULT_CODEX_MODEL = "gpt-5.4-mini"
DEFAULT_CODEX_REASONING_EFFORT: CodexReasoningEffort = "medium"
SUPPORTED_CODEX_REASONING_EFFORTS = frozenset(
    {"none", "low", "medium", "high", "xhigh"}
)


@dataclass(frozen=True)
class ClaudeCompilerConfig:
    model: str


@dataclass(frozen=True)
class CodexCompilerConfig:
    model: str
    reasoning_effort: CodexReasoningEffort


@dataclass(frozen=True)
class SanCompilerConfig:
    claude: ClaudeCompilerConfig
    codex: CodexCompilerConfig
    allow_expensive_fallback: bool


class CompilerConfigError(ValueError):
    def __init__(self, field: str, detail: str):
        self.field = field
        super().__init__(f"{field}: {detail}")


def _object(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CompilerConfigError(field, "expected JSON object")
    return value


def _model(container: Mapping[str, object], field: str, default: str) -> str:
    value = container.get("model", default)
    if not isinstance(value, str) or not value.strip():
        raise CompilerConfigError(field, "expected non-empty string")
    return value.strip()


def parse_san_compiler_config(root: Mapping[str, object]) -> SanCompilerConfig:
    section = _object(root.get("san_compiler", {}), "san_compiler")
    claude = _object(section.get("claude", {}), "san_compiler.claude")
    codex = _object(section.get("codex", {}), "san_compiler.codex")

    claude_model = _model(
        claude,
        "san_compiler.claude.model",
        DEFAULT_CLAUDE_MODEL,
    )
    codex_model = _model(
        codex,
        "san_compiler.codex.model",
        DEFAULT_CODEX_MODEL,
    )

    effort = codex.get("reasoning_effort", DEFAULT_CODEX_REASONING_EFFORT)
    if (
        not isinstance(effort, str)
        or effort not in SUPPORTED_CODEX_REASONING_EFFORTS
    ):
        raise CompilerConfigError(
            "san_compiler.codex.reasoning_effort",
            "expected one of none, low, medium, high, xhigh",
        )

    allow_expensive_fallback = section.get("allow_expensive_fallback", False)
    if allow_expensive_fallback is not False:
        raise CompilerConfigError(
            "san_compiler.allow_expensive_fallback",
            "must be false",
        )

    return SanCompilerConfig(
        claude=ClaudeCompilerConfig(model=claude_model),
        codex=CodexCompilerConfig(
            model=codex_model,
            reasoning_effort=cast(CodexReasoningEffort, effort),
        ),
        allow_expensive_fallback=False,
    )


def load_san_compiler_config(path: str | Path) -> SanCompilerConfig:
    config_path = Path(path)
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return parse_san_compiler_config({})
    except UnicodeError:
        raise CompilerConfigError(config_path.name, "invalid JSON") from None

    try:
        root = json.loads(raw)
    except json.JSONDecodeError:
        raise CompilerConfigError(config_path.name, "invalid JSON") from None

    if not isinstance(root, Mapping):
        raise CompilerConfigError(config_path.name, "expected JSON object")
    return parse_san_compiler_config(root)
