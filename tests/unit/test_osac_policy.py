"""The OSAC policy must stay equal to what the live ruleset requires (ruleset drift guard, design task T11)."""

import json
from pathlib import Path

import pytest
from helpers import ROOT

pytestmark = pytest.mark.unit


def _ruleset_contexts() -> set[str]:
    data = json.loads((ROOT / "tests" / "fixtures" / "ruleset-ci-status-checks.json").read_text())
    rule = next(r for r in data["rules"] if r["type"] == "required_status_checks")
    return {c["context"] for c in rule["parameters"]["required_status_checks"]}


def _folder_scoped(job) -> bool:  # type: ignore[no-untyped-def]
    """Jobs the policy requires only when their folder changes; they are not in the ruleset."""
    return bool(job.paths) and job.required_at == ("pr",)


def test_policy_checks_plus_check_labels_equal_the_recorded_ruleset(osac_policy) -> None:  # type: ignore[no-untyped-def]
    policy_contexts = {job.check for job in osac_policy.jobs.values() if not _folder_scoped(job)} | {"check-labels"}
    assert policy_contexts == _ruleset_contexts()
    assert len(_ruleset_contexts()) == 26


def test_check_labels_is_not_a_job(osac_policy) -> None:  # type: ignore[no-untyped-def]
    assert "check-labels" not in {job.check for job in osac_policy.jobs.values()}


def test_exactly_three_cost_gated_e2e_jobs(osac_policy) -> None:  # type: ignore[no-untyped-def]
    e2e = {j.check for j in osac_policy.jobs.values() if j.kind == "e2e"}
    assert e2e == {"e2e-vmaas-gate", "e2e-caas-gate", "e2e-bmaas-gate"}
    assert all(j.needs_readiness for j in osac_policy.jobs.values() if j.kind == "e2e")


def test_the_only_jobs_beyond_the_ruleset_are_the_osac_ui_ones(osac_policy) -> None:  # type: ignore[no-untyped-def]
    extra = {j.check for j in osac_policy.jobs.values() if _folder_scoped(j)}
    assert extra == {"Lint", "Lint proxy (Go)", "Typecheck", "Run unit tests (osac-ui)"}
    assert extra.isdisjoint(_ruleset_contexts())
    assert all(j.paths == ("osac-ui/**",) for j in osac_policy.jobs.values() if _folder_scoped(j))


def test_a_policy_can_add_checks_that_are_not_cheap_and_nothing_else_of_the_central_rules(tmp_path: Path) -> None:
    import shutil

    from osac_ci.policy import PolicyError, load_policy

    shutil.copy(ROOT / "policy" / "osac-filters.yml", tmp_path / "osac-filters.yml")
    base = (ROOT / "policy" / "osac.yml").read_text(encoding="utf-8")

    def loaded(extra: str):
        path = tmp_path / "osac.yml"
        path.write_text(base + extra, encoding="utf-8")
        return load_policy(path)

    assert loaded("\nflow:\n  checks:\n    not_cheap_extra: [deploy-preview]\n").flow == {
        "checks": {"not_cheap_extra": ["deploy-preview"]}
    }
    with pytest.raises(PolicyError, match="central rules"):
        loaded("\nflow:\n  approval:\n    trusted_bots: ['mallory[bot]']\n")
    with pytest.raises(PolicyError, match="central rules"):
        loaded("\nflow:\n  checks:\n    not_cheap: []\n")
