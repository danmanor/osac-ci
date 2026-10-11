"""One status per E2E suite on the pull request, next to the single ``E2E`` status.

The suites run in a workflow of the default branch, so their jobs are not on the pull request. Each suite that runs
gets a status named ``E2E / <Suite>`` on the pull request's head commit that follows its job: waiting for a runner,
running, then the result, and its link goes straight to the job's logs. The required status stays ``E2E`` (see
``e2e_check``); these are for the person looking at the pull request, who sees which suite is running or failed and
gets to the log in one click. (Statuses, not check runs: the pull request page does not draw check runs a workflow
writes through the API.)

``track`` creates the statuses when the suites are about to start and keeps them current until every job has a runner.
``settle`` completes them with the result of each suite job; the workflow must run it after ``track`` has ended (a job
that needs the tracking job), or a late tracking write could reopen a finished suite. A status that belongs to a newer
run is never overwritten. Only statuses of the GitHub Actions bot with these names are read. A suite that does not run
has no status.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from osac_ci.e2e_check import RESULTS, check_sha, newest_status, post_status, run_of_url
from osac_ci.github.api import GitHubClient, check_repo, paginate

LABELS = {"vmaas": "VMaaS", "caas": "CaaS", "bmaas": "BMaaS"}


def context(suite: str) -> str:
    if suite not in LABELS:
        raise ValueError(f"unknown suite {suite!r}; expected one of {', '.join(LABELS)}")
    return f"E2E / {LABELS[suite]}"


@dataclass(frozen=True)
class Job:
    url: str
    status: str  # queued | in_progress | completed


def find_jobs(client: GitHubClient, repo: str, run_id: int, suites: Sequence[str]) -> dict[str, Job]:
    """The job of each suite in the workflow run. The workflow names the calling job after the suite ("VMaaS"), and
    the called workflow's job is listed as "VMaaS / e2e-vmaas-full-install / e2e"."""
    found: dict[str, Job] = {}
    jobs = paginate(
        client, f"/repos/{check_repo(repo)}/actions/runs/{int(run_id)}/jobs", key="jobs", params={"filter": "latest"}
    )
    for job in jobs:
        name = str(job.get("name", ""))
        for suite in suites:
            if suite not in found and name.startswith(f"{LABELS[suite]} / "):
                found[suite] = Job(str(job.get("html_url", "")), str(job.get("status", "")))
    return found


def _state(job: Job | None) -> tuple[str, str]:
    if job is None or job.status == "queued":
        return "Waiting for a runner", "pending"
    return "Running", "pending"


def track(
    client: GitHubClient,
    repo: str,
    sha: str,
    run_id: int,
    run_url: str,
    suites: Sequence[str],
    *,
    timeout: float = 7200,
    interval: float = 30,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[str]:
    """Write a status for each suite and update it until every suite's job is running (or the time is up). Returns the
    suites it wrote a status for."""
    sha = check_sha(sha)
    names = {suite: context(suite) for suite in suites}
    shown: dict[str, tuple[str, str]] = {}
    deadline = clock() + timeout
    while True:
        jobs = find_jobs(client, repo, run_id, suites)
        for suite in suites:
            job = jobs.get(suite)
            description, state = _state(job)
            url = job.url if job else run_url
            if shown.get(suite) == (description, url):
                continue
            post_status(client, repo, sha, names[suite], state, description, url)
            shown[suite] = (description, url)
        if all(s in jobs and jobs[s].status != "queued" for s in suites) or clock() >= deadline:
            return list(shown)
        sleep(interval)


def settle(
    client: GitHubClient, repo: str, sha: str, run_id: int, run_url: str, results: Mapping[str, str]
) -> list[str]:
    """Complete each suite's status with the result of its job. A status that belongs to a newer run is left alone
    (the newest status of a name is what the page shows). Returns the suites it wrote."""
    sha = check_sha(sha)
    for suite, result in results.items():
        context(suite)
        if result not in RESULTS:
            raise ValueError(f"result of {suite} must be one of {', '.join(RESULTS)}, not {result!r}")
    jobs = find_jobs(client, repo, run_id, list(results))
    written: list[str] = []
    for suite, result in results.items():
        name = context(suite)
        newest = newest_status(client, repo, sha, name)
        owner = run_of_url(str(newest.get("target_url") or "")) if newest is not None else None
        if owner is not None and owner != run_id:
            continue
        title = {"success": "Passed", "failure": "Failed", "cancelled": "Cancelled"}[result]
        state = {"success": "success", "failure": "failure", "cancelled": "error"}[result]
        job = jobs.get(suite)
        post_status(client, repo, sha, name, state, title, job.url if job else run_url)
        written.append(suite)
    return written


def parse_results(items: Sequence[str]) -> dict[str, str]:
    """``vmaas=success`` pairs, as the command line gives them."""
    out: dict[str, str] = {}
    for item in items:
        suite, sep, result = item.partition("=")
        if not sep:
            raise ValueError(f"expected suite=result, got {item!r}")
        if suite in out:
            raise ValueError(f"suite {suite!r} is given more than once")
        out[suite] = result
    return out
