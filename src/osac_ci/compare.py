"""Compare: on every open PR, does "OSAC CI says ready" match "every required check passes"?

This is the evidence for the trial stage. Today the merge rules require a fixed list of checks (the repository's
branch ruleset). OSAC CI computes its own answer from the policy. Before anyone relies on that answer, the two have to
agree on the PRs people actually open, and every difference has to have a reason. Both answers are computed from the
same snapshot of the PR, so a difference is never a timing artefact.

Four outcomes per PR:

- ``agree-ready`` and ``agree-blocked``: nothing to explain.
- ``looser``: OSAC CI says ready while a required check has not passed. This is the unsafe direction: if OSAC CI
  replaced the list, this PR would get in. Each one is listed with the required checks that stand in the way.
- ``stricter``: every required check passed while OSAC CI holds the PR back. The reason is OSAC CI's own verdict (an
  approval, an authorization, an E2E signal, a lock). Safe, but it would block a PR that merges today.

Separately it compares the list itself: checks the ruleset requires that the policy does not know (OSAC CI would not
wait for them) and the other way round.

Read-only. A PR that cannot be read becomes its own row; it never aborts the run.
"""

from __future__ import annotations

import json
import urllib.parse
from collections import Counter
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from osac_ci.github.api import GitHubClient, GitHubError, check_repo, get, paginate
from osac_ci.github.snapshot import fetch_snapshot
from osac_ci.model import JobStatus, Mode, Snapshot, State
from osac_ci.planner import check_outcome, error_verdict, latest_checks, plan_or_error
from osac_ci.policy import Policy
from osac_ci.publish import CHECK_NAME
from osac_ci.replay import merged_prs
from osac_ci.report import list_open_prs, md_escape
from osac_ci.timeline import state_at

# In pull-request mode a PR in the merge queue is past the point of being ready, so it counts as ready.
READY = frozenset({State.READY_TO_ENQUEUE, State.IN_QUEUE})


class Outcome(StrEnum):
    AGREE_READY = "agree-ready"
    AGREE_BLOCKED = "agree-blocked"
    LOOSER = "looser"
    STRICTER = "stricter"
    UNREADABLE = "unreadable"  # the PR could not be read, so nothing can be claimed about it


@dataclass(frozen=True)
class Requirement:
    """One required status check. With an ``integration_id`` only a run posted by that app satisfies it."""

    context: str
    integration_id: int | None = None


@dataclass(frozen=True)
class Row:
    number: int
    title: str
    url: str
    outcome: Outcome
    state: State
    headline: str
    gaps: tuple[str, ...]  # required checks that have not passed, as "name: why"
    cause: str = ""  # what a difference comes down to, short enough to count; empty when the two agree
    merged_at: str = ""  # back-fill only: when the PR was merged
    via_queue: bool = False  # back-fill only: merged by the merge queue (False: merged directly, bypassing the rules)


@dataclass(frozen=True)
class Coverage:
    """The ruleset's list against the policy's. Names only; the planner never sees the ruleset."""

    ruleset_only: tuple[str, ...]  # required by the ruleset, unknown to the policy: OSAC CI would not wait for it
    policy_only: tuple[str, ...]  # required by the policy at PR time, absent from the ruleset: OSAC CI is stricter


@dataclass(frozen=True)
class Report:
    repo: str
    branch: str
    generated_at: str
    required: tuple[Requirement, ...]
    coverage: Coverage
    rows: tuple[Row, ...]
    days: int = 0  # back-fill: the window of merged PRs that was replayed; 0 for the open PRs

    def count(self, outcome: Outcome) -> int:
        return sum(r.outcome is outcome for r in self.rows)

    @property
    def looser(self) -> tuple[Row, ...]:
        return tuple(r for r in self.rows if r.outcome is Outcome.LOOSER)

    @property
    def stricter(self) -> tuple[Row, ...]:
        return tuple(r for r in self.rows if r.outcome is Outcome.STRICTER)


def required_contexts(client: GitHubClient, repo: str, branch: str) -> tuple[Requirement, ...]:
    """The status checks the branch rules require, in order, without repeats. Public information: it needs no
    administrative permission. OSAC CI's own check is left out so the comparison stays the same once it is required.

    The branch must exist: the rules endpoint answers 200 with an empty list for any name, and an empty list would make
    every PR look as if no check stood in its way."""
    name = urllib.parse.quote(branch, safe="")
    get(client, f"/repos/{check_repo(repo)}/branches/{name}")
    rules = get(client, f"/repos/{check_repo(repo)}/rules/branches/{name}")
    found: list[Requirement] = []
    for rule in rules if isinstance(rules, list) else []:
        if rule.get("type") != "required_status_checks":
            continue
        for entry in (rule.get("parameters") or {}).get("required_status_checks") or []:
            context = entry.get("context")
            if not isinstance(context, str) or not context or context == CHECK_NAME:
                continue
            app = entry.get("integration_id")
            requirement = Requirement(context, app if isinstance(app, int) and not isinstance(app, bool) else None)
            if requirement not in found:
                found.append(requirement)
    return tuple(found)


_GAP_CAUSE = {
    JobStatus.FAILED: "a required check failed",
    JobStatus.RUNNING: "a required check is still running",
    JobStatus.WAITING: "a required check has not reported",
}


_STATUS_OUTCOME = {
    "success": (JobStatus.PASSED, "passed"),
    "pending": (JobStatus.RUNNING, "pending"),
    "failure": (JobStatus.FAILED, "state: failure"),
    "error": (JobStatus.FAILED, "state: error"),
}


def fetch_statuses(client: GitHubClient, repo: str, sha: str) -> dict[str, str]:
    """Legacy commit statuses on a commit: context -> state, the latest per context. A requirement that names no app
    can be met by one of these as well as by a check run."""
    found: dict[str, str] = {}
    for raw in paginate(client, f"/repos/{check_repo(repo)}/commits/{sha}/status", key="statuses"):
        context, state = raw.get("context"), raw.get("state")
        if isinstance(context, str) and isinstance(state, str):
            found[context] = state
    return found


def unmet(
    snapshot: Snapshot, required: Iterable[Requirement], statuses: Mapping[str, str] | None = None
) -> tuple[tuple[str, JobStatus], ...]:
    """Required checks that have not passed on this head commit, with GitHub's meaning of passing: the latest run of
    that name, from the required app when the ruleset names one (a same-named check from another app does not count).
    A requirement that names no app is also met by a commit status of that name in the state success."""
    gaps = []
    for need in required:
        named = [r for r in snapshot.check_runs if r.name == need.context]
        runs = [r for r in named if need.integration_id is None or r.app_id == need.integration_id]
        status, detail = check_outcome(latest_checks(runs).get(need.context))
        if named and not runs:
            detail = f"only reported by another app (the ruleset requires app {need.integration_id})"
        state = (statuses or {}).get(need.context)
        if status is not JobStatus.PASSED and need.integration_id is None and state is not None:
            from_status = _STATUS_OUTCOME.get(state, (JobStatus.WAITING, f"state: {state}"))
            if from_status[0] is JobStatus.PASSED or not named:
                status, detail = from_status
        if status is not JobStatus.PASSED:
            gaps.append((f"{need.context}: {detail}", status))
    return tuple(gaps)


def gap_cause(gaps: tuple[tuple[str, JobStatus], ...]) -> str:
    """The worst thing standing in the way: a failure outranks a run in progress, which outranks silence."""
    for status in (JobStatus.FAILED, JobStatus.RUNNING, JobStatus.WAITING):
        if any(s is status for _, s in gaps):
            return _GAP_CAUSE[status]
    return "a required check has not passed"


def classify(ready: bool, legacy_ready: bool) -> Outcome:
    if ready == legacy_ready:
        return Outcome.AGREE_READY if ready else Outcome.AGREE_BLOCKED
    return Outcome.LOOSER if ready else Outcome.STRICTER


def coverage(policy: Policy, required: Iterable[Requirement]) -> Coverage:
    wanted = {job.check for job in policy.jobs.values() if "pr" in job.required_at} - {CHECK_NAME}
    listed = {need.context for need in required}
    return Coverage(ruleset_only=tuple(sorted(listed - wanted)), policy_only=tuple(sorted(wanted - listed)))


def compare(
    client: GitHubClient,
    policy: Policy,
    repo: str,
    *,
    generated_at: str,
    branch: str = "main",
    org: str | None = None,
    org_client: GitHubClient | None = None,
    include_drafts: bool = False,
    limit: int | None = None,
    lookup_membership: bool = False,
    workers: int = 6,
) -> Report:
    required = required_contexts(client, repo, branch)
    prs = list_open_prs(client, repo, include_drafts=include_drafts)
    if limit is not None:
        prs = prs[:limit]
    org = org or repo.split("/", 1)[0]
    wants_statuses = any(need.integration_id is None for need in required)

    def one(pr: dict[str, Any]) -> Row:
        number = pr["number"]
        title, url = str(pr.get("title", "")), str(pr.get("html_url", ""))
        try:
            snapshot = fetch_snapshot(
                client,
                repo,
                number,
                org=org,
                lookup_membership=lookup_membership,
                approval=policy.approval,
                protected=policy.protected_paths,
                override=policy.override,
                org_client=org_client,
                trust=policy.trust,
            )
            # One more request, and only when a requirement exists that a commit status could also satisfy.
            statuses = fetch_statuses(client, repo, snapshot.head_sha) if wants_statuses else {}
        except (GitHubError, KeyError, ValueError) as exc:
            verdict = error_verdict(f"could not read PR #{number}: {exc}", Mode.PR)
            return Row(number, title, url, Outcome.UNREADABLE, verdict.state, verdict.headline, (), "unreadable")
        verdict = plan_or_error(snapshot, policy, Mode.PR)
        gaps = unmet(snapshot, required, statuses)
        outcome = classify(verdict.state in READY, not gaps)
        cause = {Outcome.LOOSER: gap_cause(gaps), Outcome.STRICTER: verdict.state.value}.get(outcome, "")
        return Row(number, title, url, outcome, verdict.state, verdict.headline, tuple(g for g, _ in gaps), cause)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        rows = sorted(pool.map(one, prs), key=lambda r: r.number)
    return Report(repo, branch, generated_at, required, coverage(policy, required), tuple(rows))


def backfill(
    client: GitHubClient,
    policy: Policy,
    repo: str,
    *,
    now: datetime,
    days: int,
    generated_at: str,
    limit: int = 100,
    branch: str = "main",
    org: str | None = None,
    org_client: GitHubClient | None = None,
    lookup_membership: bool = False,
    workers: int = 6,
) -> Report:
    """The same comparison on PRs merged in the last ``days`` days, each rebuilt as it stood at the decision: when it
    was enqueued (queue-merged) or merged (merged directly). Commit statuses are not rebuilt for a past moment, so a
    requirement that names no app is judged on check runs alone. The required checks are today's, not those of the
    time."""
    required = required_contexts(client, repo, branch)
    prs = merged_prs(client, repo, now=now, days=days, limit=limit)
    org = org or repo.split("/", 1)[0]

    def one(pr: dict[str, Any]) -> Row:
        number = pr["number"]
        title, url, merged_at = str(pr.get("title", "")), str(pr.get("html_url", "")), str(pr["merged_at"])
        try:
            snapshot = fetch_snapshot(
                client,
                repo,
                number,
                org=org,
                lookup_membership=lookup_membership,
                approval=policy.approval,
                protected=policy.protected_paths,
                override=policy.override,
                org_client=org_client,
                trust=policy.trust,
            )
        except (GitHubError, KeyError, ValueError) as exc:
            verdict = error_verdict(f"could not read PR #{number}: {exc}", Mode.PR)
            return Row(
                number, title, url, Outcome.UNREADABLE, verdict.state, verdict.headline, (), "unreadable", merged_at
            )
        via_queue = snapshot.queued_per_events
        moment = snapshot.enqueued_at if via_queue and snapshot.enqueued_at else merged_at
        then = state_at(snapshot, moment)
        verdict = plan_or_error(then, policy, Mode.PR)
        gaps = unmet(then, required)
        outcome = classify(verdict.state in READY, not gaps)
        cause = {Outcome.LOOSER: gap_cause(gaps), Outcome.STRICTER: verdict.state.value}.get(outcome, "")
        return Row(
            number,
            title,
            url,
            outcome,
            verdict.state,
            verdict.headline,
            tuple(g for g, _ in gaps),
            cause,
            merged_at,
            via_queue,
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        rows = sorted(pool.map(one, prs), key=lambda r: r.number)
    return Report(repo, branch, generated_at, required, coverage(policy, required), tuple(rows), days)


# rendering ------------------------------------------------------------------------------------------------


def _cell(text: str, limit: int = 160) -> str:
    return md_escape(text, limit=limit)


def _route(row: Row) -> str:
    return "queue" if row.via_queue else "direct"


def render(report: Report) -> str:
    total = len(report.rows)
    if report.days:
        lines = [
            f"# OSAC CI against the required checks, merged PRs: {report.repo} ({report.branch})",
            "",
            f"Generated {report.generated_at}. {len(report.required)} required checks (today's). {total} PRs merged in "
            f"the last {report.days} days, each rebuilt as it stood when it was enqueued (merged by the queue) or "
            "merged (merged directly). Commit statuses are not rebuilt for a past moment.",
            "",
            "| Outcome | Merged by the queue | Merged directly |",
            "|---|---|---|",
            *[
                f"| {o.value} | {sum(r.outcome is o and r.via_queue for r in report.rows)} "
                f"| {sum(r.outcome is o and not r.via_queue for r in report.rows)} |"
                for o in Outcome
            ],
        ]
    else:
        lines = [
            f"# OSAC CI against the required checks: {report.repo} ({report.branch})",
            "",
            f"Generated {report.generated_at}. {len(report.required)} required checks. {total} open PRs compared.",
            "",
            "| Outcome | PRs |",
            "|---|---|",
            *[f"| {o.value} | {report.count(o)} |" for o in Outcome],
        ]
    if report.looser:
        lines += [
            "",
            "## OSAC CI says ready, a required check has not passed (the unsafe direction)",
            "",
            "| PR | Merged | OSAC CI state | Required checks not passed |"
            if report.days
            else "| PR | OSAC CI state | Required checks not passed |",
            "|---|---|---|---|" if report.days else "|---|---|---|",
            *[
                f"| [#{r.number}]({r.url}) | "
                + (f"{_route(r)} | " if report.days else "")
                + f"{r.state.value} | {_cell('; '.join(r.gaps), 300)} |"
                for r in report.looser
            ],
        ]
    if report.stricter:
        causes = Counter(r.cause for r in report.stricter)
        lines += [
            "",
            "## Every required check passed, OSAC CI holds the PR back",
            "",
            "| Cause | PRs |",
            "|---|---|",
            *[f"| {_cell(cause)} | {count} |" for cause, count in causes.most_common()],
            "",
            "| PR | Merged | OSAC CI state | Reason |" if report.days else "| PR | OSAC CI state | Reason |",
            "|---|---|---|---|" if report.days else "|---|---|---|",
            *[
                f"| [#{r.number}]({r.url}) | "
                + (f"{_route(r)} | " if report.days else "")
                + f"{r.state.value} | {_cell(r.headline)} |"
                for r in report.stricter
            ],
        ]
    unreadable = [r for r in report.rows if r.outcome is Outcome.UNREADABLE]
    if unreadable:
        lines += [
            "",
            "## Could not be read",
            "",
            "| PR | Why |",
            "|---|---|",
            *[f"| [#{r.number}]({r.url}) | {_cell(r.headline)} |" for r in unreadable],
        ]
    cov = report.coverage
    lines += ["", "## The two lists of checks", ""]
    if not (cov.ruleset_only or cov.policy_only):
        lines.append("Both lists name the same checks.")
    if cov.ruleset_only:
        lines += [
            "Required by the ruleset, unknown to the policy (OSAC CI would not wait for them):",
            "",
            *[f"- {_cell(name)}" for name in cov.ruleset_only],
            "",
        ]
    if cov.policy_only:
        lines += [
            "Required by the policy at PR time, not by the ruleset (OSAC CI is stricter):",
            "",
            *[f"- {_cell(name)}" for name in cov.policy_only],
            "",
        ]
    return "\n".join(lines).rstrip("\n") + "\n"


def to_json(report: Report) -> str:
    return json.dumps(
        {
            "repo": report.repo,
            "branch": report.branch,
            "generated_at": report.generated_at,
            "days": report.days,
            "required": list(dict.fromkeys(need.context for need in report.required)),
            "counts": {o.value: report.count(o) for o in Outcome},
            "ruleset_only": list(report.coverage.ruleset_only),
            "policy_only": list(report.coverage.policy_only),
            "rows": [
                {
                    "number": r.number,
                    "outcome": r.outcome.value,
                    "state": r.state.value,
                    "headline": r.headline,
                    "cause": r.cause,
                    "gaps": list(r.gaps),
                    "merged_at": r.merged_at,
                    "via_queue": r.via_queue,
                }
                for r in report.rows
            ],
        },
        indent=2,
    )
