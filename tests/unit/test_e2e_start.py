"""E2E start: the decision, case by case."""

from __future__ import annotations

from dataclasses import replace

import pytest

from osac_ci.compare import Requirement
from osac_ci.e2e_start import (
    Action,
    Inputs,
    Review,
    Suite,
    applicable_suites,
    approvals,
    decide,
    render_markdown,
    to_json,
)
from osac_ci.model import CheckRun

pytestmark = pytest.mark.unit
HEAD = "a" * 40
OLD = "b" * 40
APP = 15368
FILTERS = {"code": ["**/*.go"], "vm": ["vm/**"], "caas": ["caas/**"]}
SUITES = (
    Suite("vmaas", "e2e-vmaas-gate", (), ("code", "vm")),
    Suite("caas", "e2e-caas-gate", (), ("code", "caas")),
)
CHEAP = (Requirement("lint", APP), Requirement("test", APP))


def run(name: str, conclusion: str | None = "success", app: int = APP) -> CheckRun:
    return CheckRun(name, "completed" if conclusion else "in_progress", conclusion, "2026-10-10T10:00:00Z", app_id=app)


def review(
    user: str, state: str = "APPROVED", assoc: str = "MEMBER", commit: str = HEAD, perm: str | None = None
) -> Review:
    # without an explicit role, a person the repository calls owner, member or collaborator has write access here
    role = perm if perm is not None else ("write" if assoc in ("OWNER", "MEMBER", "COLLABORATOR") else "none")
    return Review(user, state, assoc, commit, permission=role)


def inputs(**changes: object) -> Inputs:
    base = Inputs(
        number=7,
        head_sha=HEAD,
        author="alice",
        state="open",
        draft=False,
        reviews=(review("bob"),),
        check_runs=(run("lint"), run("test")),
        changed_files=("svc/main.go",),
        changed_files_known=True,
        cheap=CHEAP,
        suites=SUITES,
        filters=FILTERS,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_an_approved_pr_with_nothing_failed_starts_every_affected_suite() -> None:
    d = decide(inputs())
    assert d.action is Action.START and d.starts
    assert d.suites == ("vmaas", "caas") and d.approvers == ("bob",)


@pytest.mark.parametrize(
    ("changes", "action"),
    [
        ({"state": "closed"}, Action.SKIP_CLOSED),
        ({"draft": True}, Action.SKIP_DRAFT),
        ({"changed_files": ("docs/a.md",)}, Action.SKIP_NOT_APPLICABLE),
        ({"reviews": ()}, Action.WAIT_APPROVAL),
        ({"check_runs": (run("lint"), run("test", "failure"))}, Action.BLOCKED_CHEAP_FAILED),
        ({"reviews": (review("bob"), review("carol", "CHANGES_REQUESTED"))}, Action.BLOCKED_CHANGES_REQUESTED),
    ],
)
def test_each_reason_to_wait_or_skip(changes: dict[str, object], action: Action) -> None:
    d = decide(inputs(**changes))
    assert d.action is action
    assert not d.starts and d.reason


def test_cheap_checks_that_are_still_running_do_not_delay_the_start() -> None:
    d = decide(inputs(check_runs=(run("lint"), run("test", None))))
    assert d.action is Action.START and d.starts
    assert d.cheap_pending == ("test: in_progress",) and "CI still running: test" in d.reason


def test_cheap_checks_that_have_not_reported_do_not_delay_the_start() -> None:
    d = decide(inputs(check_runs=()))
    assert d.starts and d.cheap_pending and "CI still running" in d.reason


def test_a_start_with_every_check_passed_does_not_mention_ci() -> None:
    assert decide(inputs()).reason == "approved by bob"


def test_a_closed_pr_is_skipped_before_anything_else() -> None:
    d = decide(inputs(state="closed", draft=True, reviews=(), changed_files=("docs/a.md",)))
    assert d.action is Action.SKIP_CLOSED


def test_a_draft_is_skipped_before_the_change_is_looked_at() -> None:
    assert decide(inputs(draft=True, changed_files=("docs/a.md",))).action is Action.SKIP_DRAFT


def test_a_docs_only_change_needs_no_approval_and_no_checks() -> None:
    d = decide(inputs(changed_files=("docs/a.md",), reviews=(), check_runs=()))
    assert d.action is Action.SKIP_NOT_APPLICABLE and d.suites == ()


def test_changes_requested_comes_before_the_missing_approval() -> None:
    d = decide(inputs(reviews=(review("carol", "CHANGES_REQUESTED"),)))
    assert d.action is Action.BLOCKED_CHANGES_REQUESTED and d.changes_requested_by == ("carol",)


def test_a_failure_stops_the_start_even_while_other_checks_run() -> None:
    d = decide(inputs(check_runs=(run("lint", "failure"), run("test", None))))
    assert d.action is Action.BLOCKED_CHEAP_FAILED
    assert d.cheap_failed == ("lint: conclusion: failure",) and d.cheap_pending == ("test: in_progress",)


def test_the_approval_is_asked_for_before_the_checks_are_looked_at() -> None:
    d = decide(inputs(reviews=(), check_runs=(run("lint", "failure"),)))
    assert d.action is Action.WAIT_APPROVAL
    assert d.cheap_failed  # still reported, so the summary shows both


@pytest.mark.parametrize("conclusion", ["success", "skipped", "neutral"])
def test_a_skipped_or_neutral_cheap_check_counts_as_passed(conclusion: str) -> None:
    assert decide(inputs(check_runs=(run("lint"), run("test", conclusion)))).action is Action.START


def test_a_cheap_check_from_the_wrong_app_does_not_count() -> None:
    d = decide(inputs(check_runs=(run("lint"), run("test", app=1))))
    assert "another app" in d.cheap_pending[0]  # reported as not reported yet; it does not delay the start
    assert d.starts


def test_only_the_affected_suite_is_started() -> None:
    d = decide(inputs(changed_files=("caas/x.txt",)))
    assert d.action is Action.START and d.suites == ("caas",)


def test_unknown_changed_files_never_skip_a_suite() -> None:
    assert applicable_suites(inputs(changed_files=(), changed_files_known=True)) == SUITES
    assert applicable_suites(inputs(changed_files=("docs/a.md",), changed_files_known=False)) == SUITES


def test_the_decision_names_the_line_of_each_suite_that_runs_from_the_policy() -> None:
    d = decide(inputs(changed_files=("caas/x.txt",)))
    assert d.suite_checks == (("caas", "e2e-caas-gate"),)
    d = decide(inputs())
    assert d.suite_checks == (("vmaas", "e2e-vmaas-gate"), ("caas", "e2e-caas-gate"))


def test_the_decision_json_carries_the_lines_as_an_object() -> None:
    import json

    data = json.loads(to_json(decide(inputs(changed_files=("caas/x.txt",)))))
    assert data["suite_checks"] == {"caas": "e2e-caas-gate"}


# approvals ------------------------------------------------------------------------------------------------------


def test_the_osac_ci_app_counts_as_an_approver_whatever_its_association() -> None:
    d = decide(inputs(reviews=(review("osac-ci[bot]", assoc="NONE"),)))
    assert d.action is Action.START and d.approvers == ("osac-ci[bot]",)


def test_another_app_does_not_count_as_an_approver() -> None:
    d = decide(inputs(reviews=(review("some-other-app[bot]", assoc="NONE"),)))
    assert d.action is Action.WAIT_APPROVAL


def test_the_osac_ci_app_approving_its_own_pull_request_does_not_count() -> None:
    d = decide(replace(inputs(), author="osac-ci[bot]", reviews=(review("osac-ci[bot]", assoc="NONE"),)))
    assert d.action is Action.WAIT_APPROVAL


def test_coderabbit_counts_as_an_approver_whatever_its_association() -> None:
    d = decide(inputs(reviews=(review("coderabbitai[bot]", assoc="NONE"),)))
    assert d.action is Action.START and d.approvers == ("coderabbitai[bot]",)


@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_people_with_write_access_count(assoc: str) -> None:
    assert decide(inputs(reviews=(review("bob", assoc=assoc),))).starts


@pytest.mark.parametrize("perm", ["admin", "maintain", "write"])
def test_the_roles_with_write_access_count(perm: str) -> None:
    assert decide(inputs(reviews=(review("bob", perm=perm),))).starts


@pytest.mark.parametrize("perm", ["triage", "read", "none", ""])
@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_an_association_without_write_access_does_not_count(assoc: str, perm: str) -> None:
    # an organization member or a collaborator can have read or triage access only
    d = decide(inputs(reviews=(review("bob", assoc=assoc, perm=perm),)))
    assert d.action is Action.WAIT_APPROVAL


def test_a_request_for_changes_from_someone_without_write_access_does_not_block() -> None:
    d = decide(inputs(reviews=(review("bob"), review("carol", "CHANGES_REQUESTED", "MEMBER", perm="read"))))
    assert d.starts


@pytest.mark.parametrize("assoc", ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE", ""])
def test_people_without_write_access_do_not_count(assoc: str) -> None:
    assert decide(inputs(reviews=(review("bob", assoc=assoc),))).action is Action.WAIT_APPROVAL


def test_the_author_cannot_approve_their_own_pr() -> None:
    assert decide(inputs(reviews=(review("alice"),))).action is Action.WAIT_APPROVAL


def test_a_later_review_replaces_an_earlier_one_of_the_same_person() -> None:
    assert decide(inputs(reviews=(review("bob"), review("bob", "CHANGES_REQUESTED")))).action is (
        Action.BLOCKED_CHANGES_REQUESTED
    )
    assert decide(inputs(reviews=(review("bob", "CHANGES_REQUESTED"), review("bob")))).starts


def test_a_comment_does_not_undo_an_approval() -> None:
    assert decide(inputs(reviews=(review("bob"), review("bob", "COMMENTED")))).starts


def test_a_dismissed_review_is_no_decision() -> None:
    assert decide(inputs(reviews=(review("bob", "DISMISSED"),))).action is Action.WAIT_APPROVAL
    assert decide(inputs(reviews=(review("bob", "DISMISSED"), review("carol")))).starts


def test_a_dismissed_changes_requested_no_longer_blocks() -> None:
    assert decide(inputs(reviews=(review("bob"), review("carol", "DISMISSED")))).starts


def test_coderabbit_requesting_changes_is_advice_and_does_not_block() -> None:
    d = decide(inputs(reviews=(review("bob"), review("coderabbitai[bot]", "CHANGES_REQUESTED", "NONE"))))
    assert d.starts and d.changes_requested_by == ()


def test_changes_requested_by_someone_without_write_access_does_not_block() -> None:
    assert decide(inputs(reviews=(review("bob"), review("eve", "CHANGES_REQUESTED", "NONE")))).starts


def test_an_approval_left_on_an_older_commit_is_reported_and_not_counted() -> None:
    d = decide(inputs(reviews=(review("bob", commit=OLD),)))
    assert d.action is Action.WAIT_APPROVAL and d.stale_approvals == ("bob",)
    assert "older commit" in d.reason and "bob" in d.reason


def test_an_approval_on_the_head_beats_one_on_an_older_commit() -> None:
    d = decide(inputs(reviews=(review("bob", commit=OLD), review("carol"))))
    assert d.starts and d.approvers == ("carol",) and d.stale_approvals == ("bob",)


def test_approvals_lists_are_sorted_and_split() -> None:
    reviews = (review("zed"), review("amy"), review("old", commit=OLD), review("carol", "CHANGES_REQUESTED"))
    assert approvals(inputs(reviews=reviews)) == (("amy", "zed"), ("old",), ("carol",))


# what the other flow did ---------------------------------------------------------------------------------------


def test_the_e2e_checks_already_on_the_commit_are_listed_for_comparison() -> None:
    d = decide(inputs(check_runs=(run("lint"), run("test"), run("e2e-vmaas-gate", None), run("e2e-caas-gate"))))
    assert d.e2e_checks == ("e2e-vmaas-gate: in_progress", "e2e-caas-gate: success")


def test_e2e_checks_do_not_count_as_cheap_checks() -> None:
    assert decide(inputs(check_runs=(run("lint"), run("test"), run("e2e-vmaas-gate", "failure")))).starts


# rendering -------------------------------------------------------------------------------------------------------


def test_the_markdown_names_the_action_the_reason_and_the_other_flow() -> None:
    text = render_markdown(decide(inputs(check_runs=(run("lint"), run("test"), run("e2e-vmaas-gate", None)))))
    assert "PR #7" in text and "**start**" in text and "approved by bob" in text
    assert "suites to start: vmaas, caas" in text and "e2e-vmaas-gate: in_progress" in text


def test_the_markdown_lists_what_is_blocking() -> None:
    text = render_markdown(decide(inputs(reviews=(review("bob", commit=OLD),), check_runs=(run("lint", "failure"),))))
    assert "approvals on an older commit (not counted): bob" in text
    assert "cheap checks failed: lint: conclusion: failure" in text
    assert "cheap checks not finished: test: not reported yet" in text
    assert "suites affected:" in text


def test_the_json_round_trips_the_decision() -> None:
    import json

    data = json.loads(to_json(decide(inputs())))
    assert data["action"] == "start" and data["number"] == 7 and data["suites"] == ["vmaas", "caas"]
    assert data["approvers"] == ["bob"] and data["head_sha"] == HEAD


# commit statuses ---------------------------------------------------------------------------------------------------


def test_a_commit_status_satisfies_a_cheap_requirement_that_names_no_app() -> None:
    cheap = (Requirement("lint", APP), Requirement("legacy-status"))
    d = decide(inputs(cheap=cheap, check_runs=(run("lint"),), statuses={"legacy-status": "success"}))
    assert d.starts


@pytest.mark.parametrize("state", ["failure", "error"])
def test_a_commit_status_that_failed_stops_the_start(state: str) -> None:
    cheap = (Requirement("legacy-status"),)
    d = decide(inputs(cheap=cheap, check_runs=(), statuses={"legacy-status": state}))
    assert not d.starts and d.cheap_failed


def test_a_commit_status_that_is_pending_is_listed_and_does_not_delay_the_start() -> None:
    cheap = (Requirement("legacy-status"),)
    d = decide(inputs(cheap=cheap, check_runs=(), statuses={"legacy-status": "pending"}))
    assert d.starts and d.cheap_pending


def test_a_commit_status_does_not_satisfy_a_requirement_that_names_an_app() -> None:
    cheap = (Requirement("legacy-status", APP),)
    d = decide(inputs(cheap=cheap, check_runs=(), statuses={"legacy-status": "success"}))
    assert d.cheap_pending and d.starts
