"""The early-comment action and the two workflows that call it: their shape, and the shell run with a stand-in `gh`."""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from helpers import ROOT

pytestmark = pytest.mark.unit
ACTION = ROOT / ".github" / "actions" / "early-comment" / "action.yml"
COMMENT = ROOT / ".github" / "workflows" / "e2e-pr-comment.yml"
DECIDE = ROOT / ".github" / "workflows" / "e2e-pr-decide.yml"
SHA = "a" * 40
MARKER = "<!-- osac-ci:manual-run -->"

# A stand-in for `gh` that logs every call and answers from the environment: GH_MEMBERS (logins that are members),
# GH_PULLS (the JSON the open-pulls listing returns), GH_BEFORE (comment ids the app has before the post), GH_AFTER
# (after it) and GH_MINE (the id of the comment the post creates).
GH = r"""#!/bin/bash
args="$*"
echo "${args//$'\n'/ }" >> "$GH_LOG"
case "$args" in
  *"-X DELETE"*) exit 0 ;;
  *"/members/"*) login="${args##*/members/}"; login="${login%% *}"; [[ " $GH_MEMBERS " == *" $login "* ]]; exit $? ;;
  *"/pulls?"*) printf '%s' "$GH_PULLS"; exit 0 ;;
  *"-f body="*) printf '%s\n' "$args" > "$GH_BODY"; touch "$GH_POSTED"; echo "$GH_MINE"; exit 0 ;;
  *"/comments"*) if [ -e "$GH_POSTED" ]; then printf '%s' "$GH_AFTER"; else printf '%s' "$GH_BEFORE"; fi; exit 0 ;;
esac
exit 1
"""


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def script() -> str:
    return next(s["run"] for s in load(ACTION)["runs"]["steps"] if s["name"] == "Post the placeholder")


def run(tmp_path: Path, **given: str):  # type: ignore[no-untyped-def]
    """Run the step; returns (exit code, the calls made to gh, the posted body, the output)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_LOG": str(tmp_path / "calls"), "GH_BODY": str(tmp_path / "body"),
        "GH_POSTED": str(tmp_path / "posted"), "GH_MEMBERS": "alice", "GH_PULLS": "", "GH_BEFORE": "", "GH_AFTER": "",
        "GH_MINE": "9", "GH_TOKEN": "t", "REPO": "o/r", "HEAD_SHA": SHA, "NUMBER": "7", "AUTHOR": "alice",
        "FRESH": "true",
    }  # fmt: skip
    env.update(given)
    done = subprocess.run(["bash", "-c", script()], env=env, capture_output=True, text=True, check=False)
    read = lambda name: (tmp_path / name).read_text() if (tmp_path / name).exists() else ""  # noqa: E731
    return done.returncode, read("calls").splitlines(), read("body"), done.stdout + done.stderr


def pulls(*items: tuple[int, str, str]) -> str:
    return json.dumps([{"number": n, "user": {"login": who}, "head": {"sha": sha}} for n, who, sha in items])


def test_every_input_used_is_declared_and_every_input_declared_is_used() -> None:
    used = set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", ACTION.read_text(encoding="utf-8")))
    assert used == set(load(ACTION)["inputs"])


def test_nothing_is_expanded_into_the_script_and_nothing_is_checked_out() -> None:
    for step in load(ACTION)["runs"]["steps"]:
        assert "${{" not in step.get("run", "")
        assert "checkout" not in step.get("uses", "")


def test_a_new_pull_request_of_a_member_is_commented_with_one_member_check_and_one_post(tmp_path: Path) -> None:
    code, calls, body, _ = run(tmp_path, GH_AFTER="9\n")
    assert code == 0
    assert calls[0] == "api orgs/o/members/alice --silent"
    assert calls[1].startswith("api repos/o/r/issues/7/comments -f body=")
    assert not any("pulls?" in c for c in calls), "the number is known: no listing of the open pull requests"
    assert sum("--paginate" in c for c in calls) == 1, (
        "a fresh pull request has no comment: the only list is after the post"
    )
    assert MARKER in body and f"<!-- head:{SHA} -->" in body and f"`{SHA[:7]}`" in body
    assert not any("DELETE" in c for c in calls)


def test_a_pull_request_that_is_not_new_looks_for_the_comment_before_it_posts(tmp_path: Path) -> None:
    code, calls, _, log = run(tmp_path, FRESH="false", GH_BEFORE="5\n")
    assert code == 0 and "already there" in log
    assert not any("body=" in c for c in calls)


def test_a_reopened_pull_request_without_the_comment_gets_it_and_checks_twice(tmp_path: Path) -> None:
    code, calls, _, _ = run(tmp_path, FRESH="false", GH_AFTER="9\n")
    assert code == 0
    assert any("body=" in c for c in calls)
    assert sum("--paginate" in c for c in calls) == 2


def test_an_author_who_is_not_a_member_gets_no_comment(tmp_path: Path) -> None:
    code, calls, _, log = run(tmp_path, AUTHOR="mallory")
    assert code == 0 and "not a member" in log
    assert calls == ["api orgs/o/members/mallory --silent"]


def test_two_comments_made_at_once_keep_the_oldest_and_delete_this_one(tmp_path: Path) -> None:
    code, calls, _, _ = run(tmp_path, GH_AFTER="5\n9\n")
    assert code == 0
    assert calls[-1] == "api -X DELETE repos/o/r/issues/comments/9"


def test_when_this_comment_is_the_oldest_nothing_is_deleted(tmp_path: Path) -> None:
    _, calls, _, _ = run(tmp_path, GH_AFTER="9\n12\n", GH_MINE="9")
    assert not any("DELETE" in c for c in calls)


def test_without_a_number_the_pull_request_is_found_by_its_head_commit(tmp_path: Path) -> None:
    listing = pulls((3, "bob", "b" * 40), (4, "alice", SHA))
    code, calls, _, _ = run(tmp_path, NUMBER="", AUTHOR="", GH_PULLS=listing, GH_AFTER="9\n", FRESH="false")
    assert code == 0
    assert any(c.startswith("api repos/o/r/issues/4/comments -f body=") for c in calls)
    assert calls[1] == "api orgs/o/members/alice --silent"


@pytest.mark.parametrize("listing", [pulls((3, "bob", "b" * 40)), pulls((3, "alice", SHA), (4, "alice", SHA)), "[]"])
def test_without_exactly_one_pull_request_with_this_head_nothing_is_posted(tmp_path: Path, listing: str) -> None:
    code, calls, _, log = run(tmp_path, NUMBER="", AUTHOR="", GH_PULLS=listing)
    assert code == 0 and "Not exactly one" in log
    assert not any("body=" in c for c in calls)


@pytest.mark.parametrize(
    "given",
    [
        {"HEAD_SHA": "xyz"},
        {"HEAD_SHA": ""},
        {"NUMBER": "7; id"},
        {"AUTHOR": "a/b"},
        {"AUTHOR": "$(id)"},
        {"AUTHOR": ""},
    ],
)
def test_a_value_that_is_not_what_it_should_be_posts_nothing_and_calls_nothing(tmp_path: Path, given: dict) -> None:
    code, calls, _, _ = run(tmp_path, **given)
    assert code == 0
    assert calls == []


def test_a_bot_login_is_accepted_in_the_member_check(tmp_path: Path) -> None:
    _, calls, _, _ = run(tmp_path, AUTHOR="dependabot[bot]", GH_MEMBERS="dependabot[bot]", GH_AFTER="9\n")
    assert calls[0] == "api orgs/o/members/dependabot[bot] --silent"


def test_the_workflow_for_a_new_pull_request_posts_fast_with_the_event_data() -> None:
    wf = load(COMMENT)
    assert wf[True]["workflow_call"]["secrets"]["OSAC_CI_APP_PRIVATE_KEY"]["required"] is True
    assert wf["permissions"] == {}
    job = wf["jobs"]["comment"]
    assert job["permissions"] == {} and job["if"] == "github.event_name == 'pull_request_target'"
    token, post = job["steps"]
    assert token["with"]["permission-pull-requests"] == "write" and token["with"]["permission-members"] == "read"
    assert [k for k in token["with"] if k.startswith("permission-")] == [
        "permission-pull-requests",
        "permission-members",
    ]
    assert post["uses"] == "$/.github/actions/early-comment"
    given = post["with"]
    assert given["number"] == "${{ github.event.pull_request.number }}"
    assert given["author"] == "${{ github.event.pull_request.user.login }}"
    assert given["head-sha"] == "${{ github.event.pull_request.head.sha }}"
    assert given["fresh"] == "${{ github.event.action == 'opened' }}"
    assert "uses: actions/checkout" not in COMMENT.read_text(encoding="utf-8")


def test_the_decision_keeps_its_own_early_comment_as_the_second_chance_with_the_same_action() -> None:
    job = load(DECIDE)["jobs"]["comment-early"]
    assert job["permissions"] == {}
    assert job["steps"][-1]["uses"] == "$/.github/actions/early-comment"
    given = job["steps"][-1]["with"]
    assert given["head-sha"] == "${{ github.event.workflow_run.head_sha }}"
    assert "number" not in given and "fresh" not in given, "found by the head commit, and the check is not skipped"


def test_the_decision_deletes_the_finished_run_of_the_early_comment_like_the_relay_run() -> None:
    wf = load(DECIDE)
    assert wf[True]["workflow_call"]["inputs"]["comment-workflow-name"]["default"] == "E2E comment"
    condition = " ".join(wf["jobs"]["clean-relay"]["if"].split())
    assert condition == (
        "github.event.action == 'completed' && "
        "((github.event.workflow_run.name == inputs.relay-workflow-name && "
        "github.event.workflow_run.conclusion == 'success') || "
        "github.event.workflow_run.name == inputs.comment-workflow-name)"
    ), "a relay run is deleted only when it succeeded; an early comment run whatever its result"
