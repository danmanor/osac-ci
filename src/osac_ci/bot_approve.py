"""Bot approval: may a dependency-update pull request be approved without a person reading it?

Dependabot and Konflux open many small pull requests that only move versions. Under native GitHub approval each would
need a person. This module answers, from the pull request and its commits, whether an automatic approval is safe, and
says why not otherwise. It approves nothing itself (a dry run); a workflow acts on the answer.

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

from osac_ci.github.api import GitHubClient, check_repo, get, paginate

BOTS = ("dependabot[bot]", "red-hat-konflux-kflux-prd-rh02[bot]")
GITHUB_COMMITTER = "web-flow"  # GitHub itself: the committer of a commit it signed on someone's behalf
MAX_COMMITS = 250  # the pull request commits API lists at most this many
MAX_FILES = 3000  # the pull request files API lists at most this many

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
