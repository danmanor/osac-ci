"""Run E2E suites by hand: the control comment of a pull request.

osac-ci keeps one comment on a pull request (marked by a hidden line) that lists the E2E suites of the repository, each
with a checkbox. A member of the organization who checks a box starts that suite for the head commit, whatever the
approval state of the pull request, on a draft, on their own pull request and on a fork. The box clears itself, and the
comment says what happened: started (with the run), already running, already passed. Nothing is started twice. The same
can be asked in a comment of one line, ``/test`` (the suites the change needs) or ``/test vmaas caas``.

* ``render_comment`` writes the comment, ``requested_suites`` reads the boxes that are checked;
* ``parse_command`` reads a ``/test`` comment; ``read_request`` checks that an edited comment is the control comment of
  this pull request (or that a command is the person's own), that the person is a member of the organization, and gives
  the suites asked for;
* ``refresh`` writes (creates or updates) the comment from the state of the head commit.

Only the OSAC CI app writes the comment, so a comment of anyone else with the same marker is never read as a request.
The first line of defense is that a person needs write access to edit a comment of the app at all; the code does not
rely on it and checks the member itself. The decision (``e2e_start``) never claims the E2E check for a run started by
hand: see ``e2e_check.finish_by_hand``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from osac_ci.e2e_start import CHECK_APP, NEEDED, Suite, line_state, suites_of
from osac_ci.github.api import GitHubClient, GitHubError, check_repo, get, paginate
from osac_ci.model import CheckRun
from osac_ci.planner import latest_checks
from osac_ci.policy import Policy

MARKER = "<!-- osac-ci:manual-run -->"
BOT_LOGIN = "osac-ci[bot]"  # who writes the control comment: the OSAC CI app
NOTES_START = "<!-- notes:start -->"
NOTES_END = "<!-- notes:end -->"
HEAD_MARK = re.compile(r"<!-- head:(?P<sha>[0-9a-f]{40}) -->")
SUITE_ID = r"[a-z0-9][a-z0-9_-]{0,39}"
BOX = re.compile(rf"^- \[(?P<mark>[ xX])\] <!-- suite:(?P<suite>{SUITE_ID}) -->", re.MULTILINE)
# The command: "/test" alone, or "/test" and suite ids, alone on the first line of a comment. Anything else is not one.
COMMAND = re.compile(rf"/test(?:[ \t]+(?P<names>{SUITE_ID}(?:[ \t]+{SUITE_ID}){{0,9}}))?[ \t]*")
LINE_PREFIX = "E2E / "
# What a suite's line says, with an icon the eye catches: the same four states as ``e2e_start.line_state``.
STATE_TEXT = {
    "none": "➖ not run on this commit",
    "active": "⏳ running",
    "passed": "✅ passed",
    "failed": "❌ failed",
}
RULES_URL = "https://github.com/osac-project/osac-ci/blob/main/docs/RULES.md#starting-suites-by-hand"


@dataclass(frozen=True)
class SuiteView:
    """One suite as the comment shows it: its id, its line name and what its line says on the head commit."""

    suite: str
    check: str
    state: str  # none | active | passed | failed
    url: str = ""

    @property
    def label(self) -> str:
        return self.check[len(LINE_PREFIX) :] if self.check.startswith(LINE_PREFIX) else self.check


def _alert(kind: str, lines: Sequence[str]) -> list[str]:
    """A GitHub alert block (a note, a tip): a quote whose first line names the kind."""
    return [f"> [!{kind}]", *(f"> {line}".rstrip() for line in lines)]


def render_comment(sha: str, views: Sequence[SuiteView], notes: str = "") -> str:
    """The comment: an alert that says what it is for, the suites with a checkbox each in a collapsed section, and the
    answer to the last request. Every box is unchecked: a box that was checked is cleared when the request has been
    answered."""
    lines = [
        MARKER,
        f"<!-- head:{sha} -->",
        *_alert(
            "TIP",
            [
                "## 🧪 E2E tests",
                "",
                "**Who:** any member of the organization, on any pull request (a draft, a fork or your own).",
                "",
                "**How to start a suite:**",
                "- Check a box below. The box clears itself.",
                "- Or comment `/test` for the suites this change needs.",
                "- Or comment `/test <suite>` for the ones you name (the ids are in the boxes below).",
                "",
                f"**Good to know:** suites run on the head commit `{sha[:7]}`. A suite that is already running, or "
                "has already passed on this commit, is not started again.",
            ],
        ),
        "",
        f"<details><summary><b>Run E2E tests now</b> ({len(views)} available)</summary>",
        "",
    ]
    for view in views:
        link = f" ([run]({view.url}))" if view.url else ""
        lines.append(
            f"- [ ] <!-- suite:{view.suite} --> **{view.label}** · {STATE_TEXT.get(view.state, view.state)}{link}"
        )
    lines += ["", "</details>", "", NOTES_START]
    if notes.strip():
        lines += _alert("NOTE", notes.strip().splitlines())
    lines += [NOTES_END, "", f"<sub>[How suites are started by hand]({RULES_URL}) · osac-ci</sub>", ""]
    return "\n".join(lines)


def requested_suites(body: str) -> tuple[str, ...]:
    """The suites whose box is checked in the comment, in the order they are listed."""
    return tuple(m.group("suite") for m in BOX.finditer(body) if m.group("mark") in "xX")


def parse_command(body: str) -> tuple[str, ...] | None:
    """The suites a ``/test`` comment asks for: the names after it, or ``(NEEDED,)`` for a bare ``/test`` (the suites
    the change needs). None when the comment is not a command. Only the first line counts, and it must be the command
    and nothing else, so a quote (``> /test``), a code fence, a sentence that mentions the command and a longer word
    (``/testing``) are not requests."""
    first = body.split("\n", 1)[0].rstrip("\r")
    found = COMMAND.fullmatch(first)
    if found is None:
        return None
    names = found.group("names")
    return tuple(dict.fromkeys(names.split())) if names else (NEEDED,)


def head_of(body: str) -> str:
    """The full head commit the comment was written for ("" for a comment that has none)."""
    found = HEAD_MARK.search(body)
    return found.group("sha") if found else ""


def notes_of(body: str) -> str:
    """The last answer the comment shows (its alert, without the quote marks), so a refresh keeps it until there is a
    newer one."""
    start, end = body.find(NOTES_START), body.find(NOTES_END)
    if start < 0 or end < start:
        return ""
    lines = [ln for ln in body[start + len(NOTES_START) : end].strip().splitlines() if ln.strip() != "> [!NOTE]"]
    return "\n".join(ln[2:] if ln.startswith("> ") else ln.removeprefix(">") for ln in lines).strip()


def answer_text(notes: Sequence[tuple[str, str]], by: str, run_url: str = "") -> str:
    """What happened to a request, one line per suite."""
    if not notes:
        return ""
    lines = []
    for suite, note in notes:
        subject = f"**{suite}**" if suite else "The request"
        lines.append(f"- {subject} {note}")
    tail = f" ([run]({run_url}))" if run_url else ""
    return f"**Last request by @{by}**{tail}\n" + "\n".join(lines)


def is_control_comment(comment: Mapping[str, object], bot: str = BOT_LOGIN) -> bool:
    user = comment.get("user")
    login = str(user.get("login", "")) if isinstance(user, Mapping) else ""
    return login == bot and MARKER in str(comment.get("body", ""))


def find_control_comment(
    client: GitHubClient, repo: str, number: int, bot: str = BOT_LOGIN
) -> Mapping[str, object] | None:
    for raw in paginate(client, f"/repos/{check_repo(repo)}/issues/{number}/comments"):
        if isinstance(raw, Mapping) and is_control_comment(raw, bot):
            return raw
    return None


def is_org_member(client: GitHubClient, org: str, login: str) -> bool:
    """A public or private member of the organization (204). Anything else - not a member, a bot, an unknown account -
    is not."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", org) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login
    ):
        return False
    response = client.request("GET", f"/orgs/{org}/members/{login}")
    if response.status == 204:
        return True
    if response.status in (302, 404):
        return False
    raise GitHubError(response.status, f"GET the membership of {login} in {org}")


def read_request(
    client: GitHubClient,
    repo: str,
    number: int,
    comment_id: int,
    sender: str,
    bot: str = BOT_LOGIN,
    org_client: GitHubClient | None = None,
) -> tuple[tuple[str, ...], str, str]:
    """The suites asked for by ``sender`` in the comment ``comment_id`` of pull request ``number``, the reason a
    request was refused (empty when none was) and the head commit the comment was written for (the decision refuses
    the request when the pull request has moved on). Two kinds of comment ask: an edit of the control comment of this
    pull request, written by the app, whose checked boxes name the suites; and a ``/test`` comment written by the
    person themselves (an account of a person, not a bot), which names them or asks for the suites the change needs. A
    command is not tied to a commit: it means "now". Only a member of the organization may ask. Nothing asked for is
    not a refusal: an edit that unchecks a box (the app clearing it) asks for nothing. ``org_client`` is the client
    that may read the organization's members (the main client when none is given)."""
    repo = check_repo(repo)
    comment = get(client, f"/repos/{repo}/issues/comments/{comment_id}")
    if not str(comment.get("issue_url", "")).endswith(f"/issues/{number}"):
        return (), "", ""
    body = str(comment.get("body", ""))
    if is_control_comment(comment, bot):
        asked = requested_suites(body)
        seen = head_of(body)
    else:
        author = comment.get("user")
        login = str(author.get("login", "")) if isinstance(author, Mapping) else ""
        kind = str(author.get("type", "")) if isinstance(author, Mapping) else ""
        command = parse_command(body)
        if command is None or kind != "User" or login.lower() != sender.lower():
            return (), "", ""
        asked, seen = command, ""
    if not asked:
        return (), "", ""
    org = repo.split("/", 1)[0]
    if not is_org_member(org_client or client, org, sender):
        return (), f"@{sender} is not a member of the {org} organization, so nothing was started", ""
    return asked, "", seen


def _conclusion(run: Mapping[str, object]) -> str | None:
    value = run.get("conclusion")
    return value if isinstance(value, str) else None


def _is_app(run: Mapping[str, object]) -> bool:
    app = run.get("app")
    return isinstance(app, Mapping) and app.get("slug") == CHECK_APP


def views_for(suites: Sequence[Suite], runs: Sequence[Mapping[str, object]]) -> tuple[SuiteView, ...]:
    """The suites with what their lines say, from the raw check runs of the head commit. Only the OSAC CI app's lines
    count: another app's check with the same name says nothing about a suite."""
    runs = [r for r in runs if _is_app(r)]
    latest = latest_checks(
        tuple(
            CheckRun(str(r["name"]), str(r["status"]), _conclusion(r), str(r.get("started_at") or ""), app=CHECK_APP)
            for r in runs
        )
    )
    urls: dict[str, str] = {}
    for r in sorted(runs, key=lambda r: str(r.get("started_at") or "")):
        urls[str(r["name"])] = str(r.get("details_url") or r.get("html_url") or "")
    return tuple(SuiteView(s.name, s.check, line_state(latest, s.check), urls.get(s.check, "")) for s in suites)


def refresh(
    client: GitHubClient,
    policy: Policy,
    repo: str,
    number: int,
    *,
    notes: str | None = None,
    bot: str = BOT_LOGIN,
    org_client: GitHubClient | None = None,
) -> str:
    """Create or update the control comment of the pull request from the state of its head commit. Nothing is written
    when the pull request is closed or its author is not a member of the organization, or when the text is the same
    (an edit would only wake the workflow again). ``notes`` replaces the last answer; None keeps the one the comment
    has."""
    repo = check_repo(repo)
    pr = get(client, f"/repos/{repo}/pulls/{number}")
    if str(pr.get("state")) != "open":
        return "skipped: the pull request is not open"
    suites = suites_of(policy)
    if not suites:
        return "skipped: the policy has no E2E suite"
    author = str((pr.get("user") or {}).get("login", ""))
    org = repo.split("/", 1)[0]
    existing = find_control_comment(client, repo, number, bot)
    if existing is None and not is_org_member(org_client or client, org, author):
        return "skipped: the author is not a member of the organization"
    sha = str(pr["head"]["sha"])
    runs = [
        r
        for r in paginate(client, f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs")
        if isinstance(r, Mapping)
    ]
    kept = notes if notes is not None else (notes_of(str(existing.get("body", ""))) if existing else "")
    body = render_comment(sha, views_for(suites, runs), kept)
    if existing is None:
        response = client.request("POST", f"/repos/{repo}/issues/{number}/comments", body={"body": body})
        if response.status != 201:
            raise GitHubError(response.status, "creating the control comment")
        # The early job (see e2e-pr-decide.yml) posts a placeholder at the same moment: two runs can both have found
        # none. Keep the oldest, the one that has the place in the conversation, and fill it in.
        oldest = find_control_comment(client, repo, number, bot)
        created = response.data.get("id") if isinstance(response.data, Mapping) else None
        if oldest is None or created is None or int(str(oldest["id"])) == int(created):
            return "created"
        gone = client.request("DELETE", f"/repos/{repo}/issues/comments/{created}")
        if gone.status not in (
            204,
            404,
        ):  # 404: it is already gone; anything else leaves a duplicate for the next refresh
            raise GitHubError(gone.status, "deleting the duplicate control comment")
        existing = oldest
        if str(existing.get("body", "")) == body:
            return "created (kept the older comment)"
        response = client.request("PATCH", f"/repos/{repo}/issues/comments/{existing['id']}", body={"body": body})
        if response.status != 200:
            raise GitHubError(response.status, "updating the control comment")
        return "created (kept the older comment)"
    if str(existing.get("body", "")) == body:
        return "unchanged"
    response = client.request("PATCH", f"/repos/{repo}/issues/comments/{existing['id']}", body={"body": body})
    if response.status != 200:
        raise GitHubError(response.status, "updating the control comment")
    return "updated"
