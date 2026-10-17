"""The rules page is generated from policy/flow.yml: it says what the rules say, and a stale page fails the build."""

from __future__ import annotations

from pathlib import Path

import pytest

from osac_ci import cli
from osac_ci.flow import flow
from osac_ci.policy import parse_policy
from osac_ci.rules_page import PAGE_PATH, render_rules

ROOT = Path(__file__).resolve().parents[2]

POLICY = """
version: 1
repo: example/repo
jobs:
  e2e-one:
    kind: e2e
    check: "E2E / One"
    suite: one
    filters_any: [code]
  e2e-two:
    kind: e2e
    check: "E2E / Two"
    suite: two
    filters: [touched]
    filters_any: [code]
path_filters:
  mode: enforce
  filters:
    code: ['**']
    touched: ['two/**']
"""


def test_the_committed_page_is_what_the_rules_produce() -> None:
    assert (ROOT / PAGE_PATH).read_text(encoding="utf-8") == render_rules(flow())


def test_every_rule_value_is_on_the_page() -> None:
    rules = flow()
    page = render_rules(rules)
    values = {
        *rules.trusted_bots,
        *rules.write_permissions,
        *rules.not_cheap,
        *rules.bot_approve.bots,
        rules.bot_approve.committer,
        *rules.dependency_file_names,
        *rules.bot_approve.dependency_file_patterns,
        *rules.repository_may_extend,
    }
    assert values
    assert [v for v in values if f"`{v}`" not in page] == []


def test_the_page_is_deterministic() -> None:
    assert render_rules(flow()) == render_rules(flow())


def test_the_suites_of_a_repository_are_listed() -> None:
    page = render_rules(flow(), [parse_policy(POLICY)])
    assert "### example/repo" in page
    assert "| `one` | E2E / One | one of `code` |" in page
    assert "| `two` | E2E / Two | all of `touched`; one of `code` |" in page


def test_check_passes_for_the_committed_page_and_fails_for_a_stale_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fresh = tmp_path / "RULES.md"
    assert cli.main(["explain-rules", "--output", str(fresh)]) == 0
    assert cli.main(["explain-rules", "--output", str(fresh), "--check"]) == 0
    fresh.write_text(fresh.read_text(encoding="utf-8") + "\nedited by hand\n", encoding="utf-8")
    assert cli.main(["explain-rules", "--output", str(fresh), "--check"]) == 1
    assert "stale" in capsys.readouterr().err


def test_check_needs_an_output_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["explain-rules", "--check"]) == 2
    assert "--output" in capsys.readouterr().err


def test_without_an_output_the_page_is_printed(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["explain-rules"]) == 0
    assert capsys.readouterr().out == render_rules(flow())
