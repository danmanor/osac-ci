"""The rules of the native pull request flow, loaded from ``policy/flow.yml``.

The values used to be constants in ``e2e_start.py`` and ``bot_approve.py``. They live in a YAML file so a person can
read and review them. This module loads and validates that file and fails closed: a missing or invalid file is an
error, never a default, because a default in code would be a second copy of a rule that can drift from the file a
reviewer read.

A repository may extend one thing only (``checks.not_cheap_extra``). Every other key of a repository's own ``flow``
section is refused, so a repository cannot widen who starts an E2E with secrets or what a bot may approve.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from osac_ci import yamlio

FLOW_FILE_ENV = "OSAC_CI_FLOW_FILE"
# The only roles that may count as a person who can approve with write access.
_WRITE_ROLES = frozenset({"admin", "maintain", "write"})


class FlowError(ValueError):
    """The rules file is missing, unreadable or invalid, or a repository tried to change a rule it may not change."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _non_empty(values: tuple[str, ...], what: str) -> tuple[str, ...]:
    if not values:
        raise ValueError(f"{what} must not be empty")
    if any(not v or v != v.strip() for v in values):
        raise ValueError(f"{what} must not hold an empty value or one with spaces around it")
    if len(set(values)) != len(values):
        raise ValueError(f"{what} must not repeat a value")
    return values


class Approval(_Strict):
    trusted_bots: tuple[str, ...]
    write_permissions: tuple[str, ...]

    @field_validator("trusted_bots")
    @classmethod
    def _bots(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        _non_empty(v, "approval.trusted_bots")
        bad = [b for b in v if not b.endswith("[bot]")]
        if bad:
            raise ValueError(f"approval.trusted_bots may hold only bot accounts (login ends with [bot]): {bad}")
        return v

    @field_validator("write_permissions")
    @classmethod
    def _roles(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        _non_empty(v, "approval.write_permissions")
        bad = [r for r in v if r not in _WRITE_ROLES]
        if bad:
            raise ValueError(f"approval.write_permissions may hold only {sorted(_WRITE_ROLES)}, not {bad}")
        return v


class Checks(_Strict):
    not_cheap: tuple[str, ...]

    @field_validator("not_cheap")
    @classmethod
    def _names(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _non_empty(v, "checks.not_cheap")


class BotApprove(_Strict):
    bots: tuple[str, ...]
    committer: str
    dependency_file_names: tuple[str, ...]
    dependency_file_patterns: tuple[str, ...]

    @field_validator("bots")
    @classmethod
    def _bots(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        _non_empty(v, "bot_approve.bots")
        bad = [b for b in v if not b.endswith("[bot]")]
        if bad:
            raise ValueError(f"bot_approve.bots may hold only bot accounts (login ends with [bot]): {bad}")
        return v

    @field_validator("committer")
    @classmethod
    def _committer(cls, v: str) -> str:
        if not v or v != v.strip():
            raise ValueError("bot_approve.committer must not be empty")
        return v

    @field_validator("dependency_file_names")
    @classmethod
    def _names(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        _non_empty(v, "bot_approve.dependency_file_names")
        bad = [n for n in v if "/" in n or any(c in n for c in "*?[")]
        if bad:
            raise ValueError(f"bot_approve.dependency_file_names are base names without a path or a wildcard: {bad}")
        return v

    @field_validator("dependency_file_patterns")
    @classmethod
    def _patterns(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        _non_empty(v, "bot_approve.dependency_file_patterns")
        bad = [p for p in v if not p or "/" in p or p[0] in "*?["]
        if bad:
            raise ValueError(
                "bot_approve.dependency_file_patterns match the base name and must start with a literal character "
                f"(a pattern such as '*', '*.sh', '?*.sh' or '[a-z]*' would approve far too much): {bad}"
            )
        return v


class Flow(_Strict):
    version: int
    approval: Approval
    checks: Checks
    bot_approve: BotApprove
    repository_may_extend: tuple[str, ...]

    @field_validator("version")
    @classmethod
    def _version(cls, v: int) -> int:
        if v != 1:
            raise ValueError(f"unsupported rules file version {v}")
        return v

    @field_validator("repository_may_extend")
    @classmethod
    def _extend(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if v != ("checks.not_cheap_extra",):
            raise ValueError("repository_may_extend may only list checks.not_cheap_extra")
        return v

    # The sets the decisions use. Each is built once from the validated file.
    @property
    def trusted_bots(self) -> frozenset[str]:
        return frozenset(self.approval.trusted_bots)

    @property
    def write_permissions(self) -> frozenset[str]:
        return frozenset(self.approval.write_permissions)

    @property
    def not_cheap(self) -> frozenset[str]:
        return frozenset(self.checks.not_cheap)

    @property
    def dependency_file_names(self) -> frozenset[str]:
        return frozenset(self.bot_approve.dependency_file_names)


def default_flow_path() -> Path:
    """The rules file: the one named by the environment, else the copy shipped inside the package (a wheel install),
    else ``policy/flow.yml`` next to ``src/`` (a checkout, where it is also what a reviewer reads)."""
    override = os.environ.get(FLOW_FILE_ENV)
    if override:
        return Path(override)
    packaged = Path(__file__).resolve().parent / "flow.yml"
    if packaged.is_file():
        return packaged
    return Path(__file__).resolve().parents[2] / "policy" / "flow.yml"


def load_flow(path: Path | None = None) -> Flow:
    path = path or default_flow_path()
    try:
        raw = yamlio.load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise FlowError(f"the rules file {path} does not exist") from e
    except (OSError, yaml.YAMLError) as e:
        raise FlowError(f"the rules file {path} cannot be read: {e}") from e
    if not isinstance(raw, dict):
        raise FlowError(f"the rules file {path} must be a mapping")
    try:
        return Flow.model_validate(raw)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
        raise FlowError(f"the rules file {path} is invalid: {problems}") from e


@lru_cache(maxsize=1)
def flow() -> Flow:
    """The rules, loaded once per process."""
    return load_flow()


def extend(base: Flow, repository: Mapping[str, Any] | None) -> Flow:
    """The rules plus what one repository may add (``checks.not_cheap_extra``). Anything else is refused."""
    if not repository:
        return base
    unknown = {k: v for k, v in repository.items() if k != "checks"}
    checks = repository.get("checks", {})
    if not isinstance(checks, Mapping):
        raise FlowError("flow.checks of a repository must be a mapping")
    unknown_checks = [k for k in checks if k != "not_cheap_extra"]
    if unknown or unknown_checks:
        keys = sorted(list(unknown) + [f"checks.{k}" for k in unknown_checks])
        raise FlowError(
            f"a repository may only set flow.checks.not_cheap_extra; {keys} belong to the central rules in "
            "osac-ci/policy/flow.yml and need a pull request there"
        )
    extra = checks.get("not_cheap_extra", ())
    if not isinstance(extra, (list, tuple)) or not all(isinstance(x, str) for x in extra):
        raise FlowError("flow.checks.not_cheap_extra must be a list of check names")
    merged = tuple(dict.fromkeys((*base.checks.not_cheap, *extra)))
    try:
        return base.model_copy(update={"checks": Checks(not_cheap=merged)})
    except ValidationError as e:
        raise FlowError(f"flow.checks.not_cheap_extra is invalid: {e.errors()[0]['msg']}") from e
