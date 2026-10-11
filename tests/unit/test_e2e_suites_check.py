"""The E2E / <Suite> checks: created when the suites are about to start, kept current while the jobs wait for a
runner and run, completed with each suite's result. Kept by a small in-memory GitHub: the check runs of one commit and
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

from osac_ci import cli
from osac_ci.e2e_suites_check import check_name, parse_results, settle, track
from osac_ci.github.api import Response

pytestmark = pytest.mark.unit
REPO = "o/r"
SHA = "a" * 40
RUN = 500
RUN_URL = "https://example.invalid/run/500"
BASE = f"/repos/{REPO}"


class World:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.jobs: list[dict[str, Any]] = []
        self.writes: list[tuple[str, dict[str, Any]]] = []
        self.next_id = 100

    def job(self, name: str, status: str) -> None:
        self.jobs = [j for j in self.jobs if j["name"] != name]
        self.jobs.append({"name": name, "status": status, "html_url": f"https://example.invalid/job/{name.split()[0]}"})

    def add_check(self, name: str, app: str = "github-actions") -> dict[str, Any]:
        check = {"id": self.next_id, "name": name, "app": {"slug": app}, "status": "in_progress", "output": {}}
        self.next_id += 1
        self.checks.append(check)
        return check

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        page, per = int((params or {}).get("page", 1)), int((params or {}).get("per_page", 100))
        if method == "GET" and path == f"{BASE}/actions/runs/{RUN}/jobs":
            return Response(200, {"jobs": self.jobs[(page - 1) * per : page * per]})
        if method == "GET" and path == f"{BASE}/commits/{SHA}/check-runs":
            return Response(200, {"check_runs": self.checks[(page - 1) * per : page * per]})
        if method == "POST" and path == f"{BASE}/check-runs":
            self.writes.append(("POST", body))
            check = self.add_check(body["name"])
            check.update(status=body["status"], conclusion=body.get("conclusion"), output=body["output"])
            check["details_url"] = body.get("details_url")
            return Response(201, {"id": check["id"]})
        if method == "PATCH" and (m := re.fullmatch(rf"{BASE}/check-runs/(\d+)", path)):
            self.writes.append(("PATCH", body))
            check = next(c for c in self.checks if c["id"] == int(m.group(1)))
            check.update(status=body["status"], conclusion=body.get("conclusion"), output=body["output"])
            check["details_url"] = body.get("details_url")
            return Response(200, {"id": check["id"]})
        return Response(404, {"message": f"no route for {method} {path}"})

    def by_name(self, name: str) -> dict[str, Any]:
        return next(c for c in reversed(self.checks) if c["name"] == name)


def fast(world: World, suites: list[str], **kw: Any) -> dict[str, int]:
    # a clock that moves one second per look, and no real sleeping
    ticks = iter(range(10_000))
    return track(
        world, REPO, SHA, RUN, RUN_URL, suites,
        interval=1, sleep=lambda _: None, clock=lambda: float(next(ticks)), **kw,
    )  # fmt: skip


def test_check_names_are_the_suite_labels_and_unknown_suites_are_refused() -> None:
    assert check_name("vmaas") == "E2E / VMaaS" and check_name("bmaas") == "E2E / BMaaS"
    with pytest.raises(ValueError, match="unknown suite"):
        check_name("netris")


def test_a_suite_without_a_job_yet_is_waiting_and_links_the_workflow_run() -> None:
    world = World()
    fast(world, ["vmaas"], timeout=0)
    check = world.by_name("E2E / VMaaS")
    assert check["output"]["title"] == "Waiting for a runner"
    assert check["output"]["summary"].endswith(f"[Open the workflow run]({RUN_URL})")


def test_a_queued_job_is_waiting_for_a_runner_and_links_the_job() -> None:
    world = World()
    world.job("VMaaS / e2e-vmaas-full-install / e2e", "queued")
    fast(world, ["vmaas"], timeout=0)
    check = world.by_name("E2E / VMaaS")
    assert check["output"]["title"] == "Waiting for a runner"
    assert check["output"]["summary"].endswith("[Open the job logs](https://example.invalid/job/VMaaS)")


def test_the_check_turns_to_running_when_the_job_gets_a_runner_and_the_tracking_ends() -> None:
    world = World()
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
    ids = fast(world, ["vmaas", "caas"])
    assert world.by_name("E2E / VMaaS")["output"]["title"] == "Running"
    assert world.by_name("E2E / CaaS")["output"]["title"] == "Running"
    assert ids == {"vmaas": world.by_name("E2E / VMaaS")["id"], "caas": world.by_name("E2E / CaaS")["id"]}
    # one check per suite: the second look updated the VMaaS check in place and left the CaaS one alone
    assert len(world.checks) == 2 and [m for m, _ in world.writes].count("PATCH") == 1


def test_nothing_is_written_again_when_nothing_changed() -> None:
    world = World()
    world.job("VMaaS / e2e-vmaas-full-install / e2e", "queued")
    fast(world, ["vmaas"], timeout=5)  # several looks, the job never leaves the queue
    assert len(world.writes) == 1


def test_a_job_that_only_starts_with_the_suite_label_is_not_the_suite_job() -> None:
    world = World()
    world.job("VMaaS notes", "in_progress")
    fast(world, ["vmaas"], timeout=0)
    check = world.by_name("E2E / VMaaS")
    assert check["output"]["title"] == "Waiting for a runner" and "job/" not in check["output"]["summary"]


def test_tracked_checks_are_in_progress_until_settled() -> None:
    world = World()
    world.job("CaaS / e2e-caas-full-install / e2e", "in_progress")
    fast(world, ["caas"])
    check = world.by_name("E2E / CaaS")
    assert check["status"] == "in_progress" and check.get("conclusion") is None


def test_a_suite_that_does_not_run_gets_no_check() -> None:
    world = World()
    world.job("VMaaS / e2e-vmaas-full-install / e2e", "in_progress")
    fast(world, ["vmaas"])
    assert [c["name"] for c in world.checks] == ["E2E / VMaaS"]


def test_settle_completes_the_check_with_the_result_and_the_job_link() -> None:
    world = World()
    world.job("VMaaS / e2e-vmaas-full-install / e2e", "in_progress")
    world.job("CaaS / e2e-caas-full-install / e2e", "in_progress")
    fast(world, ["vmaas", "caas"])
    settle(world, REPO, SHA, RUN, RUN_URL, {"vmaas": "success", "caas": "failure"})
    ok, bad = world.by_name("E2E / VMaaS"), world.by_name("E2E / CaaS")
    assert (ok["status"], ok["conclusion"], ok["output"]["title"]) == ("completed", "success", "Passed")
    assert (bad["status"], bad["conclusion"], bad["output"]["title"]) == ("completed", "failure", "Failed")
    assert bad["output"]["summary"].endswith("[Open the job logs](https://example.invalid/job/CaaS)")
    assert len(world.checks) == 2


def test_settle_without_a_check_creates_a_completed_one() -> None:
    world = World()
    settle(world, REPO, SHA, RUN, RUN_URL, {"bmaas": "cancelled"})
    check = world.by_name("E2E / BMaaS")
    assert (check["status"], check["conclusion"], check["output"]["title"]) == ("completed", "cancelled", "Cancelled")
    assert check["output"]["summary"].endswith(f"[Open the workflow run]({RUN_URL})")


def test_settle_updates_the_newest_check_of_the_app_not_another_apps() -> None:
    world = World()
    older = world.add_check("E2E / VMaaS")
    newer = world.add_check("E2E / VMaaS")
    foreign = world.add_check("E2E / VMaaS", app="some-other-app")
    settle(world, REPO, SHA, RUN, RUN_URL, {"vmaas": "success"})
    assert newer["status"] == "completed" and older["status"] == "in_progress" and foreign["status"] == "in_progress"


@pytest.mark.parametrize(
    ("results", "message"),
    [({"netris": "success"}, "unknown suite"), ({"vmaas": "skipped"}, "must be one of")],
)
def test_settle_refuses_unknown_suites_and_results_before_writing(results: dict[str, str], message: str) -> None:
    world = World()
    with pytest.raises(ValueError, match=message):
        settle(world, REPO, SHA, RUN, RUN_URL, {"caas": "success", **results})
    assert world.writes == []


@pytest.mark.parametrize("sha", ["", "abc", "A" * 40, "a" * 40 + "\n"])
def test_a_commit_id_that_is_not_a_full_lower_case_sha_is_refused(sha: str) -> None:
    with pytest.raises(ValueError, match="invalid commit id"):
        settle(World(), REPO, sha, RUN, RUN_URL, {"vmaas": "success"})
    with pytest.raises(ValueError, match="invalid commit id"):
        track(World(), REPO, sha, RUN, RUN_URL, ["vmaas"], timeout=0)


def test_parse_results() -> None:
    assert parse_results(["vmaas=success", "caas=failure"]) == {"vmaas": "success", "caas": "failure"}
    with pytest.raises(ValueError, match="suite=result"):
        parse_results(["vmaas"])


def test_the_settle_command_prints_the_check_ids_and_a_bad_result_exits_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    monkeypatch.setattr(cli, "build_client", lambda **_: world)
    base = ["e2e-suites-settle", "--repo", REPO, "--sha", SHA, "--run-id", str(RUN)]
    assert cli.main([*base, "--result", "vmaas=success"]) == 0
    assert json.loads(capsys.readouterr().out) == {"vmaas": world.by_name("E2E / VMaaS")["id"]}
    assert cli.main([*base, "--result", "vmaas=bogus"]) == 3
    assert "e2e-suites-settle failed" in capsys.readouterr().err


def test_the_track_command_creates_the_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
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
    assert json.loads(capsys.readouterr().out) == {"caas": world.by_name("E2E / CaaS")["id"]}


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
