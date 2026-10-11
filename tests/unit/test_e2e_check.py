"""The E2E check: what each answer of e2e-decide does to the one check run named E2E, and when a run may start the
suites. The check runs are kept by a small in-memory GitHub that behaves like the real one where it matters here: a
new check run gets the next id, the newest of a name is the one that counts, an update changes it in place."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import pytest

from osac_ci import e2e_check
from osac_ci.e2e_check import Existing, Outcome, Phase, Write, finish, latest_check, plan, report
from osac_ci.github.api import GitHubError, Response

pytestmark = pytest.mark.unit
REPO = "o/r"
SHA = "a" * 40
RUN = 500
OTHER_RUN = 400
URL = "https://example.invalid/run/500"
BASE = f"/repos/{REPO}"


class World:
    """GitHub, reduced to check runs of one commit and the status of workflow runs."""

    def __init__(self, runs: Mapping[int, str] | None = None) -> None:
        self.checks: list[dict[str, Any]] = []
        self.runs = dict(runs or {})  # workflow run id -> "in_progress" | "completed"; absent = 404
        self.writes: list[tuple[str, str, dict[str, Any]]] = []
        self.next_id = 100
        self.fail_writes = False

    def add(self, **fields: Any) -> dict[str, Any]:
        check = {
            "id": self.next_id,
            "name": "E2E",
            "status": "in_progress",
            "conclusion": None,
            "external_id": "",
            "app": {"slug": "github-actions"},
            "output": {"title": "", "summary": ""},
        }
        check.update(fields)
        self.next_id += 1
        self.checks.append(check)
        return check

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path == f"{BASE}/commits/{SHA}/check-runs":
            page, per = int((params or {}).get("page", 1)), int((params or {}).get("per_page", 100))
            return Response(200, {"check_runs": self.checks[(page - 1) * per : page * per]})
        if method == "GET" and (m := re.fullmatch(rf"{BASE}/actions/runs/(\d+)", path)):
            status = self.runs.get(int(m.group(1)))
            return Response(404, {}) if status is None else Response(200, {"status": status})
        if method == "POST" and path == f"{BASE}/check-runs":
            self.writes.append((method, path, body))
            if self.fail_writes:
                return Response(403, {"message": "Resource not accessible by integration"})
            check = self.add(
                name=body["name"],
                status=body["status"],
                conclusion=body.get("conclusion"),
                external_id=body["external_id"],
                output=body["output"],
            )
            check["head_sha"], check["details_url"] = body["head_sha"], body.get("details_url")
            return Response(201, {"id": check["id"]})
        if method == "PATCH" and (m := re.fullmatch(rf"{BASE}/check-runs/(\d+)", path)):
            self.writes.append((method, path, body))
            check = next(c for c in self.checks if c["id"] == int(m.group(1)))
            check.update(
                status=body["status"],
                conclusion=body.get("conclusion"),
                external_id=body["external_id"],
                output=body["output"],
            )
            check["details_url"] = body.get("details_url", check.get("details_url"))
            return Response(200, {"id": check["id"]})
        return Response(404, {"message": f"no route for {method} {path}"})

    @property
    def posts(self) -> list[dict[str, Any]]:
        return [b for m, _, b in self.writes if m == "POST"]

    @property
    def patches(self) -> list[tuple[str, dict[str, Any]]]:
        return [(p, b) for m, p, b in self.writes if m == "PATCH"]


def entry(action: str = "wait-approval", **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "number": 7,
        "head_sha": SHA,
        "action": action,
        "reason": "no approval from a person with write access or CodeRabbit on the head commit",
        "suites": ["vmaas", "caas"],
        "approvers": ["bob"],
    }
    base.update(extra)
    return base


def run_report(world: World, *entries: dict[str, Any], run_id: int = RUN) -> e2e_check.Report:
    return report(world, REPO, entries, run_id, URL)


# waiting -------------------------------------------------------------------------------------------------------


def test_a_pull_request_with_no_check_gets_one_that_says_what_it_waits_for() -> None:
    world = World()
    found = run_report(world, entry("wait-approval"))
    assert (found.state, found.start) == ("waiting", False)
    (post,) = world.posts
    assert post["name"] == "E2E" and post["head_sha"] == SHA and post["status"] == "in_progress"
    assert post["output"]["title"] == "Waiting for an approval"
    assert "no approval from a person with write access" in post["output"]["summary"]
    assert "vmaas, caas" in post["output"]["summary"]
    assert post["external_id"] == "waiting" and "conclusion" not in post


@pytest.mark.parametrize(
    ("action", "title"),
    [
        ("skip-draft", "Waiting: the pull request is a draft"),
        ("wait-approval", "Waiting for an approval"),
        ("blocked-changes-requested", "Waiting: changes were requested"),
        ("blocked-cheap-checks-failed", "Waiting: CI failed"),
        ("wait-cheap-checks", "Waiting for CI to finish"),
    ],
)
def test_every_waiting_answer_has_its_own_title(action: str, title: str) -> None:
    world = World()
    run_report(world, entry(action))
    assert world.posts[0]["output"]["title"] == title


def test_the_summary_lists_what_failed_and_what_is_unfinished() -> None:
    world = World()
    run_report(
        world,
        entry(
            "blocked-cheap-checks-failed",
            cheap_failed=["Run unit tests: conclusion: failure"],
            cheap_pending=["pre-commit: in_progress"],
            stale_approvals=["bob"],
        ),
    )
    summary = world.posts[0]["output"]["summary"]
    assert "Failed: Run unit tests: conclusion: failure" in summary
    assert "Not finished: pre-commit: in_progress" in summary
    assert "Approved an older commit (does not count): bob" in summary


def test_the_same_answer_twice_writes_nothing_the_second_time() -> None:
    world = World()
    run_report(world, entry("wait-approval"))
    before = len(world.writes)
    again = run_report(world, entry("wait-approval"))
    assert len(world.writes) == before and again.state == "waiting" and again.check_id == world.checks[0]["id"]


def test_a_changed_reason_updates_the_same_check_in_place() -> None:
    world = World()
    run_report(world, entry("wait-approval"))
    run_report(world, entry("wait-cheap-checks", reason="cheap checks not finished: pre-commit"))
    assert len(world.checks) == 1 and len(world.posts) == 1
    ((path, body),) = world.patches
    assert path.endswith(f"/check-runs/{world.checks[0]['id']}")
    assert body["output"]["title"] == "Waiting for CI to finish"


def test_a_waiting_answer_never_changes_a_result_that_is_already_there() -> None:
    world = World()
    world.add(status="completed", conclusion="success", external_id=f"run:{OTHER_RUN}")
    found = run_report(world, entry("blocked-changes-requested"))
    assert found.state == "already-ran" and world.writes == []


def test_a_waiting_answer_does_not_disturb_a_run_that_is_alive() -> None:
    world = World(runs={OTHER_RUN: "in_progress"})
    world.add(external_id=f"run:{OTHER_RUN}")
    found = run_report(world, entry("wait-cheap-checks"))
    assert found.state == "already-running" and world.writes == []


def test_a_run_that_died_leaves_a_check_that_is_cancelled_and_replaced_by_the_waiting_one() -> None:
    world = World(runs={OTHER_RUN: "completed"})
    dead = world.add(external_id=f"run:{OTHER_RUN}")
    found = run_report(world, entry("wait-approval"))
    assert dead["status"] == "completed" and dead["conclusion"] == "cancelled"
    assert found.state == "waiting" and world.checks[-1]["external_id"] == "waiting"


# not affected ----------------------------------------------------------------------------------------------------


def test_a_change_no_suite_cares_about_gets_a_green_check_saying_so() -> None:
    world = World()
    found = run_report(world, entry("skip-not-applicable", suites=[]))
    assert (found.state, found.start) == ("not-affected", False)
    (post,) = world.posts
    assert post["status"] == "completed" and post["conclusion"] == "success"
    assert post["output"]["title"] == "Not affected by this change"


def test_not_affected_twice_writes_once_and_turns_a_waiting_check_green_in_place() -> None:
    world = World()
    run_report(world, entry("skip-not-applicable", suites=[]))
    run_report(world, entry("skip-not-applicable", suites=[]))
    assert len(world.writes) == 1
    other = World()
    waiting = other.add(external_id="waiting")
    run_report(other, entry("skip-not-applicable", suites=[]))
    assert waiting["conclusion"] == "success" and len(other.checks) == 1


def test_a_closed_pull_request_is_left_alone() -> None:
    world = World()
    found = run_report(world, entry("skip-closed"))
    assert found.state == "ignored" and world.writes == []


# start ---------------------------------------------------------------------------------------------------------


def test_start_with_no_check_claims_the_e2e_for_this_run() -> None:
    world = World()
    found = run_report(world, entry("start"))
    assert (found.state, found.start) == ("running", True)
    (post,) = world.posts
    assert post["status"] == "in_progress" and post["external_id"] == f"run:{RUN}" and post["details_url"] == URL
    assert post["output"]["title"] == "Running: vmaas, caas"
    assert found.check_id == world.checks[0]["id"]


def test_the_running_check_carries_a_clickable_link_to_the_workflow_run() -> None:
    # GitHub keeps the check's own page as the details link when the Actions token writes it, so the text has the link
    world = World()
    run_report(world, entry("start"))
    assert world.checks[0]["output"]["summary"].endswith(f"[Open the workflow run]({URL})")


def test_a_waiting_check_has_no_run_link() -> None:
    world = World()
    run_report(world, entry("wait-approval"))
    assert "Open the workflow run" not in world.checks[0]["output"]["summary"]


def test_start_turns_the_waiting_check_into_the_running_one_without_a_second_check() -> None:
    world = World()
    waiting = world.add(external_id="waiting")
    found = run_report(world, entry("start"))
    assert found.start and found.check_id == waiting["id"] and len(world.checks) == 1
    assert waiting["external_id"] == f"run:{RUN}"


def test_a_second_run_does_not_start_while_the_first_is_alive() -> None:
    world = World(runs={OTHER_RUN: "in_progress"})
    world.add(external_id=f"run:{OTHER_RUN}")
    found = run_report(world, entry("start"))
    assert (found.state, found.start) == ("already-running", False) and world.writes == []


def test_a_run_that_died_is_cancelled_in_the_check_and_the_e2e_starts_again() -> None:
    world = World(runs={})  # the run is gone: 404
    dead = world.add(external_id=f"run:{OTHER_RUN}")
    found = run_report(world, entry("start"))
    assert found.start and dead["conclusion"] == "cancelled"
    assert world.checks[-1]["external_id"] == f"run:{RUN}" and world.checks[-1]["status"] == "in_progress"


def test_the_cancelled_check_does_not_link_to_the_new_run_and_the_new_check_does() -> None:
    # the cancelled check names the old run; the invocation's URL is the new run's, so it would point at the wrong run
    world = World(runs={})
    dead = world.add(external_id=f"run:{OTHER_RUN}")
    run_report(world, entry("start"))
    assert "Open the workflow run" not in dead["output"]["summary"] and not dead.get("details_url")
    assert world.checks[-1]["output"]["summary"].endswith(f"[Open the workflow run]({URL})")


def test_a_run_that_completed_without_finishing_its_check_counts_as_dead() -> None:
    world = World(runs={OTHER_RUN: "completed"})
    dead = world.add(external_id=f"run:{OTHER_RUN}")
    assert run_report(world, entry("start")).start and dead["conclusion"] == "cancelled"


def test_the_same_run_asking_twice_keeps_one_claim() -> None:
    world = World(runs={RUN: "in_progress"})
    first = run_report(world, entry("start"))
    second = run_report(world, entry("start"))
    assert first.start and second.start and second.check_id == first.check_id and len(world.checks) == 1


def test_a_commit_gets_its_e2e_once_a_result_from_another_run_means_no_second_start() -> None:
    for conclusion in ("success", "failure", "cancelled"):
        world = World()
        world.add(status="completed", conclusion=conclusion, external_id=f"run:{OTHER_RUN}")
        found = run_report(world, entry("start"))
        assert (found.state, found.start) == ("already-ran", False) and world.writes == []
        assert "re-run" in found.note


def test_the_same_workflow_run_started_again_from_the_actions_page_starts_again() -> None:
    world = World()
    world.add(status="completed", conclusion="failure", external_id=f"run:{RUN}")
    found = run_report(world, entry("start"))
    assert found.start and len(world.checks) == 2
    assert world.checks[-1]["status"] == "in_progress" and found.check_id == world.checks[-1]["id"]


# which check runs count -------------------------------------------------------------------------------------------


def test_only_the_e2e_check_runs_of_the_github_actions_app_are_looked_at() -> None:
    world = World()
    world.add(external_id=f"run:{OTHER_RUN}", app={"slug": "someone-else"})
    world.add(name="Unit Tests", external_id=f"run:{OTHER_RUN}")
    assert latest_check(world, REPO, SHA) is None
    found = run_report(world, entry("start"))
    assert found.start and len(world.posts) == 1


def test_the_newest_check_run_is_the_one_that_counts() -> None:
    world = World()
    world.add(status="completed", conclusion="failure", external_id=f"run:{OTHER_RUN}")
    newest = world.add(external_id="waiting")
    existing = latest_check(world, REPO, SHA)
    assert existing is not None and existing.id == newest["id"] and existing.phase is Phase.WAITING


def test_a_check_run_the_app_left_with_no_external_id_counts_as_waiting() -> None:
    world = World()
    world.add()
    found = run_report(world, entry("start"))
    assert found.start and len(world.checks) == 1


# several or no pull requests -------------------------------------------------------------------------------------


def test_nothing_is_written_unless_exactly_one_pull_request_was_decided() -> None:
    world = World()
    none = run_report(world)
    several = run_report(world, entry("start"), entry("start", number=8))
    assert none.state == several.state == "ignored" and not none.start and not several.start
    assert world.writes == []


# finishing -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "title"), [("success", "Passed"), ("failure", "Failed"), ("cancelled", "Cancelled")]
)
def test_finish_completes_the_claimed_check_with_the_result(result: str, title: str) -> None:
    world = World()
    run_report(world, entry("start"))
    claimed = world.checks[0]["id"]
    finish(world, REPO, SHA, claimed, result, RUN, URL, "")
    check = world.checks[0]
    assert (check["status"], check["conclusion"], check["output"]["title"]) == ("completed", result, title)
    assert len(world.checks) == 1 and check["details_url"] == URL
    assert check["output"]["summary"].endswith(f"[Open the workflow run]({URL})")


def test_finish_without_a_claimed_check_creates_a_completed_one() -> None:
    world = World()
    finish(world, REPO, SHA, None, "failure", RUN, URL, "vmaas failed")
    (post,) = world.posts
    assert (
        post["conclusion"] == "failure"
        and post["output"]["summary"].startswith("vmaas failed")
        and post["head_sha"] == SHA
    )


def test_finish_refuses_a_result_it_does_not_know() -> None:
    with pytest.raises(ValueError, match="result must be one of"):
        finish(World(), REPO, SHA, 1, "skipped", RUN, URL)


def test_a_new_result_after_a_failed_run_replaces_it_for_the_pull_request() -> None:
    """Re-running the same workflow run: the new claim is newer than the failure, and the result is what counts."""
    world = World()
    world.add(status="completed", conclusion="failure", external_id=f"run:{RUN}")
    again = run_report(world, entry("start"))
    finish(world, REPO, SHA, again.check_id, "success", RUN, URL)
    latest = latest_check(world, REPO, SHA)
    assert latest is not None and latest.conclusion == "success"


# failures --------------------------------------------------------------------------------------------------------


def test_a_refused_write_is_an_error_not_a_silent_answer() -> None:
    world = World()
    world.fail_writes = True
    with pytest.raises(GitHubError, match="Resource not accessible"):
        run_report(world, entry("start"))


def test_an_unexpected_answer_about_a_run_is_an_error_and_never_cancels_a_live_run() -> None:
    class Broken(World):
        def request(self, method: str, path: str, **kw: Any) -> Response:  # type: ignore[no-untyped-def]
            if "/actions/runs/" in path:
                return Response(500, {})
            return super().request(method, path, **kw)

    world = Broken()
    check = world.add(external_id=f"run:{OTHER_RUN}")
    with pytest.raises(GitHubError):
        run_report(world, entry("start"))
    assert check["status"] == "in_progress" and world.writes == []


# the pure part ---------------------------------------------------------------------------------------------------


def never(_: int) -> bool:
    raise AssertionError("the answer must not depend on whether a run is alive")


def test_the_liveness_of_a_run_is_only_asked_when_the_answer_depends_on_it() -> None:
    waiting = Existing(1, "in_progress", "", "waiting", "", "")
    done = Existing(1, "completed", "success", f"run:{OTHER_RUN}", "", "")
    assert plan(entry("start"), waiting, RUN, never).start
    assert plan(entry("start"), None, RUN, never).start
    assert not plan(entry("start"), done, RUN, never).start
    assert not plan(entry("wait-approval"), done, RUN, never).start


def test_plan_returns_writes_in_the_order_they_have_to_happen() -> None:
    dead = Existing(9, "in_progress", "", f"run:{OTHER_RUN}", "", "")
    outcome = plan(entry("start"), dead, RUN, lambda _: False)
    assert isinstance(outcome, Outcome) and [w.patch_id for w in outcome.writes] == [9, None]
    first, second = outcome.writes
    assert isinstance(first, Write) and first.conclusion == "cancelled" and second.status == "in_progress"


def test_a_run_id_that_is_not_a_number_is_treated_as_a_run_that_cannot_be_alive() -> None:
    odd = Existing(9, "in_progress", "", "run:abc", "", "")
    assert odd.run_id is None
    assert plan(entry("start"), odd, RUN, never).start is True


# commit ids ------------------------------------------------------------------------------------------------------


BAD_SHAS = ["", "abc", "A" * 40, "a" * 39, "a" * 41, "a" * 40 + "\n", "../" + "a" * 37, "a" * 39 + "/"]


@pytest.mark.parametrize("sha", BAD_SHAS)
def test_a_commit_id_that_is_not_40_lower_case_hex_digits_is_refused_before_any_request(sha: str) -> None:
    world = World()
    with pytest.raises(ValueError, match="invalid commit id"):
        latest_check(world, REPO, sha)
    with pytest.raises(ValueError, match="invalid commit id"):
        finish(world, REPO, sha, 1, "success", RUN, URL)
    with pytest.raises(ValueError, match="invalid commit id"):
        report(world, REPO, [{**entry("start"), "head_sha": sha}], RUN, URL)
    assert world.writes == []


# two runs claiming the same commit -------------------------------------------------------------------------------


class Races(World):
    """Another run's claim lands right after this run writes its own: both had read 'no claim yet'."""

    def request(self, method: str, path: str, **kw: Any) -> Response:  # type: ignore[no-untyped-def]
        response = super().request(method, path, **kw)
        body = kw.get("body")
        if method in ("POST", "PATCH") and isinstance(body, dict) and body.get("external_id") == f"run:{RUN}":
            if method == "PATCH":  # the waiting check is taken over by the other run
                next(c for c in self.checks if c["id"] == int(path.rsplit("/", 1)[1]))["external_id"] = (
                    f"run:{OTHER_RUN}"
                )
            else:
                self.add(external_id=f"run:{OTHER_RUN}")
        return response


def test_a_claim_that_another_run_overtook_does_not_start_the_suites_when_a_new_check_was_written() -> None:
    world = Races()
    found = run_report(world, entry("start"))
    assert (found.state, found.start) == ("already-running", False)
    assert "run 400 claimed the E2E for this commit first" in found.note
    assert found.check_id == world.checks[-1]["id"]


def test_a_claim_that_another_run_overtook_does_not_start_the_suites_when_the_waiting_check_was_taken() -> None:
    world = Races()
    waiting = world.add(external_id="waiting")
    found = run_report(world, entry("start"))
    assert (found.state, found.start) == ("already-running", False) and found.check_id == waiting["id"]


def test_a_claim_nobody_overtook_starts_and_reports_the_check_it_re_read() -> None:
    world = World()
    found = run_report(world, entry("start"))
    assert found.start and found.check_id == world.checks[-1]["id"]
