"""E2E start: should the expensive full-install E2E run for this pull request now?

One question, answered from the current state of the pull request, with the reason:

* the PR is open and not a draft;
* the change touches something the E2E suites test (the same path filters the workflows use today);
* a person with write access, CodeRabbit or the OSAC CI app has approved the PR, and nobody has requested changes;
* the cheap checks (every required check except the E2E gates) have passed on the head commit.

Only then does the answer say ``start``. Every other answer says what the PR is waiting for. The function is pure; the
reading of GitHub is in ``fetch_inputs``. It decides nothing by itself: a workflow acts on the answer, and for now the
answer is only logged (a dry run) next to what the label-based flow does.

An approval counts only when it is on the current head commit. GitHub keeps an approval through a clean rebase and moves
it to the new head (the repository must dismiss stale approvals on push, so a real change drops it); a review left on an
older commit is reported, not counted, so a repository without that rule is never read as approved by a stale review.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from urllib.parse import quote

from osac_ci.compare import Requirement, fetch_statuses, required_contexts, unmet
from osac_ci.github.api import GitHubClient, GitHubError, check_repo, get, paginate
from osac_ci.model import CheckRun, JobStatus, Snapshot
from osac_ci.paths import filters_hold
from osac_ci.planner import latest_checks
from osac_ci.policy import Policy

# Bots whose approval counts whatever their association with the repository: CodeRabbit, and the OSAC CI app, which
# approves dependency updates that need no person (see ``bot_approve``).
TRUSTED_BOTS = frozenset({"coderabbitai[bot]", "osac-ci[bot]"})
# What a person's approval needs, read from their EFFECTIVE permission on the repository, not from their association
# with it: an organization member or a collaborator can have read or triage access only, and an approval starts a run
# of the pull request's code with secrets.
WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})
# A required check that says nothing about the quality of the change: the label check, the Jira reference check that
# replaces it, and the E2E check itself (kept by ``e2e_check``; the E2E cannot wait for its own result). (The OSAC CI
# check, a verdict on the others, is already left out by ``required_contexts``.)
NOT_CHEAP = frozenset({"check-labels", "jira-reference", "E2E"})


class Action(StrEnum):
    START = "start"
    SKIP_CLOSED = "skip-closed"
    SKIP_DRAFT = "skip-draft"
    SKIP_NOT_APPLICABLE = "skip-not-applicable"
    WAIT_APPROVAL = "wait-approval"
    BLOCKED_CHANGES_REQUESTED = "blocked-changes-requested"
    BLOCKED_CHEAP_FAILED = "blocked-cheap-checks-failed"
    WAIT_CHEAP = "wait-cheap-checks"


@dataclass(frozen=True)
class Review:
    user: str
    state: str  # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED | PENDING
    association: str
    commit_id: str
    submitted_at: str = ""
    permission: str = (
        ""  # the reviewer's role on the repository (admin | maintain | write | triage | read | none), or ""
    )


@dataclass(frozen=True)
class Suite:
    """One full-install E2E suite and the path filters that decide whether a change needs it."""

    name: str
    check: str
    filters: tuple[str, ...]
    filters_any: tuple[str, ...]


@dataclass(frozen=True)
class Inputs:
    number: int
    head_sha: str
    author: str
    state: str  # open | closed
    draft: bool
    reviews: tuple[Review, ...]
    check_runs: tuple[CheckRun, ...]
    changed_files: tuple[str, ...]
    changed_files_known: bool
    cheap: tuple[Requirement, ...]  # the required checks the E2E waits for
    suites: tuple[Suite, ...]
    filters: Mapping[str, Sequence[str]]
    # Legacy commit statuses (context -> state) on the head commit. Read only when a cheap requirement names no app, as
    # only such a requirement can be met by a status.
    statuses: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    number: int
    head_sha: str
    action: Action
    reason: str
    suites: tuple[str, ...] = ()  # the suites to start (only for ``start``)
    approvers: tuple[str, ...] = ()
    stale_approvals: tuple[str, ...] = ()  # approvals left on an older commit, not counted
    changes_requested_by: tuple[str, ...] = ()
    cheap_failed: tuple[str, ...] = ()
    cheap_pending: tuple[str, ...] = ()
    e2e_checks: tuple[str, ...] = ()  # E2E checks already on the head commit, "name: state" (what the other flow did)

    @property
    def starts(self) -> bool:
        return self.action is Action.START


def _latest_reviews(reviews: Iterable[Review]) -> dict[str, Review]:
    """The newest review of each person that says something about the change. A comment changes nothing; a dismissed
    review takes the place of the decision it dismissed, and is none itself. GitHub lists reviews oldest first, so the
    later entry wins."""
    latest: dict[str, Review] = {}
    for review in reviews:
        if review.state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[review.user] = review
    return latest


def _qualifies(review: Review, author: str) -> bool:
    if review.user == author:
        return False
    if review.user in TRUSTED_BOTS:
        return True
    return review.permission in WRITE_PERMISSIONS


def approvals(inputs: Inputs) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """(approvers on the head commit, approvals left on an older commit, people whose latest review requests changes).
    Only people who may approve are listed; CodeRabbit never blocks (its changes-requested review is advice)."""
    approvers: list[str] = []
    stale: list[str] = []
    blocking: list[str] = []
    for user, review in sorted(_latest_reviews(inputs.reviews).items()):
        if not _qualifies(review, inputs.author):
            continue
        if review.state == "CHANGES_REQUESTED" and user not in TRUSTED_BOTS:
            blocking.append(user)
        elif review.state == "APPROVED":
            (approvers if review.commit_id == inputs.head_sha else stale).append(user)
    return tuple(approvers), tuple(stale), tuple(blocking)


def applicable_suites(inputs: Inputs) -> tuple[Suite, ...]:
    """The suites this change needs. When the changed files are not known completely nothing is skipped on a guess."""
    if not inputs.changed_files or not inputs.changed_files_known:
        return inputs.suites
    return _needed(inputs.suites, inputs.changed_files, inputs.filters)


def _needed(suites: Iterable[Suite], files: Sequence[str], filters: Mapping[str, Sequence[str]]) -> tuple[Suite, ...]:
    return tuple(s for s in suites if filters_hold(files, filters, s.filters, s.filters_any))


def suites_for_files(policy: Policy, files: Sequence[str]) -> tuple[str, ...]:
    """The names of the suites a change to these files needs, by the same rule a pull request is judged by. No files
    means nothing is known, so no suite is skipped."""
    suites = suites_of(policy)
    if not files:
        return tuple(s.name for s in suites)
    return tuple(s.name for s in _needed(suites, files, policy.path_filters.filters))


def _e2e_checks(inputs: Inputs) -> tuple[str, ...]:
    latest = latest_checks(inputs.check_runs)
    out = []
    for suite in inputs.suites:
        run = latest.get(suite.check)
        if run is not None:
            out.append(f"{suite.check}: {run.conclusion or run.status}")
    return tuple(out)


def decide(inputs: Inputs) -> Decision:
    approvers, stale, blocking = approvals(inputs)
    snapshot = Snapshot(repo="", number=inputs.number, head_sha=inputs.head_sha, check_runs=inputs.check_runs)
    gaps = unmet(snapshot, inputs.cheap, inputs.statuses)
    failed = tuple(g for g, s in gaps if s is JobStatus.FAILED)
    pending = tuple(g for g, s in gaps if s is not JobStatus.FAILED)
    base = {
        "number": inputs.number,
        "head_sha": inputs.head_sha,
        "approvers": approvers,
        "stale_approvals": stale,
        "changes_requested_by": blocking,
        "cheap_failed": failed,
        "cheap_pending": pending,
        "e2e_checks": _e2e_checks(inputs),
    }
    if inputs.state != "open":
        return Decision(action=Action.SKIP_CLOSED, reason="the pull request is not open", **base)
    if inputs.draft:
        return Decision(action=Action.SKIP_DRAFT, reason="the pull request is a draft", **base)
    needed = applicable_suites(inputs)
    if not needed:
        return Decision(
            action=Action.SKIP_NOT_APPLICABLE,
            reason="no E2E suite is affected by the changed files (docs only)",
            **base,
        )
    names = tuple(s.name for s in needed)
    if blocking:
        return Decision(
            action=Action.BLOCKED_CHANGES_REQUESTED,
            reason=f"changes requested by {', '.join(blocking)}",
            suites=names,
            **base,
        )
    if not approvers:
        extra = f" (approved an older commit: {', '.join(stale)})" if stale else ""
        return Decision(
            action=Action.WAIT_APPROVAL,
            reason=f"no approval from a person with write access or CodeRabbit on the head commit{extra}",
            suites=names,
            **base,
        )
    if failed:
        return Decision(
            action=Action.BLOCKED_CHEAP_FAILED,
            reason=f"cheap checks failed: {', '.join(g.split(':')[0] for g in failed)}",
            suites=names,
            **base,
        )
    if pending:
        return Decision(
            action=Action.WAIT_CHEAP,
            reason=f"cheap checks not finished: {', '.join(g.split(':')[0] for g in pending)}",
            suites=names,
            **base,
        )
    return Decision(
        action=Action.START,
        reason=f"approved by {', '.join(approvers)} and every cheap check passed",
        suites=names,
        **base,
    )


# reading GitHub -------------------------------------------------------------------------------------------------


def suites_of(policy: Policy) -> tuple[Suite, ...]:
    """The E2E suites of the policy: its jobs of kind ``e2e``."""
    return tuple(
        Suite(job.suite or name, job.check, job.filters, job.filters_any)
        for name, job in policy.jobs.items()
        if job.kind == "e2e"
    )


def fetch_inputs(
    client: GitHubClient,
    policy: Policy,
    repo: str,
    number: int,
    *,
    branch: str | None = None,
    required: Sequence[Requirement] | None = None,
) -> Inputs:
    """Read what ``decide`` needs. ``required`` is the branch's required checks when the caller already has them (a run
    over many pull requests reads them once)."""
    repo = check_repo(repo)
    base = f"/repos/{repo}"
    pr = get(client, f"{base}/pulls/{number}")
    head_sha = str(pr["head"]["sha"])
    suites = suites_of(policy)
    gates = {s.check for s in suites}
    if required is None:
        required = required_contexts(client, repo, branch or str(pr["base"]["ref"]))
    cheap = tuple(r for r in required if r.context not in gates and r.context not in NOT_CHEAP)
    reviews = tuple(
        Review(
            user=raw["user"]["login"],
            state=raw["state"],
            association=raw.get("author_association") or "",
            commit_id=raw.get("commit_id") or "",
            submitted_at=raw.get("submitted_at") or "",
        )
        for raw in paginate(client, f"{base}/pulls/{number}/reviews")
        if raw.get("user")
    )
    author = str((pr.get("user") or {}).get("login", ""))
    permissions = _permissions(client, repo, reviews, author)
    reviews = tuple(replace(r, permission=permissions.get(r.user, "")) for r in reviews)
    runs = tuple(
        CheckRun(
            raw["name"],
            raw["status"],
            raw.get("conclusion"),
            raw.get("started_at"),
            app_id=_app_id(raw),
        )
        for raw in paginate(client, f"{base}/commits/{head_sha}/check-runs", key="check_runs")
    )
    files: list[str] = []
    listed = 0
    for raw in paginate(client, f"{base}/pulls/{number}/files"):
        listed += 1
        files.append(str(raw["filename"]))
        if raw.get("previous_filename"):
            files.append(str(raw["previous_filename"]))
    # One more request, and only when a requirement exists that a commit status could also satisfy.
    statuses = fetch_statuses(client, repo, head_sha) if any(r.integration_id is None for r in cheap) else {}
    return Inputs(
        number=number,
        head_sha=head_sha,
        author=str((pr.get("user") or {}).get("login", "")),
        state=str(pr.get("state", "open")),
        draft=bool(pr.get("draft")),
        reviews=reviews,
        check_runs=runs,
        changed_files=tuple(files),
        changed_files_known=listed < 3000,  # the files API stops at 3000
        cheap=cheap,
        suites=suites,
        filters=policy.path_filters.filters,
        statuses=statuses,
    )


def _permissions(client: GitHubClient, repo: str, reviews: Iterable[Review], author: str) -> dict[str, str]:
    """The repository role of each person whose latest review could count (an approval or a request for changes), one
    request each. A person who is not a collaborator has none. Trusted bots and the author are not asked about."""
    # only the latest review of each person counts (a dismissed one is no decision), as in ``approvals``
    people = sorted(
        {
            r.user
            for r in _latest_reviews(reviews).values()
            if r.state in ("APPROVED", "CHANGES_REQUESTED") and r.user != author and r.user not in TRUSTED_BOTS
        }
    )
    found: dict[str, str] = {}
    for user in people:
        response = client.request("GET", f"/repos/{repo}/collaborators/{quote(user, safe='')}/permission")
        if response.status == 404:
            found[user] = "none"
        elif 200 <= response.status < 300 and isinstance(response.data, dict):
            # the legacy field says whether the person can push (admin or write; a maintainer, or a custom role based on
            # write, is "write"; triage is "read"); the role name is only a fallback
            found[user] = str(response.data.get("permission") or response.data.get("role_name") or "none")
        else:
            raise GitHubError(response.status, f"reading the permission of {user}")
    return found


def _app_id(raw: dict[str, Any]) -> int:
    value = (raw.get("app") or {}).get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# rendering ------------------------------------------------------------------------------------------------------


def render_markdown(decision: Decision) -> str:
    lines = [
        f"### E2E start (dry run): PR #{decision.number} at `{decision.head_sha[:7]}`: **{decision.action.value}**",
        "",
        decision.reason,
        "",
    ]
    if decision.suites:
        lines.append(f"- suites {'to start' if decision.starts else 'affected'}: {', '.join(decision.suites)}")
    if decision.approvers:
        lines.append(f"- approved by: {', '.join(decision.approvers)}")
    if decision.stale_approvals:
        lines.append(f"- approvals on an older commit (not counted): {', '.join(decision.stale_approvals)}")
    if decision.changes_requested_by:
        lines.append(f"- changes requested by: {', '.join(decision.changes_requested_by)}")
    if decision.cheap_failed:
        lines.append(f"- cheap checks failed: {'; '.join(decision.cheap_failed)}")
    if decision.cheap_pending:
        lines.append(f"- cheap checks not finished: {'; '.join(decision.cheap_pending)}")
    lines.append(f"- E2E checks on this commit now (the current flow): {', '.join(decision.e2e_checks) or 'none'}")
    return "\n".join(lines) + "\n"


def to_json(decision: Decision) -> str:
    return json.dumps(
        {
            "number": decision.number,
            "head_sha": decision.head_sha,
            "action": decision.action.value,
            "reason": decision.reason,
            "suites": list(decision.suites),
            "approvers": list(decision.approvers),
            "stale_approvals": list(decision.stale_approvals),
            "changes_requested_by": list(decision.changes_requested_by),
            "cheap_failed": list(decision.cheap_failed),
            "cheap_pending": list(decision.cheap_pending),
            "e2e_checks": list(decision.e2e_checks),
        },
        indent=2,
    )
