"""E2E check: the one required check named ``E2E``, kept by a single workflow from the first push to the result.

The full-install E2E starts from a workflow that runs on the default branch, after the pull request is approved on its
latest commit and CI has passed. Its jobs are therefore not on the pull request, and a required check that has not
reported blocks the merge without saying why. This module gives the pull request ONE check run named ``E2E`` and keeps
it up to date: what the E2E is waiting for, then that it is running, then how it ended (or that the change does not
affect any suite). Check runs of the same name on a commit are judged by the newest one, so an older one that was left
behind never blocks a newer result.

Two operations, both driven by the answer of ``e2e-decide`` (``e2e_start``):

* ``report`` shows what the answer means and, for ``start`` only, claims the E2E for this workflow run: it answers
  ``start=true`` when this run may start the suites. A commit gets its E2E once: a run that is alive, or a result that
  is already there, means no second start. A run that died without leaving a result is cancelled in the check and the
  E2E starts again. A second attempt of the SAME workflow run (the Actions page "re-run") starts again.
* ``finish`` records the result of the suites on the check the report claimed.

Only check runs of the GitHub Actions app named ``E2E`` are ever read or changed. ``plan`` decides and does no I/O;
``report`` and ``finish`` read and write through the client.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from osac_ci.github.api import GitHubClient, GitHubError, check_repo, paginate

CHECK_NAME = "E2E"
APP_SLUG = "github-actions"  # the app that owns the check runs of a workflow's built-in token
WAITING_ID = "waiting"  # external id of a check run that waits
NOT_AFFECTED_ID = "not-affected"
RUN_PREFIX = "run:"  # external id of a check run that belongs to a workflow run: "run:<run id>"
RESULTS = ("success", "failure", "cancelled")

HOW_IT_STARTS = (
    "The full-install E2E starts by itself once the pull request is approved on its latest commit by someone with "
    "write access (or CodeRabbit), CI has passed, and the change affects an E2E suite. A new commit starts over."
)

WAITING_TITLES = {
    "skip-draft": "Waiting: the pull request is a draft",
    "wait-approval": "Waiting for an approval",
    "blocked-changes-requested": "Waiting: changes were requested",
    "blocked-cheap-checks-failed": "Waiting: CI failed",
    "wait-cheap-checks": "Waiting for CI to finish",
}


class Phase(StrEnum):
    WAITING = "waiting"
    RUNNING = "running"
    DONE = "done"


@dataclass(frozen=True)
class Existing:
    """The newest ``E2E`` check run of the GitHub Actions app on a commit."""

    id: int
    status: str  # queued | in_progress | completed
    conclusion: str  # "" until completed
    external_id: str
    title: str
    summary: str

    @property
    def phase(self) -> Phase:
        if self.status == "completed":
            return Phase.DONE
        return Phase.RUNNING if self.external_id.startswith(RUN_PREFIX) else Phase.WAITING

    @property
    def run_id(self) -> int | None:
        """The workflow run that owns this check run, when it names one."""
        tail = self.external_id[len(RUN_PREFIX) :] if self.external_id.startswith(RUN_PREFIX) else ""
        return int(tail) if tail.isdigit() else None


@dataclass(frozen=True)
class Write:
    """One change to the check runs: create a new one (``patch_id`` None) or update an existing one."""

    patch_id: int | None
    status: str  # in_progress | completed
    conclusion: str | None
    title: str
    summary: str
    external_id: str
    link: bool = False  # point the check at the workflow run


@dataclass(frozen=True)
class Outcome:
    state: str  # waiting | running | not-affected | already-running | already-ran | ignored
    start: bool
    writes: tuple[Write, ...] = ()
    check_id: int | None = None  # the check run the answer is about when nothing has to be written for it
    note: str = ""


@dataclass(frozen=True)
class Report:
    sha: str
    number: int | None
    state: str
    start: bool
    check_id: int | None
    suites: tuple[str, ...]
    note: str


# deciding -------------------------------------------------------------------------------------------------------


def _suites(entry: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(s) for s in entry.get("suites") or ())


def waiting_text(entry: Mapping[str, Any]) -> tuple[str, str]:
    action = str(entry.get("action", ""))
    title = WAITING_TITLES.get(action, f"Waiting: {action}")
    lines = [str(entry.get("reason", "")), ""]
    if suites := _suites(entry):
        lines += [f"Suites this change affects: {', '.join(suites)}.", ""]
    if failed := entry.get("cheap_failed"):
        lines += [f"Failed: {'; '.join(str(f) for f in failed)}", ""]
    if pending := entry.get("cheap_pending"):
        lines += [f"Not finished: {'; '.join(str(p) for p in pending)}", ""]
    if stale := entry.get("stale_approvals"):
        lines += [f"Approved an older commit (does not count): {', '.join(str(s) for s in stale)}", ""]
    lines.append(HOW_IT_STARTS)
    return title, "\n".join(lines)


def _waiting(entry: Mapping[str, Any], existing: Existing | None, alive: Callable[[int], bool]) -> Outcome:
    title, summary = waiting_text(entry)
    fresh = Write(None, "in_progress", None, title, summary, WAITING_ID)
    if existing is None:
        return Outcome("waiting", False, (fresh,))
    if existing.phase is Phase.WAITING:
        if (existing.title, existing.summary) == (title, summary):
            return Outcome("waiting", False, check_id=existing.id)
        return Outcome("waiting", False, (Write(existing.id, "in_progress", None, title, summary, WAITING_ID),))
    if existing.phase is Phase.RUNNING:
        if existing.run_id is not None and alive(existing.run_id):
            return Outcome("already-running", False, check_id=existing.id, note=f"run {existing.run_id} is running it")
        return Outcome("waiting", False, (_cancel_dead(existing), fresh))
    return Outcome("already-ran", False, check_id=existing.id, note="a result is already on this commit")


def _cancel_dead(existing: Existing) -> Write:
    return Write(
        existing.id,
        "completed",
        "cancelled",
        "Cancelled: the run ended without a result",
        f"Run {existing.run_id or 'unknown'} stopped before it reported a result.",
        existing.external_id,
        link=True,
    )


def _claim(entry: Mapping[str, Any], patch_id: int | None, run_id: int) -> Write:
    suites = _suites(entry)
    return Write(
        patch_id,
        "in_progress",
        None,
        f"Running: {', '.join(suites) or 'the full-install suites'}",
        f"Approved by {', '.join(entry.get('approvers') or ()) or 'a reviewer'} and CI passed. The suites run in the "
        "workflow run this check links to.",
        f"{RUN_PREFIX}{run_id}",
        link=True,
    )


def _start(entry: Mapping[str, Any], existing: Existing | None, run_id: int, alive: Callable[[int], bool]) -> Outcome:
    if existing is None:
        return Outcome("running", True, (_claim(entry, None, run_id),))
    if existing.phase is Phase.WAITING:
        return Outcome("running", True, (_claim(entry, existing.id, run_id),))
    if existing.phase is Phase.RUNNING:
        if existing.run_id == run_id:
            return Outcome("running", True, check_id=existing.id, note="this run already claimed it")
        if existing.run_id is not None and alive(existing.run_id):
            return Outcome("already-running", False, check_id=existing.id, note=f"run {existing.run_id} is running it")
        return Outcome("running", True, (_cancel_dead(existing), _claim(entry, None, run_id)))
    if existing.run_id == run_id:  # the same workflow run, started again from the Actions page
        return Outcome("running", True, (_claim(entry, None, run_id),), note="re-run of the same workflow run")
    return Outcome(
        "already-ran",
        False,
        check_id=existing.id,
        note="the E2E already ran on this commit; re-run it from its workflow run, or push a new commit",
    )


def _not_affected(existing: Existing | None) -> Outcome:
    write = Write(
        existing.id if existing is not None and existing.phase is Phase.WAITING else None,
        "completed",
        "success",
        "Not affected by this change",
        "No E2E suite is affected by the changed files, so there is nothing to wait for.",
        NOT_AFFECTED_ID,
    )
    if existing is not None and existing.phase is not Phase.WAITING:
        return Outcome("not-affected", False, check_id=existing.id, note="the check already has its answer")
    return Outcome("not-affected", False, (write,))


def plan(entry: Mapping[str, Any], existing: Existing | None, run_id: int, alive: Callable[[int], bool]) -> Outcome:
    """What to do for one decision, given the newest ``E2E`` check run on the commit. ``alive`` says whether a
    workflow run is still going; it is only asked when the answer depends on it."""
    action = str(entry.get("action", ""))
    if action == "skip-closed":
        return Outcome("ignored", False, note="the pull request is closed")
    if action == "skip-not-applicable":
        return _not_affected(existing)
    if action == "start":
        return _start(entry, existing, run_id, alive)
    return _waiting(entry, existing, alive)


# reading and writing GitHub ---------------------------------------------------------------------------------------


def latest_check(client: GitHubClient, repo: str, sha: str) -> Existing | None:
    runs = paginate(
        client,
        f"/repos/{check_repo(repo)}/commits/{sha}/check-runs",
        key="check_runs",
        params={"check_name": CHECK_NAME, "filter": "all"},
    )
    mine = [r for r in runs if r.get("name") == CHECK_NAME and (r.get("app") or {}).get("slug") == APP_SLUG]
    if not mine:
        return None
    newest = max(mine, key=lambda r: int(r["id"]))
    output = newest.get("output") or {}
    return Existing(
        id=int(newest["id"]),
        status=str(newest.get("status", "")),
        conclusion=str(newest.get("conclusion") or ""),
        external_id=str(newest.get("external_id") or ""),
        title=str(output.get("title") or ""),
        summary=str(output.get("summary") or ""),
    )


def run_is_alive(client: GitHubClient, repo: str, run_id: int) -> bool:
    response = client.request("GET", f"/repos/{check_repo(repo)}/actions/runs/{run_id}")
    if response.status == 404:
        return False
    if not 200 <= response.status < 300 or not isinstance(response.data, dict):
        raise GitHubError(response.status, f"GET the workflow run {run_id}")
    return str(response.data.get("status")) != "completed"


def _write(client: GitHubClient, repo: str, sha: str, write: Write, run_url: str) -> int:
    body: dict[str, Any] = {
        "status": write.status,
        "external_id": write.external_id,
        "output": {"title": write.title, "summary": write.summary},
    }
    if write.conclusion:
        body["conclusion"] = write.conclusion
    if write.link and run_url:
        body["details_url"] = run_url
    base = f"/repos/{check_repo(repo)}/check-runs"
    if write.patch_id is None:
        response = client.request("POST", base, body={**body, "name": CHECK_NAME, "head_sha": sha})
    else:
        response = client.request("PATCH", f"{base}/{write.patch_id}", body=body)
    if response.status not in (200, 201) or not isinstance(response.data, dict):
        message = response.data.get("message", "") if isinstance(response.data, dict) else ""
        raise GitHubError(response.status, f"writing the {CHECK_NAME} check failed: {message}")
    return int(response.data["id"])


def report(client: GitHubClient, repo: str, entries: Sequence[Mapping[str, Any]], run_id: int, run_url: str) -> Report:
    """Show the answer of ``e2e-decide`` on the pull request and, for ``start``, claim the E2E for this run. Nothing
    is done unless exactly one pull request was decided."""
    if not all(isinstance(e, Mapping) for e in entries):
        raise ValueError("every decision must be an object")
    if len(entries) != 1:
        note = "no open pull request has this head commit" if not entries else "several pull requests have this head"
        return Report("", None, "ignored", False, None, (), note)
    entry = entries[0]
    sha = str(entry["head_sha"])
    existing = latest_check(client, repo, sha)
    outcome = plan(entry, existing, run_id, lambda rid: run_is_alive(client, repo, rid))
    check_id = outcome.check_id
    for write in outcome.writes:
        check_id = _write(client, repo, sha, write, run_url)
    return Report(sha, int(entry["number"]), outcome.state, outcome.start, check_id, _suites(entry), outcome.note)


def finish(
    client: GitHubClient,
    repo: str,
    sha: str,
    check_id: int | None,
    result: str,
    run_id: int,
    run_url: str,
    detail: str = "",
) -> int:
    """Record how the suites ended on the check run the report claimed (a new completed one when there is none)."""
    if result not in RESULTS:
        raise ValueError(f"result must be one of {', '.join(RESULTS)}, not {result!r}")
    title = {"success": "Passed", "failure": "Failed", "cancelled": "Cancelled"}[result]
    summary = (
        detail
        or {
            "success": "Every suite that ran passed.",
            "failure": "A suite failed. Re-run it from the workflow run this check links to.",
            "cancelled": "The run was cancelled before it finished.",
        }[result]
    )
    write = Write(check_id, "completed", result, title, summary, f"{RUN_PREFIX}{run_id}", link=True)
    return _write(client, repo, sha, write, run_url)


# rendering ------------------------------------------------------------------------------------------------------


def render(found: Report) -> str:
    if found.state == "ignored":
        return f"### E2E check: nothing to do\n\n{found.note}\n"
    lines = [f"### E2E check: **{found.state}**" + (" (starting the suites)" if found.start else ""), ""]
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
            "check_id": found.check_id,
            "suites": list(found.suites),
            "note": found.note,
        },
        indent=2,
    )
