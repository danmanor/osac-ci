"""One line per E2E suite on the pull request, next to the single ``E2E`` line.

The suites run in a workflow of the default branch, so their jobs are not on the pull request. Each suite that runs
gets a line on the pull request's head commit that follows its job: waiting for a runner, running, then the result,
with a link straight to the job's logs. The required line stays ``E2E`` (see ``e2e_check``); these are for the person
looking at the pull request, who sees which suite is running or failed and gets to the log in one click.

Which suites exist, and what their lines are called, is the repository's policy and not this program's: the decision
(``e2e_start``) carries, for each suite it picks, the ``check`` name of its job in the policy, and ``track`` and
``settle`` take that mapping (suite id -> line name). A new suite is a policy entry and a workflow; nothing changes
here. The test job is the deepest matching job whose final name segment is ``e2e`` (for example,
``vmaas / ... / e2e``). Until it appears, an active setup job can show progress, but a completed setup job is not a
suite result. A skipped test job must not fall back to a successful setup job.

``track`` writes the lines when the suites are about to start and keeps them current until every job has a runner.
``settle`` completes them with the result of each suite's job, read from the jobs of the run; the workflow must run
it after ``track`` has ended (a job that needs the tracking job), or a late tracking write could reopen a finished
suite. A line that belongs to a newer run is never overwritten. A suite that does not run has no line.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from osac_ci.e2e_check import check_sha, newest_status, post_status, run_of_url, validate_lines
from osac_ci.github.api import GitHubClient, check_repo, paginate


@dataclass(frozen=True)
class Job:
    url: str
    status: str  # queued | in_progress | completed
    conclusion: str = ""  # "" until completed


def parse_lines(text: str) -> dict[str, str]:
    """The suite -> line mapping, as the command line gives it: a JSON object."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"the suite checks must be a JSON object: {exc}") from exc
    return validate_lines(raw)


def find_jobs(client: GitHubClient, repo: str, run_id: int, suites: list[str]) -> dict[str, Job]:
    """Find each suite's test job, using active setup jobs for progress until the test job appears.

    The test job is the deepest matching job whose final name segment is ``e2e``. A calling workflow can hold jobs for
    other suites that are skipped for this one; those do not count. Completed setup jobs cannot stand in for a test
    result, and a skipped test job cannot fall back to a successful setup job.
    """
    best_e2e: dict[str, tuple[int, Job]] = {}
    best_active: dict[str, tuple[int, Job]] = {}
    skipped_e2e: set[str] = set()
    jobs = paginate(
        client, f"/repos/{check_repo(repo)}/actions/runs/{int(run_id)}/jobs", key="jobs", params={"filter": "latest"}
    )
    for job in jobs:
        name = str(job.get("name", ""))
        for suite in suites:
            if name == suite or name.startswith(f"{suite} / "):
                depth = name.count(" / ")
                is_e2e = name.rsplit(" / ", 1)[-1] == "e2e"
                if job.get("conclusion") == "skipped":
                    if is_e2e:
                        skipped_e2e.add(suite)
                    continue
                if is_e2e:
                    best = best_e2e
                else:
                    if job.get("status") not in {"queued", "in_progress"}:
                        continue
                    best = best_active
                if suite not in best or depth > best[suite][0]:
                    best[suite] = (
                        depth,
                        Job(
                            str(job.get("html_url", "")),
                            str(job.get("status", "")),
                            str(job.get("conclusion") or ""),
                        ),
                    )
    selected = {suite: job for suite, (_, job) in best_e2e.items()}
    selected.update(
        {suite: job for suite, (_, job) in best_active.items() if suite not in selected and suite not in skipped_e2e}
    )
    return selected


def _state(job: Job | None, *, final: bool = False) -> tuple[str, str]:
    """The line for a job: waiting or running. With ``final`` a job that has ended gets its result."""
    if job is None or job.status == "queued":
        return "Waiting for a runner", "waiting"
    if final and job.status == "completed":
        result = _result(job)
        return RESULT_TITLES[result], RESULT_STATES[result]
    return "Running", "pending"


RESULT_TITLES = {"success": "Passed", "failure": "Failed", "cancelled": "Cancelled"}
RESULT_STATES = {"success": "success", "failure": "failure", "cancelled": "error"}


def _result(job: Job | None) -> str:
    """How a suite ended, from its job: success, failure or cancelled (also with no job, or when the run stopped)."""
    if job is None or job.status != "completed":
        return "cancelled"
    return {"success": "success", "cancelled": "cancelled"}.get(job.conclusion, "failure")


def track(
    client: GitHubClient,
    repo: str,
    sha: str,
    run_id: int,
    run_url: str,
    lines: Mapping[str, str],
    *,
    timeout: float = 7200,
    interval: float = 30,
    kind: str = "statuses",
    follow: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[str]:
    """Write a line for each suite and update it until every suite's job is running (or the time is up). Returns the
    suites it wrote a line for. With ``follow`` it goes on until every job has ended, and each suite's line gets its
    result as soon as that suite's job ends, so a suite that passed shows as passed while the others still run."""
    sha = check_sha(sha)
    lines = validate_lines(lines)
    suites = list(lines)
    shown: dict[str, tuple[str, str]] = {}
    newer: set[str] = set()  # suites whose line a newer run has taken over: this run leaves them alone
    deadline = clock() + timeout
    while True:
        jobs = find_jobs(client, repo, run_id, suites)
        for suite in suites:
            job = jobs.get(suite)
            description, state = _state(job, final=follow)
            url = job.url if job else run_url
            if suite in newer or shown.get(suite) == (description, url):
                continue
            current = newest_status(client, repo, sha, lines[suite], kind)
            owner = run_of_url(str(current.get("target_url") or "")) if current is not None else None
            if owner is not None and owner != run_id:
                newer.add(suite)
                continue
            post_status(client, repo, sha, lines[suite], state, description, url, kind)
            shown[suite] = (description, url)
        if follow:
            finished = all(s in jobs and jobs[s].status == "completed" for s in suites)
        else:
            finished = all(s in jobs and jobs[s].status != "queued" for s in suites)
        if finished or clock() >= deadline:
            return list(shown)
        sleep(interval)


def settle(
    client: GitHubClient,
    repo: str,
    sha: str,
    run_id: int,
    run_url: str,
    lines: Mapping[str, str],
    kind: str = "statuses",
) -> list[str]:
    """Complete each suite's line with the result of its job. A line that belongs to a newer run is left alone (the
    newest line of a name is what the page shows). Returns the suites it wrote."""
    sha = check_sha(sha)
    lines = validate_lines(lines)
    jobs = find_jobs(client, repo, run_id, list(lines))
    written: list[str] = []
    for suite, name in lines.items():
        newest = newest_status(client, repo, sha, name, kind)
        owner = run_of_url(str(newest.get("target_url") or "")) if newest is not None else None
        if owner is not None and owner != run_id:
            continue
        job = jobs.get(suite)
        result = _result(job)
        post_status(
            client, repo, sha, name, RESULT_STATES[result], RESULT_TITLES[result], job.url if job else run_url, kind
        )
        written.append(suite)
    return written
