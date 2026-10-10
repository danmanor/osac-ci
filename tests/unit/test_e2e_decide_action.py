"""The e2e-decide action: its shape, and its shell run for real with a stand-in `uv`."""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from helpers import ROOT

pytestmark = pytest.mark.unit
ACTION = ROOT / ".github" / "actions" / "e2e-decide" / "action.yml"
SHA = "a" * 40


def action() -> dict:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


def script() -> str:
    return next(s["run"] for s in action()["runs"]["steps"] if s.get("name") == "Decide")


def test_every_input_used_is_declared_and_every_input_declared_is_used() -> None:
    used = set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", ACTION.read_text(encoding="utf-8")))
    assert used == set(action()["inputs"])


def test_nothing_is_expanded_into_a_script_and_nothing_is_checked_out() -> None:
    for step in action()["runs"]["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")
        assert "checkout" not in step.get("uses", "")


def test_the_default_policy_exists_in_this_repository() -> None:
    assert (ROOT / action()["inputs"]["policy"]["default"]).is_file()


def run(tmp_path: Path, *, number: str = "", sha: str = "", uv_exit: int = 0):  # type: ignore[no-untyped-def]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    uv = bin_dir / "uv"
    uv.write_text(f"#!/bin/bash\necho \"$@\" >> '{tmp_path}/uv-args'\necho 'decision text'\nexit {uv_exit}\n")
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    out, summary = tmp_path / "output", tmp_path / "summary"
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_TOKEN": "t", "REPO": "o/r", "POLICY": "policy/osac.yml",
        "NUMBER": number, "HEAD_SHA": sha, "ACTION_PATH": "/a/b/c", "OUT_DIR": str(tmp_path),
        "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary),
    }  # fmt: skip
    done = subprocess.run(["bash", "-c", script()], env=env, capture_output=True, text=True, check=False)
    read = lambda p: p.read_text() if p.exists() else ""  # noqa: E731
    return done.returncode, read(tmp_path / "uv-args"), read(out), read(summary), done.stdout + done.stderr


def test_a_number_is_passed_to_the_program_and_its_text_goes_to_the_summary(tmp_path: Path) -> None:
    code, args, output, summary, _ = run(tmp_path, number="12")
    assert code == 0
    assert "e2e-decide --policy /a/b/c/../../../policy/osac.yml --repo o/r --number 12 --json-file" in args
    assert "decision text" in summary and f"json-file={tmp_path}/e2e-decision.json" in output


def test_a_head_commit_is_passed_instead_when_there_is_no_number(tmp_path: Path) -> None:
    code, args, _, _, _ = run(tmp_path, sha=SHA)
    assert code == 0 and f"--head-sha {SHA}" in args and "--number" not in args


def test_a_number_wins_over_a_head_commit(tmp_path: Path) -> None:
    _, args, _, _, _ = run(tmp_path, number="3", sha=SHA)
    assert "--number 3" in args and "--head-sha" not in args


@pytest.mark.parametrize("number", ["1a", "-1", "1 2", "$(id)", "1;ls"])
def test_a_number_that_is_not_digits_is_refused_before_the_program_runs(tmp_path: Path, number: str) -> None:
    code, args, _, _, log = run(tmp_path, number=number)
    assert code == 1 and args == "" and "digits only" in log


@pytest.mark.parametrize("sha", ["abc", "A" * 40, "g" * 40, "a" * 41, "a" * 39, "$(id)"])
def test_a_head_commit_that_is_not_a_full_lower_case_sha_is_refused(tmp_path: Path, sha: str) -> None:
    code, args, _, _, log = run(tmp_path, sha=sha)
    assert code == 1 and args == "" and "head commit" in log


def test_neither_given_is_a_notice_and_not_an_error(tmp_path: Path) -> None:
    code, args, output, _, log = run(tmp_path)
    assert code == 0 and args == "" and "json-file=" in output and "nothing to decide" in log


def test_a_failing_program_fails_the_step(tmp_path: Path) -> None:
    code, _, _, _, _ = run(tmp_path, number="3", uv_exit=3)
    assert code != 0
