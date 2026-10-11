"""The bot-approve action: its shape, and its shell run for real with a stand-in `uv`."""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from helpers import ROOT

pytestmark = pytest.mark.unit
ACTION = ROOT / ".github" / "actions" / "bot-approve" / "action.yml"
SHA = "a" * 40


def action() -> dict:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


def script() -> str:
    return next(s["run"] for s in action()["runs"]["steps"] if s.get("name") == "Approve")


def test_every_input_used_is_declared_and_every_input_declared_is_used() -> None:
    used = set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", ACTION.read_text(encoding="utf-8")))
    assert used == set(action()["inputs"])


def test_nothing_is_expanded_into_a_script_and_nothing_is_checked_out() -> None:
    for step in action()["runs"]["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")
        assert "checkout" not in step.get("uses", "")


def test_the_approving_credential_reaches_only_the_program_under_its_own_name() -> None:
    env = next(s for s in action()["runs"]["steps"] if s.get("name") == "Approve")["env"]
    assert env["OSAC_CI_APPROVE_TOKEN"] == "${{ inputs.approve-token }}"
    assert env["GH_TOKEN"] == "${{ inputs.github-token }}"
    assert [k for k, v in env.items() if "approve-token" in str(v)] == ["OSAC_CI_APPROVE_TOKEN"]


def test_the_declared_outputs_are_the_ones_the_step_writes() -> None:
    declared = action()["outputs"]
    assert set(declared) == {"json-file", "approved", "reason"}
    for name in declared:
        assert declared[name]["value"] == f"${{{{ steps.approve.outputs.{name} }}}}"


def run(  # type: ignore[no-untyped-def]
    tmp_path: Path, *, number: str = "", sha: str = "", uv_exit: int = 0, entries: list | None = None
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    uv = bin_dir / "uv"
    # The stand-in writes the entries where the real program is told to write its JSON.
    uv.write_text(
        f"#!/bin/bash\necho \"$@\" >> '{tmp_path}/uv-args'\necho 'approval text'\n"
        'while [ $# -gt 0 ]; do [ "$1" = "--json-file" ] && printf "%s" "$ENTRIES" > "$2"; shift; done\n'
        f"exit {uv_exit}\n"
    )
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    out, summary = tmp_path / "output", tmp_path / "summary"
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_TOKEN": "t", "OSAC_CI_APPROVE_TOKEN": "a", "REPO": "o/r",
        "NUMBER": number, "HEAD_SHA": sha, "ACTION_PATH": "/a/b/c", "OUT_DIR": str(tmp_path),
        "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary), "ENTRIES": json.dumps(entries or []),
    }  # fmt: skip
    done = subprocess.run(["bash", "-c", script()], env=env, capture_output=True, text=True, check=False)
    read = lambda p: p.read_text() if p.exists() else ""  # noqa: E731
    return done.returncode, read(tmp_path / "uv-args"), read(out), read(summary), done.stdout + done.stderr


def outputs(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def entry(result: str, reason: str = "why") -> dict:
    return {"number": 1, "head_sha": SHA, "result": result, "reason": reason}


def test_a_number_is_passed_to_the_program_and_its_text_goes_to_the_summary(tmp_path: Path) -> None:
    code, args, output, summary, _ = run(tmp_path, number="12")
    assert code == 0
    assert "bot-approve --repo o/r --number 12 --json-file" in args
    assert "approval text" in summary and f"json-file={tmp_path}/bot-approval.json" in output


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


def test_neither_given_is_a_notice_and_not_an_approval(tmp_path: Path) -> None:
    code, args, output, _, log = run(tmp_path)
    got = outputs(output)
    assert code == 0 and args == "" and "nothing to approve" in log
    assert got["approved"] == "false" and got["json-file"] == ""


def test_a_failing_program_fails_the_step(tmp_path: Path) -> None:
    code, _, _, _, _ = run(tmp_path, number="3", uv_exit=3)
    assert code != 0


@pytest.mark.parametrize("result", ["approved", "already-approved"])
def test_one_pull_request_approved_for_its_head_is_approved(tmp_path: Path, result: str) -> None:
    _, _, output, _, _ = run(tmp_path, number="1", entries=[entry(result, "all from the bot")])
    got = outputs(output)
    assert got["approved"] == "true" and got["reason"] == "all from the bot"


def test_a_pull_request_that_is_not_approved_says_why(tmp_path: Path) -> None:
    _, _, output, _, _ = run(tmp_path, number="1", entries=[entry("not-approved", "a person's commit")])
    got = outputs(output)
    assert got["approved"] == "false" and got["reason"] == "a person's commit"


def test_an_unknown_result_is_not_an_approval(tmp_path: Path) -> None:
    _, _, output, _, _ = run(tmp_path, number="1", entries=[entry("approved-ish")])
    assert outputs(output)["approved"] == "false"


def test_no_pull_request_found_is_not_approved(tmp_path: Path) -> None:
    code, _, output, _, _ = run(tmp_path, sha=SHA, entries=[])
    got = outputs(output)
    assert code == 0 and got["approved"] == "false" and "no open pull request" in got["reason"]


def test_several_pull_requests_with_the_same_head_are_never_reported_approved(tmp_path: Path) -> None:
    _, _, output, _, _ = run(tmp_path, sha=SHA, entries=[entry("approved"), entry("approved")])
    got = outputs(output)
    assert got["approved"] == "false" and "2 pull requests" in got["reason"]


def test_a_reason_with_line_breaks_stays_on_one_output_line(tmp_path: Path) -> None:
    _, _, output, _, _ = run(tmp_path, number="1", entries=[entry("approved", "first\nsecond\r\nthird")])
    assert outputs(output)["reason"].split() == ["first", "second", "third"]
    assert len([line for line in output.splitlines() if line.startswith("reason=")]) == 1
    assert all("=" in line for line in output.splitlines())
