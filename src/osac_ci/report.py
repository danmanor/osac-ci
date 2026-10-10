"""The open-PR report: one table of every open PR with its state, the reason, who has to act and what comes next.

This is the slice 0 wedge, the answer to "why is my PR stuck?" in one place. It is read-only: it reads GitHub and
writes files. A PR that cannot be read becomes a visible `planner-error` row; it never aborts the whole report.

PR titles and authors are untrusted text. Every renderer escapes them for its own format.
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from osac_ci.github.api import GitHubClient, GitHubError, check_repo, paginate
from osac_ci.github.snapshot import fetch_snapshot
from osac_ci.model import Mode, State, Verdict
from osac_ci.planner import error_verdict, plan_or_error
from osac_ci.policy import Policy

# Most actionable first: things a person must fix, then waiting on people, then nobody, then healthy.
STATE_ORDER: tuple[State, ...] = (
    State.PLANNER_ERROR,
    State.CHECKS_FAILED,
    State.E2E_FAILED,
    State.QUEUE_FAILED,
    State.NEEDS_AUTHORIZATION,
    State.AWAITING_APPROVAL,
    State.AWAITING_E2E_SIGNAL,
    State.AWAITING_UNLOCK,
    State.DRAFT,
    State.CHECKS_RUNNING,
    State.E2E_RUNNING,
    State.QUEUE_CHECKS_RUNNING,
    State.READY_TO_ENQUEUE,
    State.IN_QUEUE,
    State.QUEUE_PASSED,
)
_TONE = {
    State.READY_TO_ENQUEUE: "ok", State.IN_QUEUE: "ok", State.QUEUE_PASSED: "ok",
    State.AWAITING_APPROVAL: "warn", State.AWAITING_E2E_SIGNAL: "warn", State.NEEDS_AUTHORIZATION: "warn",
    State.AWAITING_UNLOCK: "warn", State.DRAFT: "warn",
    State.CHECKS_RUNNING: "info", State.E2E_RUNNING: "info", State.QUEUE_CHECKS_RUNNING: "info",
    State.CHECKS_FAILED: "bad", State.E2E_FAILED: "bad", State.QUEUE_FAILED: "bad", State.PLANNER_ERROR: "bad",
}  # fmt: skip


@dataclass(frozen=True)
class PRRow:
    number: int
    title: str
    author: str
    url: str
    updated_at: str
    is_draft: bool
    verdict: Verdict


@dataclass(frozen=True)
class Report:
    repo: str
    generated_at: str
    rows: tuple[PRRow, ...]

    def counts(self) -> list[tuple[State, int]]:
        found = Counter(r.verdict.state for r in self.rows)
        return [(s, found[s]) for s in STATE_ORDER if found[s]]


def list_open_prs(
    client: GitHubClient, repo: str, *, include_drafts: bool, base: str | None = None
) -> list[dict[str, Any]]:
    """The open pull requests, most recently updated first; with ``base`` only those that target that branch."""
    prs = paginate(
        client,
        f"/repos/{check_repo(repo)}/pulls",
        params={"state": "open", "sort": "updated", "direction": "desc"},
    )
    return [
        pr
        for pr in prs
        if (include_drafts or not pr.get("draft")) and (base is None or (pr.get("base") or {}).get("ref") == base)
    ]


def build_report(
    client: GitHubClient,
    policy: Policy,
    repo: str,
    *,
    generated_at: str,
    org: str | None = None,
    include_drafts: bool = False,
    limit: int | None = None,
    lookup_membership: bool = False,
    workers: int = 6,
) -> Report:
    prs = list_open_prs(client, repo, include_drafts=include_drafts)
    if limit is not None:
        prs = prs[:limit]
    org = org or repo.split("/", 1)[0]

    def one(pr: dict[str, Any]) -> PRRow:
        number = pr["number"]
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
                trust=policy.trust,
            )
            verdict = plan_or_error(snapshot, policy, Mode.PR)
        except (GitHubError, KeyError, ValueError) as exc:
            verdict = error_verdict(f"could not read PR #{number}: {exc}")
        return PRRow(
            number=number,
            title=str(pr.get("title", "")),
            author=str((pr.get("user") or {}).get("login", "")),
            url=str(pr.get("html_url", "")),
            updated_at=str(pr.get("updated_at", "")),
            is_draft=bool(pr.get("draft")),
            verdict=verdict,
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        rows = list(pool.map(one, prs))
    rank = {s: i for i, s in enumerate(STATE_ORDER)}
    rows.sort(key=lambda r: (rank.get(r.verdict.state, len(rank)), -_epoch(r.updated_at), r.number))
    return Report(repo=repo, generated_at=generated_at, rows=tuple(rows))


def _epoch(stamp: str) -> float:
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return 0.0


# markdown -------------------------------------------------------------------------------------------------


_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|~])")


def md_escape(text: str, *, limit: int = 100) -> str:
    """Make untrusted text inert inside a markdown table cell."""
    flat = " ".join(text.split())
    if len(flat) > limit:
        flat = flat[: limit - 1] + "…"
    # A zero-width space after @ keeps a PR title from mentioning (and notifying) someone if this text is ever posted.
    return _MD_SPECIAL.sub(r"\\\1", flat).replace("@", "@\u200b")


def render_markdown(report: Report) -> str:
    lines = [
        f"# Open pull requests: {report.repo}",
        "",
        f"{len(report.rows)} open PRs, read at {report.generated_at}. Nothing here changes anything on GitHub.",
        "",
        "| State | PRs |",
        "|---|---|",
        *[f"| {state.value} | {n} |" for state, n in report.counts()],
        "",
        "| PR | State | Why | Who acts | Next | Author | Updated |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in report.rows:
        v = r.verdict
        lines.append(
            f"| [#{r.number}]({r.url}) {md_escape(r.title, limit=70)} | {v.state.value} "
            f"| {md_escape(v.headline, limit=120)} "
            f"| {md_escape(v.who_must_act)} | {md_escape(v.next_action, limit=90)} | {md_escape(r.author)} "
            f"| {r.updated_at[:10]} |"
        )
    return "\n".join(lines) + "\n"


# json -----------------------------------------------------------------------------------------------------


def to_json(report: Report) -> str:
    return json.dumps(
        {
            "repo": report.repo,
            "generated_at": report.generated_at,
            "counts": {state.value: n for state, n in report.counts()},
            "prs": [
                {
                    "number": r.number,
                    "title": r.title,
                    "author": r.author,
                    "url": r.url,
                    "updated_at": r.updated_at,
                    "draft": r.is_draft,
                    "state": r.verdict.state.value,
                    "why": r.verdict.headline,
                    "who_must_act": r.verdict.who_must_act,
                    "next": r.verdict.next_action,
                    "blockers": list(r.verdict.blockers),
                }
                for r in report.rows
            ],
        },
        indent=2,
    )


# html -----------------------------------------------------------------------------------------------------

_CSS = """
:root{--bg:#F4F6F9;--surface:#fff;--ink:#0F1A26;--ink-2:#475769;--line:#D5DDE7;--accent:#2446E8;
--ok:#0F7557;--ok-bg:#DCF3E9;--warn:#8F5400;--warn-bg:#FFEFCF;--bad:#B02318;--bad-bg:#FDE3E0;--info:#1B58B0;--info-bg:#DBEAFA}
@media (prefers-color-scheme:dark){:root{--bg:#0B121A;--surface:#121B25;--ink:#E6EDF5;--ink-2:#9DAEC0;--line:#263545;
--accent:#8EA3FF;--ok:#4FD1A5;--ok-bg:#11372C;--warn:#F2B84B;--warn-bg:#3A2A0C;--bad:#FF8F82;--bad-bg:#431A17;
--info:#7DB6F5;--info-bg:#13304E;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;padding:24px 16px 64px;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1200px;margin:0 auto;display:grid;gap:16px}h1{margin:0;font-size:1.6rem}
.sub{color:var(--ink-2);margin:0}.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{border:1px solid var(--line);background:var(--surface);color:var(--ink);border-radius:99px;padding:4px 12px;
font:inherit;cursor:pointer}.chip[aria-pressed=true]{border-color:var(--accent);outline:2px solid var(--accent)}
input{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--surface);
color:var(--ink);max-width:100%}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:var(--surface)}
table{border-collapse:collapse;width:100%}th,td{text-align:left;vertical-align:top;padding:8px 12px;
border-bottom:1px solid var(--line)}
th{font-size:.75rem;text-transform:uppercase;letter-spacing:.06em;color:var(--ink-2)}
tr:last-child td{border-bottom:0}a{color:var(--accent)}.num{white-space:nowrap;font-variant-numeric:tabular-nums}
.pill{display:inline-block;border-radius:99px;padding:2px 10px;font-size:.8rem;white-space:nowrap}
.ok{background:var(--ok-bg);color:var(--ok)}.warn{background:var(--warn-bg);color:var(--warn)}
.bad{background:var(--bad-bg);color:var(--bad)}.info{background:var(--info-bg);color:var(--info)}
"""
_JS = """
(function(){var rows=[].slice.call(document.querySelectorAll('tbody tr')),
chips=[].slice.call(document.querySelectorAll('.chip')),q=document.getElementById('q'),on=null;
function apply(){var t=(q.value||'').toLowerCase();rows.forEach(function(r){
var okState=!on||r.dataset.state===on,
okText=!t||r.textContent.toLowerCase().indexOf(t)>-1;r.hidden=!(okState&&okText)})}
chips.forEach(function(c){c.addEventListener('click',function(){on=on===c.dataset.state?null:c.dataset.state;
chips.forEach(function(x){x.setAttribute('aria-pressed',String(x.dataset.state===on))});apply()})});
q.addEventListener('input',apply)})();
"""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_html(report: Report) -> str:
    chips = "".join(
        f'<button type="button" class="chip" data-state="{_e(s.value)}" aria-pressed="false">'
        f"{_e(s.value)}: {n}</button>"
        for s, n in report.counts()
    )
    body = []
    for r in report.rows:
        v = r.verdict
        blockers = "".join(f"<li>{_e(b)}</li>" for b in v.blockers)
        details = (
            f"<details><summary>{_e(v.headline)}</summary><ul>{blockers}</ul></details>" if blockers else _e(v.headline)
        )
        body.append(
            f'<tr data-state="{_e(v.state.value)}">'
            f'<td class="num"><a href="{_e(r.url)}" rel="noopener">#{r.number}</a></td>'
            f"<td>{_e(r.title)}<br><small>{_e(r.author)}{' (draft)' if r.is_draft else ''}</small></td>"
            f'<td><span class="pill {_TONE.get(v.state, "info")}">{_e(v.state.value)}</span></td>'
            f"<td>{details}</td><td>{_e(v.who_must_act)}</td><td>{_e(v.next_action)}</td>"
            f'<td class="num">{_e(r.updated_at[:10])}</td></tr>'
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Open PRs: {_e(report.repo)}</title><style>{_CSS}</style></head><body><main>"
        f"<h1>Open pull requests: {_e(report.repo)}</h1>"
        f'<p class="sub">{len(report.rows)} open PRs, read at {_e(report.generated_at)}. '
        "Read-only: nothing here changes GitHub.</p>"
        f'<div class="chips">{chips}</div>'
        '<label>Filter <input id="q" type="search" placeholder="number, title, author"></label>'
        '<div class="wrap"><table><thead><tr><th>PR</th><th>Title</th><th>State</th><th>Why</th><th>Who acts</th>'
        f"<th>Next</th><th>Updated</th></tr></thead><tbody>{''.join(body)}</tbody></table></div>"
        f"</main><script>{_JS}</script></body></html>\n"
    )


def write_all(report: Report, out_dir: Path, formats: Iterable[str]) -> list[str]:
    """Write the requested formats into ``out_dir`` and return the file names."""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    writers = {
        "md": ("report.md", render_markdown),
        "html": ("report.html", render_html),
        "json": ("report.json", to_json),
    }
    written = []
    for fmt in formats:
        name, render = writers[fmt]
        (target / name).write_text(render(report), encoding="utf-8")
        written.append(name)
    return written
