"""The E2E / <Suite> statuses: written when the suites are about to start, kept current while the jobs wait for a
runner and run, completed with each suite's result. Kept by a small in-memory GitHub: the statuses of one commit and
the jobs of one workflow run."""

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
from osac_ci.e2e_suites_check import context, parse_results, settle, track
from osac_ci.github.api import Response

pytestmark = pytest.mark.unit
REPO = "o/r"
RUN_URL = "https://example.invalid/o/r/actions/runs/500"
NEWER_RUN_URL = "https://example.invalid/o/r/actions/runs/501/job/9"


class SuiteWorld(World):
    def __init__(self) -> None:
        super().__init__()
        self.jobs: list[dict[str, Any]] = []

    def job(self, name: str, status: str) -> None:
        self.jobs = [j for j in self.jobs if j["name"] != name]
        self.jobs.append(
            {
                "name": name,
                "status": status,
                "html_url": f"https://example.invalid/o/r/actions/runs/500/job/{name.split()[0]}",
            }
        )

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path == f"{BASE}/actions/runs/{RUN}/jobs":
            page, per = int((params or {}).get("page", 1)), int((params or {}).get("per_page", 100))
            return Response(200, {"jobs": self.jobs[(page - 1) * per : page * per]})
        return super().request(method, path, params=params, body=body)

    def suite(self, name: str) -> dict[str, Any]:
        return self.newest(f"E2E / {name}")


def fast(world: SuiteWorld, suites: list[str], **kw: Any) -> list[str]:
    # a clock that moves one second per look, and no real sleeping
    ticks = iter(range(10_000))
    return track(
        world, REPO, SHA, RUN, RUN_URL, suites,
        interval=1, sleep=lambda _: None, clock=lambda: float(next(ticks)), **kw,
    )  # fmt: skip


def test_contexts_are_the_suite_labels_and_unknown_suites_are_refused() -> None:
    assert context("vmaas") == "E2E / VMaaS" and context("bmaas") == "E2E / BMaaS"
    with pytest.raises(ValueError, match="unknown suite"):
        context("netris")


def test_a_suite_without_a_job_yet_is_waiting_and_links_the_workflow_run() -> None:
    world = SuiteWorld()
    fast(world, ["vmaas"], timeout=0)
    status = world.suite("VMaaS")
    assert (status["state"], status["description"], status["target_url"]) == (
        "pending",
        "Waiting for a runner",
        RUN_URL,
    )


def test_a_queued_job_is_waiting_for_a_runner_and_links_the_job_logs() -> None:
    world = SuiteWorld()
    world.job("VMaaS / e2e-vmaas-full-install / e2e", "queued")
    fast(world, ["vmaas"], timeout=0)
    status = world.suite("VMaaS")
    assert status["description"] == "Waiting for a runner" and status["target_url"].endswith("/job/VMaaS")


def test_the_status_turns_to_running_when_the_job_gets_a_runner_and_the_tracking_ends() -> None:
    world = SuiteWorld()
    world.job("VMaaS / e2e-vmaas-full-install / e2e", "queued")
    world.job("CaaS / e2e-caas-full-install / e2e", "in_progress")
    looks = {"n": 0}
    real = world.request

    def request(method: str, path: str, **kw: Any) -> Response:
        if path.endswith("/jobs"):
            looks["n"] += 1
            if looks["n"] == 2:
                world.job("VMaaS / e2e-vmaas-full-install / e2e", "in_progress")
        return real(method, path, **kw)

    world.request = request  # type: ignore[method-assign]
    wrote = fast(world, ["vmaas", "caas"])
    assert world.suite("VMaaS")["description"] == "Running" and world.suite("CaaS")["description"] == "Running"
    assert wrote == ["vmaas", "caas"]
    # the CaaS status was written once, VMaaS twice (waiting, then running)
    contexts = [w["context"] for w in world.writes]
    assert contexts.count("E2E / CaaS") == 1 and contexts.count("E2E / VMaaS") == 2


def test_nothing_is_written_again_when_nothing_changed() -> None:
    world = SuiteWorld()
    world.job("VMaaS / e2e-vmaas-full-install / e2e", "queued")
    fast(world, ["vmaas"], timeout=5)  # several looks, the job never leaves the queue
    assert len(world.writes) == 1


def test_a_job_that_only_starts_with_the_suite_label_is_not_the_suite_job() -> None:
    world = SuiteWorld()
    world.job("VMaaS notes", "in_progress")
    fast(world, ["vmaas"], timeout=0)
    status = world.suite("VMaaS")
    assert status["description"] == "Waiting for a runner" and "/job/" not in status["target_url"]


def test_tracked_statuses_are_pending_until_settled() -> None:
    world = SuiteWorld()
    world.job("CaaS / e2e-caas-full-install / e2e", "in_progress")
    fast(world, ["caas"])
    assert world.suite("CaaS")["state"] == "pending"


def test_a_suite_that_does_not_run_gets_no_status() -> None:
    world = SuiteWorld()
    world.job("VMaaS / e2e-vmaas-full-install / e2e", "in_progress")
    fast(world, ["vmaas"])
    assert {s["context"] for s in world.statuses} == {"E2E / VMaaS"}


def test_settle_writes_the_result_with_a_link_to_the_job_logs() -> None:
    world = SuiteWorld()
    world.job("VMaaS / e2e-vmaas-full-install / e2e", "in_progress")
    world.job("CaaS / e2e-caas-full-install / e2e", "in_progress")
    fast(world, ["vmaas", "caas"])
    wrote = settle(world, REPO, SHA, RUN, RUN_URL, {"vmaas": "success", "caas": "failure"})
    assert wrote == ["vmaas", "caas"]
    assert (world.suite("VMaaS")["state"], world.suite("VMaaS")["description"]) == ("success", "Passed")
    assert (world.suite("CaaS")["state"], world.suite("CaaS")["description"]) == ("failure", "Failed")
    assert world.suite("CaaS")["target_url"].endswith("/job/CaaS")


def test_settle_without_a_status_writes_one_that_links_the_workflow_run() -> None:
    world = SuiteWorld()
    settle(world, REPO, SHA, RUN, RUN_URL, {"bmaas": "cancelled"})
    status = world.suite("BMaaS")
    assert (status["state"], status["description"], status["target_url"]) == ("error", "Cancelled", RUN_URL)


def test_settle_never_overwrites_the_status_of_a_newer_run() -> None:
    world = SuiteWorld()
    world.add(context="E2E / VMaaS", state="pending", description="Running", target_url=NEWER_RUN_URL)
    wrote = settle(world, REPO, SHA, RUN, RUN_URL, {"vmaas": "failure", "caas": "success"})
    assert wrote == ["caas"] and world.suite("VMaaS")["description"] == "Running"


def test_settle_replaces_the_status_its_own_run_wrote() -> None:
    world = SuiteWorld()
    world.add(context="E2E / VMaaS", state="pending", description="Running", target_url=RUN_URL + "/job/1")
    assert settle(world, REPO, SHA, RUN, RUN_URL, {"vmaas": "success"}) == ["vmaas"]
    assert world.suite("VMaaS")["state"] == "success"


def test_settle_ignores_a_status_a_person_wrote_for_the_decision() -> None:
    world = SuiteWorld()
    world.add(
        context="E2E / VMaaS", state="pending", description="x", target_url=NEWER_RUN_URL, creator={"login": "me"}
    )
    assert settle(world, REPO, SHA, RUN, RUN_URL, {"vmaas": "success"}) == ["vmaas"]


@pytest.mark.parametrize(
    ("results", "message"),
    [({"netris": "success"}, "unknown suite"), ({"vmaas": "skipped"}, "must be one of")],
)
def test_settle_refuses_unknown_suites_and_results_before_writing(results: dict[str, str], message: str) -> None:
    world = SuiteWorld()
    with pytest.raises(ValueError, match=message):
        settle(world, REPO, SHA, RUN, RUN_URL, {"caas": "success", **results})
    assert world.writes == []


@pytest.mark.parametrize("sha", ["", "abc", "A" * 40, "a" * 40 + "\n"])
def test_a_commit_id_that_is_not_a_full_lower_case_sha_is_refused(sha: str) -> None:
    with pytest.raises(ValueError, match="invalid commit id"):
        settle(SuiteWorld(), REPO, sha, RUN, RUN_URL, {"vmaas": "success"})
    with pytest.raises(ValueError, match="invalid commit id"):
        track(SuiteWorld(), REPO, sha, RUN, RUN_URL, ["vmaas"], timeout=0)


def test_parse_results() -> None:
    assert parse_results(["vmaas=success", "caas=failure"]) == {"vmaas": "success", "caas": "failure"}
    with pytest.raises(ValueError, match="suite=result"):
        parse_results(["vmaas"])
    with pytest.raises(ValueError, match="more than once"):
        parse_results(["vmaas=failure", "vmaas=success"])


def test_the_settle_command_prints_the_suites_it_wrote_and_a_bad_result_exits_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = SuiteWorld()
    monkeypatch.setattr(cli, "build_client", lambda **_: world)
    base = ["e2e-suites-settle", "--repo", REPO, "--sha", SHA, "--run-id", str(RUN)]
    assert cli.main([*base, "--result", "vmaas=success"]) == 0
    assert json.loads(capsys.readouterr().out) == ["vmaas"]
    assert cli.main([*base, "--result", "vmaas=bogus"]) == 3
    assert "e2e-suites-settle failed" in capsys.readouterr().err


def test_the_track_command_writes_the_statuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = SuiteWorld()
    world.job("CaaS / e2e-caas-full-install / e2e", "in_progress")
    monkeypatch.setattr(cli, "build_client", lambda **_: world)
    argv = [
        "e2e-suites-track",
        "--repo",
        REPO,
        "--sha",
        SHA,
        "--run-id",
        str(RUN),
        "--suites",
        "caas",
        "--interval",
        "0",
    ]
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == ["caas"]


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
            "RUN_URL": "https://x/y", "ACTION_PATH": "/a/b/c", **env}  # fmt: skip
    done = subprocess.run(["bash", "-c", script], env=full, capture_output=True, text=True, check=False)
    args = tmp_path / "uv-args"
    return done.returncode, args.read_text() if args.exists() else ""


def test_the_track_action_passes_its_inputs_to_the_command(tmp_path: Path) -> None:
    code, args = run_action(tmp_path, "e2e-suites-track", "Track", {"SUITES": "vmaas,caas", "TIMEOUT": "60"})
    assert code == 0
    assert "e2e-suites-track --repo o/r --sha " + SHA + " --run-id 7 --run-url=https://x/y --suites vmaas,caas" in args
    assert "--timeout 60" in args


@pytest.mark.parametrize(("key", "value"), [("RUN_ID", "7;ls"), ("TIMEOUT", "1h")])
def test_the_track_action_refuses_a_run_id_or_timeout_that_is_not_digits(tmp_path: Path, key: str, value: str) -> None:
    code, args = run_action(tmp_path, "e2e-suites-track", "Track", {"SUITES": "vmaas", "TIMEOUT": "60", key: value})
    assert code == 1 and args == ""


def test_the_settle_action_makes_one_result_argument_per_pair(tmp_path: Path) -> None:
    code, args = run_action(tmp_path, "e2e-suites-settle", "Settle", {"RESULTS": "vmaas=success caas=failure"})
    assert code == 0 and "--result vmaas=success --result caas=failure" in args


def test_the_settle_action_does_nothing_without_results(tmp_path: Path) -> None:
    code, args = run_action(tmp_path, "e2e-suites-settle", "Settle", {"RESULTS": ""})
    assert code == 0 and args == ""
