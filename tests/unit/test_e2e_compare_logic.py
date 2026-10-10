"""E2E compare: what the current flow did, and how it is set against the decision."""

from __future__ import annotations

import pytest

from osac_ci.e2e_compare import Outcome, classify, legacy_states
from osac_ci.e2e_start import Action, Decision, Inputs, Suite
from osac_ci.model import CheckRun

pytestmark = pytest.mark.unit
SUITES = (Suite("vmaas", "e2e-vmaas-gate", (), ()), Suite("bmaas/sanity", "e2e-bmaas-gate", (), ()))


def job(name: str, conclusion: str | None) -> CheckRun:
    return CheckRun(name, "completed" if conclusion else "in_progress", conclusion, "2026-10-10T10:00:00Z")


def inputs(*runs: CheckRun) -> Inputs:
    return Inputs(
        number=1, head_sha="a" * 40, author="x", state="open", draft=False, reviews=(), check_runs=tuple(runs),
        changed_files=(), changed_files_known=True, cheap=(), suites=SUITES, filters={},
    )  # fmt: skip


def decision(action: Action) -> Decision:
    return Decision(number=1, head_sha="a" * 40, action=action, reason="because")


def test_a_suite_without_its_real_job_has_not_started() -> None:
    assert legacy_states(inputs()) == {"vmaas": "not started", "bmaas/sanity": "not started"}


def test_the_gate_placeholders_are_not_the_real_job() -> None:
    runs = (
        job("e2e-vmaas-gate", "success"),
        job("e2e-bmaas-pending", "success"),
        job("e2e-vmaas-complete-stale-gates", "success"),
    )
    assert set(legacy_states(inputs(*runs)).values()) == {"not started"}


@pytest.mark.parametrize(
    ("conclusion", "state"),
    [("skipped", "not started"), (None, "running"), ("success", "passed"), ("neutral", "passed"), ("failure", "failed"),
     ("cancelled", "failed"), ("timed_out", "failed")],
)  # fmt: skip
def test_the_state_of_the_real_job(conclusion: str | None, state: str) -> None:
    assert legacy_states(inputs(job("e2e-vmaas-full-install / e2e", conclusion)))["vmaas"] == state


def test_the_suite_name_before_the_slash_names_its_job() -> None:
    states = legacy_states(inputs(job("e2e-bmaas-full-install / e2e", "success")))
    assert states == {"vmaas": "not started", "bmaas/sanity": "passed"}


def test_a_later_run_of_the_job_replaces_an_earlier_one() -> None:
    old = CheckRun("e2e-vmaas-full-install / e2e", "completed", "failure", "2026-10-10T09:00:00Z")
    new = CheckRun("e2e-vmaas-full-install / e2e", "completed", "success", "2026-10-10T11:00:00Z")
    assert legacy_states(inputs(old, new))["vmaas"] == "passed"


@pytest.mark.parametrize(
    ("action", "legacy", "outcome"),
    [
        (Action.START, {"vmaas": "running"}, Outcome.AGREE_START),
        (Action.START, {"vmaas": "not started", "caas": "passed"}, Outcome.AGREE_START),
        (Action.START, {"vmaas": "not started"}, Outcome.NEW_ONLY),
        (Action.WAIT_APPROVAL, {"vmaas": "not started"}, Outcome.AGREE_WAIT),
        (Action.WAIT_APPROVAL, {"vmaas": "failed"}, Outcome.OLD_ONLY),
        (Action.SKIP_NOT_APPLICABLE, {"vmaas": "passed"}, Outcome.OLD_ONLY),
        (Action.SKIP_NOT_APPLICABLE, {"vmaas": "not started"}, Outcome.AGREE_WAIT),
        (Action.BLOCKED_CHEAP_FAILED, {"vmaas": "running"}, Outcome.OLD_ONLY),
        (Action.WAIT_CHEAP, {}, Outcome.AGREE_WAIT),
    ],
)
def test_the_outcome_of_a_decision_against_the_current_flow(
    action: Action, legacy: dict[str, str], outcome: Outcome
) -> None:
    assert classify(decision(action), legacy) is outcome
