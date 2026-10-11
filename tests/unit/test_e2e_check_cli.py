"""`osac-ci e2e-report` and `e2e-finish`: the files they read and write, their exit codes, and that a GitHub failure is
an error and never an answer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_e2e_check import REPO, RUN, SHA, URL, World, entry

from osac_ci import cli

pytestmark = pytest.mark.unit


def report_args(decision: Path, out: Path | None = None, *extra: str) -> list[str]:
    args = ["e2e-report", "--repo", REPO, "--decision-file", str(decision), "--run-id", str(RUN), "--run-url", URL]
    if out is not None:
        args += ["--json-file", str(out)]
    return args + list(extra)


def decision_file(tmp_path: Path, *entries: dict[str, Any]) -> Path:
    path = tmp_path / "decision.json"
    path.write_text(json.dumps(list(entries)), encoding="utf-8")
    return path


def test_report_writes_the_answer_a_workflow_step_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    monkeypatch.setattr(cli, "build_client", lambda **_: world)
    out = tmp_path / "answer.json"
    assert cli.main(report_args(decision_file(tmp_path, entry("start")), out)) == 0
    answer = json.loads(out.read_text(encoding="utf-8"))
    assert answer["start"] is True and answer["sha"] == SHA and answer["number"] == 7
    assert answer["check_id"] == world.checks[0]["id"] and answer["suites"] == ["vmaas", "caas"]
    assert "E2E check: **running** (starting the suites)" in capsys.readouterr().out


def test_report_can_print_the_answer_as_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "build_client", lambda **_: World())
    assert cli.main(report_args(decision_file(tmp_path, entry("wait-approval")), None, "--json")) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["state"] == "waiting" and printed["start"] is False


def test_an_empty_decision_file_is_nothing_to_do_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    monkeypatch.setattr(cli, "build_client", lambda **_: world)
    assert cli.main(report_args(decision_file(tmp_path))) == 0
    assert "nothing to do" in capsys.readouterr().out and world.writes == []


@pytest.mark.parametrize("content", ["not json", '{"a": 1}', "[1]"])
def test_a_decision_file_that_is_not_a_list_of_decisions_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    world = World()
    monkeypatch.setattr(cli, "build_client", lambda **_: world)
    path = tmp_path / "decision.json"
    path.write_text(content, encoding="utf-8")
    assert cli.main(report_args(path)) == 3
    assert "e2e-report failed" in capsys.readouterr().err and world.writes == []


def test_a_missing_decision_file_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "build_client", lambda **_: World())
    assert cli.main(report_args(tmp_path / "missing.json")) == 3


def test_a_refused_write_exits_3_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    world.fail_writes = True
    monkeypatch.setattr(cli, "build_client", lambda **_: world)
    assert cli.main(report_args(decision_file(tmp_path, entry("start")))) == 3
    assert "Resource not accessible" in capsys.readouterr().err


def test_finish_records_the_result_on_the_claimed_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    claimed = world.add(external_id=f"run:{RUN}")
    monkeypatch.setattr(cli, "build_client", lambda **_: world)
    args = ["e2e-finish", "--repo", REPO, "--sha", SHA, "--check-id", str(claimed["id"]), "--result", "failure"]
    assert cli.main([*args, "--run-id", str(RUN), "--run-url", URL, "--detail", "caas failed"]) == 0
    assert claimed["conclusion"] == "failure" and claimed["output"]["summary"].startswith("caas failed")
    assert f"E2E check {claimed['id']}: failure" in capsys.readouterr().out


def test_finish_rejects_a_result_the_check_cannot_have() -> None:
    with pytest.raises(SystemExit):
        cli.main(["e2e-finish", "--repo", REPO, "--sha", SHA, "--result", "skipped", "--run-id", "1"])


def test_finish_exits_3_when_github_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    world.fail_writes = True
    monkeypatch.setattr(cli, "build_client", lambda **_: world)
    assert cli.main(["e2e-finish", "--repo", REPO, "--sha", SHA, "--result", "success", "--run-id", str(RUN)]) == 3
    assert "e2e-finish failed" in capsys.readouterr().err
