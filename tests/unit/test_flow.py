"""The rules file policy/flow.yml: every value is pinned, the loader fails closed, a repository can only add checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from osac_ci import bot_approve, e2e_start
from osac_ci.flow import FLOW_FILE_ENV, FlowError, default_flow_path, extend, flow, load_flow

# A copy of the rules as they were hard-coded before they moved into policy/flow.yml. Changing the file without
# changing this test fails the build, so a change of who may approve or what a bot may approve is always deliberate.
PINNED_TRUSTED_BOTS = frozenset({"coderabbitai[bot]", "osac-ci[bot]"})
PINNED_WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})
PINNED_NOT_CHEAP = frozenset({"check-labels", "jira-reference", "E2E"})
PINNED_BOTS = ("dependabot[bot]", "red-hat-konflux-kflux-prd-rh02[bot]")
PINNED_COMMITTER = "web-flow"
PINNED_DEPENDENCY_FILE_NAMES = frozenset(
    {
        "go.mod",
        "go.sum",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "pyproject.toml",
    }
)
PINNED_DEPENDENCY_FILE_PATTERNS = ("Containerfile*", "Dockerfile*", "requirements*.txt")

GOOD = """
version: 1
approval:
  trusted_bots: ['coderabbitai[bot]']
  write_permissions: [admin, write]
checks:
  not_cheap: [E2E]
bot_approve:
  bots: ['dependabot[bot]']
  committer: web-flow
  dependency_file_names: [go.mod]
  dependency_file_patterns: ["Dockerfile*"]
repository_may_extend: [checks.not_cheap_extra]
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "flow.yml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_rules_file_holds_exactly_the_rules_that_were_hard_coded() -> None:
    rules = flow()
    assert rules.trusted_bots == PINNED_TRUSTED_BOTS
    assert rules.write_permissions == PINNED_WRITE_PERMISSIONS
    assert rules.not_cheap == PINNED_NOT_CHEAP
    assert rules.bot_approve.bots == PINNED_BOTS
    assert rules.bot_approve.committer == PINNED_COMMITTER
    assert rules.dependency_file_names == PINNED_DEPENDENCY_FILE_NAMES
    assert rules.bot_approve.dependency_file_patterns == PINNED_DEPENDENCY_FILE_PATTERNS


def test_the_decisions_read_the_rules_from_the_file() -> None:
    assert e2e_start.TRUSTED_BOTS == PINNED_TRUSTED_BOTS
    assert e2e_start.WRITE_PERMISSIONS == PINNED_WRITE_PERMISSIONS
    assert e2e_start.NOT_CHEAP == PINNED_NOT_CHEAP
    assert bot_approve.BOTS == PINNED_BOTS
    assert bot_approve.GITHUB_COMMITTER == PINNED_COMMITTER
    assert bot_approve.DEPENDENCY_FILE_NAMES == PINNED_DEPENDENCY_FILE_NAMES
    assert bot_approve.DEPENDENCY_FILE_PATTERNS == PINNED_DEPENDENCY_FILE_PATTERNS


def test_the_rules_file_lists_what_a_repository_may_extend() -> None:
    assert flow().repository_may_extend == ("checks.not_cheap_extra",)


def test_a_valid_file_loads(tmp_path: Path) -> None:
    assert load_flow(write(tmp_path, GOOD)).not_cheap == frozenset({"E2E"})


def test_a_missing_file_is_an_error_not_a_default(tmp_path: Path) -> None:
    with pytest.raises(FlowError, match="does not exist"):
        load_flow(tmp_path / "nope.yml")


def test_the_environment_can_name_another_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    other = write(tmp_path, GOOD)
    monkeypatch.setenv(FLOW_FILE_ENV, str(other))
    assert default_flow_path() == other


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("version: 1", "version: 2", "unsupported rules file version"),
        ("trusted_bots: ['coderabbitai[bot]']", "trusted_bots: []", "must not be empty"),
        ("trusted_bots: ['coderabbitai[bot]']", "trusted_bots: [alice]", "only bot accounts"),
        ("write_permissions: [admin, write]", "write_permissions: [admin, triage]", "only"),
        ("write_permissions: [admin, write]", "write_permissions: [read]", "only"),
        ("not_cheap: [E2E]", "not_cheap: [E2E, E2E]", "must not repeat"),
        ("bots: ['dependabot[bot]']", "bots: [dependabot]", "only bot accounts"),
        ("dependency_file_names: [go.mod]", "dependency_file_names: [src/go.mod]", "base names"),
        ("dependency_file_names: [go.mod]", "dependency_file_names: ['*.mod']", "base names"),
        ('dependency_file_patterns: ["Dockerfile*"]', 'dependency_file_patterns: ["*"]', "far too much"),
        ('dependency_file_patterns: ["Dockerfile*"]', 'dependency_file_patterns: ["*.sh"]', "far too much"),
        ('dependency_file_patterns: ["Dockerfile*"]', 'dependency_file_patterns: ["?*.sh"]', "far too much"),
        ('dependency_file_patterns: ["Dockerfile*"]', 'dependency_file_patterns: ["[a-z]*"]', "far too much"),
        ("repository_may_extend: [checks.not_cheap_extra]", "repository_may_extend: [approval.trusted_bots]", "only"),
        ("committer: web-flow", "committer: ''", "must not be empty"),
    ],
)
def test_an_invalid_rule_is_refused(tmp_path: Path, old: str, new: str, message: str) -> None:
    assert old in GOOD
    with pytest.raises(FlowError, match=message):
        load_flow(write(tmp_path, GOOD.replace(old, new)))


def test_an_unknown_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FlowError, match="invalid"):
        load_flow(write(tmp_path, GOOD + "extra: true\n"))


def test_a_repeated_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FlowError, match="cannot be read"):
        load_flow(write(tmp_path, GOOD + "checks:\n  not_cheap: [x]\n"))


def test_a_repository_can_add_a_check_that_is_not_cheap() -> None:
    merged = extend(flow(), {"checks": {"not_cheap_extra": ["deploy-preview"]}})
    assert merged.not_cheap == PINNED_NOT_CHEAP | {"deploy-preview"}
    assert flow().not_cheap == PINNED_NOT_CHEAP  # the central rules are not changed in place


def test_nothing_to_add_leaves_the_rules_as_they_are() -> None:
    assert extend(flow(), None) is flow()
    assert extend(flow(), {}) is flow()


@pytest.mark.parametrize(
    "repository",
    [
        {"approval": {"trusted_bots": ["mallory[bot]"]}},
        {"approval": {"write_permissions": ["read"]}},
        {"bot_approve": {"dependency_file_patterns": ["*.sh"]}},
        {"bot_approve": {"bots": ["mallory[bot]"]}},
        {"checks": {"not_cheap": []}},
        {"checks": {"not_cheap_extra": ["x"], "not_cheap": ["y"]}},
        {"repository_may_extend": ["approval.trusted_bots"]},
        {"version": 2},
    ],
)
def test_a_repository_cannot_change_a_central_rule(repository: dict[str, object]) -> None:
    with pytest.raises(FlowError, match="central rules"):
        extend(flow(), repository)


def test_the_extra_checks_must_be_names() -> None:
    with pytest.raises(FlowError, match="list of check names"):
        extend(flow(), {"checks": {"not_cheap_extra": "deploy-preview"}})
    with pytest.raises(FlowError, match="list of check names"):
        extend(flow(), {"checks": {"not_cheap_extra": [1]}})
    with pytest.raises(FlowError, match="invalid"):
        extend(flow(), {"checks": {"not_cheap_extra": [" padded "]}})


def test_the_wheel_carries_the_rules_file_next_to_the_code() -> None:
    import tomllib

    root = Path(__file__).resolve().parents[2]
    build = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"]["build"]["targets"]
    assert build["wheel"]["force-include"] == {"policy/flow.yml": "osac_ci/flow.yml"}


def test_a_packaged_copy_is_preferred_to_the_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import osac_ci.flow as flow_module

    package = tmp_path / "osac_ci"
    package.mkdir()
    (package / "flow.yml").write_text(GOOD, encoding="utf-8")
    monkeypatch.delenv(FLOW_FILE_ENV, raising=False)
    monkeypatch.setattr(flow_module, "__file__", str(package / "flow.py"))
    assert default_flow_path() == package / "flow.yml"
