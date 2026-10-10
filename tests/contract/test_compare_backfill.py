"""`osac-ci compare --backfill`: the comparison on merged PRs, each rebuilt as it stood at the decision."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fakes import REPO, FakeGitHub
from helpers import ROOT

from osac_ci import cli
from osac_ci.compare import Outcome, backfill, render, to_json
from osac_ci.model import State
from osac_ci.policy import load_policy

pytestmark = pytest.mark.contract
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
TOY = load_policy(ROOT / "policy" / "toy.yml")  # required label: approved; checks lint, test, site (docs only)
BASE = f"/repos/{REPO}"
APP = 15368
ENQUEUED = "2026-10-06T10:00:00Z"
MERGED = "2026-10-06T10:30:00Z"


def ev(kind: str, at: str, label: str | None = None, actor: str = "bot") -> dict[str, Any]:
    out: dict[str, Any] = {"event": kind, "created_at": at, "actor": {"login": actor}}
    if label:
        out["label"] = {"name": label}
    return out


def run(name: str, completed: str | None, conclusion: str = "success", app: int = APP) -> dict[str, Any]:
    return {
        "name": name,
        "status": "completed" if completed else "in_progress",
        "conclusion": conclusion if completed else None,
        "started_at": "2026-10-06T09:00:00Z",
        "completed_at": completed,
        "app": {"id": app},
    }


GREEN = [run("lint", "2026-10-06T09:10:00Z"), run("test", "2026-10-06T09:20:00Z")]
QUEUED = [ev("added_to_merge_queue", ENQUEUED), ev("merged", MERGED)]
DIRECT = [ev("merged", MERGED)]
APPROVED_EARLY = ev("labeled", "2026-10-06T09:30:00Z", "approved")


def world(*contexts: str) -> tuple[FakeGitHub, list[dict[str, Any]]]:
    fake, listing = FakeGitHub(), []
    fake.add("POST", "/graphql", {"data": {"node": {"mergeQueueEntry": None}}})
    fake.add("GET", f"{BASE}/branches/main", {"name": "main"})
    names = contexts or ("lint", "test")
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": n, "integration_id": APP} for n in names]},
        }
    ]
    fake.add("GET", f"{BASE}/rules/branches/main", rules)
    fake.routes[("GET", f"{BASE}/pulls")] = (200, listing)
    return fake, listing


def add_merged(
    fake: FakeGitHub,
    listing: list[dict[str, Any]],
    number: int,
    events: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    labels: tuple[str, ...] = ("approved",),
    merged: str = MERGED,
) -> None:
    sha = f"{number:040x}"
    listing.append(
        {
            "number": number,
            "title": f"merged {number}",
            "merged_at": merged,
            "updated_at": merged,
            "html_url": f"u/{number}",
        }
    )
    fake.add(
        "GET",
        f"{BASE}/pulls/{number}",
        {
            "number": number,
            "node_id": f"PR_{number}",
            "draft": False,
            "user": {"login": "alice"},
            "author_association": "MEMBER",
            "labels": [{"name": n} for n in labels],
            "head": {"sha": sha, "repo": {"full_name": REPO, "owner": {"login": "example"}}},
        },
    )
    fake.add("GET", f"{BASE}/pulls/{number}/reviews", [])
    fake.add("GET", f"{BASE}/issues/{number}/events", events)
    fake.add("GET", f"{BASE}/pulls/{number}/files", [{"filename": "a.go"}])
    fake.add("GET", f"{BASE}/commits/{sha}/check-runs", {"total_count": len(runs), "check_runs": runs})


def go(fake: FakeGitHub, **kwargs: Any):  # type: ignore[no-untyped-def]
    return backfill(fake, TOY, REPO, now=NOW, days=60, generated_at="2026-10-07 12:00 UTC", org="example", **kwargs)


def by_number(report) -> dict[int, Any]:  # type: ignore[no-untyped-def]
    return {r.number: r for r in report.rows}


def test_a_queue_merged_pr_that_was_green_and_approved_at_the_enqueue_agrees() -> None:
    fake, listing = world()
    add_merged(fake, listing, 1, [APPROVED_EARLY, *QUEUED], GREEN)
    (row,) = go(fake).rows
    assert (row.outcome, row.state, row.via_queue) == (Outcome.AGREE_READY, State.READY_TO_ENQUEUE, True)
    assert row.merged_at == MERGED


def test_the_decision_is_judged_at_the_enqueue_not_at_the_merge() -> None:
    fake, listing = world()
    late_lint = [run("lint", "2026-10-06T10:10:00Z"), run("test", "2026-10-06T09:20:00Z")]  # finished after the enqueue
    add_merged(fake, listing, 1, [APPROVED_EARLY, *QUEUED], late_lint)
    (row,) = go(fake).rows
    assert row.state is State.CHECKS_RUNNING
    assert row.outcome is Outcome.AGREE_BLOCKED
    assert row.gaps == ("lint: in_progress",)


def test_a_label_added_after_the_enqueue_is_not_credited() -> None:
    fake, listing = world()
    late_label = ev("labeled", "2026-10-06T10:15:00Z", "approved")
    add_merged(fake, listing, 1, [*QUEUED[:1], late_label, QUEUED[1]], GREEN)
    (row,) = go(fake).rows
    assert (row.outcome, row.state, row.cause) == (Outcome.STRICTER, State.AWAITING_APPROVAL, "awaiting-approval")
    assert row.gaps == ()


def test_a_direct_merge_is_judged_when_it_merged_and_marked_as_direct() -> None:
    fake, listing = world()
    add_merged(fake, listing, 1, [APPROVED_EARLY, *DIRECT], GREEN)
    (row,) = go(fake).rows
    assert row.via_queue is False and row.outcome is Outcome.AGREE_READY


def test_a_direct_merge_that_skipped_a_required_check_the_policy_does_not_know_is_looser() -> None:
    fake, listing = world("lint", "test", "extra")
    add_merged(fake, listing, 1, [APPROVED_EARLY, *DIRECT], GREEN)
    (row,) = go(fake).rows
    assert (row.outcome, row.via_queue) == (Outcome.LOOSER, False)
    assert row.gaps == ("extra: not reported yet",) and row.cause == "a required check has not reported"


def test_a_required_check_from_the_wrong_app_does_not_count_in_the_past_either() -> None:
    fake, listing = world()
    wrong_app = [run("lint", "2026-10-06T09:10:00Z", app=999), run("test", "2026-10-06T09:20:00Z")]
    add_merged(fake, listing, 1, [APPROVED_EARLY, *DIRECT], wrong_app)
    (row,) = go(fake).rows
    assert row.outcome is Outcome.LOOSER and "another app" in row.gaps[0]


def test_a_pr_that_cannot_be_read_is_reported_not_dropped() -> None:
    fake, listing = world()
    add_merged(fake, listing, 1, [APPROVED_EARLY, *QUEUED], GREEN)
    add_merged(fake, listing, 2, [APPROVED_EARLY, *QUEUED], GREEN)
    fake.add("GET", f"{BASE}/pulls/2", {"message": "Not Found"}, status=404)
    rows = by_number(go(fake))
    assert rows[1].outcome is Outcome.AGREE_READY and rows[2].outcome is Outcome.UNREADABLE


def test_only_prs_merged_inside_the_window_are_compared_and_the_limit_applies() -> None:
    fake, listing = world()
    add_merged(fake, listing, 3, [APPROVED_EARLY, *QUEUED], GREEN)
    add_merged(fake, listing, 2, [APPROVED_EARLY, *QUEUED], GREEN, merged="2026-06-01T10:00:00Z")  # too old
    add_merged(fake, listing, 1, [APPROVED_EARLY, *QUEUED], GREEN)
    assert list(by_number(go(fake))) == [3]  # the scan stops at the first PR last touched before the window
    assert len(go(fake, limit=1).rows) == 1


def test_the_markdown_splits_the_outcomes_by_how_the_pr_was_merged() -> None:
    fake, listing = world("lint", "test", "extra")
    green_extra = [*GREEN, run("extra", "2026-10-06T09:25:00Z")]
    add_merged(fake, listing, 1, [APPROVED_EARLY, *QUEUED], green_extra)  # queue, agrees
    add_merged(fake, listing, 2, [APPROVED_EARLY, *DIRECT], GREEN)  # direct, a required check missing: looser
    add_merged(fake, listing, 3, [*QUEUED], green_extra, labels=())  # queue, no approval: stricter
    text = render(go(fake))
    assert text.startswith("# OSAC CI against the required checks, merged PRs:")
    assert "| Outcome | Merged by the queue | Merged directly |" in text
    assert "| agree-ready | 1 | 0 |" in text and "| looser | 0 | 1 |" in text and "| stricter | 1 | 0 |" in text
    assert "| PR | Merged | OSAC CI state | Required checks not passed |" in text
    assert "| [#2](u/2) | direct |" in text and "| [#3](u/3) | queue |" in text
    assert "open PRs compared" not in text


def test_json_carries_the_window_and_how_each_pr_was_merged() -> None:
    fake, listing = world()
    add_merged(fake, listing, 1, [APPROVED_EARLY, *QUEUED], GREEN)
    data = json.loads(to_json(go(fake)))
    assert data["days"] == 60
    assert data["rows"][0]["via_queue"] is True and data["rows"][0]["merged_at"] == MERGED


def cli_run(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub, *extra: str) -> int:
    monkeypatch.setattr(cli, "build_client", lambda **_: fake)
    monkeypatch.delenv(cli.ORG_TOKEN_ENV, raising=False)
    argv = ["compare", "--policy", str(ROOT / "policy" / "toy.yml"), "--repo", REPO, "--org", "example", *extra]
    return cli.main(argv)


def test_the_command_backfills_and_defaults_to_the_last_hundred(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, listing = world()
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for n in range(1, 106):
        add_merged(fake, listing, n, [APPROVED_EARLY, *QUEUED], GREEN, merged=now)
    assert cli_run(monkeypatch, fake, "--backfill", "30", "--json") == 0
    assert len(json.loads(capsys.readouterr().out)["rows"]) == 100
    assert cli_run(monkeypatch, fake, "--backfill", "30", "--limit", "7", "--json") == 0
    assert len(json.loads(capsys.readouterr().out)["rows"]) == 7


@pytest.mark.parametrize("days", ["0", "-3"])
def test_the_command_refuses_a_window_below_one_day(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], days: str
) -> None:
    fake, _ = world()
    assert cli_run(monkeypatch, fake, "--backfill", days) == 2
    assert "--backfill" in capsys.readouterr().err
    assert not fake.calls


def test_fail_on_looser_and_json_file_work_with_backfill(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake, listing = world("lint", "test", "extra")
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    add_merged(fake, listing, 1, [APPROVED_EARLY, *DIRECT], GREEN, merged=now)
    target = tmp_path / "bf.json"
    assert cli_run(monkeypatch, fake, "--backfill", "30", "--fail-on-looser", "--json-file", str(target)) == 1
    assert json.loads(target.read_text(encoding="utf-8"))["counts"]["looser"] == 1
