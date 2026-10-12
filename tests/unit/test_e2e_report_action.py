"""The e2e-report and e2e-finish actions: their shape, and their shell run for real with a stand-in `uv`."""

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
REPORT = ROOT / ".github" / "actions" / "e2e-report" / "action.yml"
FINISH = ROOT / ".github" / "actions" / "e2e-finish" / "action.yml"
SHA = "a" * 40


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def script(path: Path, step: str) -> str:
    return next(s["run"] for s in load(path)["runs"]["steps"] if s.get("name") == step)


@pytest.mark.parametrize("path", [REPORT, FINISH])
def test_every_input_used_is_declared_and_every_input_declared_is_used(path: Path) -> None:
    used = set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", path.read_text(encoding="utf-8")))
    assert used == set(load(path)["inputs"])


@pytest.mark.parametrize(("path", "step"), [(REPORT, "Report"), (FINISH, "Finish")])
def test_nothing_is_expanded_into_a_script_and_nothing_is_checked_out(path: Path, step: str) -> None:
    for s in load(path)["runs"]["steps"]:
        assert "${{" not in s.get("run", ""), s.get("name")
        assert "checkout" not in s.get("uses", "")


def test_the_declared_outputs_are_the_ones_the_report_step_writes() -> None:
    declared = load(REPORT)["outputs"]
    assert set(declared) == {"start", "sha", "state", "suites", "number"}
    for name in declared:
        assert declared[name]["value"] == f"${{{{ steps.report.outputs.{name} }}}}"


def test_the_run_defaults_point_at_the_running_workflow() -> None:
    for path in (REPORT, FINISH):
        assert load(path)["inputs"]["run-url"]["default"].endswith("/actions/runs/${{ github.run_id }}")
    assert load(REPORT)["inputs"]["run-id"]["default"] == "${{ github.run_id }}"


def stand_in(tmp_path: Path, answer: dict | None, uv_exit: int = 0) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    uv = bin_dir / "uv"
    uv.write_text(
        f"#!/bin/bash\necho \"$@\" >> '{tmp_path}/uv-args'\necho 'check text'\n"
        'while [ $# -gt 0 ]; do [ "$1" = "--json-file" ] && printf "%s" "$ANSWER" > "$2"; shift; done\n'
        f"exit {uv_exit}\n"
    )
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "ANSWER": json.dumps(answer or {})}


def run_report(tmp_path: Path, *, decision: str | None = "d.json", run_id: str = "42", answer: dict | None = None,
               uv_exit: int = 0, kind: str = "statuses"):  # type: ignore[no-untyped-def]  # fmt: skip
    if decision == "d.json":
        (tmp_path / "d.json").write_text("[{}]")
        decision = str(tmp_path / "d.json")
    out, summary = tmp_path / "output", tmp_path / "summary"
    env = {
        **stand_in(tmp_path, answer, uv_exit), "GH_TOKEN": "t", "REPO": "o/r", "DECISION": decision or "",
        "RUN_ID": run_id, "RUN_URL": "https://x/run/42", "ACTION_PATH": "/a/b/c", "KIND": kind,
        "OUT_DIR": str(tmp_path),
        "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary),
    }  # fmt: skip
    done = subprocess.run(
        ["bash", "-c", script(REPORT, "Report")], env=env, capture_output=True, text=True, check=False
    )
    read = lambda p: p.read_text() if p.exists() else ""  # noqa: E731
    return done.returncode, read(tmp_path / "uv-args"), read(out), read(summary), done.stdout + done.stderr


def outputs(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


ANSWER = {"sha": SHA, "number": 9, "state": "running", "start": True, "suites": ["vmaas", "caas"]}


def test_the_decision_file_and_the_run_are_passed_to_the_program(tmp_path: Path) -> None:
    code, args, output, summary, _ = run_report(tmp_path, answer=ANSWER)
    assert code == 0
    assert f"e2e-report --repo o/r --decision-file {tmp_path}/d.json --run-id 42 --run-url=https://x/run/42" in args
    assert "check text" in summary
    got = outputs(output)
    assert got["start"] == "true" and got["sha"] == SHA and got["number"] == "9"
    assert got["state"] == "running" and json.loads(got["suites"]) == ["vmaas", "caas"]


def test_the_kind_is_passed_to_the_program(tmp_path: Path) -> None:
    _, args, _, _, _ = run_report(tmp_path, answer=ANSWER, kind="checks")
    assert args.rstrip().endswith("--kind checks")


@pytest.mark.parametrize("kind", ["", "status", "check", "$(id)"])
def test_a_kind_that_is_not_statuses_or_checks_is_refused_before_the_program_runs(tmp_path: Path, kind: str) -> None:
    code, args, _, _, log = run_report(tmp_path, answer=ANSWER, kind=kind)
    assert code == 1 and args == "" and "statuses or checks" in log


def test_a_run_that_may_not_start_the_suites_says_false_as_a_string(tmp_path: Path) -> None:
    answer = {**ANSWER, "state": "already-running", "start": False}
    _, _, output, _, _ = run_report(tmp_path, answer=answer)
    assert outputs(output)["start"] == "false"


def test_an_answer_with_no_pull_request_leaves_the_number_and_commit_empty(tmp_path: Path) -> None:
    answer = {**ANSWER, "state": "ignored", "start": False, "number": None, "sha": ""}
    _, _, output, _, _ = run_report(tmp_path, answer=answer)
    got = outputs(output)
    assert got["sha"] == "" and got["number"] == ""


@pytest.mark.parametrize("decision", [None, "", "missing.json"])
def test_no_decision_is_a_notice_and_never_a_start(tmp_path: Path, decision: str | None) -> None:
    path = str(tmp_path / decision) if decision else decision
    code, args, output, _, log = run_report(tmp_path, decision=path)
    got = outputs(output)
    assert code == 0 and args == "" and "no decision to report" in log
    assert got["start"] == "false" and got["state"] == "ignored" and got["suites"] == "[]"


def test_an_empty_decision_file_is_no_decision(tmp_path: Path) -> None:
    (tmp_path / "empty.json").write_text("")
    code, args, output, _, _ = run_report(tmp_path, decision=str(tmp_path / "empty.json"))
    assert code == 0 and args == "" and outputs(output)["start"] == "false"


@pytest.mark.parametrize("run_id", ["", "1a", "-1", "$(id)", "1;ls"])
def test_a_run_id_that_is_not_digits_is_refused_before_the_program_runs(tmp_path: Path, run_id: str) -> None:
    code, args, _, _, log = run_report(tmp_path, run_id=run_id)
    assert code == 1 and args == "" and "digits only" in log


def test_a_failing_program_fails_the_step_and_never_reports_a_start(tmp_path: Path) -> None:
    code, _, output, _, _ = run_report(tmp_path, uv_exit=3, answer=ANSWER)
    assert code != 0 and "start=true" not in output


def run_finish(tmp_path: Path, **over: str):  # type: ignore[no-untyped-def]
    summary = tmp_path / "summary"
    env = {
        **stand_in(tmp_path, None), "GH_TOKEN": "t", "REPO": "o/r", "SHA": SHA, "RESULT": "success",
        "DETAIL": "", "RUN_URL": "https://x/run/42", "ACTION_PATH": "/a/b/c", "KIND": "statuses",
        "GITHUB_STEP_SUMMARY": str(summary),
    }  # fmt: skip
    env.update(over)
    done = subprocess.run(
        ["bash", "-c", script(FINISH, "Finish")], env=env, capture_output=True, text=True, check=False
    )
    args = (tmp_path / "uv-args").read_text() if (tmp_path / "uv-args").exists() else ""
    return done.returncode, args, done.stdout + done.stderr


def test_finish_passes_the_result_and_the_run_link_to_the_program(tmp_path: Path) -> None:
    code, args, _ = run_finish(tmp_path, RESULT="failure", DETAIL="caas failed")
    assert code == 0
    assert f"e2e-finish --repo o/r --sha {SHA} --result failure --run-url=https://x/run/42" in args
    assert "--detail=caas failed" in args


def test_finish_passes_the_kind_and_refuses_a_bad_one(tmp_path: Path) -> None:
    code, args, _ = run_finish(tmp_path, KIND="checks")
    assert code == 0 and "--kind checks" in args
    code, args, _ = run_finish(tmp_path, KIND="nope")
    assert code == 1 and args.count("--kind") == 1


def test_finish_without_a_detail_leaves_it_out(tmp_path: Path) -> None:
    _, args, _ = run_finish(tmp_path, DETAIL="")
    assert "--detail" not in args


@pytest.mark.parametrize(
    ("name", "value"),
    [("RESULT", "skipped"), ("RESULT", ""), ("SHA", "abc"), ("SHA", "A" * 40), ("SHA", "")],
)  # fmt: skip
def test_finish_refuses_a_value_that_is_not_what_it_should_be_before_the_program_runs(
    tmp_path: Path, name: str, value: str
) -> None:
    code, args, _ = run_finish(tmp_path, **{name: value})
    assert code == 1 and args == ""
