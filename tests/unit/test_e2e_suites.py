"""Which E2E suites a list of changed files needs: the function, the command, and the action's shell."""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from helpers import ROOT

from osac_ci import cli
from osac_ci.e2e_start import suites_for_files
from osac_ci.policy import parse_policy

pytestmark = pytest.mark.unit
ACTION = ROOT / ".github" / "actions" / "e2e-suites" / "action.yml"
POLICY_TEXT = """
version: 1
repo: o/r
path_filters:
  mode: enforce
  filters:
    code: ['**', '!**/*.md']
    vm-safe: ['**', '!**/*.md', '!tests/e2e/caas/**']
    ca-safe: ['**', '!**/*.md', '!tests/e2e/vmaas/**']
    suite: ['tests/e2e/**']
jobs:
  e2e-vmaas: {kind: e2e, check: e2e-vmaas, suite: vmaas, filters: [vm-safe], filters_any: [code, suite]}
  e2e-caas: {kind: e2e, check: e2e-caas, suite: caas, filters: [ca-safe], filters_any: [code, suite]}
  lint: {check: lint}
"""


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (["README.md", "docs/x.md"], ()),
        (["tests/e2e/caas/test_a.py"], ("caas",)),
        (["tests/e2e/vmaas/test_a.py"], ("vmaas",)),
        (["fulfillment-service/main.go"], ("vmaas", "caas")),
        (["README.md", "fulfillment-service/main.go"], ("vmaas", "caas")),
        ([], ("vmaas", "caas")),
    ],
)
def test_the_suites_a_change_needs(files: list[str], expected: tuple[str, ...]) -> None:
    assert suites_for_files(parse_policy(POLICY_TEXT), files) == expected


def test_the_command_prints_the_suites_as_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "policy.yml").write_text(POLICY_TEXT, encoding="utf-8")
    (tmp_path / "files").write_text("tests/e2e/caas/test_a.py\n\n", encoding="utf-8")
    argv = ["e2e-suites", "--policy", str(tmp_path / "policy.yml"), "--files-from", str(tmp_path / "files")]
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == ["caas"]


def test_the_checks_flag_prints_each_needed_suite_with_the_name_of_its_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "policy.yml").write_text(POLICY_TEXT, encoding="utf-8")
    (tmp_path / "files").write_text("tests/e2e/caas/test_a.py\n", encoding="utf-8")
    argv = ["e2e-suites", "--policy", str(tmp_path / "policy.yml"), "--files-from", str(tmp_path / "files"), "--checks"]
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {"caas": "e2e-caas"}


def test_a_file_list_that_cannot_be_read_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "policy.yml").write_text(POLICY_TEXT, encoding="utf-8")
    argv = ["e2e-suites", "--policy", str(tmp_path / "policy.yml"), "--files-from", str(tmp_path / "missing")]
    assert cli.main(argv) == 3
    assert "cannot read" in capsys.readouterr().err


def test_a_file_list_that_is_not_text_is_an_error_too(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "policy.yml").write_text(POLICY_TEXT, encoding="utf-8")
    (tmp_path / "files").write_bytes(b"docs/\xff\xfe.md\n")
    argv = ["e2e-suites", "--policy", str(tmp_path / "policy.yml"), "--files-from", str(tmp_path / "files")]
    assert cli.main(argv) == 3
    assert "cannot read" in capsys.readouterr().err


def action() -> dict:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


def test_every_input_used_is_declared_and_nothing_is_expanded_into_a_script() -> None:
    used = set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", ACTION.read_text(encoding="utf-8")))
    assert used == set(action()["inputs"])
    for step in action()["runs"]["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")
        assert "checkout" not in step.get("uses", "")


def test_the_action_passes_both_files_to_the_command_and_sets_the_output(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    script = (
        "#!/bin/bash\n"
        f"echo \"$@\" >> '{tmp_path}/uv-args'\n"
        'if [[ "$*" == *--checks* ]]; then echo \'{"vmaas": "E2E / VMaaS Full Install"}\'; '
        "else echo '[\"vmaas\"]'; fi\n"
    )
    uv.write_text(script)
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    step = next(s["run"] for s in action()["runs"]["steps"] if s.get("name") == "List the suites")
    out = tmp_path / "output"
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "POLICY_FILE": "/w/p.yml", "FILES_FILE": "/w/files",
        "ACTION_PATH": "/a/b/c", "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }  # fmt: skip
    done = subprocess.run(["bash", "-c", step], env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert "e2e-suites --policy /w/p.yml --files-from /w/files" in (tmp_path / "uv-args").read_text()
    assert out.read_text().splitlines() == ['suites=["vmaas"]', 'suite-checks={"vmaas": "E2E / VMaaS Full Install"}']
