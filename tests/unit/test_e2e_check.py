"""The E2E status: what each answer of e2e-decide does to the one commit status named E2E, and when a run may start the
suites. The statuses are kept by a small in-memory GitHub that behaves like the real one where it matters here: a
commit's statuses are listed newest first, the newest of a context is the one that counts, and a status is never
changed, only followed by a newer one."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from osac_ci import e2e_check
from osac_ci.e2e_check import (
    CREATOR,
    Existing,
    Phase,
    Write,
    describe,
    existing_from,
    finish,
    latest_check,
    newest_status,
    plan,
    report,
    run_of_url,
)
from osac_ci.github.api import GitHubError, Response

pytestmark = pytest.mark.unit
REPO = "o/r"
SHA = "a" * 40
RUN = 500
OTHER_RUN = 400
URL = "https://example.invalid/o/r/actions/runs/500"
OTHER_URL = "https://example.invalid/o/r/actions/runs/400"
BASE = f"/repos/{REPO}"


class World:
    """GitHub, reduced to the statuses of one commit and the state of workflow runs."""

    def __init__(self, runs: Mapping[int, str] | None = None) -> None:
        self.statuses: list[dict[str, Any]] = []  # newest first, as GitHub lists them
        self.runs = dict(runs or {})  # workflow run id -> "in_progress" | "completed"; absent = 404
        self.writes: list[dict[str, Any]] = []
        self.fail_writes = False
        self.after_write: Callable[[World], None] | None = None  # something another run does right after a write
        self.next_id = 100

    def add(self, **fields: Any) -> dict[str, Any]:
        status: dict[str, Any] = {
            "id": self.next_id,
            "context": "E2E",
            "state": "pending",
            "description": "",
            "target_url": "",
            "creator": {"login": CREATOR},
        }
        status.update(fields)
        self.next_id += 1
        self.statuses.insert(0, status)
        return status

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path == f"{BASE}/commits/{SHA}/statuses":
            page, per = int((params or {}).get("page", 1)), int((params or {}).get("per_page", 100))
            return Response(200, self.statuses[(page - 1) * per : page * per])
        if method == "GET" and (m := re.fullmatch(rf"{BASE}/actions/runs/(\d+)", path)):
            status = self.runs.get(int(m.group(1)))
            return Response(404, {}) if status is None else Response(200, {"status": status})
        if method == "POST" and path == f"{BASE}/statuses/{SHA}":
            self.writes.append(body)
            if self.fail_writes:
                return Response(403, {"message": "Resource not accessible by integration"})
            self.add(
                **{k: body[k] for k in ("context", "state", "description") if k in body},
                target_url=body.get("target_url", ""),
            )
            if self.after_write is not None:
                hook, self.after_write = self.after_write, None
                hook(self)
            return Response(201, {"id": self.statuses[0]["id"]})
        return Response(404, {"message": f"no route for {method} {path}"})

    def newest(self, context: str = "E2E") -> dict[str, Any]:
        return next(s for s in self.statuses if s["context"] == context)


def entry(action: str = "wait-approval", **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "number": 7,
        "head_sha": SHA,
        "action": action,
        "reason": "no approval from a person with write access or CodeRabbit on the head commit",
        "suites": ["vmaas", "caas"],
        "suite_checks": {"vmaas": "E2E / VMaaS Full Install", "caas": "E2E / CaaS Full Install"},
        "approvers": ["bob"],
    }
    base.update(extra)
    return base


def run_report(world: World, *entries: dict[str, Any], run_id: int = RUN) -> e2e_check.Report:
    return report(world, REPO, entries, run_id, URL)


def e2e_writes(world: World) -> list[dict[str, Any]]:
    return [w for w in world.writes if w["context"] == "E2E"]


def running(world: World, run: int = OTHER_RUN) -> dict[str, Any]:
    return world.add(description="Running: vmaas, caas", target_url=f"https://x/o/r/actions/runs/{run}")


# description and reading --------------------------------------------------------------------------------------------


def test_a_description_is_one_line_of_at_most_140_characters() -> None:
    assert describe("a  b\n c") == "a b c"
    assert describe("x" * 140) == "x" * 140
    cut = describe("word " * 60)
    assert len(cut) == 140 and cut.endswith("…")


@pytest.mark.parametrize(
    ("url", "run"),
    [
        ("https://github.com/o/r/actions/runs/38171099302", 38171099302),
        ("https://github.com/o/r/actions/runs/38171099302/job/114575160506", 38171099302),
        ("https://github.com/o/r/runs/114564073637", None),
        ("", None),
    ],
)
def test_the_run_is_read_from_the_link(url: str, run: int | None) -> None:
    assert run_of_url(url) == run


def test_a_pending_status_that_says_running_with_a_run_link_is_the_claim_of_that_run() -> None:
    got = existing_from({"state": "pending", "description": "Running: vmaas", "target_url": OTHER_URL})
    assert (got.phase, got.run_id) == (Phase.RUNNING, OTHER_RUN)


def test_any_other_pending_status_waits_even_with_a_run_link() -> None:
    got = existing_from({"state": "pending", "description": "Waiting for CI to finish", "target_url": OTHER_URL})
    assert got.phase is Phase.WAITING and got.run_id is None


def test_a_running_status_without_a_run_link_counts_as_waiting() -> None:
    assert existing_from({"state": "pending", "description": "Running: vmaas", "target_url": ""}).phase is Phase.WAITING


def test_the_not_affected_answer_belongs_to_no_run_even_though_its_link_names_one() -> None:
    got = existing_from({"state": "success", "description": "Not affected by this change", "target_url": OTHER_URL})
    assert (got.external_id, got.run_id, got.phase) == ("not-affected", None, Phase.DONE)


@pytest.mark.parametrize("state", ["success", "failure", "error"])
def test_a_finished_status_is_the_result_of_the_run_its_link_names(state: str) -> None:
    got = existing_from({"state": state, "description": "Passed", "target_url": OTHER_URL})
    assert (got.phase, got.run_id) == (Phase.DONE, OTHER_RUN)


# waiting -------------------------------------------------------------------------------------------------------


def test_a_pull_request_with_no_status_gets_one_that_says_what_it_waits_for() -> None:
    world = World()
    found = run_report(world, entry("wait-approval"))
    assert (found.state, found.start) == ("waiting", False)
    (post,) = world.writes
    assert post["context"] == "E2E" and post["state"] == "pending" and post["target_url"] == URL
    assert post["description"].startswith("Waiting for an approval: no approval from a person with write access")


@pytest.mark.parametrize(
    ("action", "title"),
    [
        ("skip-draft", "Waiting: the pull request is a draft"),
        ("wait-approval", "Waiting for an approval"),
        ("blocked-changes-requested", "Waiting: changes were requested"),
        ("blocked-cheap-checks-failed", "Waiting: CI failed"),
    ],
)
def test_every_waiting_answer_has_its_own_title(action: str, title: str) -> None:
    world = World()
    run_report(world, entry(action, reason="why"))
    assert world.newest()["description"] == f"{title}: why"


def test_a_reason_that_only_repeats_the_title_is_left_out() -> None:
    world = World()
    run_report(world, entry("skip-draft", reason="the pull request is a draft"))
    assert world.newest()["description"] == "Waiting: the pull request is a draft"


def test_a_long_reason_is_cut_to_what_a_status_can_hold() -> None:
    world = World()
    run_report(world, entry("wait-approval", reason="because " * 40))
    assert len(world.newest()["description"]) == 140


def test_the_same_answer_twice_writes_nothing_the_second_time() -> None:
    world = World()
    run_report(world, entry("wait-approval"))
    again = run_report(world, entry("wait-approval"))
    assert again.state == "waiting" and len(world.writes) == 1


def test_a_changed_reason_writes_a_newer_status_for_the_same_context() -> None:
    world = World()
    run_report(world, entry("skip-draft", reason="the pull request is a draft"))
    run_report(world, entry("blocked-cheap-checks-failed", reason="cheap checks failed: unit tests"))
    assert len(world.writes) == 2
    assert world.newest()["description"] == "Waiting: CI failed: cheap checks failed: unit tests"


def test_a_waiting_answer_never_changes_a_result_that_is_already_there() -> None:
    world = World()
    world.add(state="success", description="Passed", target_url=OTHER_URL)
    found = run_report(world, entry("wait-approval"))
    assert found.state == "already-ran" and world.writes == []


def test_a_waiting_answer_does_not_disturb_a_run_that_is_alive() -> None:
    world = World(runs={OTHER_RUN: "in_progress"})
    running(world)
    found = run_report(world, entry("wait-approval"))
    assert found.state == "already-running" and world.writes == []


def test_a_run_that_died_is_replaced_by_the_waiting_status() -> None:
    world = World(runs={})  # the run is gone: 404
    running(world)
    found = run_report(world, entry("wait-approval"))
    assert found.state == "waiting" and world.newest()["description"].startswith("Waiting for an approval")


def test_a_change_no_suite_cares_about_gets_a_green_status_saying_so() -> None:
    world = World()
    found = run_report(world, entry("skip-not-applicable"))
    assert found.state == "not-affected"
    assert (world.newest()["state"], world.newest()["description"]) == ("success", "Not affected by this change")


def test_not_affected_twice_writes_once_and_turns_a_waiting_status_green() -> None:
    world = World()
    run_report(world, entry("wait-approval"))
    run_report(world, entry("skip-not-applicable"))
    run_report(world, entry("skip-not-applicable"))
    assert [w["state"] for w in world.writes] == ["pending", "success"]


def test_a_closed_pull_request_is_left_alone() -> None:
    world = World()
    found = run_report(world, entry("skip-closed"))
    assert found.state == "ignored" and world.writes == []


# start ---------------------------------------------------------------------------------------------------------


def test_start_with_no_status_claims_the_e2e_for_this_run_and_links_the_run() -> None:
    world = World()
    found = run_report(world, entry("start"))
    assert (found.state, found.start) == ("running", True)
    (post,) = e2e_writes(world)
    assert post["state"] == "pending" and post["description"] == "Running: vmaas, caas" and post["target_url"] == URL


def test_the_claim_writes_the_suite_lines_with_it_so_they_appear_together() -> None:
    world = World()
    run_report(world, entry("start"))
    suites = {w["context"]: w for w in world.writes if w["context"] != "E2E"}
    assert set(suites) == {"E2E / VMaaS Full Install", "E2E / CaaS Full Install"}
    assert all(w["state"] == "pending" and w["description"] == "Waiting for a runner" for w in suites.values())
    assert all(w["target_url"] == URL for w in suites.values())


def test_a_claim_replaces_the_suite_lines_an_earlier_attempt_left() -> None:
    world = World()
    world.add(context="E2E / VMaaS Full Install", state="error", description="Cancelled", target_url=URL)
    run_report(world, entry("start"))
    assert world.newest("E2E / VMaaS Full Install")["description"] == "Waiting for a runner"


def test_a_decision_with_no_line_names_at_all_still_claims_and_writes_no_suite_lines() -> None:
    world = World()
    found = run_report(world, entry("start", suite_checks={}))
    assert found.start and {w["context"] for w in world.writes} == {"E2E"}


def test_a_suite_the_decision_gives_no_line_name_is_refused_before_anything_is_written() -> None:
    world = World()
    with pytest.raises(ValueError, match="no line name for: netris"):
        run_report(world, entry("start", suites=["vmaas", "netris"]))
    assert world.writes == []


def test_the_lines_are_whatever_the_decision_names_so_a_new_suite_needs_no_change_here() -> None:
    world = World()
    checks = {"vmaas": "E2E / VMaaS Full Install", "netris": "E2E / Netris Fabric"}
    found = run_report(world, entry("start", suites=["vmaas", "netris"], suite_checks=checks))
    assert dict(found.suite_checks) == checks
    assert {w["context"] for w in world.writes} == {"E2E", *checks.values()}


def test_a_suite_not_in_the_decision_gets_no_line_even_when_it_has_a_name() -> None:
    world = World()
    run_report(world, entry("start", suites=["vmaas"]))
    assert {w["context"] for w in world.writes} == {"E2E", "E2E / VMaaS Full Install"}


@pytest.mark.parametrize(
    "checks",
    [
        {"vmaas": "E2E"},  # the required line itself
        {"vmaas": ""},
        {"vmaas": "x" * 101},
        {"vmaas": "two\nlines"},
        {"VMaaS": "E2E / VMaaS"},  # an id is a lower-case word
        {"../x": "E2E / X"},
        {"vmaas": "Same", "caas": "Same"},
        {"vmaas": 3},
        ["vmaas"],
    ],
)
def test_suite_lines_that_are_not_plain_names_are_refused_before_anything_is_written(checks: object) -> None:
    world = World()
    with pytest.raises(ValueError, match="suite|line|E2E"):
        run_report(world, entry("start", suite_checks=checks))
    assert world.writes == []


def test_waiting_and_not_affected_answers_write_no_suite_lines() -> None:
    world = World()
    run_report(world, entry("wait-approval"))
    run_report(world, entry("skip-not-applicable"))
    assert {w["context"] for w in world.writes} == {"E2E"}


def test_a_claim_another_run_overtook_writes_no_suite_lines() -> None:
    world = World(runs={OTHER_RUN: "in_progress"})
    world.after_write = lambda w: running(w)
    run_report(world, entry("start"))
    assert {w["context"] for w in world.writes} == {"E2E"}


def test_start_turns_the_waiting_status_into_the_running_one() -> None:
    world = World()
    run_report(world, entry("wait-approval"))
    found = run_report(world, entry("start"))
    assert found.start and world.newest()["description"] != "" and len(e2e_writes(world)) == 2
    assert world.newest("E2E")["description"] == "Running: vmaas, caas"


def test_a_second_run_does_not_start_while_the_first_is_alive() -> None:
    world = World(runs={OTHER_RUN: "in_progress"})
    running(world)
    found = run_report(world, entry("start"))
    assert (found.state, found.start) == ("already-running", False) and world.writes == []


def test_a_run_that_died_is_replaced_and_the_e2e_starts_again() -> None:
    world = World(runs={})
    running(world)
    found = run_report(world, entry("start"))
    assert found.start and "ended without a result" in found.note
    assert world.newest()["target_url"] == URL


def test_a_run_that_completed_without_finishing_counts_as_dead() -> None:
    world = World(runs={OTHER_RUN: "completed"})
    running(world)
    assert run_report(world, entry("start")).start


def test_the_same_run_asking_twice_keeps_one_claim() -> None:
    world = World()
    run_report(world, entry("start"))
    again = run_report(world, entry("start"))
    assert again.start and len(e2e_writes(world)) == 1


def test_a_commit_gets_its_e2e_once_a_result_from_another_run_means_no_second_start() -> None:
    world = World()
    world.add(state="failure", description="Failed", target_url=OTHER_URL)
    found = run_report(world, entry("start"))
    assert (found.state, found.start) == ("already-ran", False) and world.writes == []
    assert "re-run it from its workflow run" in found.note


def test_the_same_workflow_run_started_again_from_the_actions_page_starts_again() -> None:
    world = World()
    world.add(state="failure", description="Failed", target_url=URL)  # this very run, an earlier attempt
    found = run_report(world, entry("start"))
    assert found.start and "re-run" in found.note
    assert world.newest()["description"] == "Running: vmaas, caas"


# reading the statuses ------------------------------------------------------------------------------------------


def test_only_statuses_the_github_actions_bot_wrote_are_looked_at() -> None:
    world = World()
    world.add(state="success", description="Passed", creator={"login": "someone"})  # a person's, ignored
    world.add(context="CodeRabbit", state="success", description="Review completed")
    assert latest_check(world, REPO, SHA) is None
    found = run_report(world, entry("start"))
    assert found.start


def test_the_newest_status_is_the_one_that_counts() -> None:
    world = World()
    world.add(state="failure", description="Failed", target_url=OTHER_URL)
    world.add(description="Waiting for an approval: x")
    got = latest_check(world, REPO, SHA)
    assert got is not None and got.phase is Phase.WAITING


def test_statuses_of_other_contexts_are_not_the_e2e() -> None:
    world = World()
    world.add(context="E2E / VMaaS Full Install", state="success", description="Passed")
    assert newest_status(world, REPO, SHA, "E2E") is None
    assert newest_status(world, REPO, SHA, "E2E / VMaaS Full Install") is not None


def test_nothing_is_written_unless_exactly_one_pull_request_was_decided() -> None:
    world = World()
    none = run_report(world)
    two = run_report(world, entry("start"), entry("start", number=8))
    assert (none.state, two.state) == ("ignored", "ignored") and world.writes == []


def test_a_decision_that_is_not_an_object_is_an_error_before_anything_is_written() -> None:
    world = World()
    with pytest.raises(ValueError, match="object"):
        report(world, REPO, ["start"], RUN, URL)  # type: ignore[list-item]
    assert world.writes == []


# finish --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "state", "title"),
    [("success", "success", "Passed"), ("failure", "failure", "Failed"), ("cancelled", "error", "Cancelled")],
)
def test_finish_writes_the_result_linked_to_the_run(result: str, state: str, title: str) -> None:
    world = World()
    run_report(world, entry("start"))
    finish(world, REPO, SHA, result, URL)
    assert (world.newest()["state"], world.newest()["description"]) == (state, title)
    assert world.newest()["target_url"] == URL


def test_finish_adds_the_detail_after_the_result() -> None:
    world = World()
    finish(world, REPO, SHA, "failure", URL, "vmaas=failure caas=success")
    assert world.newest()["description"] == "Failed: vmaas=failure caas=success"


def test_finish_refuses_a_result_it_does_not_know() -> None:
    world = World()
    with pytest.raises(ValueError, match="result must be one of"):
        finish(world, REPO, SHA, "skipped", URL)
    assert world.writes == []


def test_the_same_run_started_again_after_its_own_failure_is_claimed_again_and_finishes_again() -> None:
    world = World()
    run_report(world, entry("start"))
    finish(world, REPO, SHA, "failure", URL)
    assert run_report(world, entry("start")).start
    finish(world, REPO, SHA, "success", URL)
    assert world.newest()["state"] == "success" and len(e2e_writes(world)) == 4


def test_a_refused_write_is_an_error_not_a_silent_answer() -> None:
    world = World()
    world.fail_writes = True
    with pytest.raises(GitHubError, match="Resource not accessible"):
        run_report(world, entry("start"))


# liveness ------------------------------------------------------------------------------------------------------


def test_an_unexpected_answer_about_a_run_is_an_error_and_never_replaces_a_live_run() -> None:
    world = World()
    running(world)
    real = world.request

    def request(method: str, path: str, **kw: Any) -> Response:
        if "/actions/runs/" in path:
            return Response(500, {"message": "boom"})
        return real(method, path, **kw)

    world.request = request  # type: ignore[method-assign]
    with pytest.raises(GitHubError):
        run_report(world, entry("start"))
    assert world.writes == []


def test_the_liveness_of_a_run_is_only_asked_when_the_answer_depends_on_it() -> None:
    asked: list[int] = []

    def alive(run: int) -> bool:
        asked.append(run)
        return True

    plan(entry("start"), None, RUN, alive)
    plan(entry("start"), Existing("pending", "Waiting", "waiting"), RUN, alive)
    plan(entry("start"), Existing("success", "Passed", f"run:{OTHER_RUN}"), RUN, alive)
    plan(entry("start"), Existing("pending", "Running", f"run:{RUN}"), RUN, alive)
    assert asked == []
    plan(entry("start"), Existing("pending", "Running", f"run:{OTHER_RUN}"), RUN, alive)
    assert asked == [OTHER_RUN]


def test_plan_replaces_a_dead_claim_with_one_write() -> None:
    outcome = plan(entry("start"), Existing("pending", "Running", f"run:{OTHER_RUN}"), RUN, lambda _: False)
    assert outcome.start and outcome.writes == (Write("pending", "Running: vmaas, caas"),)


# the commit id -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("sha", ["", "abc", "A" * 40, "a" * 40 + "\n", "../x" + "a" * 37, "a" * 41])
def test_a_commit_id_that_is_not_40_lower_case_hex_digits_is_refused_before_any_request(sha: str) -> None:
    world = World()
    with pytest.raises(ValueError, match="invalid commit id"):
        latest_check(world, REPO, sha)
    with pytest.raises(ValueError, match="invalid commit id"):
        finish(world, REPO, sha, "success", URL)
    with pytest.raises(ValueError, match="invalid commit id"):
        report(world, REPO, [entry("start", head_sha=sha)], RUN, URL)
    assert world.writes == []


# a second run claiming the same commit -------------------------------------------------------------------------


def test_a_claim_that_another_run_overtook_does_not_start_the_suites() -> None:
    world = World(runs={OTHER_RUN: "in_progress"})
    world.after_write = lambda w: running(w)  # another run claims it right after this run wrote
    found = run_report(world, entry("start"))
    assert (found.state, found.start) == ("already-running", False)
    assert f"run {OTHER_RUN}" in found.note


def test_a_claim_nobody_overtook_starts() -> None:
    world = World()
    found = run_report(world, entry("start"))
    assert found.start and latest_check(world, REPO, SHA) == Existing("pending", "Running: vmaas, caas", f"run:{RUN}")


def test_a_comment_that_asks_for_nothing_writes_nothing() -> None:
    out = e2e_check.plan({"action": "skip-not-a-request"}, None, 500, lambda rid: False)
    assert out.state == "ignored" and not out.start and not out.writes
