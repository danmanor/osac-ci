"""`osac-ci bot-approve` against a fake GitHub: an approval is submitted only for an `approve` answer, for the commit
the answer was made for, with the bot's own credential, and never twice."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeGitHub
from test_bot_decide_cli import BASE, BOT, REPO, SHA, raw_commit, world

from osac_ci import cli
from osac_ci.bot_approve import APPROVAL_MARKER, Result, approve
from osac_ci.github.api import GitHubError, Response

pytestmark = pytest.mark.contract
REVIEWS = f"{BASE}/pulls/5/reviews"
OTHER = "d" * 40


def writer(status: int = 200, data: Any = None) -> FakeGitHub:
    fake = FakeGitHub()
    fake.add("POST", REVIEWS, {"id": 1} if data is None else data, status=status)
    return fake


def posts(fake: FakeGitHub) -> list[tuple[str, str, Any]]:
    return [b for b in fake.bodies if b[0] == "POST"]


def with_reviews(fake: FakeGitHub, *reviews: dict[str, Any]) -> FakeGitHub:
    fake.add("GET", REVIEWS, list(reviews))
    return fake


def review(state: str = "APPROVED", commit_id: str = SHA, body: str = APPROVAL_MARKER) -> dict[str, Any]:
    return {"state": state, "commit_id": commit_id, "body": body}


class Drifts(FakeGitHub):
    """The pull request reads as the world has it the first two times (what `fetch_inputs` reads) and, from the third
    read on (the last look before submitting), with `change` applied: the pull request moved after the answer."""

    def __init__(self, change: dict[str, Any]) -> None:
        super().__init__()
        self.change = change
        self.reads = 0

    def request(self, method: str, path: str, **kw: Any) -> Response:  # type: ignore[no-untyped-def]
        response = super().request(method, path, **kw)
        if (method, path) == ("GET", f"{BASE}/pulls/5"):
            self.reads += 1
            if self.reads > 2:
                return Response(response.status, {**response.data, **self.change}, response.headers)
        return response


def test_an_approve_answer_submits_one_approving_review_for_the_head_it_was_made_on() -> None:
    read, write = with_reviews(world()), writer()
    result = approve(read, write, REPO, 5)
    assert result.result is Result.APPROVED and result.decision.head_sha == SHA
    ((method, path, body),) = posts(write)
    assert (method, path) == ("POST", REVIEWS)
    assert body["event"] == "APPROVE" and body["commit_id"] == SHA and APPROVAL_MARKER in body["body"]
    assert posts(read) == []  # the reading credential never submits


def test_the_credential_that_submits_is_not_used_to_read() -> None:
    write = writer()
    approve(with_reviews(world()), write, REPO, 5)
    assert write.calls == [("POST", REVIEWS)]


@pytest.mark.parametrize(
    "fake",
    [
        world(commits=(raw_commit(author="someone"),)),  # a person's commit
        world(commits=(raw_commit(sha="e" * 40, committer="someone"), raw_commit())),
        world(files=({"filename": "go.mod"}, {"filename": ".github/workflows/ci.yml"})),
        world(pr={"user": {"login": "someone", "type": "User"}}),
        world(pr={"draft": True}),
        world(pr={"state": "closed"}),
    ],
    ids=["person-author", "person-committer", "workflow-file", "not-a-bot", "draft", "closed"],
)
def test_every_other_answer_submits_nothing(fake: FakeGitHub) -> None:
    write = writer()
    result = approve(with_reviews(fake), write, REPO, 5)
    assert result.result is Result.NOT_APPROVED and write.calls == []


@pytest.mark.parametrize(
    "change",
    [{"head": {"sha": OTHER}}, {"state": "closed"}, {"draft": True}],
    ids=["new-head", "closed", "draft"],
)
def test_a_pull_request_that_moves_after_the_answer_submits_nothing(change: dict[str, Any]) -> None:
    read = Drifts(change)
    read.routes.update(world().routes)
    with_reviews(read)
    write = writer()
    with pytest.raises(ValueError, match="changed after the decision"):
        approve(read, write, REPO, 5)
    assert write.calls == []


def test_an_approval_already_made_for_this_commit_is_not_repeated() -> None:
    write = writer()
    result = approve(with_reviews(world(), review()), write, REPO, 5)
    assert result.result is Result.ALREADY_APPROVED and write.calls == []


@pytest.mark.parametrize(
    "existing",
    [
        review(commit_id=OTHER),  # an approval of an older commit does not cover this one
        review(state="COMMENTED"),
        review(state="DISMISSED"),
        review(body="looks fine"),  # an approval by someone else, not made here
        review(body=""),
    ],
    ids=["older-commit", "comment", "dismissed", "not-ours", "empty-body"],
)
def test_a_review_that_is_not_an_approval_made_here_for_this_commit_does_not_stop_a_new_one(
    existing: dict[str, Any],
) -> None:
    write = writer()
    assert approve(with_reviews(world(), existing), write, REPO, 5).result is Result.APPROVED
    assert len(posts(write)) == 1


@pytest.mark.parametrize("status", [401, 403, 404, 422, 500])
def test_a_refused_review_is_an_error_not_an_approval(status: int) -> None:
    with pytest.raises(GitHubError, match=f"HTTP {status}.*refused: nope"):
        approve(with_reviews(world()), writer(status, {"message": "nope"}), REPO, 5)


def test_a_review_that_cannot_be_listed_is_an_error() -> None:
    read = world()
    read.add("GET", REVIEWS, {"message": "boom"}, status=500)
    write = writer()
    with pytest.raises(GitHubError):
        approve(read, write, REPO, 5)
    assert write.calls == []


# the command -----------------------------------------------------------------------------------------------------


def cli_run(monkeypatch: pytest.MonkeyPatch, read: FakeGitHub, write: FakeGitHub, *extra: str) -> int:
    monkeypatch.setattr(cli, "build_client", lambda **_: read)
    monkeypatch.setattr(cli, "build_approve_client", lambda: write)
    return cli.main(["bot-approve", "--repo", REPO, *extra])


def test_the_command_approves_and_says_so(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    write = writer()
    assert cli_run(monkeypatch, with_reviews(world()), write, "--number", "5") == 0
    out = capsys.readouterr().out
    assert "PR #5" in out and "**approved**" in out and len(posts(write)) == 1


def test_the_command_finds_the_pull_request_by_head_and_writes_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write, out = writer(), tmp_path / "out.json"
    assert cli_run(monkeypatch, with_reviews(world()), write, "--head-sha", SHA, "--json-file", str(out)) == 0
    (entry,) = json.loads(out.read_text())
    assert entry["result"] == "approved" and entry["head_sha"] == SHA and entry["number"] == 5


def test_a_head_without_a_pull_request_approves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    write = writer()
    assert cli_run(monkeypatch, with_reviews(world()), write, "--head-sha", "e" * 40) == 0
    assert write.calls == []


def test_a_refusal_by_github_is_exit_3_with_no_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write = writer(403, {"message": "nope"})
    assert cli_run(monkeypatch, with_reviews(world()), write, "--number", "5") == 3
    captured = capsys.readouterr()
    assert "bot-approve failed" in captured.err and "approved" not in captured.out


def test_without_the_approval_credential_the_command_stops_before_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read = with_reviews(world())
    monkeypatch.setattr(cli, "build_client", lambda **_: read)
    monkeypatch.delenv(cli.APPROVE_TOKEN_ENV, raising=False)
    with pytest.raises(SystemExit, match=cli.APPROVE_TOKEN_ENV):
        cli.main(["bot-approve", "--repo", REPO, "--number", "5"])
    assert read.calls == []


def test_bot_decide_still_approves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    read = with_reviews(world())
    monkeypatch.setattr(cli, "build_client", lambda **_: read)
    assert cli.main(["bot-decide", "--repo", REPO, "--number", "5"]) == 0
    assert posts(read) == [] and BOT  # the read-only command has no way to submit
