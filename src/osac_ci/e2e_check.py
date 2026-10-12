"""E2E status: the one required ``E2E`` status of a pull request, kept by a single workflow from the first push to the
result.

The full-install E2E starts from a workflow that runs on the default branch, after the pull request is approved on
its latest commit (CI runs in parallel and is not waited for). Its jobs are therefore not on the pull request, and a
required check that has not reported blocks the merge without saying why. This module keeps ONE commit status named
``E2E`` on the pull request's head commit: what the E2E is waiting for, then that it is running, then how it ended (or
that the change does not affect any suite). A commit status, not a check run: the pull request page lists statuses, but
not check runs that a workflow writes through the API (they hang on an unrelated workflow's check group and are not
drawn). The newest status of a context counts, so a status that was left behind never blocks a newer result, and
nothing has to be patched.

Two operations, both driven by the answer of ``e2e-decide`` (``e2e_start``):

* ``report`` shows what the answer means and, for ``start`` only, claims the E2E for this workflow run: it answers
  ``start=true`` when this run may start the suites. A commit gets its E2E once: a run that is alive, or a result that
  is already there, means no second start. A run that died without leaving a result is replaced and the E2E starts
  again. A second attempt of the SAME workflow run (the Actions page "re-run") starts again.
* ``finish`` records the result of the suites.

The workflow run that owns a status is read from its link (``.../actions/runs/<id>``). Only statuses created by the
GitHub Actions bot are read. ``plan`` decides and does no I/O; ``report`` and ``finish`` read and write through the
client.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from osac_ci.github.api import GitHubClient, GitHubError, check_repo, paginate
from osac_ci.github.snapshot import COMMIT_SHA

CONTEXT = "E2E"
CREATOR = "github-actions[bot]"  # who a workflow's built-in token writes statuses as
CHECK_APP = "osac-ci"  # the GitHub App whose check runs are read when the kind is "checks"
KINDS = ("statuses", "checks")  # where the E2E lines are written: commit statuses, or check runs of the app
DESCRIPTION_MAX = 140  # what GitHub keeps of a status description
WAITING_ID = "waiting"  # external id of a status that waits
NOT_AFFECTED_ID = "not-affected"
RUN_PREFIX = "run:"  # external id of a status that belongs to a workflow run: "run:<run id>"
RUNNING_PREFIX = "Running"  # a pending status that starts like this is the claim of a run
NOT_AFFECTED_TITLE = "Not affected by this change"
RESULTS = ("success", "failure", "cancelled")
STATES = ("pending", "success", "failure", "error")  # what GitHub accepts for a status
RUN_IN_URL = re.compile(r"/actions/runs/(\d+)")
SUITE_LABELS = {"vmaas": "VMaaS", "caas": "CaaS", "bmaas": "BMaaS"}  # the suites that have a line of their own

WAITING_TITLES = {
    "skip-draft": "Waiting: the pull request is a draft",
    "wait-approval": "Waiting for an approval",
    "blocked-changes-requested": "Waiting: changes were requested",
    "blocked-cheap-checks-failed": "Waiting: CI failed",
}


class Phase(StrEnum):
    WAITING = "waiting"
    RUNNING = "running"
    DONE = "done"


@dataclass(frozen=True)
class Existing:
    """The newest ``E2E`` status of the GitHub Actions bot on a commit."""

    state: str  # pending | success | failure | error
    description: str
    external_id: str  # waiting | not-affected | run:<id> | "" (see ``existing_from``)

    @property
    def phase(self) -> Phase:
        if self.state != "pending":
            return Phase.DONE
        return Phase.RUNNING if self.external_id.startswith(RUN_PREFIX) else Phase.WAITING

    @property
    def run_id(self) -> int | None:
        """The workflow run that owns this status, when it names one."""
        tail = self.external_id[len(RUN_PREFIX) :] if self.external_id.startswith(RUN_PREFIX) else ""
        return int(tail) if tail.isdigit() else None


@dataclass(frozen=True)
class Write:
    """One status to write. ``link`` points it at the workflow run that is writing it."""

    state: str  # pending | success | failure | error
    description: str
    link: bool = True


@dataclass(frozen=True)
class Outcome:
    state: str  # waiting | running | not-affected | already-running | already-ran | ignored
    start: bool
    writes: tuple[Write, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Report:
    sha: str
    number: int | None
    state: str
    start: bool
    suites: tuple[str, ...]
    note: str


def suite_context(suite: str) -> str:
    """The status of one suite, next to ``E2E``: ``E2E / VMaaS``."""
    if suite not in SUITE_LABELS:
        raise ValueError(f"unknown suite {suite!r}; expected one of {', '.join(SUITE_LABELS)}")
    return f"{CONTEXT} / {SUITE_LABELS[suite]}"


def describe(text: str) -> str:
    """A status description is one line of at most 140 characters; a longer one is cut with an ellipsis."""
    one = " ".join(text.split())
    return one if len(one) <= DESCRIPTION_MAX else one[: DESCRIPTION_MAX - 1].rstrip() + "…"


# deciding -------------------------------------------------------------------------------------------------------


def _suites(entry: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(s) for s in entry.get("suites") or ())


def waiting_description(entry: Mapping[str, Any]) -> str:
    """What the E2E waits for, in the 140 characters a status has: the title, then the reason. The decision's whole
    explanation is in the job summary of the run the status links to."""
    action = str(entry.get("action", ""))
    title = WAITING_TITLES.get(action, f"Waiting: {action}")
    reason = str(entry.get("reason", "")).strip()
    return describe(f"{title}: {reason}" if reason else title)


def _waiting(entry: Mapping[str, Any], existing: Existing | None, alive: Callable[[int], bool]) -> Outcome:
    write = Write("pending", waiting_description(entry))
    if existing is None:
        return Outcome("waiting", False, (write,))
    if existing.phase is Phase.WAITING:
        if existing.description == write.description:
            return Outcome("waiting", False)
        return Outcome("waiting", False, (write,))
    if existing.phase is Phase.RUNNING:
        if existing.run_id is not None and alive(existing.run_id):
            return Outcome("already-running", False, note=f"run {existing.run_id} is running it")
        return Outcome("waiting", False, (write,))
    return Outcome("already-ran", False, note="a result is already on this commit")


def _claim(entry: Mapping[str, Any]) -> Write:
    suites = _suites(entry)
    return Write("pending", describe(f"{RUNNING_PREFIX}: {', '.join(suites) or 'the full-install suites'}"))


def _start(entry: Mapping[str, Any], existing: Existing | None, run_id: int, alive: Callable[[int], bool]) -> Outcome:
    if existing is None or existing.phase is Phase.WAITING:
        return Outcome("running", True, (_claim(entry),))
    if existing.phase is Phase.RUNNING:
        if existing.run_id == run_id:
            return Outcome("running", True, note="this run already claimed it")
        if existing.run_id is not None and alive(existing.run_id):
            return Outcome("already-running", False, note=f"run {existing.run_id} is running it")
        return Outcome("running", True, (_claim(entry),), note=f"run {existing.run_id} ended without a result")
    if existing.run_id == run_id:  # the same workflow run, started again from the Actions page
        return Outcome("running", True, (_claim(entry),), note="re-run of the same workflow run")
    return Outcome(
        "already-ran",
        False,
        note="the E2E already ran on this commit; re-run it from its workflow run, or push a new commit",
    )


def _not_affected(existing: Existing | None) -> Outcome:
    if existing is not None and existing.phase is not Phase.WAITING:
        return Outcome("not-affected", False, note="the status already has its answer")
    return Outcome("not-affected", False, (Write("success", NOT_AFFECTED_TITLE),))


def plan(entry: Mapping[str, Any], existing: Existing | None, run_id: int, alive: Callable[[int], bool]) -> Outcome:
    """What to do for one decision, given the newest ``E2E`` status on the commit. ``alive`` says whether a workflow
    run is still going; it is only asked when the answer depends on it."""
    action = str(entry.get("action", ""))
    if action == "skip-closed":
        return Outcome("ignored", False, note="the pull request is closed")
    if action == "skip-not-applicable":
        return _not_affected(existing)
    if action == "start":
        return _start(entry, existing, run_id, alive)
    return _waiting(entry, existing, alive)


# reading and writing GitHub ---------------------------------------------------------------------------------------


def check_sha(sha: str) -> str:
    """A commit id ends up in a URL path and in a request body: only 40 lower-case hex digits, nothing around them (a
    trailing newline would pass ``match``, so the whole string has to match)."""
    if not COMMIT_SHA.fullmatch(sha):
        raise ValueError(f"invalid commit id: {sha!r}")
    return sha


def run_of_url(url: str) -> int | None:
    """The workflow run a link points into (a run page or one of its jobs)."""
    found = RUN_IN_URL.search(url or "")
    return int(found.group(1)) if found else None


def newest_status(
    client: GitHubClient, repo: str, sha: str, context: str, kind: str = "statuses"
) -> Mapping[str, Any] | None:
    """The newest line of this name that this workflow side wrote on the commit, as a status-shaped mapping (``state``,
    ``description``, ``target_url``). Statuses: those of the GitHub Actions bot (GitHub lists them newest first). Check
    runs: those of the OSAC CI app, the newest by id; an in-progress run is ``pending``, a finished one ``success``,
    ``failure`` or ``error`` (a cancelled one)."""
    if kind == "checks":
        runs = paginate(
            client,
            f"/repos/{check_repo(repo)}/commits/{check_sha(sha)}/check-runs",
            key="check_runs",
            params={"check_name": context, "filter": "all"},
        )
        mine = [r for r in runs if r.get("name") == context and (r.get("app") or {}).get("slug") == CHECK_APP]
        if not mine:
            return None
        run = max(mine, key=lambda r: int(r["id"]))
        done = str(run.get("status")) == "completed"
        state = (
            {"success": "success", "failure": "failure"}.get(str(run.get("conclusion")), "error") if done else "pending"
        )
        return {
            "state": state,
            "description": str((run.get("output") or {}).get("title") or ""),
            "target_url": run.get("details_url") or "",
        }
    statuses = paginate(client, f"/repos/{check_repo(repo)}/commits/{check_sha(sha)}/statuses")
    for status in statuses:
        if status.get("context") == context and (status.get("creator") or {}).get("login") == CREATOR:
            return status  # type: ignore[no-any-return]
    return None


def existing_from(status: Mapping[str, Any]) -> Existing:
    """What ``plan`` needs to know about a status, read back from what was written: a pending status that says
    "Running" is the claim of the run its link names; any other pending one waits; the success that says "not
    affected" is that answer; any other finished one is the result of the run its link names."""
    state = str(status.get("state", ""))
    description = str(status.get("description") or "")
    run = run_of_url(str(status.get("target_url") or ""))
    if state == "pending":
        external = f"{RUN_PREFIX}{run}" if run is not None and description.startswith(RUNNING_PREFIX) else WAITING_ID
    elif state == "success" and description == NOT_AFFECTED_TITLE:
        external = NOT_AFFECTED_ID
    else:
        external = f"{RUN_PREFIX}{run}" if run is not None else ""
    return Existing(state, description, external)


def latest_check(client: GitHubClient, repo: str, sha: str, kind: str = "statuses") -> Existing | None:
    status = newest_status(client, repo, sha, CONTEXT, kind)
    return existing_from(status) if status is not None else None


def run_is_alive(client: GitHubClient, repo: str, run_id: int) -> bool:
    response = client.request("GET", f"/repos/{check_repo(repo)}/actions/runs/{run_id}")
    if response.status == 404:
        return False
    if not 200 <= response.status < 300 or not isinstance(response.data, dict):
        raise GitHubError(response.status, f"GET the workflow run {run_id}")
    return str(response.data.get("status")) != "completed"


def post_status(
    client: GitHubClient,
    repo: str,
    sha: str,
    context: str,
    state: str,
    description: str,
    target_url: str = "",
    kind: str = "statuses",
) -> None:
    """Write one line. Statuses: a commit status. Check runs: a check run of the token's app (the OSAC CI app), in
    progress for ``pending`` (the page draws a spinner), else completed with ``success``, ``failure`` or ``cancelled``;
    the newest of a name counts, so nothing is patched."""
    if state not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)}, not {state!r}")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
    text = describe(description)
    if kind == "checks":
        body: dict[str, Any] = {
            "name": context,
            "head_sha": check_sha(sha),
            "output": {"title": text, "summary": text + (f"\n\n[Details]({target_url})" if target_url else "")},
        }
        if state == "pending":
            body["status"] = "in_progress"
        else:
            body["status"] = "completed"
            body["conclusion"] = {"success": "success", "failure": "failure", "error": "cancelled"}[state]
        if target_url:
            body["details_url"] = target_url
        response = client.request("POST", f"/repos/{check_repo(repo)}/check-runs", body=body)
        what = "check"
    else:
        body = {"state": state, "context": context, "description": text}
        if target_url:
            body["target_url"] = target_url
        response = client.request("POST", f"/repos/{check_repo(repo)}/statuses/{check_sha(sha)}", body=body)
        what = "status"
    if response.status not in (200, 201):
        message = response.data.get("message", "") if isinstance(response.data, dict) else ""
        raise GitHubError(response.status, f"writing the {context} {what} failed: {message}")


def write_status(client: GitHubClient, repo: str, sha: str, write: Write, run_url: str, kind: str = "statuses") -> None:
    post_status(client, repo, sha, CONTEXT, write.state, write.description, run_url if write.link else "", kind)


def report(
    client: GitHubClient,
    repo: str,
    entries: Sequence[Mapping[str, Any]],
    run_id: int,
    run_url: str,
    kind: str = "statuses",
) -> Report:
    """Show the answer of ``e2e-decide`` on the pull request and, for ``start``, claim the E2E for this run. Nothing
    is done unless exactly one pull request was decided."""
    if not all(isinstance(e, Mapping) for e in entries):
        raise ValueError("every decision must be an object")
    if len(entries) != 1:
        note = "no open pull request has this head commit" if not entries else "several pull requests have this head"
        return Report("", None, "ignored", False, (), note)
    entry = entries[0]
    sha = str(entry["head_sha"])
    existing = latest_check(client, repo, sha, kind)
    outcome = plan(entry, existing, run_id, lambda rid: run_is_alive(client, repo, rid))
    for write in outcome.writes:
        write_status(client, repo, sha, write, run_url, kind)
    number = int(entry["number"])
    if outcome.start:
        # Two runs for the same commit can both have read "no claim yet". Look again after writing: if the newest status
        # is not this run's, another run claimed the E2E in between and this one must not start the suites. The calling
        # workflow must also run one decision at a time per commit; this look only narrows the window.
        newest = latest_check(client, repo, sha, kind)
        if newest is None or newest.run_id != run_id:
            other = f"run {newest.run_id}" if newest is not None and newest.run_id else "another run"
            return Report(sha, number, "already-running", False, _suites(entry), f"{other} claimed the E2E first")
        # The suite lines appear together with E2E, not a job start later (and replace a result left by an earlier
        # attempt of this run). The tracking job keeps them current from here on.
        for suite in _suites(entry):
            if suite in SUITE_LABELS:
                post_status(client, repo, sha, suite_context(suite), "pending", "Waiting for a runner", run_url, kind)
    return Report(sha, number, outcome.state, outcome.start, _suites(entry), outcome.note)


def finish(
    client: GitHubClient, repo: str, sha: str, result: str, run_url: str, detail: str = "", kind: str = "statuses"
) -> None:
    """Record how the suites ended."""
    if result not in RESULTS:
        raise ValueError(f"result must be one of {', '.join(RESULTS)}, not {result!r}")
    title = {"success": "Passed", "failure": "Failed", "cancelled": "Cancelled"}[result]
    state = {"success": "success", "failure": "failure", "cancelled": "error"}[result]
    write_status(client, repo, sha, Write(state, f"{title}: {detail}" if detail else title), run_url, kind)


# rendering ------------------------------------------------------------------------------------------------------


def render(found: Report) -> str:
    if found.state == "ignored":
        return f"### E2E status: nothing to do\n\n{found.note}\n"
    lines = [f"### E2E status: **{found.state}**" + (" (starting the suites)" if found.start else ""), ""]
    if found.suites:
        lines.append(f"- suites: {', '.join(found.suites)}")
    lines.append(f"- commit: `{found.sha[:7]}`")
    if found.note:
        lines.append(f"- {found.note}")
    return "\n".join(lines) + "\n"


def to_json(found: Report) -> str:
    return json.dumps(
        {
            "sha": found.sha,
            "number": found.number,
            "state": found.state,
            "start": found.start,
            "suites": list(found.suites),
            "note": found.note,
        },
        indent=2,
    )
