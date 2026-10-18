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
    required: tuple[str, ...] = ("pre-commit", "e2e-vmaas-gate", "check-labels", "jira-reference", "E2E", "OSAC CI"),
    pr: dict[str, Any] | None = None,
    roles: dict[str, str] | None = None,
) -> FakeGitHub:
    fake = FakeGitHub()
    payload = {
        "number": 7,
        "state": "open",
        "draft": False,
        "user": {"login": "alice"},
        "head": {"sha": SHA, "repo": {"full_name": REPO}},
        "base": {"ref": "main"},
        **(pr or {}),
    }
    fake.add("GET", f"{BASE}/pulls/7", payload)
    fake.add("GET", f"{BASE}/pulls/7/reviews", list(reviews))
    # the role of each reviewer: by default the one their association suggests (a person the repository calls owner,
    # member or collaborator has write access, anyone else is not a collaborator); a test can say otherwise
    for raw in reviews:
        if raw.get("user") is None:
            continue
        login = raw["user"]["login"]
        role = (roles or {}).get(login)
        if role is None and raw.get("author_association") in ("OWNER", "MEMBER", "COLLABORATOR"):
            role = "write"
        if role is not None:
            fake.add("GET", f"{BASE}/collaborators/{login}/permission", {"permission": role, "role_name": role})
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
    kind = "Bot" if user.endswith("[bot]") else "User"
    return {"user": {"login": user, "type": kind}, "state": state, "author_association": assoc, "commit_id": commit}


def bot_approval(commit: str = SHA) -> dict[str, Any]:
    return approval("coderabbitai[bot]", "NONE", commit)


def test_coderabbit_approves_a_pull_request_of_this_repository() -> None:
    fake = world(reviews=(bot_approval(),), checks=(check("pre-commit"),))
    inputs = fetch_inputs(fake, POLICY, REPO, 7)
    assert not inputs.fork
    assert decide(inputs).action is Action.START


@pytest.mark.parametrize("head_repo", [{"full_name": "mallory/app"}, {"full_name": "Example/App-fork"}, None])
def test_coderabbit_does_not_approve_a_pull_request_from_a_fork_or_a_deleted_fork(head_repo: object) -> None:
    pr = {"head": {"sha": SHA, "repo": head_repo}}
    fake = world(pr=pr, reviews=(bot_approval(),), checks=(check("pre-commit"),))
    inputs = fetch_inputs(fake, POLICY, REPO, 7)
    assert inputs.fork
    d = decide(inputs)
    assert d.action is Action.WAIT_APPROVAL and d.approvers == ()
    assert "does not count for a pull request from a fork" in d.reason


def test_the_repository_name_is_compared_without_regard_to_case() -> None:
    pr = {"head": {"sha": SHA, "repo": {"full_name": "EXAMPLE/App"}}}
    assert not fetch_inputs(world(pr=pr), POLICY, REPO, 7).fork


def test_a_person_with_write_access_approves_a_pull_request_from_a_fork() -> None:
    pr = {"head": {"sha": SHA, "repo": {"full_name": "mallory/app"}}}
    fake = world(pr=pr, reviews=(bot_approval(), approval("bob")), checks=(check("pre-commit"),))
    d = decide(fetch_inputs(fake, POLICY, REPO, 7))
    assert d.action is Action.START and d.approvers == ("bob",)


def test_an_app_with_write_access_does_not_approve_a_pull_request_from_a_fork() -> None:
    pr = {"head": {"sha": SHA, "repo": {"full_name": "mallory/app"}}}
    fake = world(pr=pr, reviews=(approval("other-app[bot]"),), checks=(check("pre-commit"),))
    d = decide(fetch_inputs(fake, POLICY, REPO, 7))
    assert d.action is Action.WAIT_APPROVAL and d.approvers == ()


def test_a_review_without_an_account_type_does_not_approve_a_pull_request_from_a_fork() -> None:
    pr = {"head": {"sha": SHA, "repo": {"full_name": "mallory/app"}}}
    raw = {"user": {"login": "bob"}, "state": "APPROVED", "author_association": "MEMBER", "commit_id": SHA}
    d = decide(fetch_inputs(world(pr=pr, reviews=(raw,), checks=(check("pre-commit"),)), POLICY, REPO, 7))
    assert d.action is Action.WAIT_APPROVAL


def test_the_cheap_checks_are_the_required_ones_minus_the_gates_the_label_checks_the_e2e_check_and_osac_ci() -> None:
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


def test_an_approval_counts_by_the_role_on_the_repository_not_by_the_association() -> None:
    read_only = world(reviews=(approval("bob", "MEMBER"),), roles={"bob": "read"}, checks=(check("pre-commit"),))
    assert decide(fetch_inputs(read_only, POLICY, REPO, 7)).action is Action.WAIT_APPROVAL
    triage = world(reviews=(approval("bob", "COLLABORATOR"),), roles={"bob": "triage"}, checks=(check("pre-commit"),))
    assert decide(fetch_inputs(triage, POLICY, REPO, 7)).action is Action.WAIT_APPROVAL
    maintainer = world(
        reviews=(approval("bob", "CONTRIBUTOR"),), roles={"bob": "maintain"}, checks=(check("pre-commit"),)
    )
    assert decide(fetch_inputs(maintainer, POLICY, REPO, 7)).action is Action.START


def test_a_custom_role_that_can_push_counts_and_triage_does_not() -> None:
    custom = world(reviews=(approval("bob", "CONTRIBUTOR"),), checks=(check("pre-commit"),))
    custom.add("GET", f"{BASE}/collaborators/bob/permission", {"permission": "write", "role_name": "release-managers"})
    assert decide(fetch_inputs(custom, POLICY, REPO, 7)).action is Action.START
    triage = world(reviews=(approval("bob", "MEMBER"),), checks=(check("pre-commit"),))
    triage.add("GET", f"{BASE}/collaborators/bob/permission", {"permission": "read", "role_name": "triage"})
    assert decide(fetch_inputs(triage, POLICY, REPO, 7)).action is Action.WAIT_APPROVAL


def test_a_reviewer_who_is_not_a_collaborator_has_no_role_and_does_not_count() -> None:
    fake = world(reviews=(approval("bob", "MEMBER"),), checks=(check("pre-commit"),))
    del fake.routes[("GET", f"{BASE}/collaborators/bob/permission")]  # GitHub answers 404
    assert decide(fetch_inputs(fake, POLICY, REPO, 7)).action is Action.WAIT_APPROVAL


def test_the_role_is_asked_once_per_person_and_not_for_trusted_bots_or_the_author() -> None:
    fake = world(
        reviews=(
            approval("bob"), approval("bob", state="COMMENTED"), approval("coderabbitai[bot]", "NONE"),
            approval("alice"), approval("dave", state="COMMENTED"),
        ),
        checks=(check("pre-commit"),),
    )  # fmt: skip
    fetch_inputs(fake, POLICY, REPO, 7)
    asked = [p for (_, p) in fake.calls if "/collaborators/" in p]
    assert asked == [f"{BASE}/collaborators/bob/permission"]


def test_the_role_of_someone_whose_latest_review_was_dismissed_is_not_asked() -> None:
    fake = world(
        reviews=(approval("bob"), approval("carol"), approval("carol", state="DISMISSED")),
        checks=(check("pre-commit"),),
    )
    fetch_inputs(fake, POLICY, REPO, 7)
    asked = [p for (_, p) in fake.calls if "/collaborators/" in p]
    assert asked == [f"{BASE}/collaborators/bob/permission"]


def test_a_role_that_cannot_be_read_for_someone_whose_review_no_longer_counts_is_not_an_error() -> None:
    fake = world(reviews=(approval("bob"), approval("carol"), approval("carol", state="DISMISSED")),
                 checks=(check("pre-commit"),))  # fmt: skip
    fake.routes[("GET", f"{BASE}/collaborators/carol/permission")] = (500, {"message": "boom"})
    assert decide(fetch_inputs(fake, POLICY, REPO, 7)).action is Action.START


def test_a_failure_to_read_a_role_is_an_error_and_never_an_approval() -> None:
    fake = world(reviews=(approval("bob"),), checks=(check("pre-commit"),))
    fake.routes[("GET", f"{BASE}/collaborators/bob/permission")] = (500, {"message": "boom"})
    with pytest.raises(GitHubError):
        fetch_inputs(fake, POLICY, REPO, 7)


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


def test_statuses_are_read_only_when_a_cheap_requirement_names_no_app() -> None:
    fake = world(reviews=(approval(),), checks=(check("pre-commit"),))
    fetch_inputs(fake, POLICY, REPO, 7)
    assert not [c for c in fake.calls if c[1].endswith("/status")]  # every required check names the Actions app

    fake = world(reviews=(approval(),), checks=(check("pre-commit"),))
    fake.add(
        "GET",
        f"{BASE}/rules/branches/main",
        [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "ci/legacy"}]}}],
    )
    fake.add("GET", f"{BASE}/commits/{SHA}/status", {"statuses": [{"context": "ci/legacy", "state": "success"}]})
    d = decide(fetch_inputs(fake, POLICY, REPO, 7))
    assert d.action is Action.START
    assert [c for c in fake.calls if c[1].endswith("/status")]


def test_a_failure_to_read_the_statuses_is_an_error_not_a_start() -> None:
    fake = world(reviews=(approval(),))
    fake.add(
        "GET",
        f"{BASE}/rules/branches/main",
        [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "ci/legacy"}]}}],
    )
    fake.add("GET", f"{BASE}/commits/{SHA}/status", {"message": "boom"}, status=500)
    with pytest.raises(GitHubError):
        fetch_inputs(fake, POLICY, REPO, 7)


def test_a_check_the_repository_marks_as_not_cheap_does_not_block_the_e2e() -> None:
    required = ("pre-commit", "deploy-preview", "jira-reference", "E2E")
    fake = world(checks=(check("pre-commit"), check("deploy-preview", "failure")), required=required)
    assert [r.context for r in fetch_inputs(fake, POLICY, REPO, 7).cheap] == ["pre-commit", "deploy-preview"]
    own = POLICY.model_copy(update={"flow": {"checks": {"not_cheap_extra": ["deploy-preview"]}}})
    assert [r.context for r in fetch_inputs(fake, own, REPO, 7).cheap] == ["pre-commit"]


# a comment that asks for nothing -----------------------------------------------------------------------------------


def finished_world() -> FakeGitHub:
    """Approved, and the E2E check already has its answer: the automatic path then starts every suite again."""
    return world(reviews=(approval(),), checks=(check("pre-commit"), check("E2E", "success")))


def test_a_pull_request_that_is_approved_and_answered_runs_everything_again_for_an_ordinary_event() -> None:
    d = decide(fetch_inputs(finished_world(), POLICY, REPO, 7))
    assert d.action is Action.START  # the reason a stray comment must not reach this path


def test_a_comment_that_asks_for_nothing_decides_nothing() -> None:
    inputs = fetch_inputs(finished_world(), POLICY, REPO, 7, comment_given=True)
    assert inputs.comment_ignored
    d = decide(inputs)
    assert d.action is Action.SKIP_NOT_A_REQUEST and not d.starts and d.suites == ()


def test_a_comment_that_asks_for_something_or_is_refused_is_not_ignored() -> None:
    asked = fetch_inputs(
        finished_world(), POLICY, REPO, 7, comment_given=True, requested=("vmaas",), requested_by="bob"
    )
    refused = fetch_inputs(finished_world(), POLICY, REPO, 7, comment_given=True, request_refused="not a member")
    assert not asked.comment_ignored and not refused.comment_ignored
    assert decide(refused).action is Action.NOTHING_TO_START


def test_an_event_without_a_comment_is_decided_as_before() -> None:
    assert not fetch_inputs(finished_world(), POLICY, REPO, 7).comment_ignored


def test_the_command_ignores_a_comment_that_only_starts_like_the_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = finished_world()
    fake.add(
        "GET",
        f"{BASE}/issues/comments/9",
        {
            "id": 9,
            "user": {"login": "eve", "type": "User"},
            "body": "/testing foo",
            "issue_url": "https://api/x/issues/7",
        },
    )
    monkeypatch.setattr(cli, "build_org_client", lambda **_: fake)
    assert cli_run(monkeypatch, fake, "--number", "7", "--manual-comment-id", "9", "--manual-by", "eve") == 0
    out = capsys.readouterr().out
    assert "**skip-not-a-request**" in out and "**start**" not in out
