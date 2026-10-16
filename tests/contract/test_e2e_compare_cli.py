"""`osac-ci e2e-compare` against a fake GitHub with several open pull requests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeGitHub
from helpers import ROOT

from osac_ci import cli
from osac_ci.e2e_compare import Outcome, compare, render, to_json
from osac_ci.policy import load_policy

pytestmark = pytest.mark.contract
REPO = "example/app"
BASE = f"/repos/{REPO}"
POLICY = load_policy(ROOT / "policy" / "osac.yml")
POLICY_PATH = str(ROOT / "policy" / "osac.yml")
APP = 15368
NOW = "2026-10-11 06:00 UTC"


def check(name: str, conclusion: str | None = "success") -> dict[str, Any]:
    return {
        "name": name,
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
        "started_at": "2026-10-10T10:00:00Z",
        "app": {"id": APP},
    }


def world() -> tuple[FakeGitHub, list[dict[str, Any]]]:
    fake, listing = FakeGitHub(), []
    fake.add("GET", f"{BASE}/branches/main", {"name": "main"})
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "pre-commit", "integration_id": APP}]},
        }
    ]
    fake.add("GET", f"{BASE}/rules/branches/main", rules)
    fake.routes[("GET", f"{BASE}/pulls")] = (200, listing)
    return fake, listing


def add_pr(
    fake: FakeGitHub,
    listing: list[dict[str, Any]],
    number: int,
    *,
    approved: bool = True,
    e2e: str | None = None,
    draft: bool = False,
    files: tuple[str, ...] = ("fulfillment-service/main.go",),
    base: str = "main",
) -> None:
    sha = f"{number:040x}"
    listing.append(
        {
            "number": number,
            "title": f"change {number}",
            "html_url": f"u/{number}",
            "draft": draft,
            "updated_at": "2026-10-10T10:00:00Z",
            "base": {"ref": base},
        }
    )
    fake.add(
        "GET",
        f"{BASE}/pulls/{number}",
        {
            "number": number,
            "state": "open",
            "draft": draft,
            "user": {"login": "alice"},
            "head": {"sha": sha, "repo": {"full_name": REPO}},
            "base": {"ref": "main"},
        },
    )
    reviews = (
        [
            {
                "user": {"login": "bob", "type": "User"},
                "state": "APPROVED",
                "author_association": "MEMBER",
                "commit_id": sha,
            }
        ]
        if approved
        else []
    )
    fake.add("GET", f"{BASE}/pulls/{number}/reviews", reviews)
    fake.add("GET", f"{BASE}/collaborators/bob/permission", {"permission": "write", "role_name": "write"})
    fake.add("GET", f"{BASE}/pulls/{number}/files", [{"filename": f} for f in files])
    runs = [check("pre-commit")]
    if e2e == "running":
        runs += [check("e2e-vmaas-full-install / e2e", None)]
    elif e2e == "passed":
        runs += [check("e2e-vmaas-full-install / e2e")]
    elif e2e == "skipped":
        runs += [check("e2e-vmaas-full-install / e2e", "skipped")]
    fake.add("GET", f"{BASE}/commits/{sha}/check-runs", {"total_count": len(runs), "check_runs": runs})


def run(fake: FakeGitHub, **kwargs: Any):  # type: ignore[no-untyped-def]
    return compare(fake, POLICY, REPO, generated_at=NOW, **kwargs)


def outcomes(report) -> dict[int, Outcome]:  # type: ignore[no-untyped-def]
    return {r.number: r.outcome for r in report.rows}


def test_each_outcome() -> None:
    fake, listing = world()
    add_pr(fake, listing, 1, e2e="running")  # decision start, flow running
    add_pr(fake, listing, 2, e2e=None)  # decision start, flow has not started
    add_pr(fake, listing, 3, approved=False, e2e=None)  # both wait
    add_pr(fake, listing, 4, approved=False, e2e="passed")  # flow ran, the decision would wait
    add_pr(fake, listing, 5, files=("docs/a.md",), e2e="passed")  # flow ran, docs only
    assert outcomes(run(fake)) == {
        1: Outcome.AGREE_START,
        2: Outcome.NEW_ONLY,
        3: Outcome.AGREE_WAIT,
        4: Outcome.OLD_ONLY,
        5: Outcome.OLD_ONLY,
    }


def test_a_skipped_job_means_the_flow_has_not_started() -> None:
    fake, listing = world()
    add_pr(fake, listing, 1, approved=False, e2e="skipped")
    assert outcomes(run(fake)) == {1: Outcome.AGREE_WAIT}


def test_drafts_are_left_out_unless_asked_for() -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    add_pr(fake, listing, 2, draft=True)
    assert list(outcomes(run(fake))) == [1]
    assert list(outcomes(run(fake, include_drafts=True))) == [1, 2]


def test_a_pull_request_that_cannot_be_read_is_reported_and_the_rest_continue() -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    add_pr(fake, listing, 2)
    fake.add("GET", f"{BASE}/pulls/2", {"message": "boom"}, status=500)
    report = run(fake)
    assert outcomes(report) == {1: Outcome.NEW_ONLY, 2: Outcome.UNREADABLE}
    assert "Could not be read" in render(report)


def test_the_required_checks_are_read_once_not_once_per_pull_request() -> None:
    fake, listing = world()
    for n in (1, 2, 3):
        add_pr(fake, listing, n)
    run(fake)
    assert len([c for c in fake.calls if c[1].endswith("/rules/branches/main")]) == 1


def test_limit_applies_and_rows_are_ordered_by_number() -> None:
    fake, listing = world()
    for n in (9, 3, 5):
        add_pr(fake, listing, n)
    assert [r.number for r in run(fake).rows] == [3, 5, 9]
    assert len(run(fake, limit=2).rows) == 2


def test_the_markdown_lists_the_differences_with_the_reason_and_what_the_flow_did() -> None:
    fake, listing = world()
    add_pr(fake, listing, 2, e2e=None)
    add_pr(fake, listing, 4, approved=False, e2e="passed")
    text = render(run(fake))
    assert "## The current flow started the E2E, the decision would not" in text
    assert "## The decision would start the E2E, the current flow has not" in text
    assert "| [#4](u/4) | wait-approval |" in text and "vmaas: passed" in text
    assert "| [#2](u/2) | start |" in text


def test_the_markdown_is_short_when_everything_agrees() -> None:
    fake, listing = world()
    add_pr(fake, listing, 1, e2e="running")
    text = render(run(fake))
    assert "started the E2E, the decision would not" not in text and "| agree-start | 1 |" in text


def test_the_json_has_the_counts_and_the_rows() -> None:
    fake, listing = world()
    add_pr(fake, listing, 1, e2e="running")
    data = json.loads(to_json(run(fake)))
    assert data["counts"]["agree-start"] == 1 and data["rows"][0]["legacy"][0] == "vmaas: running"


def cli_run(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub, *extra: str) -> int:
    monkeypatch.setattr(cli, "build_client", lambda **_: fake)
    return cli.main(["e2e-compare", "--policy", POLICY_PATH, "--repo", REPO, *extra])


def test_the_command_prints_markdown_json_and_a_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake, listing = world()
    add_pr(fake, listing, 1, e2e="running")
    assert cli_run(monkeypatch, fake) == 0
    assert capsys.readouterr().out.startswith("# E2E start decision against the current flow")
    target = tmp_path / "c.json"
    assert cli_run(monkeypatch, fake, "--json", "--json-file", str(target)) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["agree-start"] == 1
    assert json.loads(target.read_text(encoding="utf-8"))["rows"][0]["number"] == 1


def test_an_unwritable_file_and_a_github_failure_exit_three(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    assert cli_run(monkeypatch, fake, "--json-file", str(tmp_path / "x" / "c.json")) == 3
    assert "cannot write" in capsys.readouterr().err
    broken, _ = world()
    broken.add("GET", f"{BASE}/rules/branches/main", {"message": "boom"}, status=500)
    assert cli_run(monkeypatch, broken) == 3
    assert "e2e-compare failed" in capsys.readouterr().err


def test_only_prs_that_target_the_branch_whose_rules_are_read_are_compared() -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    add_pr(fake, listing, 2, base="release-1")
    assert list(outcomes(run(fake))) == [1]
    # the decision of a PR that is compared always used the required checks of its own base branch
    assert [c[1] for c in fake.calls if "/pulls/2" in c[1]] == []
