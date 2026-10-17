"""Command line: validate a policy and explain PRs.

    osac-ci policy check policy/osac.yml
    osac-ci explain --policy policy/osac.yml --snapshot snapshot.json [--mode pr|queue]
    GH_TOKEN=... osac-ci explain-pr --policy policy/osac.yml --repo osac-project/osac --number 1438
    GH_TOKEN=... osac-ci replay --policy policy/osac.yml --repo osac-project/osac --days 60 --limit 100
    GH_TOKEN=... osac-ci report --policy policy/osac.yml --repo osac-project/osac --out-dir report

`explain` is offline: it turns a recorded snapshot into the verdict the control plane would publish.
`explain-pr` reads one live PR (read-only) and does the same. `replay` reads recently merged PRs and reports
whether the planner agrees they were ready (the parity evidence). None of them writes anything to GitHub.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from osac_ci import bot_approve, e2e_check, e2e_start, e2e_suites_check, lockfacts
from osac_ci import compare as compare_mod
from osac_ci import e2e_compare as e2e_compare_mod
from osac_ci.authorize import authorize, override, reply
from osac_ci.flow import FlowError, flow
from osac_ci.github.api import GitHubClient, GitHubError, HttpClient, Response, rate_remaining
from osac_ci.github.snapshot import fetch_snapshot, parse_queue_branch
from osac_ci.model import CheckRun, LabelEvent, Mode, Review, Snapshot
from osac_ci.planner import plan_or_error
from osac_ci.policy import PolicyError, load_policy
from osac_ci.publish import CHECK_NAME, describe, find_open_prs, publish_pr, publish_queue, sweep
from osac_ci.render import render_markdown
from osac_ci.replay import load_explained, render, replay, to_json
from osac_ci.report import build_report, write_all
from osac_ci.rules_page import render_rules
from osac_ci.stale import StaleRules

TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN")
COMMENT_ENV = "OSAC_CI_COMMENT"  # the comment text; it is user input, so it never travels on the command line
APPROVE_TOKEN_ENV = "OSAC_CI_APPROVE_TOKEN"  # the bot's own credential, used only to submit an approval
ORG_TOKEN_ENV = "OSAC_CI_ORG_TOKEN"  # optional: a token that can only read the organization (membership, teams)


def snapshot_from_dict(data: dict[str, Any]) -> Snapshot:
    """Build a Snapshot from JSON. Unknown keys are rejected so a recorded fixture cannot silently drift."""
    allowed = {
        "repo", "number", "head_sha", "is_draft", "is_fork", "author", "author_is_org_member", "fork_owner",
        "fork_owner_is_org_member", "labels", "reviews", "label_events", "check_runs", "changed_files",
        "in_merge_queue", "queued_per_events", "base_ref", "codeowners", "team_members", "change_fingerprints",
    }  # fmt: skip
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown snapshot keys: {sorted(unknown)}")
    kwargs: dict[str, Any] = {
        k: v
        for k, v in data.items()
        if k not in {"labels", "reviews", "label_events", "check_runs", "changed_files", "team_members"}
    }
    return Snapshot(
        **kwargs,
        labels=frozenset(data.get("labels", ())),
        reviews=tuple(Review(**r) for r in data.get("reviews", ())),
        label_events=tuple(LabelEvent(**e) for e in data.get("label_events", ())),
        check_runs=tuple(CheckRun(**c) for c in data.get("check_runs", ())),
        changed_files=tuple(data.get("changed_files", ())),
        team_members={t: (None if m is None else frozenset(m)) for t, m in data.get("team_members", {}).items()},
    )


class _NoOrgToken:
    """Stands in for the organization client when it was required but is missing: every lookup fails loudly, so the
    PR gets a visible planner-error with the cause instead of an answer from a token that cannot see the org."""

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        raise GitHubError(0, f"{ORG_TOKEN_ENV} is not available (the organization app token could not be created)")


def _non_negative(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must not be negative, got {value}")
    return value


def build_org_client(*, required: bool = False) -> GitHubClient | None:
    """A client for organization lookups only, when ``OSAC_CI_ORG_TOKEN`` is set; otherwise the main client is used.

    With ``required`` a missing token never falls back to the main client (it cannot read the organization): lookups
    fail instead, which the planner reports as an error."""
    token = os.environ.get(ORG_TOKEN_ENV)
    if token:
        return HttpClient(token)
    return _NoOrgToken() if required else None


def build_approve_client() -> GitHubClient:
    """The client that submits approvals. Its token is separate from the one that reads, so the read token never needs
    the right to approve and a missing approval token fails before anything is decided."""
    token = os.environ.get(APPROVE_TOKEN_ENV)
    if not token:
        raise SystemExit(f"error: set {APPROVE_TOKEN_ENV} to the credential that submits the approval")
    return HttpClient(token)


def build_client(*, anonymous_ok: bool = False) -> GitHubClient:
    """The real client, with the token taken from the environment (never from an argument or a file).

    ``anonymous_ok`` lets a command that reads public data only go on without one; the rate limit is then GitHub's low
    one for unauthenticated requests, and a private repository answers "not found"."""
    for name in TOKEN_ENV:
        if token := os.environ.get(name):
            return HttpClient(token)
    if anonymous_ok:
        return HttpClient("", anonymous=True)
    raise SystemExit(f"error: set one of {' or '.join(TOKEN_ENV)} (for example GH_TOKEN=$(gh auth token))")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="osac-ci", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("policy", help="policy tools").add_subparsers(dest="policy_command", required=True)
    check.add_parser("check", help="validate a policy file").add_argument("policy", type=Path)

    explain = sub.add_parser("explain", help="print the verdict for a recorded snapshot")
    explain.add_argument("--policy", type=Path, required=True)
    explain.add_argument("--snapshot", type=Path, required=True)
    explain.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.PR.value)

    live = sub.add_parser("explain-pr", help="read one live PR (read-only) and print its verdict")
    live.add_argument("--policy", type=Path, required=True)
    live.add_argument("--repo", required=True, help="owner/name")
    live.add_argument("--number", type=int, required=True)
    live.add_argument("--org", help="org used for membership lookups (default: the repo owner)")
    live.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.PR.value)
    live.add_argument(
        "--no-membership-lookup",
        action="store_true",
        help="approximate org membership from author_association (needs no org-scoped token)",
    )

    rpt = sub.add_parser("report", help="write a table of every open PR with its state, reason and next step")
    rpt.add_argument("--policy", type=Path, required=True)
    rpt.add_argument("--repo", required=True, help="owner/name")
    rpt.add_argument("--out-dir", type=Path, default=Path("report"))
    rpt.add_argument("--formats", default="md,html,json", help="comma separated: md, html, json")
    rpt.add_argument("--include-drafts", action="store_true")
    rpt.add_argument("--limit", type=int)
    rpt.add_argument("--workers", type=int, default=6)
    rpt.add_argument("--org", help="org used for membership lookups (default: the repo owner)")
    rpt.add_argument(
        "--lookup-membership", action="store_true", help="look up org membership (needs an org-scoped token)"
    )

    pub = sub.add_parser("publish", help="post the verdict of one PR (or every open PR) as the OSAC CI check run")
    pub.add_argument("--policy", type=Path, required=True)
    pub.add_argument("--repo", required=True, help="owner/name")
    target = pub.add_mutually_exclusive_group(required=True)
    target.add_argument("--number", type=int, help="publish for this PR")
    target.add_argument("--all", action="store_true", help="publish for every open PR (the periodic sweep)")
    pub.add_argument("--check-name", default=CHECK_NAME)
    pub.add_argument("--note", default="", help="text placed above the verdict in the check summary")
    pub.add_argument("--org", help="org used for membership lookups (default: the repo owner)")
    pub.add_argument("--limit", type=_non_negative, help="with --all: stop after this many PRs")
    pub.add_argument(
        "--recent", type=_non_negative, help="with --all: look at this many most recently updated PRs per sweep"
    )
    pub.add_argument(
        "--rotate", type=_non_negative, help="with --all: plus this many of the others, a different slice each sweep"
    )
    pub.add_argument(
        "--interval", type=_non_negative, default=600, help="seconds between sweeps, for the rotation (default 600)"
    )
    pub.add_argument(
        "--reserve",
        type=_non_negative,
        default=0,
        help="with --all: skip the PRs not started once fewer requests are left (approximate with several workers)",
    )
    pub.add_argument(
        "--stale-only",
        action="store_true",
        help="with --all: read only the PRs whose posted verdict is missing, a planner error, older than the PR's last "
        "change, stuck in progress or old (one listing instead of reading every PR); not with --recent or --rotate",
    )
    pub.add_argument(
        "--stale-after",
        type=_non_negative,
        default=1800,
        help="with --stale-only: seconds an in-progress verdict may stand before it is looked at again (default 1800)",
    )
    pub.add_argument(
        "--verdict-app-id",
        type=_non_negative,
        default=15368,
        help="with --stale-only: id of the GitHub App that posts the verdicts (default 15368, the Actions built-in "
        "token); 0 accepts a verdict posted by any app, for a check posted with another app's token",
    )
    pub.add_argument(
        "--max-age",
        type=_non_negative,
        default=21600,
        help="with --stale-only: refresh any verdict older than this many seconds, 0 for never (default 21600)",
    )
    pub.add_argument("--dry-run", action="store_true", help="print what would be posted, post nothing")
    pub.add_argument(
        "--require-org-token",
        action="store_true",
        help=f"never fall back to the main token for organization lookups: without {ORG_TOKEN_ENV} they fail visibly",
    )
    pub.add_argument(
        "--lookup-membership", action="store_true", help="look up org membership (needs an org-scoped token)"
    )

    que = sub.add_parser("publish-queue", help="post the verdict of a merge-queue commit as the OSAC CI check run")
    que.add_argument("--policy", type=Path, required=True)
    que.add_argument("--repo", required=True, help="owner/name")
    que.add_argument("--sha", required=True, help="the queue commit (40 hex digits)")
    que.add_argument("--branch", required=True, help="the queue branch, gh-readonly-queue/<base>/pr-<n>-<sha>")
    que.add_argument("--check-name", default=CHECK_NAME)
    que.add_argument("--note", default="")
    que.add_argument("--dry-run", action="store_true")

    auth = sub.add_parser(
        "authorize", help="handle an /ok-to-test <sha> comment (the comment text is read from the environment)"
    )
    auth.add_argument("--policy", type=Path, required=True)
    auth.add_argument("--repo", required=True, help="owner/name")
    auth.add_argument("--number", type=int, required=True)
    auth.add_argument("--commenter", required=True, help="login of whoever commented")
    auth.add_argument("--org", help="org used for the membership check (default: the repo owner)")
    auth.add_argument("--dry-run", action="store_true", help="decide, but post neither the check nor the reply")

    fnd = sub.add_parser("find-pr", help="print the number of the open PR(s) a workflow run belongs to, one per line")
    fnd.add_argument("--repo", required=True, help="owner/name")
    fnd.add_argument("--sha", required=True, help="head commit of the run (exact)")

    lst = sub.add_parser(
        "lock-status", help="print whether a job may start on a commit (locked, open, not-applicable or unknown)"
    )
    lst.add_argument("--repo", required=True, help="owner/name")
    lst.add_argument("--sha", required=True, help="the commit the job runs on (full sha)")
    lst.add_argument("--job", required=True, help="the job id in the policy")
    lst.add_argument("--check-name", default=CHECK_NAME)

    ovr = sub.add_parser(
        "override",
        help="handle an /override <sha> <reason> comment: waive the protected-path approval (text from env)",
    )
    ovr.add_argument("--policy", type=Path, required=True)
    ovr.add_argument("--repo", required=True, help="owner/name")
    ovr.add_argument("--number", type=int, required=True)
    ovr.add_argument("--commenter", required=True, help="login of whoever commented")
    ovr.add_argument("--dry-run", action="store_true", help="decide, but post neither the check nor the reply")

    cmp = sub.add_parser(
        "compare", help="compare 'OSAC CI says ready' with 'every required check passed' on every open PR"
    )
    cmp.add_argument("--policy", type=Path, required=True)
    cmp.add_argument("--repo", required=True, help="owner/name")
    cmp.add_argument("--branch", default="main", help="branch whose rules list the required checks")
    cmp.add_argument("--include-drafts", action="store_true")
    cmp.add_argument("--limit", type=int, help="most PRs to read (with --backfill: 100 unless given)")
    cmp.add_argument(
        "--backfill",
        type=int,
        metavar="DAYS",
        help="compare PRs merged in the last DAYS days, each as it stood when it was enqueued or merged, "
        "instead of the open PRs",
    )
    cmp.add_argument("--workers", type=int, default=6)
    cmp.add_argument("--org", help="org used for membership lookups (default: the repo owner)")
    cmp.add_argument("--lookup-membership", action="store_true", help="look up org membership (needs an org token)")
    cmp.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    cmp.add_argument("--json-file", type=Path, help="also write the JSON here, so one run gives both formats")
    cmp.add_argument(
        "--fail-on-looser", action="store_true", help="exit 1 when OSAC CI says ready while a required check has not"
    )

    ec = sub.add_parser(
        "e2e-compare", help="compare the E2E start decision with what the current flow did, on every open pull request"
    )
    ec.add_argument("--policy", type=Path, required=True)
    ec.add_argument("--repo", required=True, help="owner/name")
    ec.add_argument("--branch", default="main", help="branch whose rules list the required checks")
    ec.add_argument("--include-drafts", action="store_true")
    ec.add_argument("--limit", type=int)
    ec.add_argument("--workers", type=int, default=6)
    ec.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    ec.add_argument("--json-file", type=Path, help="also write the JSON here, so one run gives both formats")

    bot = sub.add_parser(
        "bot-decide",
        help="say whether a dependency-update pull request may be approved automatically, and why (approves nothing)",
    )
    bot.add_argument("--repo", required=True, help="owner/name")
    bot_which = bot.add_mutually_exclusive_group(required=True)
    bot_which.add_argument("--number", type=int, help="the pull request")
    bot_which.add_argument("--head-sha", help="the open pull request(s) with exactly this head commit")
    bot.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    bot.add_argument("--json-file", type=Path, help="also write the JSON (a list, one entry per pull request) here")

    botlist = sub.add_parser(
        "bot-list", help="print, as a JSON list, the open pull requests of the update bots (the sweep looks at these)"
    )
    botlist.add_argument("--repo", required=True, help="owner/name")

    rules = sub.add_parser(
        "explain-rules",
        help="write the page that says how a pull request merges (who approves, which bots), from policy/flow.yml",
    )
    rules.add_argument(
        "--policy", type=Path, action="append", default=[], help="a repository policy file whose suites to list"
    )
    rules.add_argument("--output", type=Path, help="write the page here instead of printing it")
    rules.add_argument("--check", action="store_true", help="fail when --output is not what would be written now")

    approve = sub.add_parser(
        "bot-approve",
        help="approve a dependency-update pull request when bot-decide says so, for the commit it decided on",
    )
    approve.add_argument("--repo", required=True, help="owner/name")
    approve_which = approve.add_mutually_exclusive_group(required=True)
    approve_which.add_argument("--number", type=int, help="the pull request")
    approve_which.add_argument("--head-sha", help="the open pull request(s) with exactly this head commit")
    approve.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    approve.add_argument("--json-file", type=Path, help="also write the JSON (a list, one entry per pull request) here")

    dec = sub.add_parser(
        "e2e-decide",
        help="say whether the full-install E2E should start for a pull request now, and why (starts nothing)",
    )
    dec.add_argument("--policy", type=Path, required=True)
    dec.add_argument("--repo", required=True, help="owner/name")
    which = dec.add_mutually_exclusive_group(required=True)
    which.add_argument("--number", type=int, help="the pull request")
    which.add_argument("--head-sha", help="the open pull request(s) with exactly this head commit")
    dec.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    dec.add_argument("--json-file", type=Path, help="also write the JSON (a list, one entry per pull request) here")

    suites_cmd = sub.add_parser(
        "e2e-suites",
        help="list the E2E suites a set of changed files needs (a merge queue entry has no pull request to ask)",
    )
    suites_cmd.add_argument("--policy", type=Path, required=True)
    suites_cmd.add_argument(
        "--files-from", type=Path, required=True, help="a file with one changed path per line (as git diff --name-only)"
    )
    suites_cmd.add_argument(
        "--checks", action="store_true", help="print a JSON object, suite -> line name, instead of the list of suites"
    )

    erep = sub.add_parser(
        "e2e-report",
        help="show the e2e-decide answer on the pull request as the one E2E status, and say whether this run starts it",
    )
    erep.add_argument("--repo", required=True, help="owner/name")
    erep.add_argument("--decision-file", type=Path, required=True, help="the JSON file written by e2e-decide")
    erep.add_argument("--run-id", type=int, required=True, help="the workflow run that would start the suites")
    erep.add_argument("--run-url", default="", help="link of that run, shown on the check")
    erep.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    erep.add_argument("--json-file", type=Path, help="also write the JSON answer here")
    erep.add_argument(
        "--kind", choices=e2e_check.KINDS, default="statuses", help="write commit statuses (default) or check runs"
    )

    efin = sub.add_parser("e2e-finish", help="record how the suites ended, as the E2E status")
    efin.add_argument("--repo", required=True, help="owner/name")
    efin.add_argument("--sha", required=True, help="the head commit the E2E ran for")
    efin.add_argument("--result", required=True, choices=e2e_check.RESULTS)
    efin.add_argument("--run-url", default="", help="link of the workflow run, shown with the status")
    efin.add_argument("--detail", default="", help="text after the result (default: none)")
    efin.add_argument(
        "--kind", choices=e2e_check.KINDS, default="statuses", help="write commit statuses (default) or check runs"
    )

    strack = sub.add_parser(
        "e2e-suites-track",
        help="create the suite lines on the pull request and keep them current until the jobs run",
    )
    strack.add_argument("--repo", required=True, help="owner/name")
    strack.add_argument("--sha", required=True, help="the head commit the suites run for")
    strack.add_argument("--run-id", type=int, required=True, help="the workflow run that holds the suite jobs")
    strack.add_argument("--run-url", default="")
    strack.add_argument(
        "--suite-checks", required=True, metavar="JSON", help="JSON object: suite id -> line name (from e2e-decide)"
    )
    strack.add_argument("--timeout", type=float, default=7200, help="seconds to wait for the jobs to get a runner")
    strack.add_argument("--interval", type=float, default=30, help="seconds between looks")
    strack.add_argument(
        "--kind", choices=e2e_check.KINDS, default="statuses", help="write commit statuses (default) or check runs"
    )

    ssettle = sub.add_parser("e2e-suites-settle", help="complete the suite lines with the result of each suite job")
    ssettle.add_argument("--repo", required=True, help="owner/name")
    ssettle.add_argument("--sha", required=True)
    ssettle.add_argument("--run-id", type=int, required=True)
    ssettle.add_argument("--run-url", default="")
    ssettle.add_argument(
        "--suite-checks", required=True, metavar="JSON", help="JSON object: suite id -> line name (from e2e-decide)"
    )
    ssettle.add_argument(
        "--kind", choices=e2e_check.KINDS, default="statuses", help="write commit statuses (default) or check runs"
    )

    rep = sub.add_parser("replay", help="check the planner against recently merged PRs (parity evidence)")
    rep.add_argument("--policy", type=Path, required=True)
    rep.add_argument("--repo", required=True, help="owner/name")
    rep.add_argument("--days", type=int, default=60)
    rep.add_argument("--limit", type=int, default=100)
    rep.add_argument("--org", help="org used for membership lookups (default: the repo owner)")
    rep.add_argument("--explained", type=Path, help="YAML ledger of signed-off disagreements (PR number: reason)")
    rep.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    rep.add_argument("--no-membership-lookup", action="store_true")
    rep.add_argument(
        "--at",
        choices=["final", "enqueue"],
        default="final",
        help="judge each PR as it is now (final) or as it stood when it was enqueued or merged (enqueue)",
    )
    return parser


def _decide_command(
    args: argparse.Namespace,
    decide: Callable[[GitHubClient, int], Any],
    render: Callable[[Any], str],
    to_json: Callable[[Any], str],
    name: str,
) -> int:
    """Shared by the commands that answer a question about pull requests without changing anything: find the pull
    requests (one number, or whatever open ones have this head commit), decide each, print markdown or JSON, optionally
    write the JSON. A GitHub failure exits 3 and prints no answer."""
    try:
        client = build_client()
        numbers = [args.number] if args.number is not None else find_open_prs(client, args.repo, sha=args.head_sha)
        decisions = [decide(client, n) for n in numbers]
    except (GitHubError, KeyError, ValueError) as exc:
        print(f"error: {name} failed: {exc}", file=sys.stderr)
        return 3
    as_json = json.dumps([json.loads(to_json(d)) for d in decisions], indent=2)
    if args.json_file:
        try:
            args.json_file.write_text(as_json + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot write {args.json_file}: {exc}", file=sys.stderr)
            return 3
    if args.json:
        print(as_json)
    else:
        print("\n".join(render(d) for d in decisions) or "No open pull request has this head.", end="")
    return 0


def _e2e_report(args: argparse.Namespace) -> int:
    try:
        entries = json.loads(args.decision_file.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("the decision file must hold a list")
        found = e2e_check.report(build_client(), args.repo, entries, args.run_id, args.run_url, args.kind)
    except (OSError, GitHubError, KeyError, ValueError) as exc:
        print(f"error: e2e-report failed: {exc}", file=sys.stderr)
        return 3
    if args.json_file:
        try:
            args.json_file.write_text(e2e_check.to_json(found) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot write {args.json_file}: {exc}", file=sys.stderr)
            return 3
    print(e2e_check.to_json(found) if args.json else e2e_check.render(found), end="")
    return 0


def _e2e_suites(args: argparse.Namespace) -> int:
    try:
        if args.command == "e2e-suites-track":
            ids = e2e_suites_check.track(
                build_client(), args.repo, args.sha, args.run_id, args.run_url,
                e2e_suites_check.parse_lines(args.suite_checks),
                timeout=args.timeout, interval=args.interval, kind=args.kind,
            )  # fmt: skip
        else:
            ids = e2e_suites_check.settle(
                build_client(), args.repo, args.sha, args.run_id, args.run_url,
                e2e_suites_check.parse_lines(args.suite_checks), args.kind,
            )  # fmt: skip
    except (GitHubError, KeyError, ValueError) as exc:
        print(f"error: {args.command} failed: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(ids))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "find-pr":
        try:
            numbers = find_open_prs(build_client(), args.repo, sha=args.sha)
        except (GitHubError, ValueError) as exc:
            print(f"error: find-pr failed: {exc}", file=sys.stderr)
            return 3
        print("\n".join(str(n) for n in numbers))
        return 0

    if args.command == "lock-status":
        try:
            print(lockfacts.lookup(build_client(anonymous_ok=True), args.repo, args.sha, args.job, args.check_name))
        except ValueError as exc:
            print(f"error: lock-status failed: {exc}", file=sys.stderr)
            return 3
        return 0

    if args.command == "explain-rules":
        try:
            page = render_rules(flow(), [load_policy(p) for p in args.policy])
        except (FlowError, PolicyError, OSError, UnicodeDecodeError) as exc:
            print(f"error: explain-rules failed: {exc}", file=sys.stderr)
            return 3
        if args.check:
            if args.output is None:
                print("error: --check needs --output", file=sys.stderr)
                return 2
            try:
                current = args.output.read_text(encoding="utf-8") if args.output.exists() else None
            except (OSError, UnicodeDecodeError) as exc:
                print(f"error: explain-rules failed: cannot read {args.output}: {exc}", file=sys.stderr)
                return 3
            if current != page:
                print(
                    f"error: {args.output} is stale; run: osac-ci explain-rules --output {args.output}", file=sys.stderr
                )
                return 1
            return 0
        if args.output is not None:
            try:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(page, encoding="utf-8")
            except OSError as exc:
                print(f"error: explain-rules failed: cannot write {args.output}: {exc}", file=sys.stderr)
                return 3
            return 0
        print(page, end="")
        return 0

    if args.command == "bot-list":
        try:
            numbers = bot_approve.list_bot_pulls(build_client(), args.repo)
        except (GitHubError, ValueError) as exc:
            print(f"error: bot-list failed: {exc}", file=sys.stderr)
            return 3
        print(json.dumps(numbers))
        return 0

    if args.command == "bot-decide":
        return _decide_command(
            args,
            lambda client, n: bot_approve.decide(bot_approve.fetch_inputs(client, args.repo, n)),
            bot_approve.render_markdown,
            bot_approve.to_json,
            "bot-decide",
        )

    if args.command == "bot-approve":
        write = build_approve_client()
        return _decide_command(
            args,
            lambda client, n: bot_approve.approve(client, write, args.repo, n),
            bot_approve.render_approval,
            bot_approve.approval_json,
            "bot-approve",
        )

    if args.command == "e2e-report":
        return _e2e_report(args)

    if args.command == "e2e-finish":
        try:
            e2e_check.finish(build_client(), args.repo, args.sha, args.result, args.run_url, args.detail, args.kind)
        except (GitHubError, KeyError, ValueError) as exc:
            print(f"error: e2e-finish failed: {exc}", file=sys.stderr)
            return 3
        print(f"E2E status: {args.result}")
        return 0

    if args.command in ("e2e-suites-track", "e2e-suites-settle"):
        return _e2e_suites(args)

    try:
        policy = load_policy(args.policy)
    except PolicyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "policy":
        print(f"OK: {policy.repo}: {len(policy.jobs)} jobs")
        return 0

    if args.command == "e2e-suites":
        try:
            names = [n for n in args.files_from.read_text(encoding="utf-8").splitlines() if n]
        except (OSError, ValueError) as exc:
            print(f"error: cannot read {args.files_from}: {exc}", file=sys.stderr)
            return 3
        if args.checks:
            print(json.dumps(dict(e2e_start.suite_checks_for_files(policy, names))))
        else:
            print(json.dumps(list(e2e_start.suites_for_files(policy, names))))
        return 0

    if args.command == "report":
        formats = [f.strip() for f in args.formats.split(",") if f.strip()]
        unknown = set(formats) - {"md", "html", "json"}
        if unknown or not formats:
            print(f"error: unknown formats {sorted(unknown)}; use md, html, json", file=sys.stderr)
            return 2
        try:
            report = build_report(
                build_client(),
                policy,
                args.repo,
                generated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
                org=args.org,
                include_drafts=args.include_drafts,
                limit=args.limit,
                lookup_membership=args.lookup_membership,
                workers=args.workers,
            )
            names = write_all(report, args.out_dir, formats)
        except (GitHubError, ValueError, OSError) as exc:
            print(f"error: report failed: {exc}", file=sys.stderr)
            return 3
        summary = ", ".join(f"{n} {s.value}" for s, n in report.counts()) or "no open PRs"
        print(f"{len(report.rows)} open PRs ({summary}); wrote {', '.join(names)} to {args.out_dir}")
        return 0

    if args.command == "publish":
        common = {
            "org": args.org,
            "lookup_membership": args.lookup_membership,
            "check_name": args.check_name,
            "note": args.note,
            "dry_run": args.dry_run,
            "org_client": build_org_client(required=args.require_org_token),
        }
        if args.stale_only and not args.all:
            print("error: --stale-only needs --all", file=sys.stderr)
            return 2
        if args.stale_only and (args.recent is not None or args.rotate is not None):
            print(
                "error: --stale-only chooses its PRs itself; do not combine it with --recent or --rotate",
                file=sys.stderr,
            )
            return 2
        try:
            client = build_client()
            if args.all:
                outcomes = sweep(
                    client,
                    policy,
                    args.repo,
                    limit=args.limit,
                    recent=args.recent,
                    rotate=args.rotate,
                    tick=int(time.time() // max(1, args.interval)),
                    reserve=args.reserve,
                    stale=StaleRules(args.stale_after, args.max_age) if args.stale_only else None,
                    verdict_app_id=args.verdict_app_id or None,
                    **common,
                )
            else:
                outcomes = [publish_pr(client, policy, args.repo, args.number, **common)]
        except (GitHubError, ValueError) as exc:
            print(f"error: publish failed: {exc}", file=sys.stderr)
            return 3
        print(describe(outcomes), end="")
        left = rate_remaining(client)
        if left is not None and args.all and (args.recent is not None or args.rotate is not None or args.reserve):
            print(
                f"requests left in this token's window: {left}"
            )  # only for a budgeted sweep: other output is unchanged
        return 1 if any(o.action == "failed" for o in outcomes) else 0

    if args.command == "publish-queue":
        base = parse_queue_branch(args.branch)
        if base is None:
            print(f"error: {args.branch!r} is not a merge-queue branch", file=sys.stderr)
            return 2
        try:
            outcome = publish_queue(
                build_client(),
                policy,
                args.repo,
                args.sha,
                base,
                check_name=args.check_name,
                note=args.note,
                dry_run=args.dry_run,
            )
        except (GitHubError, ValueError) as exc:
            print(f"error: publish-queue failed: {exc}", file=sys.stderr)
            return 3
        print(describe([outcome]), end="")
        return 0

    if args.command == "authorize":
        try:
            client = build_client()
            result = authorize(
                client,
                build_org_client(required=True) or client,
                policy,
                args.repo,
                args.number,
                args.commenter,
                os.environ.get(COMMENT_ENV, ""),
                org=args.org,
                dry_run=args.dry_run,
            )
            if result.handled and not args.dry_run:
                reply(client, args.repo, args.number, result.message)
        except (GitHubError, ValueError) as exc:
            print(f"error: authorize failed: {exc}", file=sys.stderr)
            return 3
        state = "granted" if result.granted else ("refused" if result.handled else "ignored")
        print(f"PR #{args.number}: {state}: {result.message}")
        return 0

    if args.command == "override":
        try:
            client = build_client()
            result = override(
                client,
                build_org_client(required=True) or client,
                policy,
                args.repo,
                args.number,
                args.commenter,
                os.environ.get(COMMENT_ENV, ""),
                dry_run=args.dry_run,
            )
            if result.handled and not args.dry_run:
                reply(client, args.repo, args.number, result.message)
        except (GitHubError, ValueError) as exc:
            print(f"error: override failed: {exc}", file=sys.stderr)
            return 3
        state = "granted" if result.granted else ("refused" if result.handled else "ignored")
        print(f"PR #{args.number}: {state}: {result.message}")
        return 0

    if args.command == "compare":
        if args.backfill is not None and args.backfill < 1:
            print("error: --backfill needs a number of days of 1 or more", file=sys.stderr)
            return 2
        try:
            when = datetime.now(UTC)
            common: dict[str, Any] = {
                "generated_at": when.strftime("%Y-%m-%d %H:%M UTC"),
                "branch": args.branch,
                "org": args.org,
                "org_client": build_org_client(),
                "lookup_membership": args.lookup_membership,
                "workers": args.workers,
            }
            if args.backfill is not None:
                found = compare_mod.backfill(
                    build_client(),
                    policy,
                    args.repo,
                    now=when,
                    days=args.backfill,
                    limit=100 if args.limit is None else args.limit,
                    **common,
                )
            else:
                found = compare_mod.compare(
                    build_client(),
                    policy,
                    args.repo,
                    include_drafts=args.include_drafts,
                    limit=args.limit,
                    **common,
                )
        except (GitHubError, ValueError) as exc:
            print(f"error: compare failed: {exc}", file=sys.stderr)
            return 3
        if args.json_file:
            try:
                args.json_file.write_text(compare_mod.to_json(found) + "\n", encoding="utf-8")
            except OSError as exc:
                print(f"error: cannot write {args.json_file}: {exc}", file=sys.stderr)
                return 3
        print(compare_mod.to_json(found) if args.json else compare_mod.render(found), end="")
        return 1 if args.fail_on_looser and found.looser else 0

    if args.command == "e2e-decide":
        return _decide_command(
            args,
            lambda client, n: e2e_start.decide(e2e_start.fetch_inputs(client, policy, args.repo, n)),
            e2e_start.render_markdown,
            e2e_start.to_json,
            "e2e-decide",
        )

    if args.command == "e2e-compare":
        try:
            found_e2e = e2e_compare_mod.compare(
                build_client(),
                policy,
                args.repo,
                generated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
                branch=args.branch,
                include_drafts=args.include_drafts,
                limit=args.limit,
                workers=args.workers,
            )
        except (GitHubError, ValueError) as exc:
            print(f"error: e2e-compare failed: {exc}", file=sys.stderr)
            return 3
        if args.json_file:
            try:
                args.json_file.write_text(e2e_compare_mod.to_json(found_e2e) + "\n", encoding="utf-8")
            except OSError as exc:
                print(f"error: cannot write {args.json_file}: {exc}", file=sys.stderr)
                return 3
        print(e2e_compare_mod.to_json(found_e2e) if args.json else e2e_compare_mod.render(found_e2e), end="")
        return 0

    if args.command == "replay":
        try:
            report = replay(
                build_client(),
                policy,
                args.repo,
                now=datetime.now(UTC),
                days=args.days,
                limit=args.limit,
                org=args.org,
                explained=load_explained(args.explained),
                lookup_membership=not args.no_membership_lookup,
                at=args.at,
            )
        except (GitHubError, ValueError, OSError) as exc:
            print(f"error: replay failed: {exc}", file=sys.stderr)
            return 3
        print(to_json(report) if args.json else render(report), end="")
        return 1 if report.unexplained else 0

    if args.command == "explain":
        try:
            snapshot = snapshot_from_dict(json.loads(args.snapshot.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as exc:
            print(f"error: cannot load snapshot: {exc}", file=sys.stderr)
            return 2
    else:
        try:
            snapshot = fetch_snapshot(
                build_client(),
                args.repo,
                args.number,
                org=args.org or args.repo.split("/", 1)[0],
                lookup_membership=not args.no_membership_lookup,
                approval=policy.approval,
                protected=policy.protected_paths,
                override=policy.override,
                org_client=build_org_client(),
                trust=policy.trust,
            )
        except (GitHubError, ValueError) as exc:
            print(f"error: cannot read the PR: {exc}", file=sys.stderr)
            return 3

    print(render_markdown(plan_or_error(snapshot, policy, Mode(args.mode))), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
