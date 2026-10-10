"""E2E compare: does the new "should the E2E start?" decision match what the label-based flow did?

For every open pull request, the decision of ``e2e_start`` and the state of the real E2E jobs on the same head commit,
both read from the same moment. The real E2E jobs are the check runs ``e2e-<suite>-full-install / e2e``: absent or
skipped means the current flow has not started that suite, in progress means it is running, anything else means it ran.

Four outcomes per pull request:

* ``agree-start`` and ``agree-wait``: both say the E2E should run (it has started), or both say it should not;
* ``new-only``: the decision says ``start`` while the current flow has not started the E2E. If the new flow replaced the
  old one, the E2E would run here and does not today;
* ``old-only``: the current flow started the E2E while the decision says it should not (no approval, a cheap check
  failed, changes requested, not affected by the change). If the new flow replaced the old one, the E2E would not run
  here, and the reason is the decision's.

Read-only. A pull request that cannot be read is its own row; it never aborts the run.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from osac_ci.compare import Requirement, required_contexts
from osac_ci.e2e_start import Action, Decision, Inputs, decide, fetch_inputs
from osac_ci.github.api import GitHubClient, GitHubError
from osac_ci.planner import latest_checks
from osac_ci.policy import Policy
from osac_ci.report import list_open_prs, md_escape


class Outcome(StrEnum):
    AGREE_START = "agree-start"
    AGREE_WAIT = "agree-wait"
    NEW_ONLY = "new-only"
    OLD_ONLY = "old-only"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Row:
    number: int
    title: str
    url: str
    outcome: Outcome
    action: str  # the decision's action, or "unreadable"
    reason: str
    legacy: tuple[str, ...]  # "suite: not started | running | passed | failed"


def legacy_states(inputs: Inputs) -> dict[str, str]:
    """What the current flow did for each suite on the head commit, from its real E2E job, not the gate placeholders."""
    latest = latest_checks(inputs.check_runs)
    states: dict[str, str] = {}
    for suite in inputs.suites:
        job = latest.get(f"e2e-{suite.name.split('/')[0]}-full-install / e2e")
        if job is None or (job.status == "completed" and job.conclusion == "skipped"):
            states[suite.name] = "not started"
        elif job.status != "completed":
            states[suite.name] = "running"
        else:
            states[suite.name] = "passed" if job.conclusion in ("success", "neutral") else "failed"
    return states


def classify(decision: Decision, legacy: dict[str, str]) -> Outcome:
    started = any(state != "not started" for state in legacy.values())
    if decision.action is Action.START:
        return Outcome.AGREE_START if started else Outcome.NEW_ONLY
    return Outcome.OLD_ONLY if started else Outcome.AGREE_WAIT


@dataclass(frozen=True)
class Report:
    repo: str
    generated_at: str
    rows: tuple[Row, ...]

    def count(self, outcome: Outcome) -> int:
        return sum(r.outcome is outcome for r in self.rows)


def compare(
    client: GitHubClient,
    policy: Policy,
    repo: str,
    *,
    generated_at: str,
    branch: str = "main",
    include_drafts: bool = False,
    limit: int | None = None,
    workers: int = 6,
) -> Report:
    required: Sequence[Requirement] = required_contexts(client, repo, branch)
    prs = list_open_prs(client, repo, include_drafts=include_drafts)
    if limit is not None:
        prs = prs[:limit]

    def one(pr: dict[str, Any]) -> Row:
        number = pr["number"]
        title, url = str(pr.get("title", "")), str(pr.get("html_url", ""))
        try:
            inputs = fetch_inputs(client, policy, repo, number, required=required)
        except (GitHubError, KeyError, ValueError) as exc:
            return Row(
                number, title, url, Outcome.UNREADABLE, "unreadable", f"could not read the pull request: {exc}", ()
            )
        decision = decide(inputs)
        legacy = legacy_states(inputs)
        return Row(
            number,
            title,
            url,
            classify(decision, legacy),
            decision.action.value,
            decision.reason,
            tuple(f"{suite}: {state}" for suite, state in legacy.items()),
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        rows = sorted(pool.map(one, prs), key=lambda r: r.number)
    return Report(repo, generated_at, tuple(rows))


# rendering ------------------------------------------------------------------------------------------------------


def _cell(text: str, limit: int = 170) -> str:
    return md_escape(text, limit=limit)


def render(report: Report) -> str:
    lines = [
        f"# E2E start decision against the current flow: {report.repo}",
        "",
        f"Generated {report.generated_at}. {len(report.rows)} open pull requests; the decision and the real E2E jobs "
        "are read from the same moment.",
        "",
        "| Outcome | PRs |",
        "|---|---|",
        *[f"| {o.value} | {report.count(o)} |" for o in Outcome],
    ]
    for outcome, title, hint in (
        (
            Outcome.OLD_ONLY,
            "The current flow started the E2E, the decision would not",
            "If the new flow replaced the old one, the E2E would not run here. The reason is the decision's.",
        ),
        (
            Outcome.NEW_ONLY,
            "The decision would start the E2E, the current flow has not",
            "If the new flow replaced the old one, the E2E would run here and does not today.",
        ),
    ):
        rows = [r for r in report.rows if r.outcome is outcome]
        if not rows:
            continue
        causes = Counter(r.action for r in rows)
        lines += [
            "",
            f"## {title}",
            "",
            hint,
            "",
            "| Decision | PRs |",
            "|---|---|",
            *[f"| {_cell(a)} | {n} |" for a, n in causes.most_common()],
            "",
            "| PR | Decision | Reason | Current flow |",
            "|---|---|---|---|",
            *[
                f"| [#{r.number}]({r.url}) | {r.action} | {_cell(r.reason)} | {_cell('; '.join(r.legacy), 120)} |"
                for r in rows
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
            *[f"| [#{r.number}]({r.url}) | {_cell(r.reason)} |" for r in unreadable],
        ]
    return "\n".join(lines).rstrip("\n") + "\n"


def to_json(report: Report) -> str:
    return json.dumps(
        {
            "repo": report.repo,
            "generated_at": report.generated_at,
            "counts": {o.value: report.count(o) for o in Outcome},
            "rows": [
                {
                    "number": r.number,
                    "outcome": r.outcome.value,
                    "action": r.action,
                    "reason": r.reason,
                    "legacy": list(r.legacy),
                }
                for r in report.rows
            ],
        },
        indent=2,
    )
