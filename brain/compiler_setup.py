import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

try:
    from .compiler_config import (
        SanCompilerConfig,
        load_san_compiler_config,
        parse_san_compiler_config,
    )
except ImportError:
    from compiler_config import (  # type: ignore[no-redef]
        SanCompilerConfig,
        load_san_compiler_config,
        parse_san_compiler_config,
    )


ManagedState = Literal["missing", "current", "stale", "conflict"]
Provider = Literal["claude", "codex"]
ADAPTER_VERSION = 1
MANAGED_MARKER = "agent-brain-managed:san-compiler"


@dataclass(frozen=True)
class ManagedArtifactStatus:
    path: Path
    state: ManagedState
    expected_version: int
    installed_version: int | None


@dataclass(frozen=True)
class InstallResult:
    path: Path
    previous_state: ManagedState
    changed: bool


class ManagedArtifactConflict(RuntimeError):
    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"{path}: unmanaged SAN compiler artifact; preserved")


def inspect_managed_artifact(
    path: str | Path,
    expected_content: str,
) -> ManagedArtifactStatus:
    artifact_path = Path(path)
    try:
        installed = artifact_path.read_bytes()
    except FileNotFoundError:
        return ManagedArtifactStatus(
            path=artifact_path,
            state="missing",
            expected_version=ADAPTER_VERSION,
            installed_version=None,
        )

    marker = MANAGED_MARKER.encode("utf-8")
    if marker not in installed:
        return ManagedArtifactStatus(
            path=artifact_path,
            state="conflict",
            expected_version=ADAPTER_VERSION,
            installed_version=None,
        )

    version_match = re.search(rb"\bversion=(\d+)\b", installed)
    installed_version = int(version_match.group(1)) if version_match else None
    state: ManagedState = (
        "current" if installed == expected_content.encode("utf-8") else "stale"
    )
    return ManagedArtifactStatus(
        path=artifact_path,
        state=state,
        expected_version=ADAPTER_VERSION,
        installed_version=installed_version,
    )


def install_managed_artifact(
    path: str | Path,
    rendered_content: str,
) -> InstallResult:
    artifact_path = Path(path)
    status = inspect_managed_artifact(artifact_path, rendered_content)
    if status.state == "conflict":
        raise ManagedArtifactConflict(artifact_path)
    if status.state == "current":
        return InstallResult(
            path=artifact_path,
            previous_state="current",
            changed=False,
        )

    _atomic_write(artifact_path, rendered_content.encode("utf-8"))

    return InstallResult(
        path=artifact_path,
        previous_state=status.state,
        changed=True,
    )


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            delete=False,
            dir=path.parent,
            prefix=f"{path.name}.tmp-",
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _restore_artifact(
    path: Path,
    previous_bytes: bytes | None,
    previous_stat: os.stat_result | None,
) -> None:
    if previous_bytes is None:
        path.unlink(missing_ok=True)
        return
    _atomic_write(path, previous_bytes)
    if previous_stat is not None:
        os.chmod(path, previous_stat.st_mode)
        os.utime(
            path,
            ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns),
        )


def _effective_config(config: SanCompilerConfig) -> SanCompilerConfig:
    return parse_san_compiler_config({
        "san_compiler": {
            "claude": {"model": config.claude.model},
            "codex": {
                "model": config.codex.model,
                "reasoning_effort": config.codex.reasoning_effort,
            },
            "allow_expensive_fallback": config.allow_expensive_fallback,
        }
    })


def _render(template: str, replacements: dict[str, str]) -> str:
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    unresolved = re.search(r"\{\{[A-Z0-9_]+\}\}", rendered)
    if unresolved:
        raise ValueError(f"unresolved adapter placeholder: {unresolved.group(0)}")
    return rendered


def render_claude_adapter(
    config: SanCompilerConfig,
    contract_path: Path,
    template: str,
) -> str:
    effective = _effective_config(config)
    return _render(template, {
        "{{CLAUDE_MODEL}}": effective.claude.model,
        "{{CONTRACT_PATH}}": str(contract_path),
    })


def render_codex_agent(
    config: SanCompilerConfig,
    skill_path: Path,
    contract_path: Path,
    template: str,
) -> str:
    effective = _effective_config(config)
    return _render(template, {
        "{{CODEX_MODEL}}": effective.codex.model,
        "{{CODEX_REASONING_EFFORT}}": effective.codex.reasoning_effort,
        "{{CODEX_SKILL_PATH}}": str(skill_path),
        "{{CONTRACT_PATH}}": str(contract_path),
    })


def render_codex_skill(contract_path: Path, template: str) -> str:
    rendered = _render(template, {"{{CONTRACT_PATH}}": str(contract_path)})
    marker = (
        f"<!-- {MANAGED_MARKER} provider=codex artifact=skill "
        f"version={ADAPTER_VERSION} -->"
    )
    return f"{rendered.rstrip()}\n\n{marker}\n"


def install_claude_adapter(
    *,
    claude_home: str | Path,
    config: SanCompilerConfig,
    assets_root: str | Path,
) -> InstallResult:
    root = Path(assets_root)
    contract_path = root / "compiler-contract.md"
    template = (root / "adapters" / "claude" / "brain-compiler.md").read_text(
        encoding="utf-8"
    )
    rendered = render_claude_adapter(config, contract_path, template)
    return install_managed_artifact(
        Path(claude_home) / "agents" / "brain-compiler.md",
        rendered,
    )


def install_codex_adapters(
    *,
    codex_home: str | Path,
    config: SanCompilerConfig,
    assets_root: str | Path,
) -> tuple[InstallResult, InstallResult]:
    root = Path(assets_root)
    contract_path = root / "compiler-contract.md"
    home = Path(codex_home)
    agent_path = home / "agents" / "brain-compiler.toml"
    skill_path = home / "skills" / "brain-compiler" / "SKILL.md"
    agent_template = (
        root / "adapters" / "codex" / "brain-compiler.toml"
    ).read_text(encoding="utf-8")
    skill_template = (
        root / "adapters" / "codex" / "brain-compiler" / "SKILL.md"
    ).read_text(encoding="utf-8")
    effective = _effective_config(config)
    rendered_agent = render_codex_agent(
        effective,
        skill_path,
        contract_path,
        agent_template,
    )
    rendered_skill = render_codex_skill(contract_path, skill_template)
    statuses = (
        inspect_managed_artifact(agent_path, rendered_agent),
        inspect_managed_artifact(skill_path, rendered_skill),
    )
    for status in statuses:
        if status.state == "conflict":
            raise ManagedArtifactConflict(status.path)

    agent_bytes = agent_path.read_bytes() if agent_path.exists() else None
    agent_stat = agent_path.stat() if agent_path.exists() else None
    agent_result = install_managed_artifact(agent_path, rendered_agent)
    try:
        skill_result = install_managed_artifact(skill_path, rendered_skill)
    except Exception:
        if agent_result.changed:
            _restore_artifact(agent_path, agent_bytes, agent_stat)
        raise
    return agent_result, skill_result
