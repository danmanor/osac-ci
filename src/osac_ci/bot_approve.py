"""Bot approval: may a dependency-update pull request be approved without a person reading it?

Dependabot and Konflux open many small pull requests that only move versions. Under native GitHub approval each would
need a person. This module answers, from the pull request and its commits, whether an automatic approval is safe, and
says why not otherwise. ``decide`` approves nothing (a dry run); ``approve`` acts on the answer.

The answer is ``approve`` only when ALL of these hold on the exact head commit:

* the pull request is open, not a draft, and its author is one of the known bot accounts;
* every commit is on the list (a pull request with more than 250 commits cannot be listed, so it is refused) and is a
  plain commit (one parent: no merge commit), with the bot as its author, the bot itself or GitHub (``web-flow``) as its
  committer, and a valid signature. A person who adds a commit, rebases by hand, or presses "Update branch" breaks one
  of these, and then a person has to approve;
* every changed file is a dependency file (manifests, lock files, container files) and the files list is complete. A
  change to a workflow, a script or a source file is never approved here, whoever wrote it.

The approval has to be made for the head commit it was checked on (the caller passes ``head_sha`` as the review's
commit), so a later push is not covered by it, and the repository's own rule dismisses it. The author identity alone is
metadata: the signature and the committer are what make it hard to forge, and the file list is what limits the harm if
it were.
"""

from __future__ import annotations

import fnmatch
import json
import posixpath
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from osac_ci.github.api import GitHubClient, GitHubError, check_repo, get, paginate

BOTS = ("dependabot[bot]", "red-hat-konflux-kflux-prd-rh02[bot]")
GITHUB_COMMITTER = "web-flow"  # GitHub itself: the committer of a commit it signed on someone's behalf
MAX_COMMITS = 250  # the pull request commits API lists at most this many
MAX_FILES = 3000  # the pull request files API lists at most this many
APPROVAL_MARKER = "<!-- osac-ci bot-approve -->"  # in the body of every approval made here: a repeat is not stacked

DEPENDENCY_FILE_NAMES = frozenset(
    {
        "go.mod",
        "go.sum",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "pyproject.toml",
    }
)
DEPENDENCY_FILE_PATTERNS = ("Containerfile*", "Dockerfile*", "requirements*.txt")


class Action(StrEnum):
    APPROVE = "approve"
    SKIP_CLOSED = "skip-closed"
    SKIP_DRAFT = "skip-draft"
    SKIP_NOT_A_BOT = "skip-not-a-bot"
    REFUSE_COMMITS = "refuse-commits"
    REFUSE_FILES = "refuse-files"


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str  # login of the account the author address belongs to ("" when none)
    committer: str
    parents: int
    verified: bool


@dataclass(frozen=True)
class Inputs:
    number: int
    head_sha: str
    author: str
    author_type: str
    state: str
    draft: bool
    commit_count: int  # what the pull request says it has
    commits: tuple[Commit, ...]  # what the commits API listed
    files: tuple[str, ...]
    files_complete: bool


@dataclass(frozen=True)
class Decision:
    number: int
    head_sha: str
    action: Action
    reason: str
    problems: tuple[str, ...] = ()  # every reason a commit or a file is refused, not only the first

    @property
    def approves(self) -> bool:
        return self.action is Action.APPROVE


def is_dependency_file(path: str) -> bool:
    name = posixpath.basename(path)
    return name in DEPENDENCY_FILE_NAMES or any(fnmatch.fnmatchcase(name, p) for p in DEPENDENCY_FILE_PATTERNS)


def commit_problems(inputs: Inputs) -> tuple[str, ...]:
    problems: list[str] = []
    if inputs.commit_count > MAX_COMMITS:
        return (f"{inputs.commit_count} commits: more than the {MAX_COMMITS} that can be listed",)
    if len(inputs.commits) != inputs.commit_count or not inputs.commits:
        return (f"{len(inputs.commits)} of {inputs.commit_count} commits could be listed",)
    for c in inputs.commits:
        short = c.sha[:7]
        if c.parents != 1:
            problems.append(f"{short} is a merge commit" if c.parents > 1 else f"{short} has no parent")
        if c.author != inputs.author:
            problems.append(f"{short} is authored by {c.author or 'an unknown account'}, not {inputs.author}")
        if c.committer not in (inputs.author, GITHUB_COMMITTER):
            problems.append(f"{short} is committed by {c.committer or 'an unknown account'}")
        if not c.verified:
            problems.append(f"{short} has no valid signature")
    return tuple(problems)


def file_problems(inputs: Inputs) -> tuple[str, ...]:
    if not inputs.files_complete:
        return (f"more than {MAX_FILES} files: the list is incomplete",)
    if not inputs.files:
        return ("no changed files",)
    return tuple(f"{f} is not a dependency file" for f in inputs.files if not is_dependency_file(f))


def decide(inputs: Inputs, bots: Sequence[str] = BOTS) -> Decision:
    base: dict[str, Any] = {"number": inputs.number, "head_sha": inputs.head_sha}
    if inputs.state != "open":
        return Decision(action=Action.SKIP_CLOSED, reason="the pull request is not open", **base)
    if inputs.draft:
        return Decision(action=Action.SKIP_DRAFT, reason="the pull request is a draft", **base)
    if inputs.author not in bots or inputs.author_type != "Bot":
        return Decision(action=Action.SKIP_NOT_A_BOT, reason=f"{inputs.author} is not a known update bot", **base)
    commits = commit_problems(inputs)
    if commits:
        return Decision(
            action=Action.REFUSE_COMMITS,
            reason="not every commit is the bot's own, plain and signed: a person has to approve",
            problems=commits,
            **base,
        )
    files = file_problems(inputs)
    if files:
        return Decision(
            action=Action.REFUSE_FILES,
            reason="the change is not only dependency files: a person has to approve",
            problems=files,
            **base,
        )
    return Decision(
        action=Action.APPROVE,
        reason=f"{inputs.commit_count} commit(s) all from {inputs.author}, signed, and only dependency files changed",
        **base,
    )


# reading GitHub -------------------------------------------------------------------------------------------------


def fetch_inputs(client: GitHubClient, repo: str, number: int) -> Inputs:
    repo = check_repo(repo)
    base = f"/repos/{repo}"
    pr = get(client, f"{base}/pulls/{number}")
    user = pr.get("user") or {}
    count = int(pr.get("commits", 0))
    commits: list[Commit] = []
    if count <= MAX_COMMITS:
        for raw in paginate(client, f"{base}/pulls/{number}/commits"):
            commits.append(
                Commit(
                    sha=str(raw["sha"]),
                    author=str((raw.get("author") or {}).get("login", "")),
                    committer=str((raw.get("committer") or {}).get("login", "")),
                    parents=len(raw.get("parents") or []),
                    verified=((raw.get("commit") or {}).get("verification") or {}).get("verified") is True,
                )
            )
    files: list[str] = []
    listed = 0
    for raw in paginate(client, f"{base}/pulls/{number}/files"):
        listed += 1
        files.append(str(raw["filename"]))
        if raw.get("previous_filename"):
            files.append(str(raw["previous_filename"]))
    # Everything above must describe the same head commit. A push while the pull request was being read would mix the
    # commits or files of one head with the sha of another, so a change of head (or a commit list that does not end at
    # the head) is an error, never an answer.
    head_sha = str(pr["head"]["sha"])
    if commits and commits[-1].sha != head_sha:
        raise ValueError(
            f"the listed commits do not end at the head {head_sha[:7]}: the pull request changed while read"
        )
    if str(get(client, f"{base}/pulls/{number}")["head"]["sha"]) != head_sha:
        raise ValueError(f"the head of pull request {number} changed while it was read")
    return Inputs(
        number=number,
        head_sha=head_sha,
        author=str(user.get("login", "")),
        author_type=str(user.get("type", "")),
        state=str(pr.get("state", "open")),
        draft=bool(pr.get("draft")),
        commit_count=count,
        commits=tuple(commits),
        files=tuple(files),
        files_complete=listed < MAX_FILES,
    )


# approving ------------------------------------------------------------------------------------------------------


class Result(StrEnum):
    APPROVED = "approved"
    ALREADY_APPROVED = "already-approved"
    NOT_APPROVED = "not-approved"


@dataclass(frozen=True)
class Approval:
    decision: Decision
    result: Result


def has_approval(client: GitHubClient, repo: str, number: int, head_sha: str) -> bool:
    """Is there already an approval made here for exactly this commit? A repeated event must not stack reviews."""
    reviews = paginate(client, f"/repos/{check_repo(repo)}/pulls/{number}/reviews")
    return any(
        r.get("state") == "APPROVED" and r.get("commit_id") == head_sha and APPROVAL_MARKER in str(r.get("body") or "")
        for r in reviews
    )


def approve(read: GitHubClient, write: GitHubClient, repo: str, number: int) -> Approval:
    """Decide with ``read``, and only for an ``approve`` answer submit an approving review with ``write`` (the bot's
    own credential) for the very commit the answer was made for. A change of the head, the state or the draft flag
    between the answer and the review is an error and nothing is submitted; so is GitHub refusing the review."""
    repo = check_repo(repo)
    decision = decide(fetch_inputs(read, repo, number))
    if not decision.approves:
        return Approval(decision, Result.NOT_APPROVED)
    base = f"/repos/{repo}/pulls/{number}"
    now = get(read, base)
    if str(now["head"]["sha"]) != decision.head_sha or now.get("state") != "open" or now.get("draft"):
        raise ValueError(f"pull request {number} changed after the decision: nothing was approved")
    if has_approval(read, repo, number, decision.head_sha):
        return Approval(decision, Result.ALREADY_APPROVED)
    body = f"{APPROVAL_MARKER}\nApproved automatically: {decision.reason}."
    response = write.request(
        "POST", f"{base}/reviews", body={"commit_id": decision.head_sha, "event": "APPROVE", "body": body}
    )
    if response.status not in (200, 201):
        message = response.data.get("message", "") if isinstance(response.data, dict) else ""
        raise GitHubError(response.status, f"the approval of pull request {number} was refused: {message}")
    return Approval(decision, Result.APPROVED)


# rendering ------------------------------------------------------------------------------------------------------


def render_markdown(decision: Decision) -> str:
    lines = [
        f"### Bot approval (dry run): PR #{decision.number} at `{decision.head_sha[:7]}`: **{decision.action.value}**",
        "",
        decision.reason,
        "",
    ]
    lines += [f"- {p}" for p in decision.problems]
    return "\n".join(lines).rstrip("\n") + "\n"


def to_json(decision: Decision) -> str:
    return json.dumps(
        {
            "number": decision.number,
            "head_sha": decision.head_sha,
            "action": decision.action.value,
            "reason": decision.reason,
            "problems": list(decision.problems),
        },
        indent=2,
    )


def render_approval(approval: Approval) -> str:
    d = approval.decision
    lines = [f"### Bot approval: PR #{d.number} at `{d.head_sha[:7]}`: **{approval.result.value}**", "", d.reason, ""]
    lines += [f"- {p}" for p in d.problems]
    return "\n".join(lines).rstrip("\n") + "\n"


def approval_json(approval: Approval) -> str:
    return json.dumps({**json.loads(to_json(approval.decision)), "result": approval.result.value}, indent=2)
