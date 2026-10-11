"""One check per E2E suite on the pull request, next to the single ``E2E`` check.

The suites run in a workflow of the default branch, so their jobs are not on the pull request. Each suite that runs
gets a check named ``E2E / <Suite>`` on the pull request's head commit that follows its job: waiting for a runner,
running, then the result, with a link to the job's logs. The required check stays ``E2E`` (see ``e2e_check``); these are
for the person looking at the pull request, who sees which suite is running or failed and gets to the log in one click.

``track`` creates the checks when the suites are about to start and keeps them current until every job has a runner.
``settle`` completes them with the result of each suite job. Only check runs of the GitHub Actions app with these names
are read or changed. A suite that does not run has no check.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from osac_ci.e2e_check import APP_SLUG, RESULTS, RUN_PREFIX, Write, _check_sha, write_check
from osac_ci.github.api import GitHubClient, check_repo, paginate

LABELS = {"vmaas": "VMaaS", "caas": "CaaS", "bmaas": "BMaaS"}
JOB_LINK = "Open the job logs"
RUN_LINK = "Open the workflow run"


def check_name(suite: str) -> str:
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


def _latest(client: GitHubClient, repo: str, sha: str, name: str) -> int | None:
    runs = paginate(
        client,
        f"/repos/{check_repo(repo)}/commits/{_check_sha(sha)}/check-runs",
        key="check_runs",
        params={"check_name": name, "filter": "all"},
    )
    mine = [r for r in runs if r.get("name") == name and (r.get("app") or {}).get("slug") == APP_SLUG]
    return max((int(r["id"]) for r in mine), default=None)


def _state(job: Job | None) -> tuple[str, str]:
    if job is None or job.status == "queued":
        return "Waiting for a runner", "The job is queued until a runner is free."
    return "Running", "The suite is running."


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
) -> dict[str, int]:
    """Create a check for each suite and update it until every suite's job is running (or the time is up). Returns the
    check id of each suite."""
    sha = _check_sha(sha)
    names = {suite: check_name(suite) for suite in suites}
    ids: dict[str, int] = {}
    shown: dict[str, tuple[tuple[str, str], str]] = {}
    deadline = clock() + timeout
    while True:
        jobs = find_jobs(client, repo, run_id, suites)
        for suite in suites:
            job = jobs.get(suite)
            state = _state(job)
            if shown.get(suite) == (state, job.url if job else ""):
                continue
            title, summary = state
            write = Write(ids.get(suite), "in_progress", None, title, summary, f"{RUN_PREFIX}{run_id}", link=True)
            url, label = (job.url, JOB_LINK) if job else (run_url, RUN_LINK)
            ids[suite] = write_check(client, repo, sha, write, url, name=names[suite], link_label=label)
            shown[suite] = (state, job.url if job else "")
        if all(s in jobs and jobs[s].status != "queued" for s in suites) or clock() >= deadline:
            return ids
        sleep(interval)


def settle(
    client: GitHubClient, repo: str, sha: str, run_id: int, run_url: str, results: Mapping[str, str]
) -> dict[str, int]:
    """Complete each suite's check with the result of its job. A suite without a check gets a completed one."""
    sha = _check_sha(sha)
    for suite, result in results.items():
        check_name(suite)
        if result not in RESULTS:
            raise ValueError(f"result of {suite} must be one of {', '.join(RESULTS)}, not {result!r}")
    jobs = find_jobs(client, repo, run_id, list(results))
    done: dict[str, int] = {}
    for suite, result in results.items():
        name = check_name(suite)
        title = {"success": "Passed", "failure": "Failed", "cancelled": "Cancelled"}[result]
        summary = {
            "success": "The suite passed.",
            "failure": "The suite failed. The job log shows where.",
            "cancelled": "The run was cancelled before the suite finished.",
        }[result]
        job = jobs.get(suite)
        write = Write(
            _latest(client, repo, sha, name), "completed", result, title, summary, f"{RUN_PREFIX}{run_id}", link=True
        )
        url, label = (job.url, JOB_LINK) if job else (run_url, RUN_LINK)
        done[suite] = write_check(client, repo, sha, write, url, name=name, link_label=label)
    return done


def parse_results(items: Sequence[str]) -> dict[str, str]:
    """``vmaas=success`` pairs, as the command line gives them."""
    out: dict[str, str] = {}
    for item in items:
        suite, sep, result = item.partition("=")
        if not sep:
            raise ValueError(f"expected suite=result, got {item!r}")
        out[suite] = result
    return out
