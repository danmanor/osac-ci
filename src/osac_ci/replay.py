"""Replay: does the planner agree with what actually happened to recently merged PRs?

This is the evidence for the parity gate (design decision D7). For every PR merged in the window the planner reads
the PR's final state and must say it was ready to merge. Any other verdict is a DISAGREEMENT. A disagreement is
either explained (listed with a reason in an explained-ledger that the infra group signs off) or unexplained;
only unexplained ones fail the gate.

Two modes. ``final`` (the default) reads the PR's final state, labels and checks as they are now: it proves the
planner never calls a legitimately merged PR blocked on today's data, but not that it would have let the PR in when
the decision was made. ``enqueue`` rebuilds each PR as it stood when it was enqueued (or merged, for a direct merge)
from the timestamps on labels, reviews and check runs (see ``timeline.py``). There, a queue-merged PR agrees when
today's enqueue rule held at that moment (the labels or approval, no draft), because that is the rule the system
actually applied: it never reads check results. The full verdict is reported next to it, so the report also shows what
the planner would have held back that today's flow let in.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from osac_ci import yamlio
from osac_ci.github.api import GitHubClient, check_repo, paginate
from osac_ci.github.snapshot import fetch_snapshot
from osac_ci.model import Mode, State
from osac_ci.planner import plan_or_error
from osac_ci.policy import Policy
from osac_ci.timeline import state_at

EXPECTED = State.READY_TO_ENQUEUE
FILTER_NOTE = "path filter:"  # prefix of the shadow-mode notes the planner adds (planner._filter_note)


@dataclass(frozen=True)
class Row:
    number: int
    merged_at: str
    state: State
    headline: str
    agrees: bool
    via_queue: bool
    explanation: str | None = None
    decision_at: str = ""  # enqueue mode: the moment judged (when it was enqueued, or merged for a direct merge)
    filter_notes: tuple[str, ...] = ()  # shadow mode: where the path filters and what the checks did disagree

    @property
    def unexplained(self) -> bool:
        """Only PRs the queue merged are held to the planner. A direct merge is a bypass, reported separately."""
        return self.via_queue and not self.agrees and self.explanation is None


@dataclass(frozen=True)
class Report:
    repo: str
    days: int
    rows: tuple[Row, ...]
    at: str = "final"  # "final" or "enqueue": which state of each PR the planner judged

    @property
    def disagreements(self) -> tuple[Row, ...]:
        return tuple(r for r in self.rows if not r.agrees)

    @property
    def unexplained(self) -> tuple[Row, ...]:
        return tuple(r for r in self.rows if r.unexplained)

    @property
    def queue_rows(self) -> tuple[Row, ...]:
        return tuple(r for r in self.rows if r.via_queue)

    @property
    def bypass_rows(self) -> tuple[Row, ...]:
        """Merged outside the queue. The planner's verdict says which requirement was bypassed."""
        return tuple(r for r in self.rows if not r.via_queue)

    @property
    def filter_rows(self) -> tuple[Row, ...]:
        """PRs where a job's path filters and its check disagreed (shadow mode); they are not planner disagreements."""
        return tuple(r for r in self.rows if r.filter_notes)

    @property
    def queue_agreement(self) -> float:
        rows = self.queue_rows
        return 1.0 if not rows else sum(r.agrees for r in rows) / len(rows)

    @property
    def agreement(self) -> float:
        return 1.0 if not self.rows else sum(r.agrees for r in self.rows) / len(self.rows)


def load_explained(path: Path | None) -> dict[int, str]:
    """`{pr_number: reason}`. Every entry is a signed-off decision; keep the file small and reviewed."""
    if path is None:
        return {}
    try:
        raw: Any = yamlio.load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # includes a repeated PR number: the later reason must not hide the first
        raise ValueError(f"{path}: {exc}") from exc
    if not isinstance(raw, dict) or not all(isinstance(v, str) and v.strip() for v in raw.values()):
        raise ValueError(f"{path}: expected a mapping of PR number to a non-empty reason")
    return {int(k): v for k, v in raw.items()}


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def merged_prs(
    client: GitHubClient, repo: str, *, now: datetime, days: int, limit: int, base: str | None = None
) -> list[dict[str, Any]]:
    """The ``limit`` most recently merged PRs from the last ``days`` days, newest merge first; with ``base`` only those
    merged into that branch. Listing is by updated time, descending, so the scan can stop at the first PR last touched
    before the cutoff (updated_at is never earlier than merged_at). Everything in the window is collected before the
    cut: a merged PR that was commented on later is listed early, and must not displace a newer merge."""
    cutoff = now - timedelta(days=days)
    found: list[dict[str, Any]] = []
    for pr in paginate(
        client,
        f"/repos/{check_repo(repo)}/pulls",
        params={"state": "closed", "sort": "updated", "direction": "desc"},
    ):
        if _parse(pr["updated_at"]) < cutoff:
            break
        if base is not None and (pr.get("base") or {}).get("ref") != base:
            continue
        if pr.get("merged_at") and _parse(pr["merged_at"]) >= cutoff:
            found.append(pr)
    found.sort(key=lambda pr: _parse(pr["merged_at"]), reverse=True)
    return found[:limit]


def replay(
    client: GitHubClient,
    policy: Policy,
    repo: str,
    *,
    now: datetime,
    days: int = 60,
    limit: int = 100,
    org: str | None = None,
    explained: Mapping[int, str] | None = None,
    lookup_membership: bool = True,
    at: str = "final",
) -> Report:
    if at not in ("final", "enqueue"):
        raise ValueError(f"at must be 'final' or 'enqueue', got {at!r}")
    explained = explained or {}
    org = org or repo.split("/", 1)[0]
    rows = []
    for pr in merged_prs(client, repo, now=now, days=days, limit=limit):
        snapshot = fetch_snapshot(
            client,
            repo,
            pr["number"],
            org=org,
            lookup_membership=lookup_membership,
            approval=policy.approval,
            protected=policy.protected_paths,
            override=policy.override,
            trust=policy.trust,
        )
        moment = ""
        if at == "enqueue":
            # The decision was the enqueue; a direct merge has none, so its decision is the merge itself.
            moment = snapshot.enqueued_at if snapshot.queued_per_events and snapshot.enqueued_at else pr["merged_at"]
        verdict = plan_or_error(state_at(snapshot, moment) if moment else snapshot, policy, Mode.PR)
        # Today's enqueue step reads labels and draft state, never check results, so that is what a queue-merged PR
        # is held to when judged at the moment it was enqueued. Everything else is held to the full verdict.
        agrees = verdict.label_gate_ok if at == "enqueue" and snapshot.queued_per_events else verdict.state is EXPECTED
        rows.append(
            Row(
                number=pr["number"],
                merged_at=pr["merged_at"],
                state=verdict.state,
                headline=verdict.headline,
                agrees=agrees,
                via_queue=snapshot.queued_per_events,
                explanation=None if agrees else explained.get(pr["number"]),
                decision_at=moment,
                filter_notes=tuple(n for n in verdict.notes if n.startswith(FILTER_NOTE)),
            )
        )
    return Report(repo=repo, days=days, rows=tuple(rows), at=at)


def _explain(report: Report) -> list[str]:
    if report.at == "enqueue":
        held = [r for r in report.queue_rows if r.agrees and r.state is not EXPECTED]
        return [
            "Each PR is judged as it stood when it was enqueued (queue-merged) or merged (direct merge), rebuilt",
            "from the timestamps on its labels, reviews and check runs. A queue-merged PR agrees when today's enqueue",
            "rule held then (labels or approval, not a draft): that step never reads check results. The verdict",
            "column is the full planner verdict at that moment. A PR counts as queue-merged when the queue still held",
            "it at the end of its event history; a direct merge is a bypass, not a planner error.",
            "",
            f"- queue-merged PRs the planner would also have held back (checks or E2E not ready then): {len(held)}",
        ]
    return [
        "Reads each PR's final state, not its state when it was enqueued. A PR counts as queue-merged when the",
        "queue still held it at the end of its event history; a direct merge is a bypass, not a planner error.",
    ]


def render(report: Report) -> str:
    queue, bypass = report.queue_rows, report.bypass_rows
    queue_ok = sum(r.agrees for r in queue)
    blocked_bypass = [r for r in bypass if not r.agrees]
    held = "today's enqueue rule held at the enqueue" if report.at == "enqueue" else "planner agrees (ready)"
    lines = [
        f"# Replay: {report.repo}, merged in the last {report.days} days",
        "",
        f"- merged PRs replayed: {len(report.rows)}",
        f"- merged by the queue: {len(queue)}; {held}: {queue_ok} ({report.queue_agreement:.1%})",
        f"- queue-merged disagreements: {len(queue) - queue_ok} "
        f"(explained: {len(queue) - queue_ok - len(report.unexplained)}, UNEXPLAINED: {len(report.unexplained)})",
        f"- merged directly, outside the queue (bypass): {len(bypass)} "
        f"({len(bypass) / len(report.rows):.0%} of all merges)"
        if report.rows
        else "- no merged PRs in the window",
        f"  - of which the planner would have blocked: {len(blocked_bypass)}",
        "",
        *_explain(report),
    ]
    by_state = Counter(r.state.value for r in report.rows)
    lines += ["", "| Verdict | PRs |", "|---|---|", *[f"| {s} | {c} |" for s, c in by_state.most_common()]]
    queue_bad = [r for r in queue if not r.agrees]
    if queue_bad:
        lines += [
            "",
            "## Queue-merged PRs whose enqueue rule did not hold then"
            if report.at == "enqueue"
            else "## Queue-merged PRs the planner disagrees with",
            "",
            "| PR | Verdict | Why | Explained |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| #{r.number} | {r.state.value} | {r.headline} | {r.explanation or '**no**'} |"
            for r in sorted(queue_bad, key=lambda r: r.number)
        ]
    if report.filter_rows:
        lines += [
            "",
            "## Path filters vs what the checks did (shadow mode)",
            "",
            f"{len(report.filter_rows)} of {len(report.rows)} PRs have a job whose path filters and check disagree. "
            "Not a planner disagreement: it shows where the policy's filter mapping or the workflow's own filtering "
            "is wrong, so fix the mapping (or the workflow) before switching to `enforce`.",
            "",
            "| PR | Disagreement |",
            "|---|---|",
        ]
        lines += [
            f"| #{r.number} | {'; '.join(n.removeprefix(FILTER_NOTE).strip() for n in r.filter_notes)[:300]} |"
            for r in sorted(report.filter_rows, key=lambda r: r.number)
        ]
    if blocked_bypass:
        lines += [
            "",
            "## Bypass merges (what the rules would have said)",
            "",
            "| PR | Verdict | Why |",
            "|---|---|---|",
        ]
        lines += [
            f"| #{r.number} | {r.state.value} | {r.headline[:140]} |"
            for r in sorted(blocked_bypass, key=lambda r: r.number)
        ]
    return "\n".join(lines) + "\n"


def to_json(report: Report) -> str:
    return json.dumps(
        {
            "repo": report.repo,
            "days": report.days,
            "replayed": len(report.rows),
            "queue_merged": len(report.queue_rows),
            "bypass_merged": len(report.bypass_rows),
            "at": report.at,
            "queue_agreement": report.queue_agreement,
            "unexplained": [r.number for r in report.unexplained],
            "filter_disagreements": {str(r.number): list(r.filter_notes) for r in report.filter_rows},
            "rows": [
                {
                    "number": r.number,
                    "merged_at": r.merged_at,
                    "state": r.state.value,
                    "headline": r.headline,
                    "agrees": r.agrees,
                    "via_queue": r.via_queue,
                    "explanation": r.explanation,
                    "decision_at": r.decision_at,
                }
                for r in report.rows
            ],
        },
        indent=2,
    )
