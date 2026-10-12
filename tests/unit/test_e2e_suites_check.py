"""The suite lines: written when the suites are about to start, kept current while the jobs wait for a runner and run,
completed with the result of each suite's job. Which suites exist and what their lines are called comes from the policy
(the decision), never from this program. Kept by a small in-memory GitHub: the statuses of one commit and the jobs of
one workflow run."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from helpers import ROOT
from test_e2e_check import BASE, RUN, SHA, World

from osac_ci import cli
from osac_ci.e2e_suites_check import find_jobs, parse_lines, settle, track
from osac_ci.github.api import Response

pytestmark = pytest.mark.unit
REPO = "o/r"
RUN_URL = "https://example.invalid/o/r/actions/runs/500"
NEWER_RUN_URL = "https://example.invalid/o/r/actions/runs/501/job/9"
VM = "E2E / VMaaS Full Install"
CA = "E2E / CaaS Full Install"
NET = "E2E / Netris Fabric"
LINES = {"vmaas": VM, "caas": CA}


class SuiteWorld(World):
    def __init__(self) -> None:
        super().__init__()
        self.jobs: list[dict[str, Any]] = []

    def job(self, name: str, status: str, conclusion: str | None = None) -> None:
        self.jobs = [j for j in self.jobs if j["name"] != name]
        self.jobs.append(
            {
                "name": name,
                "status": status,
                "conclusion": conclusion,
                "html_url": f"https://example.invalid/o/r/actions/runs/500/job/{name.split()[0]}",
            }
        )

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path == f"{BASE}/actions/runs/{RUN}/jobs":
            page, per = int((params or {}).get("page", 1)), int((params or {}).get("per_page", 100))
            return Response(200, {"jobs": self.jobs[(page - 1) * per : page * per]})
        return super().request(method, path, params=params, body=body)

    def line(self, name: str) -> dict[str, Any]:
        return self.newest(name)


def fast(world: SuiteWorld, lines: Mapping[str, str], **kw: Any) -> list[str]:
    # a clock that moves one second per look, and no real sleeping
    ticks = iter(range(10_000))
    return track(
        world, REPO, SHA, RUN, RUN_URL, lines,
        interval=1, sleep=lambda _: None, clock=lambda: float(next(ticks)), **kw,
    )  # fmt: skip


# which job is a suite's ----------------------------------------------------------------------------------------


def test_the_job_of_a_suite_is_found_by_the_suite_id_and_the_deepest_one_that_ran_counts() -> None:
    world = SuiteWorld()
    world.job("vmaas", "queued")
    world.job("vmaas / dispatch", "in_progress")
    world.job("vmaas / dispatch / e2e", "in_progress")
    world.job("caas / dispatch / e2e", "queued")
    jobs = find_jobs(world, REPO, RUN, ["vmaas", "caas"])
    assert jobs["vmaas"].url.endswith("/job/vmaas") and jobs["vmaas"].status == "in_progress"
    assert jobs["caas"].status == "queued"


def test_a_skipped_job_of_another_suite_in_the_same_caller_does_not_count() -> None:
    world = SuiteWorld()
    world.job("caas / dispatch / e2e", "completed", "skipped")
    assert find_jobs(world, REPO, RUN, ["caas"]) == {}


def test_a_job_that_only_starts_with_the_suite_id_is_not_the_suite_job() -> None:
    world = SuiteWorld()
    world.job("vmaas-notes", "in_progress")
    world.job("vmaas2 / e2e", "in_progress")
    assert find_jobs(world, REPO, RUN, ["vmaas"]) == {}


# track -----------------------------------------------------------------------------------------------------------


def test_a_suite_without_a_job_yet_is_waiting_and_links_the_workflow_run() -> None:
    world = SuiteWorld()
    fast(world, {"vmaas": VM}, timeout=0)
    status = world.line(VM)
    assert (status["state"], status["description"], status["target_url"]) == (
        "pending",
        "Waiting for a runner",
        RUN_URL,
    )


def test_a_queued_job_is_waiting_for_a_runner_and_links_the_job_logs() -> None:
    world = SuiteWorld()
    world.job("vmaas / e2e", "queued")
    fast(world, {"vmaas": VM}, timeout=0)
    status = world.line(VM)
    assert status["description"] == "Waiting for a runner" and status["target_url"].endswith("/job/vmaas")


def test_the_line_turns_to_running_when_the_job_gets_a_runner_and_the_tracking_ends() -> None:
    world = SuiteWorld()
    world.job("vmaas / e2e", "queued")
    world.job("caas / e2e", "in_progress")
    looks = {"n": 0}
    real = world.request

    def request(method: str, path: str, **kw: Any) -> Response:
        if path.endswith("/jobs"):
            looks["n"] += 1
            if looks["n"] == 2:
                world.job("vmaas / e2e", "in_progress")
        return real(method, path, **kw)

    world.request = request  # type: ignore[method-assign]
    wrote = fast(world, LINES)
    assert world.line(VM)["description"] == "Running" and world.line(CA)["description"] == "Running"
    assert wrote == ["vmaas", "caas"]
    # the CaaS line was written once, VMaaS twice (waiting, then running)
    contexts = [w["context"] for w in world.writes]
    assert contexts.count(CA) == 1 and contexts.count(VM) == 2


def test_nothing_is_written_again_when_nothing_changed() -> None:
    world = SuiteWorld()
    world.job("vmaas / e2e", "queued")
    fast(world, {"vmaas": VM}, timeout=5)  # several looks, the job never leaves the queue
    assert len(world.writes) == 1


def test_a_new_suite_is_only_a_policy_entry_its_line_is_named_by_the_decision() -> None:
    world = SuiteWorld()
    world.job("netris / e2e", "in_progress")
    fast(world, {"netris": NET})
    assert {s["context"] for s in world.statuses} == {NET} and world.line(NET)["description"] == "Running"


def test_a_suite_that_does_not_run_gets_no_line() -> None:
    world = SuiteWorld()
    world.job("vmaas / e2e", "in_progress")
    fast(world, {"vmaas": VM})
    assert {s["context"] for s in world.statuses} == {VM}


def test_tracked_lines_are_pending_until_settled() -> None:
    world = SuiteWorld()
    world.job("caas / e2e", "in_progress")
    fast(world, {"caas": CA})
    assert world.line(CA)["state"] == "pending"


# settle ----------------------------------------------------------------------------------------------------------


def test_settle_reads_each_result_from_the_job_and_links_its_logs() -> None:
    world = SuiteWorld()
    world.job("vmaas / e2e", "completed", "success")
    world.job("caas / e2e", "completed", "failure")
    assert settle(world, REPO, SHA, RUN, RUN_URL, LINES) == ["vmaas", "caas"]
    assert (world.line(VM)["state"], world.line(VM)["description"]) == ("success", "Passed")
    assert (world.line(CA)["state"], world.line(CA)["description"]) == ("failure", "Failed")
    assert world.line(CA)["target_url"].endswith("/job/caas")


@pytest.mark.parametrize(
    ("status", "conclusion", "state", "title"),
    [
        ("completed", "success", "success", "Passed"),
        ("completed", "failure", "failure", "Failed"),
        ("completed", "timed_out", "failure", "Failed"),
        ("completed", "action_required", "failure", "Failed"),
        ("completed", "cancelled", "error", "Cancelled"),
        ("in_progress", None, "error", "Cancelled"),  # the run stopped under it
        ("queued", None, "error", "Cancelled"),
    ],
)
def test_how_a_job_ended_is_how_its_line_ends(status: str, conclusion: str | None, state: str, title: str) -> None:
    world = SuiteWorld()
    world.job("vmaas / e2e", status, conclusion)
    settle(world, REPO, SHA, RUN, RUN_URL, {"vmaas": VM})
    assert (world.line(VM)["state"], world.line(VM)["description"]) == (state, title)


def test_settle_without_a_job_writes_a_cancelled_line_that_links_the_workflow_run() -> None:
    world = SuiteWorld()
    settle(world, REPO, SHA, RUN, RUN_URL, {"vmaas": VM})
    status = world.line(VM)
    assert (status["state"], status["description"], status["target_url"]) == ("error", "Cancelled", RUN_URL)


def test_settle_never_overwrites_the_line_of_a_newer_run() -> None:
    world = SuiteWorld()
    world.job("vmaas / e2e", "completed", "failure")
    world.job("caas / e2e", "completed", "success")
    world.add(context=VM, state="pending", description="Running", target_url=NEWER_RUN_URL)
    assert settle(world, REPO, SHA, RUN, RUN_URL, LINES) == ["caas"]
    assert world.line(VM)["description"] == "Running"


def test_settle_replaces_the_line_its_own_run_wrote() -> None:
    world = SuiteWorld()
    world.job("vmaas / e2e", "completed", "success")
    world.add(context=VM, state="pending", description="Running", target_url=RUN_URL + "/job/1")
    assert settle(world, REPO, SHA, RUN, RUN_URL, {"vmaas": VM}) == ["vmaas"]
    assert world.line(VM)["state"] == "success"


def test_settle_ignores_a_status_a_person_wrote_with_the_same_name() -> None:
    world = SuiteWorld()
    world.job("vmaas / e2e", "completed", "success")
    world.add(context=VM, state="pending", description="x", target_url=NEWER_RUN_URL, creator={"login": "me"})
    assert settle(world, REPO, SHA, RUN, RUN_URL, {"vmaas": VM}) == ["vmaas"]


# what the decision may carry -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lines",
    [
        {"vmaas": "E2E"},
        {"VMaaS": VM},
        {"vmaas": ""},
        {"vmaas": "a\nb"},
        {"vmaas": VM, "caas": VM},
        ["vmaas"],
    ],
)
def test_lines_that_are_not_plain_names_are_refused_before_anything_is_written(lines: object) -> None:
    world = SuiteWorld()
    with pytest.raises(ValueError, match="suite|line|E2E"):
        settle(world, REPO, SHA, RUN, RUN_URL, lines)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="suite|line|E2E"):
        track(world, REPO, SHA, RUN, RUN_URL, lines, timeout=0)  # type: ignore[arg-type]
    assert world.writes == []


@pytest.mark.parametrize("sha", ["", "abc", "A" * 40, "a" * 40 + "\n"])
def test_a_commit_id_that_is_not_a_full_lower_case_sha_is_refused(sha: str) -> None:
    with pytest.raises(ValueError, match="invalid commit id"):
        settle(SuiteWorld(), REPO, sha, RUN, RUN_URL, {"vmaas": VM})
    with pytest.raises(ValueError, match="invalid commit id"):
        track(SuiteWorld(), REPO, sha, RUN, RUN_URL, {"vmaas": VM}, timeout=0)


def test_parse_lines() -> None:
    assert parse_lines(json.dumps(LINES)) == LINES
    assert parse_lines("{}") == {}
    with pytest.raises(ValueError, match="JSON object"):
        parse_lines("vmaas,caas")
    with pytest.raises(ValueError, match="suite checks"):
        parse_lines('["vmaas"]')


# the commands ----------------------------------------------------------------------------------------------------


def test_the_settle_command_prints_the_suites_it_wrote_and_bad_lines_exit_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = SuiteWorld()
    world.job("vmaas / e2e", "completed", "success")
    monkeypatch.setattr(cli, "build_client", lambda **_: world)
    base = ["e2e-suites-settle", "--repo", REPO, "--sha", SHA, "--run-id", str(RUN)]
    assert cli.main([*base, "--suite-checks", json.dumps({"vmaas": VM})]) == 0
    assert json.loads(capsys.readouterr().out) == ["vmaas"]
    assert cli.main([*base, "--suite-checks", json.dumps({"vmaas": "E2E"})]) == 3
    assert "e2e-suites-settle failed" in capsys.readouterr().err
    assert cli.main([*base, "--suite-checks", "not json"]) == 3


def test_the_track_command_writes_the_lines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = SuiteWorld()
    world.job("caas / e2e", "in_progress")
    monkeypatch.setattr(cli, "build_client", lambda **_: world)
    argv = [
        "e2e-suites-track", "--repo", REPO, "--sha", SHA, "--run-id", str(RUN),
        "--suite-checks", json.dumps({"caas": CA}), "--interval", "0",
    ]  # fmt: skip
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == ["caas"]
    assert world.line(CA)["description"] == "Running"


# the two actions ------------------------------------------------------------------------------------------------

ACTIONS = ROOT / ".github" / "actions"


def action(name: str) -> dict:
    return yaml.safe_load((ACTIONS / name / "action.yml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["e2e-suites-track", "e2e-suites-settle"])
def test_every_input_used_is_declared_and_nothing_is_expanded_into_a_script(name: str) -> None:
    text = (ACTIONS / name / "action.yml").read_text(encoding="utf-8")
    assert set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", text)) == set(action(name)["inputs"])
    for step in action(name)["runs"]["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")
        assert "checkout" not in step.get("uses", "")


def run_action(tmp_path: Path, name: str, step: str, env: dict[str, str]) -> tuple[int, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(f"#!/bin/bash\necho \"$@\" >> '{tmp_path}/uv-args'\n")
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    script = next(s["run"] for s in action(name)["runs"]["steps"] if s.get("name") == step)
    full = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_TOKEN": "t", "REPO": "o/r", "SHA": SHA, "RUN_ID": "7",
            "RUN_URL": "https://x/y", "ACTION_PATH": "/a/b/c", "KIND": "statuses", **env}  # fmt: skip
    done = subprocess.run(["bash", "-c", script], env=full, capture_output=True, text=True, check=False)
    args = tmp_path / "uv-args"
    return done.returncode, args.read_text() if args.exists() else ""


CHECKS_JSON = json.dumps(LINES)


def test_the_track_action_passes_its_inputs_to_the_command(tmp_path: Path) -> None:
    code, args = run_action(tmp_path, "e2e-suites-track", "Track", {"SUITE_CHECKS": CHECKS_JSON, "TIMEOUT": "60"})
    assert code == 0
    assert "e2e-suites-track --repo o/r --sha " + SHA + " --run-id 7 --run-url=https://x/y" in args
    assert f"--suite-checks {CHECKS_JSON}" in args and "--timeout 60" in args


@pytest.mark.parametrize(("key", "value"), [("RUN_ID", "7;ls"), ("TIMEOUT", "1h")])
def test_the_track_action_refuses_a_run_id_or_timeout_that_is_not_digits(tmp_path: Path, key: str, value: str) -> None:
    env = {"SUITE_CHECKS": CHECKS_JSON, "TIMEOUT": "60", key: value}
    code, args = run_action(tmp_path, "e2e-suites-track", "Track", env)
    assert code == 1 and args == ""


@pytest.mark.parametrize(
    ("name", "step", "extra"),
    [
        ("e2e-suites-track", "Track", {"SUITE_CHECKS": CHECKS_JSON, "TIMEOUT": "60"}),
        ("e2e-suites-settle", "Settle", {"SUITE_CHECKS": CHECKS_JSON}),
    ],
)
def test_both_suite_actions_pass_the_kind_and_refuse_a_bad_one(
    tmp_path: Path, name: str, step: str, extra: dict
) -> None:
    code, args = run_action(tmp_path, name, step, {**extra, "KIND": "checks"})
    assert code == 0 and "--kind checks" in args
    bad = tmp_path / "bad"
    bad.mkdir()
    code, args = run_action(bad, name, step, {**extra, "KIND": "x"})
    assert code == 1 and args == ""


def test_the_settle_action_passes_the_lines_to_the_command(tmp_path: Path) -> None:
    code, args = run_action(tmp_path, "e2e-suites-settle", "Settle", {"SUITE_CHECKS": CHECKS_JSON})
    assert code == 0 and f"--suite-checks {CHECKS_JSON}" in args and "--result" not in args


@pytest.mark.parametrize("empty", ["", "{}"])
def test_the_settle_action_does_nothing_without_lines(tmp_path: Path, empty: str) -> None:
    code, args = run_action(tmp_path, "e2e-suites-settle", "Settle", {"SUITE_CHECKS": empty})
    assert code == 0 and args == ""
