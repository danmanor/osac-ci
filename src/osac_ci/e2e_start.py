"""E2E start: should the expensive full-install E2E run for this pull request now?

One question, answered from the current state of the pull request, with the reason:

* the PR is open and not a draft;
* the change touches something the E2E suites test (the same path filters the workflows use today);
* a person with write access, CodeRabbit or the OSAC CI app has approved the PR, and nobody has requested changes;
* no cheap check (every required check except the E2E gates) has already failed on the head commit.

Only then does the answer say ``start``. The cheap checks do NOT delay the start: the E2E runs in parallel with them
(the integration tests alone can take an hour), and both must pass for the merge. A check that is still running or has
not reported is only listed; one that has already failed stops the start, because a commit that is known to be broken
needs a new commit anyway. Every other answer says what the PR is waiting for. The function is pure; the
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
from osac_ci.flow import extend, flow
from osac_ci.github.api import GitHubClient, GitHubError, check_repo, get, paginate
from osac_ci.model import CheckRun, JobStatus, Snapshot
from osac_ci.paths import filters_hold
from osac_ci.planner import latest_checks
from osac_ci.policy import Policy

# The rules below are read from policy/flow.yml (see osac_ci.flow), where each one is explained and pinned by a test:
# TRUSTED_BOTS  bots whose approval counts on a branch of the repository itself, never on a fork (the author of a fork
#               controls what CodeRabbit reads, and the approval starts a run of that code with secrets);
# WRITE_PERMISSIONS  the effective repository roles that make a person's approval count;
# NOT_CHEAP  required checks that say nothing about the quality of the change (the E2E cannot wait for its own result;
#            the OSAC CI check, a verdict on the others, is already left out by ``required_contexts``).
CHECK_APP = (
    "osac-ci"  # the GitHub App that writes the E2E check and the suite lines: only its check runs are read as them
)
_FLOW = flow()
TRUSTED_BOTS = _FLOW.trusted_bots
WRITE_PERMISSIONS = _FLOW.write_permissions
NOT_CHEAP = _FLOW.not_cheap


class Action(StrEnum):
    START = "start"
    SKIP_CLOSED = "skip-closed"
    SKIP_DRAFT = "skip-draft"
    SKIP_NOT_APPLICABLE = "skip-not-applicable"
    SKIP_NOT_A_REQUEST = "skip-not-a-request"  # a comment woke the decision and asks for nothing: nothing is decided
    WAIT_APPROVAL = "wait-approval"
    BLOCKED_CHANGES_REQUESTED = "blocked-changes-requested"
    BLOCKED_CHEAP_FAILED = "blocked-cheap-checks-failed"
    ALREADY_PASSED = "already-passed"  # every suite the change needs has already passed on this commit
    NOTHING_TO_START = "nothing-to-start"  # a person asked for suites by hand, and none needs to start


# A request by hand that names no suite ("/test"): the suites the change needs. It cannot be typed as a suite name.
NEEDED = "@needed"


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
    user_type: str = "User"  # the kind of account (User | Bot | Organization), as GitHub names it; "" when unknown


@dataclass(frozen=True)
class Suite:
    """One full-install E2E suite and the path filters that decide whether a change needs it."""

    name: str
    check: str
    filters: tuple[str, ...]
    filters_any: tuple[str, ...]
    manual_only: bool = False  # only ever started by hand (the control comment), never chosen by the decision


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
    # The head commit is in a fork (or in a repository that no longer exists): no bot approval counts.
    fork: bool = False
    # Suites a member of the organization asked for by hand, through the control comment, and who asked. The caller has
    # already checked the member and the comment; the decision only checks that each suite exists and is not running.
    requested: tuple[str, ...] = ()
    requested_by: str = ""
    # Why a request was refused before the decision (the person is not a member): shown in the comment, nothing starts.
    request_refused: str = ""
    # The decision was woken by a comment (an edit of the control comment, or a new one) that asks for nothing and is
    # not refused: a comment is not an approval or a push, so nothing is decided for it. Without this, any comment that
    # only starts like the command (`/testing`) would fall through to the automatic path, which starts every suite
    # again once the E2E check has an answer.
    comment_ignored: bool = False


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
    # (suite, line) for each suite listed in ``suites``: the name of the line the suite gets on the pull request, from
    # the ``check`` of its job in the policy, so a new suite needs no change here
    suite_checks: tuple[tuple[str, str], ...] = ()
    # A run started by hand: it starts only the suites asked for, whatever the approval, draft or cheap-check state, and
    # it never claims the E2E check (the sum of the suites the change needs is written when they have all passed).
    manual: bool = False
    selected_checks: tuple[tuple[str, str], ...] = ()  # (suite, line) of every suite the change needs: the E2E's sum
    notes: tuple[tuple[str, str], ...] = ()  # (suite, what happened to the request), for the control comment

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


def _qualifies(review: Review, author: str, fork: bool) -> bool:
    if review.user == author:
        return False
    if review.user in TRUSTED_BOTS:
        return not fork
    if fork and review.user_type != "User":
        # on a fork only a person counts, not some other app that happens to hold write access
        return False
    return review.permission in WRITE_PERMISSIONS


def approvals(inputs: Inputs) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """(approvers on the head commit, approvals left on an older commit, people whose latest review requests changes).
    Only people who may approve are listed; CodeRabbit never blocks (its changes-requested review is advice)."""
    approvers: list[str] = []
    stale: list[str] = []
    blocking: list[str] = []
    for user, review in sorted(_latest_reviews(inputs.reviews).items()):
        if not _qualifies(review, inputs.author, inputs.fork):
            continue
        if review.state == "CHANGES_REQUESTED" and user not in TRUSTED_BOTS:
            blocking.append(user)
        elif review.state == "APPROVED":
            (approvers if review.commit_id == inputs.head_sha else stale).append(user)
    return tuple(approvers), tuple(stale), tuple(blocking)


def automatic(suites: Iterable[Suite]) -> tuple[Suite, ...]:
    """The suites the decision may choose: not the ones that are only started by hand."""
    return tuple(s for s in suites if not s.manual_only)


def applicable_suites(inputs: Inputs) -> tuple[Suite, ...]:
    """The suites this change needs. When the changed files are not known completely nothing is skipped on a guess."""
    suites = automatic(inputs.suites)
    if not inputs.changed_files or not inputs.changed_files_known:
        return suites
    return _needed(suites, inputs.changed_files, inputs.filters)


def line_state(latest: Mapping[str, CheckRun], check: str) -> str:
    """What a suite's line says on the head commit: none (no line), active (waiting or running), passed or failed."""
    run = latest.get(check)
    if run is None:
        return "none"
    if run.status != "completed":
        return "active"
    return "passed" if run.conclusion == "success" else "failed"


def _needed(suites: Iterable[Suite], files: Sequence[str], filters: Mapping[str, Sequence[str]]) -> tuple[Suite, ...]:
    return tuple(s for s in suites if filters_hold(files, filters, s.filters, s.filters_any))


def suite_checks_for_files(policy: Policy, files: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """The suites a change to these files needs, by the same rule a pull request is judged by, each with the name of
    its line on the pull request. No files means nothing is known, so no suite is skipped."""
    suites = automatic(suites_of(policy))
    needed = suites if not files else _needed(suites, files, policy.path_filters.filters)
    return tuple((s.name, s.check) for s in needed)


def suites_for_files(policy: Policy, files: Sequence[str]) -> tuple[str, ...]:
    """The names of the suites a change to these files needs (see ``suite_checks_for_files``)."""
    return tuple(name for name, _ in suite_checks_for_files(policy, files))


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
    # Only the app's own check runs say what a suite or the E2E did: another app (or a workflow) that posts a check with
    # the same name must not make a suite look passed or running.
    latest = latest_checks(tuple(r for r in inputs.check_runs if r.app == CHECK_APP))
    selected = tuple((s.name, s.check) for s in applicable_suites(inputs))
    if inputs.requested or inputs.request_refused:
        return _decide_by_hand(inputs, base, latest, selected)
    if inputs.comment_ignored:
        return Decision(
            action=Action.SKIP_NOT_A_REQUEST,
            reason="the comment asks for nothing, so nothing is decided for it",
            selected_checks=selected,
            **base,
        )
    if inputs.draft:
        return Decision(action=Action.SKIP_DRAFT, reason="the pull request is a draft", **base)
    needed = applicable_suites(inputs)
    if not needed:
        return Decision(
            action=Action.SKIP_NOT_APPLICABLE,
            reason="no E2E suite is affected by the changed files (docs only)",
            **base,
        )
    # Suites a person already ran by hand on this commit and that passed are not run again; when that is all of them,
    # the E2E is done (the sum is written by the report, not by a run).
    # Only while the E2E check has no answer yet: once it has one, a re-run of the finished run runs everything again.
    answer = latest.get("E2E")
    answered = answer is not None and answer.status == "completed"
    todo = needed if answered else tuple(s for s in needed if line_state(latest, s.check) != "passed")
    if not todo:
        return Decision(
            action=Action.ALREADY_PASSED,
            reason=f"every suite this change needs has passed on this commit: {', '.join(s.name for s in needed)}",
            suites=(),
            selected_checks=selected,
            **base,
        )
    names = tuple(s.name for s in todo)
    checks = tuple((s.name, s.check) for s in todo)
    if blocking:
        return Decision(
            action=Action.BLOCKED_CHANGES_REQUESTED,
            reason=f"changes requested by {', '.join(blocking)}",
            suites=names,
            suite_checks=checks,
            **base,
        )
    if not approvers:
        extra = f" (approved an older commit: {', '.join(stale)})" if stale else ""
        return Decision(
            action=Action.WAIT_APPROVAL,
            reason=(
                f"no approval from a person with write access on the head commit{extra} "
                "(CodeRabbit's approval does not count for a pull request from a fork)"
                if inputs.fork
                else f"no approval from a person with write access or CodeRabbit on the head commit{extra}"
            ),
            suites=names,
            suite_checks=checks,
            **base,
        )
    if failed:
        return Decision(
            action=Action.BLOCKED_CHEAP_FAILED,
            reason=f"cheap checks failed: {', '.join(g.split(':')[0] for g in failed)}",
            suites=names,
            suite_checks=checks,
            **base,
        )
    return Decision(
        action=Action.START,
        reason=f"approved by {', '.join(approvers)}"
        + (f"; CI still running: {', '.join(g.split(':')[0] for g in pending)}" if pending else ""),
        suites=names,
        suite_checks=checks,
        **base,
    )


def _decide_by_hand(
    inputs: Inputs,
    base: dict[str, Any],
    latest: Mapping[str, CheckRun],
    selected: tuple[tuple[str, str], ...],
) -> Decision:
    """A member of the organization asked for suites (a box in the control comment, or a ``/test`` comment). Approval,
    draft and cheap checks do not matter (the person decides to spend the run); what matters is that each suite exists
    and is not already running or passed on this commit. Nothing is claimed: the E2E check stays what the automatic path
    says."""
    known = {s.name: s for s in inputs.suites}
    starts: list[Suite] = []
    notes: list[tuple[str, str]] = []
    if inputs.request_refused:
        notes.append(("", inputs.request_refused))
    names: list[str] = []
    for name in inputs.requested:
        if name != NEEDED:
            names.append(name)
            continue
        needed = applicable_suites(inputs)
        names.extend(s.name for s in needed)
        if not needed:
            notes.append(
                ("", "no E2E suite is affected by the changed files (docs only); name a suite to run it anyway")
            )
    for name in dict.fromkeys(names):
        suite = known.get(name)
        if suite is None:
            notes.append((name, f"is not a suite of this repository (suites: {', '.join(known)})"))
            continue
        state = line_state(latest, suite.check)
        if state == "active":
            notes.append((name, "is already running on this commit; it was not started again"))
        elif state == "passed":
            notes.append((name, "already passed on this commit; it was not run again"))
        else:
            starts.append(suite)
            notes.append((name, f"started by {inputs.requested_by}"))
    if not starts:
        return Decision(
            action=Action.NOTHING_TO_START,
            reason="; ".join(f"{n or 'request'}: {t}" for n, t in notes) or "nothing was asked for",
            manual=True,
            selected_checks=selected,
            notes=tuple(notes),
            **base,
        )
    return Decision(
        action=Action.START,
        reason=f"started by hand by {inputs.requested_by}: {', '.join(s.name for s in starts)}",
        suites=tuple(s.name for s in starts),
        suite_checks=tuple((s.name, s.check) for s in starts),
        manual=True,
        selected_checks=selected,
        notes=tuple(notes),
        **base,
    )


# reading GitHub -------------------------------------------------------------------------------------------------


def suites_of(policy: Policy) -> tuple[Suite, ...]:
    """The E2E suites of the policy: its jobs of kind ``e2e``."""
    return tuple(
        Suite(job.suite or name, job.check, job.filters, job.filters_any, job.manual_only)
        for name, job in policy.jobs.items()
        if job.kind == "e2e"
    )


def refuse_stale_request(
    requested: tuple[str, ...], refused: str, request_sha: str, head_sha: str
) -> tuple[tuple[str, ...], str]:
    """A box was checked in a comment written for ``request_sha``; the pull request is at ``head_sha`` now. When they
    differ the person did not see this commit, so nothing is started and the comment says to check the box again. A
    comment that names no commit (an older one) is not checked."""
    if requested and request_sha and request_sha != head_sha:
        return (), (
            f"the pull request changed since the comment was written ({request_sha[:7]} is now {head_sha[:7]}); "
            "nothing was started: check the box again"
        )
    return requested, refused


def fetch_inputs(
    client: GitHubClient,
    policy: Policy,
    repo: str,
    number: int,
    *,
    branch: str | None = None,
    required: Sequence[Requirement] | None = None,
    requested: tuple[str, ...] = (),
    requested_by: str = "",
    request_refused: str = "",
    request_sha: str = "",
    comment_given: bool = False,
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
    not_cheap = extend(flow(), policy.flow).not_cheap  # the central rules plus what this repository added
    cheap = tuple(r for r in required if r.context not in gates and r.context not in not_cheap)
    reviews = tuple(
        Review(
            user=raw["user"]["login"],
            state=raw["state"],
            association=raw.get("author_association") or "",
            commit_id=raw.get("commit_id") or "",
            submitted_at=raw.get("submitted_at") or "",
            user_type=raw["user"].get("type") or "",
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
            app=str((raw.get("app") or {}).get("slug") or ""),
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
    requested, request_refused = refuse_stale_request(requested, request_refused, request_sha, head_sha)
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
        fork=str(((pr["head"].get("repo")) or {}).get("full_name", "")).lower() != repo.lower(),
        requested=requested,
        requested_by=requested_by,
        request_refused=request_refused,
        comment_ignored=comment_given and not requested and not request_refused,
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
            "suite_checks": dict(decision.suite_checks),
            "approvers": list(decision.approvers),
            "stale_approvals": list(decision.stale_approvals),
            "changes_requested_by": list(decision.changes_requested_by),
            "cheap_failed": list(decision.cheap_failed),
            "cheap_pending": list(decision.cheap_pending),
            "e2e_checks": list(decision.e2e_checks),
            "manual": decision.manual,
            "selected_checks": dict(decision.selected_checks),
            "notes": [{"suite": s, "note": n} for s, n in decision.notes],
        },
        indent=2,
    )
