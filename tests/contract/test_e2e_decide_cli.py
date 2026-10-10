"""`osac-ci e2e-decide` against a fake GitHub: reading the pull request, and the command around it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeGitHub
from helpers import ROOT

from osac_ci import cli
from osac_ci.e2e_start import Action, decide, fetch_inputs
from osac_ci.github.api import GitHubError
from osac_ci.policy import load_policy

pytestmark = pytest.mark.contract
REPO = "example/app"
BASE = f"/repos/{REPO}"
POLICY = load_policy(ROOT / "policy" / "osac.yml")  # suites vmaas, caas, bmaas/sanity; its own path filters
POLICY_PATH = str(ROOT / "policy" / "osac.yml")
APP = 15368
SHA = "c" * 40


def check(name: str, conclusion: str | None = "success", app: int = APP) -> dict[str, Any]:
    return {
        "name": name,
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        "started_at": "2026-10-10T10:00:00Z",
        "app": {"id": app},
    }


def world(
    *,
    files: tuple[str, ...] = ("fulfillment-service/main.go",),
    reviews: tuple[dict[str, Any], ...] = (),
    checks: tuple[dict[str, Any], ...] = (),
    required: tuple[str, ...] = ("pre-commit", "e2e-vmaas-gate", "check-labels", "OSAC CI"),
    pr: dict[str, Any] | None = None,
) -> FakeGitHub:
    fake = FakeGitHub()
    payload = {
        "number": 7,
        "state": "open",
        "draft": False,
        "user": {"login": "alice"},
        "head": {"sha": SHA},
        "base": {"ref": "main"},
        **(pr or {}),
    }
    fake.add("GET", f"{BASE}/pulls/7", payload)
    fake.add("GET", f"{BASE}/pulls/7/reviews", list(reviews))
    fake.add("GET", f"{BASE}/pulls/7/files", [{"filename": f} for f in files])
    fake.add("GET", f"{BASE}/commits/{SHA}/check-runs", {"total_count": len(checks), "check_runs": list(checks)})
    fake.add("GET", f"{BASE}/branches/main", {"name": "main"})
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": n, "integration_id": APP} for n in required]},
        }
    ]
    fake.add("GET", f"{BASE}/rules/branches/main", rules)
    fake.routes[("GET", f"{BASE}/pulls")] = (200, [{"number": 7, "head": {"sha": SHA}}])
    return fake


def approval(user: str = "bob", assoc: str = "MEMBER", commit: str = SHA, state: str = "APPROVED") -> dict[str, Any]:
    return {"user": {"login": user}, "state": state, "author_association": assoc, "commit_id": commit}


def test_the_cheap_checks_are_the_required_ones_minus_the_e2e_gates_the_label_check_and_osac_ci() -> None:
    inputs = fetch_inputs(world(), POLICY, REPO, 7)
    assert [r.context for r in inputs.cheap] == ["pre-commit"]
    assert [r.integration_id for r in inputs.cheap] == [APP]


def test_the_suites_and_filters_come_from_the_policy() -> None:
    inputs = fetch_inputs(world(), POLICY, REPO, 7)
    assert [s.name for s in inputs.suites] == ["vmaas", "caas", "bmaas/sanity"]
    assert "e2e-code" in inputs.filters


def test_an_approved_green_pr_starts_every_affected_suite() -> None:
    fake = world(reviews=(approval(),), checks=(check("pre-commit"),))
    d = decide(fetch_inputs(fake, POLICY, REPO, 7))
    assert d.action is Action.START and d.suites == ("vmaas", "caas", "bmaas/sanity")
    assert d.approvers == ("bob",)


def test_a_docs_only_pr_is_skipped() -> None:
    d = decide(fetch_inputs(world(files=("docs/README.md",), reviews=(approval(),)), POLICY, REPO, 7))
    assert d.action is Action.SKIP_NOT_APPLICABLE


def test_a_renamed_file_counts_by_both_names() -> None:
    fake = world(reviews=(approval(),), checks=(check("pre-commit"),))
    fake.add(
        "GET", f"{BASE}/pulls/7/files", [{"filename": "docs/new.md", "previous_filename": "fulfillment-service/old.go"}]
    )
    d = decide(fetch_inputs(fake, POLICY, REPO, 7))
    assert d.action is Action.START  # the old name is a code file


def test_the_reviews_carry_the_association_and_the_commit() -> None:
    fake = world(
        reviews=(approval("bob", "NONE"), approval("carol", "OWNER", commit="d" * 40)), checks=(check("pre-commit"),)
    )
    d = decide(fetch_inputs(fake, POLICY, REPO, 7))
    assert d.action is Action.WAIT_APPROVAL and d.stale_approvals == ("carol",)


def test_a_review_from_a_deleted_account_is_ignored() -> None:
    fake = world(reviews=({"user": None, "state": "APPROVED", "author_association": "MEMBER", "commit_id": SHA},))
    assert decide(fetch_inputs(fake, POLICY, REPO, 7)).action is Action.WAIT_APPROVAL


def test_the_state_of_the_pr_is_read() -> None:
    assert decide(fetch_inputs(world(pr={"draft": True}), POLICY, REPO, 7)).action is Action.SKIP_DRAFT
    assert decide(fetch_inputs(world(pr={"state": "closed"}), POLICY, REPO, 7)).action is Action.SKIP_CLOSED


def test_the_rules_of_the_base_branch_of_the_pr_are_read() -> None:
    fake = world(pr={"base": {"ref": "release-1"}})
    fake.add("GET", f"{BASE}/branches/release-1", {"name": "release-1"})
    fake.add(
        "GET",
        f"{BASE}/rules/branches/release-1",
        [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "other", "integration_id": APP}]},
            }
        ],
    )
    assert [r.context for r in fetch_inputs(fake, POLICY, REPO, 7).cheap] == ["other"]


def test_a_pr_that_cannot_be_read_is_an_error_not_a_start() -> None:
    fake = world()
    fake.add("GET", f"{BASE}/pulls/7", {"message": "Not Found"}, status=404)
    with pytest.raises(GitHubError):
        fetch_inputs(fake, POLICY, REPO, 7)


# the command -----------------------------------------------------------------------------------------------------


def cli_run(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub, *extra: str) -> int:
    monkeypatch.setattr(cli, "build_client", lambda **_: fake)
    return cli.main(["e2e-decide", "--policy", POLICY_PATH, "--repo", REPO, *extra])


def test_the_command_prints_the_decision_for_a_number(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = world(reviews=(approval(),), checks=(check("pre-commit"),))
    assert cli_run(monkeypatch, fake, "--number", "7") == 0
    out = capsys.readouterr().out
    assert "PR #7" in out and "**start**" in out


def test_the_command_finds_the_pr_by_head_commit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = world(reviews=(approval(),), checks=(check("pre-commit"),))
    assert cli_run(monkeypatch, fake, "--head-sha", SHA) == 0
    assert "PR #7" in capsys.readouterr().out


def test_a_head_commit_without_an_open_pr_says_so_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_run(monkeypatch, world(), "--head-sha", "e" * 40) == 0
    assert "No open pull request" in capsys.readouterr().out


def test_the_command_prints_json_and_writes_a_json_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake = world(reviews=(approval(),), checks=(check("pre-commit"),))
    target = tmp_path / "d.json"
    assert cli_run(monkeypatch, fake, "--number", "7", "--json", "--json-file", str(target)) == 0
    assert json.loads(capsys.readouterr().out)[0]["action"] == "start"
    assert json.loads(target.read_text(encoding="utf-8"))[0]["suites"] == ["vmaas", "caas", "bmaas/sanity"]


def test_an_unwritable_json_file_exits_three(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert cli_run(monkeypatch, world(), "--number", "7", "--json-file", str(tmp_path / "x" / "d.json")) == 3
    assert "cannot write" in capsys.readouterr().err


def test_a_github_failure_exits_three_and_never_prints_a_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = world()
    fake.add("GET", f"{BASE}/pulls/7", {"message": "boom"}, status=500)
    assert cli_run(monkeypatch, fake, "--number", "7") == 3
    captured = capsys.readouterr()
    assert "e2e-decide failed" in captured.err and "start" not in captured.out


def test_number_and_head_sha_are_exclusive_and_one_is_needed() -> None:
    for argv in (["--number", "7", "--head-sha", SHA], []):
        with pytest.raises(SystemExit):
            cli.main(["e2e-decide", "--policy", POLICY_PATH, "--repo", REPO, *argv])


def test_a_pr_with_the_most_files_the_api_lists_is_not_trusted_to_be_docs_only() -> None:
    # the files API stops at 3000: past it a docs-looking list proves nothing, so no suite is skipped on a guess
    names = tuple(f"docs/f{i}.md" for i in range(3000))
    fake = world(files=names, reviews=(approval(),), checks=(check("pre-commit"),))
    inputs = fetch_inputs(fake, POLICY, REPO, 7)
    assert not inputs.changed_files_known and decide(inputs).action is Action.START
    short = fetch_inputs(world(files=names[:2999], reviews=(approval(),)), POLICY, REPO, 7)
    assert short.changed_files_known and decide(short).action is Action.SKIP_NOT_APPLICABLE
