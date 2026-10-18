"""Run E2E suites by hand: the decision for a request, the E2E check around it, and the control comment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from osac_ci import e2e_check, manual_run
from osac_ci.compare import Requirement
from osac_ci.e2e_start import Action, Inputs, Suite, decide, to_json
from osac_ci.github.api import GitHubError, Response
from osac_ci.model import CheckRun

pytestmark = pytest.mark.unit
HEAD = "a" * 40
APP = 15368
REPO = "o/r"
SUITES = (
    Suite("vmaas", "E2E / VMaaS Full Install", (), ("code",)),
    Suite("caas", "E2E / CaaS Full Install", (), ("code",)),
    Suite("netris", "E2E / Netris", (), (), manual_only=True),
)
FILTERS = {"code": ["**/*.go"]}


def line(name: str, conclusion: str | None = "success", app: str = "osac-ci") -> CheckRun:
    return CheckRun(
        name, "completed" if conclusion else "in_progress", conclusion, "2026-10-10T10:00:00Z", app=app, app_id=1
    )


def inputs(**changes: object) -> Inputs:
    base = Inputs(
        number=7,
        head_sha=HEAD,
        author="alice",
        state="open",
        draft=False,
        reviews=(),
        check_runs=(),
        changed_files=("svc/main.go",),
        changed_files_known=True,
        cheap=(Requirement("lint", APP),),
        suites=SUITES,
        filters=FILTERS,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


# the decision for a request ----------------------------------------------------------------------------------------


def test_a_request_starts_the_suite_without_an_approval_and_on_a_draft() -> None:
    d = decide(inputs(requested=("vmaas",), requested_by="bob", draft=True))
    assert d.action is Action.START and d.manual and d.suites == ("vmaas",)
    assert d.suite_checks == (("vmaas", "E2E / VMaaS Full Install"),)
    assert d.notes == (("vmaas", "started by bob"),)


def test_a_request_can_start_a_suite_that_is_only_started_by_hand() -> None:
    d = decide(inputs(requested=("netris",), requested_by="bob"))
    assert d.starts and d.suites == ("netris",)


def test_the_sum_the_e2e_stands_for_is_the_suites_the_change_needs_not_the_hand_only_ones() -> None:
    d = decide(inputs(requested=("netris",), requested_by="bob"))
    assert [name for name, _ in d.selected_checks] == ["vmaas", "caas"]


def test_a_suite_that_is_running_is_not_started_again() -> None:
    running = line("E2E / VMaaS Full Install", None)
    d = decide(inputs(requested=("vmaas", "caas"), requested_by="bob", check_runs=(running,)))
    assert d.suites == ("caas",)
    assert ("vmaas", "is already running on this commit; it was not started again") in d.notes


def test_a_suite_that_passed_is_not_run_again() -> None:
    d = decide(
        inputs(requested=("vmaas",), requested_by="bob", check_runs=(line("E2E / VMaaS Full Install", "success"),))
    )
    assert d.action is Action.NOTHING_TO_START and not d.starts
    assert d.notes == (("vmaas", "already passed on this commit; it was not run again"),)


def test_a_suite_that_failed_can_be_asked_for_again() -> None:
    d = decide(
        inputs(requested=("vmaas",), requested_by="bob", check_runs=(line("E2E / VMaaS Full Install", "failure"),))
    )
    assert d.starts and d.suites == ("vmaas",)


def test_an_unknown_suite_starts_nothing() -> None:
    d = decide(inputs(requested=("nope",), requested_by="bob"))
    assert d.action is Action.NOTHING_TO_START and d.notes == (("nope", "is not a suite of this repository"),)


def test_a_refused_request_starts_nothing_and_says_why() -> None:
    d = decide(inputs(request_refused="@eve is not a member of the o organization, so nothing was started"))
    assert d.action is Action.NOTHING_TO_START and not d.starts and "eve" in d.reason


def test_a_closed_pull_request_starts_nothing_even_when_asked() -> None:
    assert decide(inputs(requested=("vmaas",), requested_by="bob", state="closed")).action is Action.SKIP_CLOSED


def test_the_automatic_decision_never_chooses_a_hand_only_suite() -> None:
    d = decide(inputs(reviews=()))
    assert "netris" not in d.suites and "netris" not in [n for n, _ in d.selected_checks]


# suites that passed by hand are not run again by the approval ------------------------------------------------------

from osac_ci.e2e_start import Review  # noqa: E402


def approved() -> tuple[Review, ...]:
    return (Review("bob", "APPROVED", "MEMBER", HEAD, permission="write"),)


def test_the_approval_runs_only_what_has_not_passed() -> None:
    d = decide(inputs(reviews=approved(), check_runs=(line("E2E / VMaaS Full Install", "success"),)))
    assert d.starts and d.suites == ("caas",)


def test_when_every_needed_suite_passed_by_hand_the_e2e_is_done() -> None:
    both = (line("E2E / VMaaS Full Install"), line("E2E / CaaS Full Install"))
    d = decide(inputs(reviews=approved(), check_runs=both))
    assert d.action is Action.ALREADY_PASSED and not d.starts


def test_a_rerun_of_a_finished_run_still_runs_everything() -> None:
    both = (line("E2E / VMaaS Full Install"), line("E2E / CaaS Full Install"), line("E2E"))
    d = decide(inputs(reviews=approved(), check_runs=both))
    assert d.starts and d.suites == ("vmaas", "caas")


# the E2E check around a run started by hand ------------------------------------------------------------------------


def entry(**changes: Any) -> dict[str, Any]:
    d = decide(inputs(requested=("vmaas",), requested_by="bob"))
    import json

    return {**json.loads(to_json(d)), **changes}


def test_a_run_started_by_hand_does_not_claim_the_e2e_check() -> None:
    out = e2e_check.plan(entry(), None, 500, lambda rid: False)
    assert out.start and out.writes == () and out.state == "running"


def test_already_passed_writes_the_sum_when_the_e2e_still_waits() -> None:
    d = decide(
        inputs(reviews=approved(), check_runs=(line("E2E / VMaaS Full Install"), line("E2E / CaaS Full Install")))
    )
    import json

    out = e2e_check.plan(json.loads(to_json(d)), None, 500, lambda rid: False)
    assert [w.state for w in out.writes] == ["success"] and "vmaas, caas" in out.writes[0].description


class Fake:
    """A GitHub with check runs and the comments of one pull request."""

    def __init__(
        self, runs: list[dict[str, Any]], comments: list[dict[str, Any]] | None = None, pr: dict[str, Any] | None = None
    ):
        self.runs = runs
        self.comments = comments or []
        self.pr = pr or {"state": "open", "user": {"login": "alice"}, "head": {"sha": HEAD}}
        self.members = {"alice", "bob"}
        self.posted: list[tuple[str, str, Any]] = []
        self.next_id = 9
        self.early: dict[str, Any] | None = None  # a control comment another run posts while this one creates its own
        self.delete_status = 204

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None, **_: Any) -> Response:
        self.posted.append((method, path, body))
        if method == "GET" and path.startswith("/orgs/o/members/"):
            return Response(204 if path.rsplit("/", 1)[1] in self.members else 404, {}, {})
        if method == "GET" and path.endswith("/check-runs") or "/check-runs?" in path:
            return Response(200, {"check_runs": self.runs}, {})
        if method == "GET" and "/issues/" in path and "/comments" in path:
            return Response(200, self.comments, {})
        if method == "GET" and path == f"/repos/{REPO}/pulls/7":
            return Response(200, self.pr, {})
        if method == "POST" and path.endswith("/check-runs"):
            self.runs.append({"name": body["name"], "status": body.get("status"), "conclusion": body.get("conclusion")})  # type: ignore[index]
            return Response(201, {}, {})
        if method == "POST" and path.endswith("/comments"):
            if self.early is not None:
                self.comments.append(self.early)
                self.early = None
            created = self.next_id
            self.next_id += 1
            self.comments.append({"id": created, "user": {"login": "osac-ci[bot]"}, "body": body["body"]})  # type: ignore[index]
            return Response(201, {"id": created}, {})
        if method == "PATCH" and "/issues/comments/" in path:
            wanted = int(path.rsplit("/", 1)[1])
            next(c for c in self.comments if c["id"] == wanted)["body"] = body["body"]  # type: ignore[index]
            return Response(200, {}, {})
        if method == "DELETE" and "/issues/comments/" in path:
            wanted = int(path.rsplit("/", 1)[1])
            if self.delete_status == 204:
                self.comments = [c for c in self.comments if c["id"] != wanted]
            return Response(self.delete_status, {}, {})
        raise AssertionError(f"unexpected request {method} {path}")


def passed(name: str, app: str = "osac-ci") -> dict[str, Any]:
    return {
        "name": name, "status": "completed", "conclusion": "success", "started_at": "2026-10-10T10:00:00Z",
        "app": {"slug": app},
    }  # fmt: skip


def test_the_sum_is_written_only_when_every_needed_suite_passed() -> None:
    lines = {"vmaas": "E2E / VMaaS Full Install", "caas": "E2E / CaaS Full Install"}
    partial = Fake([passed("E2E / VMaaS Full Install")])
    note = e2e_check.finish_by_hand(partial, REPO, HEAD, lines, "", "checks")  # type: ignore[arg-type]
    assert "left as it is" in note and not [p for p in partial.posted if p[0] == "POST"]
    full = Fake([passed("E2E / VMaaS Full Install"), passed("E2E / CaaS Full Install")])
    note = e2e_check.finish_by_hand(full, REPO, HEAD, lines, "", "checks")  # type: ignore[arg-type]
    assert "green" in note
    assert any(p[0] == "POST" and p[2]["name"] == "E2E" and p[2]["conclusion"] == "success" for p in full.posted)


def test_a_failed_suite_never_turns_the_sum_green() -> None:
    lines = {"vmaas": "E2E / VMaaS Full Install"}
    fake = Fake([{**passed("E2E / VMaaS Full Install"), "conclusion": "failure"}])
    assert "left as it is" in e2e_check.finish_by_hand(fake, REPO, HEAD, lines, "", "checks")  # type: ignore[arg-type]


def test_runs_by_hand_need_check_runs() -> None:
    with pytest.raises(ValueError, match="check runs"):
        e2e_check.finish_by_hand(Fake([]), REPO, HEAD, {"vmaas": "E2E / V"}, "", "statuses")  # type: ignore[arg-type]


# the control comment -----------------------------------------------------------------------------------------------


def views() -> tuple[manual_run.SuiteView, ...]:
    return (
        manual_run.SuiteView("vmaas", "E2E / VMaaS Full Install", "passed", "https://x/run/1"),
        manual_run.SuiteView("caas", "E2E / CaaS Full Install", "none"),
    )


def test_the_comment_lists_every_suite_with_an_unchecked_box() -> None:
    body = manual_run.render_comment(HEAD, views())
    assert body.startswith(manual_run.MARKER) and "<details>" in body and "> [!TIP]" in body
    assert "- [ ] <!-- suite:vmaas --> **VMaaS Full Install** · ✅ passed ([run](https://x/run/1))" in body
    assert manual_run.requested_suites(body) == ()


def test_checked_boxes_are_read_in_order() -> None:
    body = manual_run.render_comment(HEAD, views()).replace("- [ ] <!-- suite:caas", "- [x] <!-- suite:caas")
    body = body.replace("- [ ] <!-- suite:vmaas", "- [X] <!-- suite:vmaas")
    assert manual_run.requested_suites(body) == ("vmaas", "caas")


def test_a_box_written_by_hand_in_the_text_is_not_a_request() -> None:
    assert manual_run.requested_suites("- [x] caas\n- [x] <!-- suite:Caas -->") == ()


def test_the_last_answer_survives_a_refresh() -> None:
    body = manual_run.render_comment(HEAD, views(), "**Last request by @bob**\n- **vmaas** started by bob")
    assert manual_run.notes_of(body) == "**Last request by @bob**\n- **vmaas** started by bob"
    assert "> [!NOTE]" in body


def test_only_the_app_writes_the_control_comment() -> None:
    ok = {"user": {"login": "osac-ci[bot]"}, "body": manual_run.MARKER}
    assert manual_run.is_control_comment(ok)
    assert not manual_run.is_control_comment({"user": {"login": "eve"}, "body": manual_run.MARKER})
    assert not manual_run.is_control_comment({"user": {"login": "osac-ci[bot]"}, "body": "hello"})


def comment_api(body: str, comment_id: int = 9, issue: int = 7, author: str = "osac-ci[bot]") -> Fake:
    fake = Fake([])

    def request(method: str, path: str, body_: Any = None, **_: Any) -> Response:
        if method == "GET" and path == f"/repos/{REPO}/issues/comments/{comment_id}":
            return Response(
                200,
                {
                    "id": comment_id,
                    "user": {"login": author},
                    "body": body,
                    "issue_url": f"https://api/x/issues/{issue}",
                },
                {},
            )
        return Fake.request(fake, method, path, body_)

    fake.request = request  # type: ignore[method-assign]
    return fake


def checked(suite: str) -> str:
    return manual_run.render_comment(HEAD, views()).replace(f"- [ ] <!-- suite:{suite}", f"- [x] <!-- suite:{suite}")


def test_a_member_checking_a_box_asks_for_that_suite() -> None:
    got = manual_run.read_request(comment_api(checked("caas")), REPO, 7, 9, "bob")  # type: ignore[arg-type]
    assert got == (("caas",), "", HEAD)


def test_anyone_in_the_organization_may_ask_even_for_their_own_pull_request() -> None:
    fake = comment_api(checked("caas"))
    assert manual_run.read_request(fake, REPO, 7, 9, "alice") == (("caas",), "", HEAD)  # type: ignore[arg-type]


def test_someone_outside_the_organization_is_refused_and_nothing_starts() -> None:
    asked, refused, _ = manual_run.read_request(comment_api(checked("caas")), REPO, 7, 9, "eve")  # type: ignore[arg-type]
    assert asked == () and "not a member" in refused


@pytest.mark.parametrize(
    "api",
    [
        comment_api(checked("caas"), issue=8),  # the control comment of another pull request
        comment_api(checked("caas"), author="eve"),  # a comment of someone else with the marker
        comment_api("no marker here"),
    ],
)
def test_a_comment_that_is_not_this_pull_requests_control_comment_asks_for_nothing(api: Fake) -> None:
    assert manual_run.read_request(api, REPO, 7, 9, "bob") == ((), "", "")  # type: ignore[arg-type]


def test_an_edit_that_unchecks_a_box_asks_for_nothing() -> None:
    assert manual_run.read_request(comment_api(manual_run.render_comment(HEAD, views())), REPO, 7, 9, "bob") == (
        (),
        "",
        "",
    )  # type: ignore[arg-type]


# creating and refreshing the comment -------------------------------------------------------------------------------

from osac_ci.policy import parse_policy  # noqa: E402

POLICY_TEXT = """
version: 1
repo: o/r
path_filters:
  mode: enforce
  filters:
    code: ['**']
jobs:
  e2e-vmaas:
    kind: e2e
    check: "E2E / VMaaS Full Install"
    suite: vmaas
    filters_any: [code]
  e2e-netris:
    kind: e2e
    check: "E2E / Netris"
    suite: netris
    manual_only: true
"""
POLICY = parse_policy(POLICY_TEXT)


def test_the_comment_is_created_for_a_member_s_pull_request_and_lists_every_suite() -> None:
    fake = Fake([passed("E2E / VMaaS Full Install")])
    assert manual_run.refresh(fake, POLICY, REPO, 7) == "created"  # type: ignore[arg-type]
    body = fake.comments[0]["body"]
    assert "suite:vmaas" in body and "suite:netris" in body and "VMaaS Full Install** · ✅ passed" in body


def test_a_second_refresh_with_nothing_new_writes_nothing() -> None:
    fake = Fake([])
    manual_run.refresh(fake, POLICY, REPO, 7)  # type: ignore[arg-type]
    before = len([p for p in fake.posted if p[0] in ("POST", "PATCH")])
    assert manual_run.refresh(fake, POLICY, REPO, 7) == "unchanged"  # type: ignore[arg-type]
    assert len([p for p in fake.posted if p[0] in ("POST", "PATCH")]) == before


def test_a_changed_line_updates_the_comment_and_keeps_the_last_answer() -> None:
    fake = Fake([])
    manual_run.refresh(fake, POLICY, REPO, 7, notes="Last request by @bob:\n- **vmaas** started by bob")  # type: ignore[arg-type]
    fake.runs.append(passed("E2E / VMaaS Full Install"))
    assert manual_run.refresh(fake, POLICY, REPO, 7) == "updated"  # type: ignore[arg-type]
    body = fake.comments[0]["body"]
    assert "VMaaS Full Install** · ✅ passed" in body and "started by bob" in body


def test_an_answer_replaces_the_old_one() -> None:
    fake = Fake([])
    manual_run.refresh(fake, POLICY, REPO, 7, notes="old answer")  # type: ignore[arg-type]
    manual_run.refresh(fake, POLICY, REPO, 7, notes="new answer")  # type: ignore[arg-type]
    assert "new answer" in fake.comments[0]["body"] and "old answer" not in fake.comments[0]["body"]


def test_no_comment_is_made_for_a_pull_request_of_someone_outside_the_organization() -> None:
    fake = Fake([], pr={"state": "open", "user": {"login": "eve"}, "head": {"sha": HEAD}})
    assert manual_run.refresh(fake, POLICY, REPO, 7).startswith("skipped")  # type: ignore[arg-type]
    assert fake.comments == []


def test_no_comment_is_made_for_a_closed_pull_request() -> None:
    fake = Fake([], pr={"state": "closed", "user": {"login": "alice"}, "head": {"sha": HEAD}})
    assert manual_run.refresh(fake, POLICY, REPO, 7).startswith("skipped")  # type: ignore[arg-type]


def test_a_draft_pull_request_gets_the_comment_too() -> None:
    fake = Fake([], pr={"state": "open", "draft": True, "user": {"login": "alice"}, "head": {"sha": HEAD}})
    assert manual_run.refresh(fake, POLICY, REPO, 7) == "created"  # type: ignore[arg-type]


# the command line ---------------------------------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from osac_ci import cli  # noqa: E402


def test_the_comment_id_and_the_person_go_together(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    policy = tmp_path / "policy.yml"
    policy.write_text(POLICY_TEXT, encoding="utf-8")
    assert cli.main(["e2e-decide", "--policy", str(policy), "--repo", REPO, "--number", "7", "--manual-by", "bob"]) == 2
    assert "go together" in capsys.readouterr().err
    assert (
        cli.main(["e2e-decide", "--policy", str(policy), "--repo", REPO, "--number", "7", "--manual-comment-id", "9"])
        == 2
    )


def test_a_request_by_hand_needs_a_pull_request_number(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    policy = tmp_path / "policy.yml"
    policy.write_text(POLICY_TEXT, encoding="utf-8")
    argv = ["e2e-decide", "--policy", str(policy), "--repo", REPO, "--head-sha", HEAD]
    assert cli.main([*argv, "--manual-comment-id", "9", "--manual-by", "bob"]) == 2
    assert "--number" in capsys.readouterr().err


# the review findings -------------------------------------------------------------------------------------------------


def test_a_check_of_another_app_with_a_suite_s_name_does_not_make_it_passed_or_running() -> None:
    other = (line("E2E / VMaaS Full Install", "success", app="someone-else"),)
    d = decide(inputs(requested=("vmaas",), requested_by="bob", check_runs=other))
    assert d.starts and d.suites == ("vmaas",)
    d = decide(inputs(reviews=approved(), check_runs=(line("E2E / VMaaS Full Install", "success", app="x"),)))
    assert d.starts and d.suites == ("vmaas", "caas")  # not "already passed"


def test_a_check_of_another_app_does_not_make_the_sum_green() -> None:
    lines = {"vmaas": "E2E / VMaaS Full Install"}
    fake = Fake([passed("E2E / VMaaS Full Install", app="someone-else")])
    assert "left as it is" in e2e_check.finish_by_hand(fake, REPO, HEAD, lines, "", "checks")  # type: ignore[arg-type]


def test_the_comment_does_not_show_another_app_s_check_as_the_suite() -> None:
    runs = [passed("E2E / VMaaS Full Install", app="someone-else")]
    assert manual_run.views_for(SUITES, runs)[0].state == "none"
    assert manual_run.views_for(SUITES, [passed("E2E / VMaaS Full Install")])[0].state == "passed"


def existing(state: str, external_id: str = "") -> e2e_check.Existing:
    return e2e_check.Existing(state, "x", external_id)


@pytest.mark.parametrize(
    ("current", "ok"),
    [
        (None, True),
        (existing("pending", "waiting"), True),  # waiting
        (existing("failure"), True),  # a run that failed does not stop the suites passed by hand
        (existing("error"), True),
        (existing("success"), False),  # a pass stays
        (existing("pending", "run:500"), False),  # a run that is running stays
    ],
)
def test_the_sum_may_replace_a_wait_or_a_failure_but_not_a_pass_or_a_running_run(
    current: e2e_check.Existing | None, ok: bool
) -> None:
    assert e2e_check.replaceable(current) is ok
    entry = {"action": "already-passed", "selected_checks": {"vmaas": "E2E / VMaaS Full Install"}}
    assert bool(e2e_check.plan(entry, current, 1, lambda rid: True).writes) is ok


def test_a_failed_result_is_replaced_when_every_suite_has_passed_by_hand() -> None:
    lines = {"vmaas": "E2E / VMaaS Full Install"}
    fake = Fake(
        [
            passed("E2E / VMaaS Full Install"),
            {
                "name": "E2E",
                "status": "completed",
                "conclusion": "failure",
                "id": 1,
                "app": {"slug": "osac-ci"},
                "output": {"title": "Failed: vmaas"},
            },
        ]
    )
    assert "green" in e2e_check.finish_by_hand(fake, REPO, HEAD, lines, "", "checks")  # type: ignore[arg-type]


def test_a_comment_written_for_an_older_commit_cannot_start_anything() -> None:
    from osac_ci.e2e_start import refuse_stale_request

    old = "b" * 40
    asked, refused = refuse_stale_request(("vmaas",), "", old, HEAD)
    assert asked == () and "changed since the comment" in refused and "bbbbbbb is now aaaaaaa" in refused
    d = decide(inputs(requested=asked, request_refused=refused))
    assert d.action is Action.NOTHING_TO_START and not d.starts


def test_a_comment_for_the_current_commit_or_with_no_commit_is_not_refused() -> None:
    from osac_ci.e2e_start import refuse_stale_request

    assert refuse_stale_request(("vmaas",), "", HEAD, HEAD) == (("vmaas",), "")
    assert refuse_stale_request(("vmaas",), "", "", HEAD) == (("vmaas",), "")
    assert refuse_stale_request((), "", "b" * 40, HEAD) == ((), "")


def test_the_head_commit_is_in_the_comment_and_read_back() -> None:
    body = manual_run.render_comment(HEAD, views())
    assert f"<!-- head:{HEAD} -->" in body and manual_run.head_of(body) == HEAD
    assert manual_run.head_of("no marker") == ""


def test_two_comments_created_at_once_leave_the_oldest_filled_in() -> None:
    placeholder = {"id": 3, "user": {"login": "osac-ci[bot]"}, "body": f"{manual_run.MARKER}\nloading"}
    fake = Fake([passed("E2E / VMaaS Full Install")])
    fake.early = placeholder  # the early job posts its placeholder between our look and our POST
    assert manual_run.refresh(fake, POLICY, REPO, 7).startswith("created (kept the older")  # type: ignore[arg-type]
    assert [c["id"] for c in fake.comments] == [3]  # ours was deleted, the older one has the place
    assert "VMaaS Full Install** · ✅ passed" in fake.comments[0]["body"]  # and it was filled in


def test_a_comment_created_alone_is_kept() -> None:
    fake = Fake([])
    assert manual_run.refresh(fake, POLICY, REPO, 7) == "created"  # type: ignore[arg-type]
    assert len(fake.comments) == 1 and not [p for p in fake.posted if p[0] == "DELETE"]


def test_a_duplicate_that_cannot_be_deleted_fails_before_anything_is_updated() -> None:
    placeholder = {"id": 3, "user": {"login": "osac-ci[bot]"}, "body": f"{manual_run.MARKER}\nloading"}
    fake = Fake([passed("E2E / VMaaS Full Install")])
    fake.early = placeholder
    fake.delete_status = 403
    with pytest.raises(GitHubError, match="duplicate"):
        manual_run.refresh(fake, POLICY, REPO, 7)  # type: ignore[arg-type]
    assert fake.comments[0]["body"].endswith("loading")  # the older comment was not touched
    assert not [p for p in fake.posted if p[0] == "PATCH"]


def test_a_duplicate_that_is_already_gone_is_not_an_error() -> None:
    placeholder = {"id": 3, "user": {"login": "osac-ci[bot]"}, "body": f"{manual_run.MARKER}\nloading"}
    fake = Fake([passed("E2E / VMaaS Full Install")])
    fake.early = placeholder
    fake.delete_status = 404
    assert manual_run.refresh(fake, POLICY, REPO, 7).startswith("created (kept")  # type: ignore[arg-type]
