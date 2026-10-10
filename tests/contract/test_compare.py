"""`osac-ci compare`: OSAC CI's answer against the required checks, on a fake GitHub with several open PRs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fakes import REPO, FakeGitHub
from helpers import ROOT

from osac_ci import cli
from osac_ci.compare import Outcome, Requirement, classify, compare, render, required_contexts, to_json
from osac_ci.github.api import GitHubError
from osac_ci.model import State
from osac_ci.policy import load_policy

pytestmark = pytest.mark.contract
TOY = load_policy(ROOT / "policy" / "toy.yml")  # required label: approved; checks: lint, test, site (docs only)
BASE = f"/repos/{REPO}"
NOW = "2026-10-10 06:00 UTC"
APP = 15368  # the app id the real ruleset requires its checks from (GitHub Actions)


def ruleset(*contexts: str, extra_rules: tuple[dict[str, Any], ...] = ()) -> list[dict[str, Any]]:
    checks = [{"context": name, "integration_id": APP} for name in contexts]
    return [
        {"type": "pull_request", "parameters": {}},
        {"type": "required_status_checks", "parameters": {"required_status_checks": checks}},
        *extra_rules,
    ]


def world(*contexts: str) -> tuple[FakeGitHub, list[dict[str, Any]]]:
    fake, listing = FakeGitHub(), []
    fake.add("POST", "/graphql", {"data": {"node": {"mergeQueueEntry": None}}})
    fake.add("GET", f"{BASE}/branches/main", {"name": "main"})
    fake.add("GET", f"{BASE}/rules/branches/main", ruleset(*(contexts or ("lint", "test"))))
    fake.routes[("GET", f"{BASE}/pulls")] = (200, listing)
    return fake, listing


def add_pr(
    fake: FakeGitHub,
    listing: list[dict[str, Any]],
    number: int,
    *,
    draft: bool = False,
    labels: tuple[str, ...] = ("approved",),
    checks: dict[str, tuple[str, str | None]] | None = None,
    base: str = "main",
) -> None:
    """``checks`` maps a check name to (status, conclusion); the default is lint and test passing."""
    sha = f"{number:040x}"
    listing.append(
        {
            "number": number,
            "title": f"change {number}",
            "user": {"login": "alice"},
            "html_url": f"https://github.com/{REPO}/pull/{number}",
            "updated_at": "2026-10-09T10:00:00Z",
            "draft": draft,
            "base": {"ref": base},
        }
    )
    fake.add(
        "GET",
        f"{BASE}/pulls/{number}",
        {
            "number": number,
            "node_id": f"PR_{number}",
            "draft": draft,
            "user": {"login": "alice"},
            "author_association": "MEMBER",
            "labels": [{"name": name} for name in labels],
            "head": {"sha": sha, "repo": {"full_name": REPO, "owner": {"login": "example"}}},
        },
    )
    fake.add("GET", f"{BASE}/pulls/{number}/reviews", [])
    fake.add("GET", f"{BASE}/issues/{number}/events", [])
    fake.add("GET", f"{BASE}/pulls/{number}/files", [{"filename": "a.go"}])
    wanted = checks if checks is not None else {"lint": ("completed", "success"), "test": ("completed", "success")}
    runs = [
        {
            "name": n,
            "status": s,
            "conclusion": c,
            "started_at": f"2026-01-01T00:00:{i:02d}Z",
            "app": {"id": APP, "slug": "github-actions"},
        }
        for i, (n, (s, c)) in enumerate(wanted.items())
    ]
    fake.add("GET", f"{BASE}/commits/{sha}/check-runs", {"total_count": len(runs), "check_runs": runs})


def bare(branch: str = "main") -> FakeGitHub:
    """A fake that knows the branch exists and nothing else."""
    fake = FakeGitHub()
    fake.add("GET", f"{BASE}/branches/{branch}", {"name": branch})
    return fake


def run(fake: FakeGitHub, **kwargs: Any):  # type: ignore[no-untyped-def]
    return compare(fake, TOY, REPO, generated_at=NOW, org="example", **kwargs)


def outcomes(report) -> dict[int, Outcome]:  # type: ignore[no-untyped-def]
    return {r.number: r.outcome for r in report.rows}


def test_the_four_outcomes() -> None:
    fake, listing = world("lint", "test", "extra")
    green = {"lint": ("completed", "success"), "test": ("completed", "success"), "extra": ("completed", "success")}
    add_pr(fake, listing, 1, checks=green)  # ready, and every required check passed
    add_pr(fake, listing, 2, checks={**green, "lint": ("completed", "failure")})  # not ready, a check failed
    add_pr(fake, listing, 3, checks=green, labels=())  # every check passed, but the approval is missing
    add_pr(fake, listing, 4, checks={"lint": ("completed", "success"), "test": ("completed", "success")})  # extra: none
    report = run(fake)
    assert outcomes(report) == {
        1: Outcome.AGREE_READY,
        2: Outcome.AGREE_BLOCKED,
        3: Outcome.STRICTER,
        4: Outcome.LOOSER,
    }


def test_a_stricter_difference_is_explained_by_the_osac_ci_state() -> None:
    fake, listing = world()
    add_pr(fake, listing, 3, labels=())
    (row,) = run(fake).rows
    assert row.outcome is Outcome.STRICTER
    assert row.state is State.AWAITING_APPROVAL
    assert row.cause == "awaiting-approval"
    assert row.headline and not row.gaps


def test_a_looser_difference_lists_the_required_checks_that_stand_in_the_way() -> None:
    fake, listing = world("lint", "test", "extra", "other")
    both = {"lint": ("completed", "success"), "test": ("completed", "success")}
    add_pr(fake, listing, 4, checks={**both, "other": ("completed", "success")})
    (row,) = run(fake).rows
    assert row.outcome is Outcome.LOOSER
    assert row.gaps == ("extra: not reported yet",)
    assert row.cause == "a required check has not reported"


@pytest.mark.parametrize(
    ("extra", "cause"),
    [
        (("completed", "failure"), "a required check failed"),
        (("in_progress", None), "a required check is still running"),
        (None, "a required check has not reported"),
    ],
)
def test_the_cause_of_a_looser_difference_names_the_kind_of_gap(
    extra: tuple[str, str | None] | None, cause: str
) -> None:
    fake, listing = world("lint", "test", "extra")
    checks = {"lint": ("completed", "success"), "test": ("completed", "success")}
    if extra:
        checks["extra"] = extra
    add_pr(fake, listing, 4, checks=checks)
    (row,) = run(fake).rows
    assert (row.outcome, row.cause) == (Outcome.LOOSER, cause)


def test_a_failure_outranks_a_run_in_progress_when_naming_the_cause() -> None:
    fake, listing = world("lint", "test", "slow", "broken")
    add_pr(
        fake,
        listing,
        4,
        checks={
            "lint": ("completed", "success"),
            "test": ("completed", "success"),
            "slow": ("in_progress", None),
            "broken": ("completed", "failure"),
        },
    )
    (row,) = run(fake).rows
    assert row.cause == "a required check failed"
    assert len(row.gaps) == 2


@pytest.mark.parametrize("conclusion", ["success", "neutral", "skipped"])
def test_a_skipped_or_neutral_required_check_counts_as_passed_like_github_does(conclusion: str) -> None:
    fake, listing = world("lint", "test", "extra")
    checks = {"lint": ("completed", "success"), "test": ("completed", "success"), "extra": ("completed", conclusion)}
    add_pr(fake, listing, 4, checks=checks)
    (row,) = run(fake).rows
    assert row.outcome is Outcome.AGREE_READY


def test_the_latest_run_of_a_check_is_the_one_that_counts() -> None:
    fake, listing = world("lint", "test")
    add_pr(fake, listing, 4)
    sha = f"{4:040x}"
    fake.add(
        "GET",
        f"{BASE}/commits/{sha}/check-runs",
        {
            "total_count": 3,
            "check_runs": [
                {
                    "name": "lint",
                    "status": "completed",
                    "conclusion": "failure",
                    "started_at": "2026-01-01T00:00:01Z",
                    "app": {"id": APP},
                },
                {
                    "name": "lint",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "2026-01-01T00:05:00Z",
                    "app": {"id": APP},
                },
                {
                    "name": "test",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "2026-01-01T00:00:02Z",
                    "app": {"id": APP},
                },
            ],
        },
    )
    assert outcomes(run(fake)) == {4: Outcome.AGREE_READY}


def test_a_pr_that_cannot_be_read_is_reported_not_dropped() -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    add_pr(fake, listing, 2)
    fake.add("GET", f"{BASE}/pulls/2", {"message": "Not Found"}, status=404)
    report = run(fake)
    assert outcomes(report) == {1: Outcome.AGREE_READY, 2: Outcome.UNREADABLE}
    assert "Could not be read" in render(report)


def test_drafts_are_left_out_unless_asked_for() -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    add_pr(fake, listing, 2, draft=True)
    assert list(outcomes(run(fake))) == [1]
    assert list(outcomes(run(fake, include_drafts=True))) == [1, 2]


def test_rows_are_ordered_by_pr_number_and_limit_applies() -> None:
    fake, listing = world()
    for number in (5, 3, 9):
        add_pr(fake, listing, number)
    assert [r.number for r in run(fake).rows] == [3, 5, 9]
    assert len(run(fake, limit=2).rows) == 2


def test_the_two_lists_of_checks_are_compared() -> None:
    fake, listing = world("lint", "check-labels", "only-in-ruleset")
    add_pr(fake, listing, 1)
    cov = run(fake).coverage
    assert cov.ruleset_only == ("check-labels", "only-in-ruleset")
    assert cov.policy_only == ("site", "test")  # `test` is required by the policy but not listed in this ruleset


def test_both_lists_agreeing_is_said_so() -> None:
    fake, listing = world("lint", "test", "site")
    add_pr(fake, listing, 1)
    text = render(run(fake))
    assert "Both lists name the same checks." in text


def test_the_required_checks_come_from_the_status_check_rules_only() -> None:
    fake = bare()
    fake.add(
        "GET",
        f"{BASE}/rules/branches/main",
        [
            {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "a"}]}},
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "a"}, {"context": "b"}]},
            },
            {"type": "required_status_checks"},
            {"type": "required_status_checks", "parameters": None},
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": ""}, {}, {"context": 3}]},
            },
            {"type": "merge_queue", "parameters": {"required_status_checks": [{"context": "not-me"}]}},
        ],
    )
    assert required_contexts(fake, REPO, "main") == (Requirement("a"), Requirement("b"))


def test_osac_ci_is_left_out_of_the_required_checks_once_it_is_required_itself() -> None:
    fake = bare()
    fake.add("GET", f"{BASE}/rules/branches/main", ruleset("lint", "OSAC CI", "test"))
    assert required_contexts(fake, REPO, "main") == (Requirement("lint", APP), Requirement("test", APP))


def test_an_unexpected_answer_for_the_rules_means_no_required_checks_not_a_crash() -> None:
    fake = bare()
    fake.add("GET", f"{BASE}/rules/branches/main", {"message": "odd"})
    assert required_contexts(fake, REPO, "main") == ()


def test_the_rules_of_the_chosen_branch_are_read() -> None:
    fake, listing = world()
    fake.add("GET", f"{BASE}/branches/release-1", {"name": "release-1"})
    fake.add("GET", f"{BASE}/rules/branches/release-1", ruleset("only-release"))
    add_pr(fake, listing, 1)
    assert run(fake, branch="release-1").required == (Requirement("only-release", APP),)


def test_a_failure_to_read_the_rules_stops_the_comparison() -> None:
    fake, _ = world()
    fake.add("GET", f"{BASE}/rules/branches/main", {"message": "Not Found"}, status=404)
    with pytest.raises(GitHubError, match="rules/branches/main"):
        run(fake)


@pytest.mark.parametrize(
    ("ready", "legacy_ready", "outcome"),
    [
        (True, True, Outcome.AGREE_READY),
        (False, False, Outcome.AGREE_BLOCKED),
        (True, False, Outcome.LOOSER),
        (False, True, Outcome.STRICTER),
    ],
)
def test_classify(ready: bool, legacy_ready: bool, outcome: Outcome) -> None:
    assert classify(ready, legacy_ready) is outcome


def test_the_markdown_lists_each_difference_and_escapes_what_it_prints() -> None:
    fake, listing = world("lint", "test", "extra|<b>")
    add_pr(fake, listing, 1)
    add_pr(fake, listing, 3, labels=())
    text = render(run(fake))
    assert "(the unsafe direction)" in text and "[#1](" in text  # PR 1 lacks the odd check, so it is looser
    assert "Every required check passed, OSAC CI holds the PR back" not in text  # PR 3 lacks it too
    assert "<b>" not in text and "extra\\|" in text


def test_the_markdown_groups_the_stricter_prs_by_cause() -> None:
    fake, listing = world()
    for number in (1, 2, 3):
        add_pr(fake, listing, number, labels=())
    add_pr(fake, listing, 4, labels=("approved",))
    text = render(run(fake))
    assert "| awaiting-approval | 3 |" in text
    assert "(the unsafe direction)" not in text


def test_json_carries_the_counts_the_rows_and_the_lists() -> None:
    fake, listing = world("lint", "test", "check-labels")
    add_pr(fake, listing, 1)
    add_pr(fake, listing, 2, labels=())
    data = json.loads(to_json(run(fake)))
    assert data["required"] == ["lint", "test", "check-labels"]
    assert data["counts"] == {"agree-ready": 0, "agree-blocked": 1, "looser": 1, "stricter": 0, "unreadable": 0}
    assert data["ruleset_only"] == ["check-labels"]
    assert {r["number"] for r in data["rows"]} == {1, 2}
    assert all("cause" in r and "gaps" in r for r in data["rows"])


def cli_run(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub, *extra: str) -> int:
    monkeypatch.setattr(cli, "build_client", lambda **_: fake)
    monkeypatch.delenv(cli.ORG_TOKEN_ENV, raising=False)
    return cli.main(
        ["compare", "--policy", str(ROOT / "policy" / "toy.yml"), "--repo", REPO, "--org", "example", *extra]
    )


def test_the_command_prints_markdown_and_exits_zero_even_with_differences(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, listing = world("lint", "test", "extra")
    add_pr(fake, listing, 1)
    assert cli_run(monkeypatch, fake) == 0
    assert capsys.readouterr().out.startswith("# OSAC CI against the required checks")


def test_fail_on_looser_exits_one_only_for_the_unsafe_direction(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, listing = world("lint", "test", "extra")
    add_pr(fake, listing, 1)
    assert cli_run(monkeypatch, fake, "--fail-on-looser") == 1
    fake, listing = world()
    add_pr(fake, listing, 1, labels=())  # stricter only
    assert cli_run(monkeypatch, fake, "--fail-on-looser") == 0


def test_the_command_prints_json_on_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    assert cli_run(monkeypatch, fake, "--json") == 0
    assert json.loads(capsys.readouterr().out)["counts"]["agree-ready"] == 1


def test_a_github_failure_exits_three(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake, _ = world()
    fake.add("GET", f"{BASE}/rules/branches/main", {"message": "Not Found"}, status=404)
    assert cli_run(monkeypatch, fake) == 3
    assert "compare failed" in capsys.readouterr().err


def test_a_pr_already_in_the_merge_queue_counts_as_ready() -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    fake.add("POST", "/graphql", {"data": {"node": {"mergeQueueEntry": {"id": "entry"}}}})
    (row,) = run(fake).rows
    assert (row.state, row.outcome) == (State.IN_QUEUE, Outcome.AGREE_READY)


def test_json_file_gives_both_formats_from_one_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    target = tmp_path / "compare.json"
    assert cli_run(monkeypatch, fake, "--json-file", str(target)) == 0
    assert capsys.readouterr().out.startswith("# OSAC CI against the required checks")
    assert json.loads(target.read_text(encoding="utf-8"))["counts"]["agree-ready"] == 1


def test_an_unwritable_json_file_exits_three(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    assert cli_run(monkeypatch, fake, "--json-file", str(tmp_path / "missing" / "c.json")) == 3
    assert "cannot write" in capsys.readouterr().err


def test_a_branch_that_does_not_exist_stops_the_comparison_instead_of_meaning_no_required_checks() -> None:
    fake = FakeGitHub()
    fake.add("GET", f"{BASE}/branches/mian", {"message": "Branch not found"}, status=404)
    fake.add("GET", f"{BASE}/rules/branches/mian", [])  # GitHub answers 200 with nothing for any name
    with pytest.raises(GitHubError, match="branches/mian"):
        required_contexts(fake, REPO, "mian")
    assert ("GET", f"{BASE}/rules/branches/mian") not in fake.calls


def test_an_existing_branch_with_no_required_checks_is_valid() -> None:
    fake = bare()
    fake.add("GET", f"{BASE}/rules/branches/main", [])
    assert required_contexts(fake, REPO, "main") == ()


def test_a_branch_name_is_quoted_in_the_request_path() -> None:
    fake = FakeGitHub()
    fake.add("GET", f"{BASE}/branches/release%2Fone", {"name": "release/one"})
    fake.add("GET", f"{BASE}/rules/branches/release%2Fone", ruleset("lint"))
    assert required_contexts(fake, REPO, "release/one") == (Requirement("lint", APP),)


def test_the_required_app_is_kept_and_a_flag_or_odd_value_means_any_app() -> None:
    fake = bare()
    entries = [
        {"context": "a", "integration_id": 7},
        {"context": "b"},
        {"context": "c", "integration_id": True},
        {"context": "d", "integration_id": "7"},
        {"context": "a", "integration_id": 8},
    ]
    fake.add(
        "GET",
        f"{BASE}/rules/branches/main",
        [{"type": "required_status_checks", "parameters": {"required_status_checks": entries}}],
    )
    assert required_contexts(fake, REPO, "main") == (
        Requirement("a", 7),
        Requirement("b"),
        Requirement("c"),
        Requirement("d"),
        Requirement("a", 8),
    )


def lint_from(app_id: int | None, conclusion: str = "success") -> dict[str, Any]:
    run_: dict[str, Any] = {
        "name": "lint",
        "status": "completed",
        "conclusion": conclusion,
        "started_at": "2026-01-01T00:00:01Z",
    }
    if app_id is not None:
        run_["app"] = {"id": app_id, "slug": "x"}
    return run_


def one_pr_with(runs: list[dict[str, Any]], *contexts: str) -> Any:
    fake, listing = world(*contexts)
    add_pr(fake, listing, 1, checks={"test": ("completed", "success")})
    fake.add("GET", f"{BASE}/commits/{1:040x}/check-runs", {"total_count": len(runs), "check_runs": runs})
    return run(fake).rows[0]


def test_a_check_with_the_right_name_from_the_wrong_app_does_not_satisfy_the_ruleset() -> None:
    test_run = {"name": "test", "status": "completed", "conclusion": "success", "app": {"id": APP}}
    row = one_pr_with([lint_from(99999), test_run], "lint", "test")
    assert row.outcome is Outcome.LOOSER
    assert row.gaps == (f"lint: only reported by another app (the ruleset requires app {APP})",)


def test_a_run_from_the_wrong_app_is_ignored_even_when_it_is_the_newest() -> None:
    newer_wrong = {**lint_from(99999, "failure"), "started_at": "2026-01-02T00:00:00Z"}
    test_run = {"name": "test", "status": "completed", "conclusion": "success", "app": {"id": APP}}
    row = one_pr_with([lint_from(APP), newer_wrong, test_run], "lint", "test")
    # the ruleset is satisfied by the right app's run; OSAC CI, which reads by name, sees the newer failure
    assert (row.outcome, row.gaps) == (Outcome.STRICTER, ())


def test_a_run_without_a_known_app_does_not_satisfy_a_requirement_that_names_one() -> None:
    test_run = {"name": "test", "status": "completed", "conclusion": "success", "app": {"id": APP}}
    assert one_pr_with([lint_from(None), test_run], "lint", "test").outcome is Outcome.LOOSER


def test_a_requirement_without_an_app_accepts_any_app() -> None:
    fake, listing = world()
    fake.add(
        "GET",
        f"{BASE}/rules/branches/main",
        [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "lint"}]}}],
    )
    add_pr(fake, listing, 1)
    test_run = {"name": "test", "status": "completed", "conclusion": "success"}
    fake.add("GET", f"{BASE}/commits/{1:040x}/status", {"statuses": []})
    fake.add(
        "GET", f"{BASE}/commits/{1:040x}/check-runs", {"total_count": 2, "check_runs": [lint_from(None), test_run]}
    )
    assert run(fake).rows[0].outcome is Outcome.AGREE_READY


def status_world(*, status_state: str | None, app_check: bool = False, integration: int | None = None):  # type: ignore[no-untyped-def]
    """A PR whose `lint` requirement may be met by a commit status; `test` is an ordinary passing check run."""
    fake, listing = world()
    entry: dict[str, Any] = (
        {"context": "lint"} if integration is None else {"context": "lint", "integration_id": integration}
    )
    rules = [{"type": "required_status_checks", "parameters": {"required_status_checks": [entry, {"context": "test"}]}}]
    fake.add("GET", f"{BASE}/rules/branches/main", rules)
    add_pr(fake, listing, 1, checks={"test": ("completed", "success")})
    runs = [{"name": "test", "status": "completed", "conclusion": "success", "app": {"id": APP}}]
    if app_check:
        runs.append({**lint_from(APP, "failure")})
    fake.add("GET", f"{BASE}/commits/{1:040x}/check-runs", {"total_count": len(runs), "check_runs": runs})
    statuses = [] if status_state is None else [{"context": "lint", "state": status_state}]
    fake.add("GET", f"{BASE}/commits/{1:040x}/status", {"state": "x", "statuses": statuses})
    return fake


def test_a_commit_status_satisfies_a_requirement_that_names_no_app() -> None:
    row = run(status_world(status_state="success")).rows[0]
    assert row.gaps == ()


@pytest.mark.parametrize(
    ("state", "detail"), [("pending", "pending"), ("failure", "state: failure"), ("error", "state: error")]
)
def test_a_commit_status_that_is_not_success_leaves_the_requirement_open(state: str, detail: str) -> None:
    assert run(status_world(status_state=state)).rows[0].gaps == (f"lint: {detail}",)


def test_an_unknown_commit_status_state_leaves_the_requirement_open() -> None:
    row = run(status_world(status_state="mystery")).rows[0]
    assert row.gaps == ("lint: state: mystery",)


def test_a_commit_status_does_not_satisfy_a_requirement_that_names_an_app() -> None:
    fake = status_world(status_state="success", integration=APP)
    assert run(fake).rows[0].gaps == ("lint: not reported yet",)


def test_a_passing_status_beats_a_failed_check_run_but_a_failed_status_does_not_hide_one() -> None:
    assert run(status_world(status_state="success", app_check=True)).rows[0].gaps == ()
    row = run(status_world(status_state="failure", app_check=True)).rows[0]
    assert row.gaps == ("lint: conclusion: failure",)


def test_statuses_are_only_fetched_when_a_requirement_names_no_app() -> None:
    every_app = world()[0]
    listing: list[dict[str, Any]] = every_app.routes[("GET", f"{BASE}/pulls")][1]
    add_pr(every_app, listing, 1)
    run(every_app)
    assert not [c for c in every_app.calls if c[1].endswith("/status")]
    some_open = status_world(status_state=None)
    run(some_open)
    assert [c for c in some_open.calls if c[1].endswith("/status")]


def test_a_failure_to_read_the_statuses_makes_the_pr_unreadable_not_ready() -> None:
    fake = status_world(status_state="success")
    fake.add("GET", f"{BASE}/commits/{1:040x}/status", {"message": "boom"}, status=500)
    assert run(fake).rows[0].outcome is Outcome.UNREADABLE


def test_a_malformed_status_entry_is_ignored() -> None:
    fake = status_world(status_state=None)
    fake.add(
        "GET",
        f"{BASE}/commits/{1:040x}/status",
        {
            "statuses": [
                {"context": 1, "state": "success"},
                {"context": "lint"},
                {"context": "lint", "state": 5},
                {"state": "success"},
            ]
        },
    )
    assert run(fake).rows[0].gaps == ("lint: not reported yet",)


def test_only_prs_that_target_the_branch_whose_rules_are_read_are_compared() -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    add_pr(fake, listing, 2, base="release-1")
    assert list(outcomes(run(fake))) == [1]
