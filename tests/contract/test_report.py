"""The open-PR report against a fake GitHub with several open PRs."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fakes import REPO, FakeGitHub
from helpers import ROOT

from osac_ci import cli
from osac_ci.model import State
from osac_ci.policy import load_policy
from osac_ci.report import build_report, md_escape, render_html, render_markdown, to_json, write_all

pytestmark = pytest.mark.contract
TOY = load_policy(ROOT / "policy" / "toy.yml")  # required label: approved; checks: lint, test
BASE = f"/repos/{REPO}"
NOW = "2026-10-08 06:00 UTC"


def add_pr(
    fake: FakeGitHub,
    listing: list[dict[str, Any]],
    number: int,
    *,
    title: str = "A change",
    updated: str = "2026-10-07T10:00:00Z",
    draft: bool = False,
    labels: tuple[str, ...] = ("approved",),
    lint: str = "success",
    author: str = "alice",
) -> None:
    sha = f"{number:040x}"
    listing.append(
        {
            "number": number,
            "title": title,
            "user": {"login": author},
            "html_url": f"https://github.com/{REPO}/pull/{number}",
            "updated_at": updated,
            "draft": draft,
        }
    )
    fake.add(
        "GET",
        f"{BASE}/pulls/{number}",
        {
            "number": number,
            "node_id": f"PR_{number}",
            "draft": draft,
            "user": {"login": author},
            "author_association": "MEMBER",
            "labels": [{"name": name} for name in labels],
            "head": {"sha": sha, "repo": {"full_name": REPO, "owner": {"login": "example"}}},
        },
    )
    fake.add("GET", f"{BASE}/pulls/{number}/reviews", [])
    fake.add("GET", f"{BASE}/issues/{number}/events", [])
    fake.add("GET", f"{BASE}/pulls/{number}/files", [{"filename": "a.go"}])
    runs = [
        {"name": "lint", "status": "completed", "conclusion": lint, "started_at": "2026-01-01T00:00:01Z"},
        {"name": "test", "status": "completed", "conclusion": "success", "started_at": "2026-01-01T00:00:02Z"},
    ]
    fake.add("GET", f"{BASE}/commits/{sha}/check-runs", {"total_count": 2, "check_runs": runs})


def world() -> tuple[FakeGitHub, list[dict[str, Any]]]:
    fake, listing = FakeGitHub(), []
    fake.add("POST", "/graphql", {"data": {"node": {"mergeQueueEntry": None}}})
    fake.routes[("GET", f"{BASE}/pulls")] = (200, listing)
    return fake, listing


def report(fake: FakeGitHub, **kwargs: Any):  # type: ignore[no-untyped-def]
    return build_report(fake, TOY, REPO, generated_at=NOW, org="example", **kwargs)


def test_one_row_per_open_pr_most_actionable_first() -> None:
    fake, listing = world()
    add_pr(fake, listing, 1, title="ready one")
    add_pr(fake, listing, 2, title="no approval", labels=())
    add_pr(fake, listing, 3, title="lint broke", lint="failure")
    rep = report(fake)
    assert [r.number for r in rep.rows] == [3, 2, 1]
    assert [r.verdict.state for r in rep.rows] == [State.CHECKS_FAILED, State.AWAITING_APPROVAL, State.READY_TO_ENQUEUE]
    assert rep.counts() == [
        (State.CHECKS_FAILED, 1),
        (State.AWAITING_APPROVAL, 1),
        (State.READY_TO_ENQUEUE, 1),
    ]


def test_within_a_state_the_most_recently_updated_pr_comes_first() -> None:
    fake, listing = world()
    add_pr(fake, listing, 1, updated="2026-10-01T00:00:00Z")
    add_pr(fake, listing, 2, updated="2026-10-07T00:00:00Z")
    add_pr(fake, listing, 3, updated="2026-10-04T00:00:00Z")
    assert [r.number for r in report(fake).rows] == [2, 3, 1]


def test_drafts_are_left_out_unless_asked_for() -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    add_pr(fake, listing, 2, draft=True)
    assert [r.number for r in report(fake).rows] == [1]
    with_drafts = report(fake, include_drafts=True)
    assert [r.number for r in with_drafts.rows] == [2, 1]  # a draft needs more attention than a ready PR
    assert next(r for r in with_drafts.rows if r.number == 2).verdict.state is State.DRAFT


def test_limit_is_respected() -> None:
    fake, listing = world()
    for n in range(1, 6):
        add_pr(fake, listing, n)
    assert len(report(fake, limit=2).rows) == 2


def test_a_pr_that_cannot_be_read_becomes_a_visible_error_row_and_the_rest_survive() -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    add_pr(fake, listing, 2)
    fake.add("GET", f"{BASE}/pulls/2/files", {"message": "Bad Gateway"}, status=502)
    rep = report(fake)
    by_number = {r.number: r for r in rep.rows}
    assert by_number[1].verdict.state is State.READY_TO_ENQUEUE
    assert by_number[2].verdict.state is State.PLANNER_ERROR
    assert "could not read PR #2" in by_number[2].verdict.headline and "502" in by_number[2].verdict.headline
    assert rep.rows[0].number == 2  # errors sort first, they must not hide


def test_listing_failure_raises_instead_of_writing_an_empty_report() -> None:
    from osac_ci.github.api import GitHubError

    fake, _ = world()
    fake.routes[("GET", f"{BASE}/pulls")] = (502, {"message": "Bad Gateway"})
    with pytest.raises(GitHubError):
        report(fake)


# untrusted text ------------------------------------------------------------------------------------------

HOSTILE = '<script>alert(1)</script> | [click](http://evil.example) `x` *y* "q"'


def hostile_report() -> Any:
    fake, listing = world()
    add_pr(fake, listing, 1, title=HOSTILE, author="<img src=x onerror=alert(1)>")
    return report(fake)


def test_html_escapes_titles_and_authors() -> None:
    page = render_html(hostile_report())
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<img src=x" not in page and "&lt;img src=x" in page
    assert len(re.findall(r"<script>", page)) == 1  # only the report's own filter script


def test_markdown_neutralizes_table_breaks_links_and_html() -> None:
    text = render_markdown(hostile_report())
    row = next(line for line in text.splitlines() if line.startswith("| [#1]"))
    assert "<script>" not in row and "[click](" not in row.split("|")[1] + row  # no live link from the title
    assert row.count("|") - row.count("\\|") == 8  # 7 cells: the title's pipe did not add a column


def test_md_escape_keeps_ordinary_text_readable_and_defuses_mentions() -> None:
    assert md_escape("pre-commit (osac-operator) #12") == "pre-commit (osac-operator) #12"
    assert md_escape("thanks @octocat") == "thanks @\u200bocto" + "cat"


def test_md_escape_flattens_whitespace_and_truncates() -> None:
    assert md_escape("a\nb\t c") == "a b c"
    assert md_escape("x" * 300, limit=10).endswith("…") and len(md_escape("x" * 300, limit=10)) == 10


def test_json_has_counts_and_every_pr() -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    add_pr(fake, listing, 2, labels=())
    data = json.loads(to_json(report(fake)))
    assert data["counts"] == {"awaiting-approval": 1, "ready-to-enqueue": 1}
    assert [p["number"] for p in data["prs"]] == [2, 1]
    assert data["prs"][0]["blockers"] == ["missing label: approved"]


def test_output_is_deterministic_for_the_same_input() -> None:
    fake, listing = world()
    for n in (3, 1, 2):
        add_pr(fake, listing, n)
    assert render_markdown(report(fake)) == render_markdown(report(fake))
    assert render_html(report(fake)) == render_html(report(fake))


def test_html_is_self_contained_and_lists_filter_chips() -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    page = render_html(report(fake))
    assert page.startswith("<!doctype html>") and 'data-state="ready-to-enqueue"' in page
    assert "http://" not in page.replace("https://github.com", "") and "src=" not in page  # no external loads


# the command ---------------------------------------------------------------------------------------------


def run_cli(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub, out: Path, *extra: str) -> int:
    monkeypatch.setattr(cli, "build_client", lambda: fake)
    return cli.main(
        ["report", "--policy", str(ROOT / "policy" / "toy.yml"), "--repo", REPO, "--org", "example",
         "--out-dir", str(out), *extra]
    )  # fmt: skip


def test_cli_writes_all_formats(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    assert run_cli(monkeypatch, fake, tmp_path / "out") == 0
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["report.html", "report.json", "report.md"]
    assert "1 open PRs" in capsys.readouterr().out


def test_cli_formats_option_and_validation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    assert run_cli(monkeypatch, fake, tmp_path, "--formats", "md") == 0
    assert [p.name for p in tmp_path.iterdir()] == ["report.md"]
    assert run_cli(monkeypatch, fake, tmp_path, "--formats", "pdf") == 2


def test_cli_exits_3_when_github_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake, _ = world()
    fake.routes[("GET", f"{BASE}/pulls")] = (403, {"message": "rate limited"})
    assert run_cli(monkeypatch, fake, tmp_path / "out") == 3
    assert "report failed" in capsys.readouterr().err and not (tmp_path / "out" / "report.md").exists()


def test_write_all_creates_the_directory(tmp_path: Path) -> None:
    fake, listing = world()
    add_pr(fake, listing, 1)
    names = write_all(report(fake), tmp_path / "a" / "b", ["json"])
    assert names == ["report.json"] and (tmp_path / "a" / "b" / "report.json").is_file()


def test_listing_open_prs_can_be_limited_to_one_base_branch_and_otherwise_lists_all() -> None:
    from osac_ci.report import list_open_prs

    fake, listing = world()
    add_pr(fake, listing, 1)
    add_pr(fake, listing, 2)
    listing[0]["base"] = {"ref": "main"}
    listing[1]["base"] = {"ref": "release-1"}
    assert [p["number"] for p in list_open_prs(fake, REPO, include_drafts=False)] == [1, 2]
    assert [p["number"] for p in list_open_prs(fake, REPO, include_drafts=False, base="main")] == [1]
    assert [p["number"] for p in list_open_prs(fake, REPO, include_drafts=False, base="nope")] == []
    listing.append({"number": 3, "draft": False})  # a listing entry without a base never matches a branch
    assert [p["number"] for p in list_open_prs(fake, REPO, include_drafts=False, base="main")] == [1]
