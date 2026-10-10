"""`osac-ci bot-decide` against a fake GitHub: reading the pull request and its commits, and the command around it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeGitHub

from osac_ci import cli
from osac_ci.bot_approve import Action, decide, fetch_inputs
from osac_ci.github.api import GitHubError

pytestmark = pytest.mark.contract
REPO = "example/app"
BASE = f"/repos/{REPO}"
SHA = "c" * 40
BOT = "dependabot[bot]"


def raw_commit(
    sha: str = SHA,
    author: str | None = BOT,
    committer: str | None = "web-flow",
    parents: int = 1,
    verified: bool = True,
) -> dict[str, Any]:
    return {
        "sha": sha,
        "author": {"login": author} if author else None,
        "committer": {"login": committer} if committer else None,
        "parents": [{"sha": str(i) * 40} for i in range(parents)],
        "commit": {"verification": {"verified": verified}},
    }


def world(
    *,
    commits: tuple[dict[str, Any], ...] = (raw_commit(),),
    files: tuple[dict[str, Any], ...] = ({"filename": "go.mod"}, {"filename": "go.sum"}),
    pr: dict[str, Any] | None = None,
) -> FakeGitHub:
    fake = FakeGitHub()
    payload = {
        "number": 5,
        "state": "open",
        "draft": False,
        "user": {"login": BOT, "type": "Bot"},
        "head": {"sha": SHA},
        "commits": len(commits),
        **(pr or {}),
    }
    fake.add("GET", f"{BASE}/pulls/5", payload)
    fake.add("GET", f"{BASE}/pulls/5/commits", list(commits))
    fake.add("GET", f"{BASE}/pulls/5/files", list(files))
    fake.routes[("GET", f"{BASE}/pulls")] = (200, [{"number": 5, "head": {"sha": SHA}}])
    return fake


def test_a_pure_bot_pull_request_is_approved() -> None:
    inputs = fetch_inputs(world(), REPO, 5)
    assert inputs.author == BOT and inputs.author_type == "Bot" and inputs.head_sha == SHA
    assert inputs.commit_count == 1 and inputs.commits[0].committer == "web-flow" and inputs.commits[0].verified
    assert decide(inputs).approves


def test_the_commit_fields_are_read_as_github_gives_them() -> None:
    fake = world(commits=(raw_commit(author=None, committer=None, parents=2, verified=False),))
    (c,) = fetch_inputs(fake, REPO, 5).commits
    assert (c.author, c.committer, c.parents, c.verified) == ("", "", 2, False)


def test_a_missing_verification_block_is_not_a_signature() -> None:
    odd = raw_commit()
    odd["commit"] = {}
    assert not fetch_inputs(world(commits=(odd,)), REPO, 5).commits[0].verified
    odd["commit"] = {"verification": {"verified": "true"}}  # a string is not the boolean GitHub sends
    assert not fetch_inputs(world(commits=(odd,)), REPO, 5).commits[0].verified
    odd["commit"] = {"verification": {"verified": 1}}
    assert not fetch_inputs(world(commits=(odd,)), REPO, 5).commits[0].verified


def test_a_renamed_file_is_judged_by_both_names() -> None:
    fake = world(files=({"filename": "go.mod", "previous_filename": "src/main.go"},))
    assert decide(fetch_inputs(fake, REPO, 5)).action is Action.REFUSE_FILES


def test_the_state_and_author_of_the_pull_request_are_read() -> None:
    assert decide(fetch_inputs(world(pr={"draft": True}), REPO, 5)).action is Action.SKIP_DRAFT
    assert decide(fetch_inputs(world(pr={"state": "closed"}), REPO, 5)).action is Action.SKIP_CLOSED
    person = {"user": {"login": BOT, "type": "User"}}
    assert decide(fetch_inputs(world(pr=person), REPO, 5)).action is Action.SKIP_NOT_A_BOT


def test_a_pull_request_with_more_commits_than_the_api_lists_is_refused_without_listing_them() -> None:
    fake = world(pr={"commits": 400})
    fake.routes.pop(("GET", f"{BASE}/pulls/5/commits"))
    inputs = fetch_inputs(fake, REPO, 5)
    assert inputs.commits == () and decide(inputs).action is Action.REFUSE_COMMITS
    assert ("GET", f"{BASE}/pulls/5/commits") not in fake.calls


def test_the_files_list_at_the_limit_is_not_trusted() -> None:
    files = tuple({"filename": f"go{i}.sum"} for i in range(3000))
    inputs = fetch_inputs(world(files=files), REPO, 5)
    assert not inputs.files_complete
    assert fetch_inputs(world(files=files[:2999]), REPO, 5).files_complete


def test_a_pr_that_cannot_be_read_is_an_error_not_an_approval() -> None:
    fake = world()
    fake.add("GET", f"{BASE}/pulls/5/commits", {"message": "boom"}, status=500)
    with pytest.raises(GitHubError):
        fetch_inputs(fake, REPO, 5)


# the command -----------------------------------------------------------------------------------------------------


def cli_run(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub, *extra: str) -> int:
    monkeypatch.setattr(cli, "build_client", lambda **_: fake)
    return cli.main(["bot-decide", "--repo", REPO, *extra])


def test_the_command_needs_no_policy_and_prints_the_decision(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_run(monkeypatch, world(), "--number", "5") == 0
    out = capsys.readouterr().out
    assert "PR #5" in out and "**approve**" in out


def test_the_command_finds_the_pr_by_head_commit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli_run(monkeypatch, world(), "--head-sha", SHA) == 0
    assert "PR #5" in capsys.readouterr().out
    assert cli_run(monkeypatch, world(), "--head-sha", "e" * 40) == 0
    assert "No open pull request" in capsys.readouterr().out


def test_the_command_prints_json_and_writes_a_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    target = tmp_path / "d.json"
    assert cli_run(monkeypatch, world(), "--number", "5", "--json", "--json-file", str(target)) == 0
    assert json.loads(capsys.readouterr().out)[0]["action"] == "approve"
    assert json.loads(target.read_text(encoding="utf-8"))[0]["head_sha"] == SHA


def test_an_unwritable_file_and_a_github_failure_exit_three_and_never_print_an_approval(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert cli_run(monkeypatch, world(), "--number", "5", "--json-file", str(tmp_path / "x" / "d.json")) == 3
    assert "cannot write" in capsys.readouterr().err
    fake = world()
    fake.add("GET", f"{BASE}/pulls/5", {"message": "boom"}, status=500)
    assert cli_run(monkeypatch, fake, "--number", "5") == 3
    captured = capsys.readouterr()
    assert "bot-decide failed" in captured.err and "approve" not in captured.out


def test_number_and_head_sha_are_exclusive_and_one_is_needed() -> None:
    for argv in (["--number", "5", "--head-sha", SHA], []):
        with pytest.raises(SystemExit):
            cli.main(["bot-decide", "--repo", REPO, *argv])


class MovingHead(FakeGitHub):
    """The pull request answers with a different head the second time it is read, as when someone pushes meanwhile."""

    def __init__(self, base: FakeGitHub, later_head: str) -> None:
        super().__init__()
        self.routes = base.routes
        self.later_head = later_head
        self.reads = 0

    def request(self, method: str, path: str, **kwargs: Any):  # type: ignore[no-untyped-def]
        response = super().request(method, path, **kwargs)
        if (method, path) == ("GET", f"{BASE}/pulls/5"):
            self.reads += 1
            if self.reads > 1:
                response.data["head"] = {"sha": self.later_head}
        return response


def test_a_push_while_the_pull_request_is_read_is_an_error_not_an_answer() -> None:
    moving = MovingHead(world(), "d" * 40)
    with pytest.raises(ValueError, match="changed while it was read"):
        fetch_inputs(moving, REPO, 5)


def test_a_head_that_stays_the_same_is_read_twice_and_accepted() -> None:
    moving = MovingHead(world(), SHA)
    assert decide(fetch_inputs(moving, REPO, 5)).approves and moving.reads == 2


def test_commits_that_do_not_end_at_the_head_are_an_error() -> None:
    other = raw_commit(sha="e" * 40)
    with pytest.raises(ValueError, match="do not end at the head"):
        fetch_inputs(world(commits=(other,), pr={"head": {"sha": SHA}}), REPO, 5)


def test_a_pull_request_with_no_commits_is_still_refused_not_approved() -> None:
    inputs = fetch_inputs(world(commits=(), pr={"commits": 0}), REPO, 5)
    assert decide(inputs).action is Action.REFUSE_COMMITS


def test_the_command_exits_three_when_the_head_moved(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "build_client", lambda **_: MovingHead(world(), "d" * 40))
    assert cli.main(["bot-decide", "--repo", REPO, "--number", "5"]) == 3
    captured = capsys.readouterr()
    assert "changed while it was read" in captured.err and "approve" not in captured.out


def test_the_last_listed_commit_is_the_head_not_the_first() -> None:
    first, last = raw_commit(sha="1" * 40), raw_commit(sha=SHA)
    ok = fetch_inputs(world(commits=(first, last)), REPO, 5)  # the API lists oldest first
    assert [c.sha for c in ok.commits] == ["1" * 40, SHA]
    with pytest.raises(ValueError, match="do not end at the head"):
        fetch_inputs(world(commits=(last, first)), REPO, 5)
