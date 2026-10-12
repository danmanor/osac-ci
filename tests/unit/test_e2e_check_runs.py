"""The E2E lines written as check runs of the OSAC CI app (kind "checks"): the same behavior as statuses, but each
line is a check run, in progress while it waits or runs (the page draws a spinner) and completed with a result. Kept by
a small in-memory GitHub where check runs are listed, and the newest of a name by this app is the one that counts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import pytest

from osac_ci import e2e_check
from osac_ci.e2e_check import CHECK_APP, existing_from, finish, latest_check, newest_status, post_status, report
from osac_ci.e2e_suites_check import settle, track
from osac_ci.github.api import GitHubError, Response

pytestmark = pytest.mark.unit
REPO = "o/r"
SHA = "a" * 40
RUN = 500
URL = "https://example.invalid/o/r/actions/runs/500"
OTHER_URL = "https://example.invalid/o/r/actions/runs/400"
BASE = f"/repos/{REPO}"


class Checks:
    """GitHub, reduced to the check runs of one commit and the state of workflow runs."""

    def __init__(self, runs: Mapping[int, str] | None = None) -> None:
        self.runs: list[dict[str, Any]] = []
        self.states = dict(runs or {})
        self.jobs: list[dict[str, Any]] = []
        self.writes: list[dict[str, Any]] = []
        self.fail = False
        self.next_id = 100

    def add(self, name: str = "E2E", app: str = CHECK_APP, **fields: Any) -> dict[str, Any]:
        run = {"id": self.next_id, "name": name, "app": {"slug": app}, "status": "in_progress", "conclusion": None,
               "output": {"title": ""}, "details_url": ""}  # fmt: skip
        run.update(fields)
        self.next_id += 1
        self.runs.append(run)
        return run

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        page, per = int((params or {}).get("page", 1)), int((params or {}).get("per_page", 100))
        if method == "GET" and path == f"{BASE}/commits/{SHA}/check-runs":
            name = (params or {}).get("check_name")
            items = [r for r in self.runs if name is None or r["name"] == name]
            return Response(200, {"check_runs": items[(page - 1) * per : page * per]})
        if method == "GET" and (m := re.fullmatch(rf"{BASE}/actions/runs/(\d+)", path)):
            state = self.states.get(int(m.group(1)))
            return Response(404, {}) if state is None else Response(200, {"status": state})
        if method == "GET" and path.endswith("/jobs"):
            return Response(200, {"jobs": self.jobs[(page - 1) * per : page * per]})
        if method == "POST" and path == f"{BASE}/check-runs":
            self.writes.append(body)
            if self.fail:
                return Response(403, {"message": "Resource not accessible by integration"})
            self.add(body["name"], status=body["status"], conclusion=body.get("conclusion"), output=body["output"],
                     details_url=body.get("details_url", ""))  # fmt: skip
            return Response(201, {"id": self.runs[-1]["id"]})
        return Response(404, {"message": f"no route for {method} {path}"})

    def newest(self, name: str = "E2E") -> dict[str, Any]:
        return max((r for r in self.runs if r["name"] == name), key=lambda r: r["id"])


def entry(action: str = "wait-approval", **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "number": 7,
        "head_sha": SHA,
        "action": action,
        "reason": "why",
        "suites": ["vmaas", "caas"],
    }
    base.update(extra)
    return base


def run_report(world: Checks, *entries: dict[str, Any], run_id: int = RUN) -> e2e_check.Report:
    return report(world, REPO, entries, run_id, URL, "checks")


def test_a_waiting_line_is_an_in_progress_check_run_with_the_text_as_its_title() -> None:
    world = Checks()
    run_report(world, entry("wait-approval", reason="no approval yet"))
    (post,) = world.writes
    assert (post["name"], post["status"], post["head_sha"]) == ("E2E", "in_progress", SHA)
    assert "conclusion" not in post
    assert post["output"]["title"] == "Waiting for an approval: no approval yet"
    assert post["details_url"] == URL


def test_the_claim_is_in_progress_and_writes_the_suite_lines_with_it() -> None:
    world = Checks()
    found = run_report(world, entry("start"))
    assert found.start
    assert {w["name"] for w in world.writes} == {"E2E", "E2E / VMaaS", "E2E / CaaS"}
    assert all(w["status"] == "in_progress" for w in world.writes)
    assert world.newest("E2E")["output"]["title"] == "Running: vmaas, caas"


def test_not_affected_is_a_completed_success() -> None:
    world = Checks()
    run_report(world, entry("skip-not-applicable"))
    (post,) = world.writes
    assert (post["status"], post["conclusion"], post["output"]["title"]) == (
        "completed", "success", "Not affected by this change",
    )  # fmt: skip


@pytest.mark.parametrize(
    ("result", "conclusion"), [("success", "success"), ("failure", "failure"), ("cancelled", "cancelled")]
)
def test_finish_completes_with_the_matching_conclusion(result: str, conclusion: str) -> None:
    world = Checks()
    finish(world, REPO, SHA, result, URL, "", "checks")
    assert (world.newest()["status"], world.newest()["conclusion"]) == ("completed", conclusion)


def test_only_check_runs_of_the_osac_ci_app_are_read() -> None:
    world = Checks()
    world.add(app="github-actions", status="completed", conclusion="success")  # the Actions token's own: ignored
    assert latest_check(world, REPO, SHA, "checks") is None
    assert run_report(world, entry("start")).start


def test_the_newest_check_run_of_the_name_counts() -> None:
    world = Checks()
    world.add(status="completed", conclusion="failure", output={"title": "Failed"}, details_url=OTHER_URL)
    world.add(output={"title": "Waiting for an approval: x"})
    got = latest_check(world, REPO, SHA, "checks")
    assert got is not None and got.phase is e2e_check.Phase.WAITING


@pytest.mark.parametrize(
    ("status", "conclusion", "state"),
    [("in_progress", None, "pending"), ("queued", None, "pending"), ("completed", "success", "success"),
     ("completed", "failure", "failure"), ("completed", "cancelled", "error"), ("completed", "timed_out", "error")],
)  # fmt: skip
def test_a_check_run_reads_back_as_the_status_state(status: str, conclusion: str | None, state: str) -> None:
    world = Checks()
    world.add(status=status, conclusion=conclusion, output={"title": "t"}, details_url=URL)
    got = newest_status(world, REPO, SHA, "E2E", "checks")
    assert got is not None and got["state"] == state and got["target_url"] == URL


def test_a_running_claim_reads_back_as_the_run_it_names() -> None:
    world = Checks()
    world.add(output={"title": "Running: vmaas"}, details_url=OTHER_URL)
    got = latest_check(world, REPO, SHA, "checks")
    assert got is not None and got.run_id == 400


def test_a_second_run_does_not_start_while_the_first_is_alive_and_a_dead_one_is_replaced() -> None:
    world = Checks(runs={400: "in_progress"})
    world.add(output={"title": "Running: vmaas"}, details_url=OTHER_URL)
    assert run_report(world, entry("start")).state == "already-running"
    dead = Checks(runs={})
    dead.add(output={"title": "Running: vmaas"}, details_url=OTHER_URL)
    assert run_report(dead, entry("start")).start


def test_the_same_answer_twice_writes_nothing_the_second_time() -> None:
    world = Checks()
    run_report(world, entry("wait-approval"))
    run_report(world, entry("wait-approval"))
    assert len(world.writes) == 1


def test_a_refused_write_is_an_error_naming_the_check() -> None:
    world = Checks()
    world.fail = True
    with pytest.raises(GitHubError, match="E2E check failed"):
        post_status(world, REPO, SHA, "E2E", "pending", "x", "", "checks")


def test_an_unknown_kind_is_refused_before_anything_is_written() -> None:
    world = Checks()
    with pytest.raises(ValueError, match="kind must be one of"):
        post_status(world, REPO, SHA, "E2E", "pending", "x", "", "labels")
    assert world.writes == []


def test_a_description_longer_than_a_title_is_cut_the_same_way() -> None:
    world = Checks()
    post_status(world, REPO, SHA, "E2E", "pending", "word " * 60, URL, "checks")
    assert len(world.writes[0]["output"]["title"]) == 140
    assert world.writes[0]["output"]["summary"].endswith(f"[Details]({URL})")


def job(world: Checks, name: str, status: str) -> None:
    world.jobs.append(
        {"name": name, "status": status, "html_url": f"https://example.invalid/o/r/actions/runs/{RUN}/job/1"}
    )


def test_track_and_settle_write_the_suite_lines_as_check_runs_and_leave_a_newer_runs_alone() -> None:
    world = Checks()
    job(world, "VMaaS / e2e-vmaas-full-install / e2e", "in_progress")
    ticks = iter(range(100))
    track(world, REPO, SHA, RUN, URL, ["vmaas"], interval=0, sleep=lambda _: None, clock=lambda: float(next(ticks)),
          kind="checks")  # fmt: skip
    assert (world.newest("E2E / VMaaS")["status"], world.newest("E2E / VMaaS")["output"]["title"]) == (
        "in_progress", "Running",
    )  # fmt: skip
    assert settle(world, REPO, SHA, RUN, URL, {"vmaas": "failure"}, "checks") == ["vmaas"]
    assert (world.newest("E2E / VMaaS")["status"], world.newest("E2E / VMaaS")["conclusion"]) == (
        "completed",
        "failure",
    )
    world.add(
        "E2E / CaaS", output={"title": "Running"}, details_url="https://example.invalid/o/r/actions/runs/501/job/9"
    )
    assert settle(world, REPO, SHA, RUN, URL, {"caas": "success"}, "checks") == []


def test_existing_from_is_unchanged_for_statuses() -> None:
    got = existing_from({"state": "pending", "description": "Running: vmaas", "target_url": OTHER_URL})
    assert got.run_id == 400
