"""Bot approval: the decision, case by case."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from osac_ci.bot_approve import (
    BOTS,
    MAX_COMMITS,
    MAX_FILES,
    Action,
    Commit,
    Inputs,
    decide,
    is_dependency_file,
    render_markdown,
    to_json,
)

pytestmark = pytest.mark.unit
HEAD = "a" * 40
BOT = "dependabot[bot]"
KONFLUX = "red-hat-konflux-kflux-prd-rh02[bot]"


def commit(
    sha: str = HEAD, author: str = BOT, committer: str = "web-flow", parents: int = 1, verified: bool = True
) -> Commit:
    return Commit(sha, author, committer, parents, verified)


def inputs(**changes: object) -> Inputs:
    base = Inputs(
        number=5,
        head_sha=HEAD,
        author=BOT,
        author_type="Bot",
        state="open",
        draft=False,
        commit_count=1,
        commits=(commit(),),
        files=("go.mod", "go.sum"),
        files_complete=True,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_a_pure_bot_pull_request_with_only_dependency_files_is_approved() -> None:
    d = decide(inputs())
    assert d.action is Action.APPROVE and d.approves and not d.problems
    assert d.head_sha == HEAD and d.number == 5


@pytest.mark.parametrize("bot", [BOT, KONFLUX])
def test_both_known_bots_are_accepted(bot: str) -> None:
    assert decide(inputs(author=bot, commits=(commit(author=bot),))).approves


def test_the_known_bots_are_exactly_these_two() -> None:
    assert set(BOTS) == {BOT, KONFLUX}


def test_the_committer_may_be_the_bot_itself_if_the_commit_is_signed() -> None:
    assert decide(inputs(commits=(commit(committer=BOT),))).approves


@pytest.mark.parametrize(
    ("changes", "action"),
    [
        ({"state": "closed"}, Action.SKIP_CLOSED),
        ({"draft": True}, Action.SKIP_DRAFT),
        ({"author": "alice", "author_type": "User"}, Action.SKIP_NOT_A_BOT),
        ({"author_type": "User"}, Action.SKIP_NOT_A_BOT),  # a person with the bot's name is not the bot
        ({"author": "evil[bot]", "author_type": "Bot"}, Action.SKIP_NOT_A_BOT),
        ({"commits": (commit(author="alice"),)}, Action.REFUSE_COMMITS),
        ({"files": ("go.mod", "main.go")}, Action.REFUSE_FILES),
    ],
)
def test_each_reason_not_to_approve(changes: dict[str, object], action: Action) -> None:
    d = decide(inputs(**changes))
    assert d.action is action and not d.approves and d.reason


@pytest.mark.parametrize(
    ("bad", "problem"),
    [
        (commit(author="alice"), "authored by alice"),
        (commit(author=""), "an unknown account"),
        (commit(committer="alice"), "committed by alice"),
        (commit(committer=""), "committed by an unknown account"),
        (commit(parents=2), "merge commit"),
        (commit(parents=0), "no parent"),
        (commit(verified=False), "no valid signature"),
    ],
)
def test_each_way_a_commit_is_refused(bad: Commit, problem: str) -> None:
    d = decide(inputs(commits=(bad,)))
    assert d.action is Action.REFUSE_COMMITS and any(problem in p for p in d.problems)


def test_one_foreign_commit_among_the_bots_refuses_the_whole_pull_request() -> None:
    commits = (commit("1" * 40), commit("2" * 40, author="alice", committer="alice", verified=False), commit("3" * 40))
    d = decide(inputs(commit_count=3, commits=commits))
    assert d.action is Action.REFUSE_COMMITS
    assert [p.split()[0] for p in d.problems] == ["2222222"] * 3  # every problem of the bad commit is listed


def test_a_person_pressing_update_branch_is_refused() -> None:
    merge = commit("9" * 40, author="alice", committer="web-flow", parents=2)
    d = decide(inputs(commit_count=2, commits=(commit(), merge)))
    assert d.action is Action.REFUSE_COMMITS and any("merge commit" in p for p in d.problems)


def test_a_commit_list_that_is_incomplete_is_refused() -> None:
    assert decide(inputs(commit_count=2)).action is Action.REFUSE_COMMITS
    assert decide(inputs(commit_count=1, commits=())).action is Action.REFUSE_COMMITS


def test_more_commits_than_can_be_listed_are_refused_even_if_all_listed_look_fine() -> None:
    d = decide(inputs(commit_count=MAX_COMMITS + 1, commits=(commit(),) * MAX_COMMITS))
    assert d.action is Action.REFUSE_COMMITS and "more than" in d.problems[0]
    assert decide(inputs(commit_count=MAX_COMMITS, commits=(commit(),) * MAX_COMMITS)).approves


def test_the_commits_come_before_the_files() -> None:
    d = decide(inputs(commits=(commit(author="alice"),), files=("main.go",)))
    assert d.action is Action.REFUSE_COMMITS


def test_an_incomplete_or_empty_file_list_is_refused() -> None:
    assert decide(inputs(files_complete=False)).action is Action.REFUSE_FILES
    assert decide(inputs(files=())).action is Action.REFUSE_FILES
    assert MAX_FILES == 3000


def test_every_non_dependency_file_is_listed() -> None:
    d = decide(inputs(files=("go.mod", ".github/workflows/ci.yml", "scripts/run.sh")))
    assert d.problems == (
        ".github/workflows/ci.yml is not a dependency file",
        "scripts/run.sh is not a dependency file",
    )


@pytest.mark.parametrize(
    "path",
    [
        "go.mod", "fulfillment-service/go.sum", "osac-ui/pnpm-lock.yaml", "osac-ui/libs/types/package.json", "uv.lock",
        "execution-environment/requirements.txt", "requirements-dev.txt", "metering-service/Containerfile",
        "adapters/Containerfile.echo-adapter", "build/Dockerfile", "pyproject.toml", "Pipfile.lock",
    ],
)  # fmt: skip
def test_dependency_files(path: str) -> None:
    assert is_dependency_file(path)


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml", ".github/actions/x/action.yml", "main.go", "scripts/go.mod.sh", "README.md",
        "go.mod.bak", "requirements.sh", "osac-ui/src/App.tsx", ".tekton/pipeline.yaml", "Makefile", "x/go.modx",
        "Containerfiles/run.sh",
    ],
)  # fmt: skip
def test_files_that_are_not_dependency_files(path: str) -> None:
    assert not is_dependency_file(path)


def test_a_renamed_dependency_file_is_judged_by_both_names() -> None:
    assert decide(inputs(files=("go.mod", "old/main.go"))).action is Action.REFUSE_FILES


def test_the_markdown_and_json_say_what_was_decided_and_why() -> None:
    d = decide(inputs(commits=(commit(author="alice"),)))
    text = render_markdown(d)
    assert "PR #5" in text and "**refuse-commits**" in text and "authored by alice" in text and "dry run" in text
    data = json.loads(to_json(d))
    assert data["action"] == "refuse-commits" and data["problems"] and data["head_sha"] == HEAD
    assert "problems" not in render_markdown(decide(inputs())).lower()
