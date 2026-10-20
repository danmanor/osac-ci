# Using the shared flow in a repository

osac-ci hosts the shared workflows of the native pull request flow, so a repository keeps only short callers, its own
policy and its own tests. Nothing on the pull request page changes: the same `CI`, `E2E` and `jira-reference` checks, the
same suite lines (`E2E / VMaaS Full Install` ...), the same wait reasons. The rules are in `policy/flow.yml` (read them as a page: [`RULES.md`](RULES.md), generated from the file) (who counts as
an approver, which bots may be approved without a person, which checks are not "cheap"); a repository cannot change them.

## What stays in the repository

- the triggers (GitHub requires them in the repository): `workflow_run`, `merge_group`, `schedule`, `workflow_dispatch`;
- the suite jobs (`uses: ./.github/workflows/e2e-<suite>-full-install.yml`): a `uses:` cannot be dynamic, and the suites,
  their inputs and their tests belong to the people who change the product;
- `.github/e2e-policy.yml` (which suites a change needs) and the path filters it points at;
- `ci.yml` (the cheap checks and the `CI` aggregate) and `jira-reference.yml`, so the required check names stay in the
  repository's own workflows.

## What the shared workflows do

| Workflow | Used by | What it does |
|---|---|---|
| `e2e-pr-comment.yml` | the repo's `e2e-comment.yml` | posts the placeholder of the E2E control comment the moment a pull request of a member opens, so it is above CodeRabbit's |
| `e2e-pr-decide.yml` | the repo's `e2e-pr.yml` | decides whether the E2E may start and keeps the `E2E` check; housekeeping: auto-merge for an approved pull request, deletes the finished review relay run, early comment run and the superseded Jira check runs |
| `e2e-pr-track.yml` | `e2e-pr.yml`, next to the suites | one check per suite on the pull request |
| `e2e-pr-finish.yml` | `e2e-pr.yml`, after the suites | records the result, also after a cancel |
| `e2e-queue-plan.yml` | `e2e-queue.yml` | plans the suites of a merge queue entry; cancels superseded entries |
| `e2e-queue-finish.yml` | `e2e-queue.yml`, after the suites | writes the required `E2E` check of the queue entry |
| `bot-sweep.yml` | `bot-approve.yml` | approves dependency updates of the update bots when no person needs to read them |

Call each one by a full commit SHA of an osac-ci release, with the tag in a comment, for example
`osac-project/osac-ci/.github/workflows/e2e-pr-decide.yml@<40 hex digits>  # v1.2.0`. Pass the app key explicitly:
`secrets: { OSAC_CI_APP_PRIVATE_KEY: ${{ secrets.OSAC_CI_APP_PRIVATE_KEY }} }`, never `secrets: inherit`.

## The caller of the pull request flow (`.github/workflows/e2e-pr.yml`)

```yaml
name: E2E for pull requests
# The decision cancels the E2E of older commits by reading run titles: keep this exact format.
run-name: >-
  ${{ github.event_name == 'workflow_dispatch' && format('E2E #{0} (started by hand)', inputs.number) ||
  github.event_name == 'issue_comment' && format('E2E #{0} (suites checked in the comment)', github.event.issue.number) ||
  format('E2E {0}:{1} @ {2}', github.event.workflow_run.head_repository.full_name, github.event.workflow_run.head_branch,
  github.event.workflow_run.head_sha) }}

on:
  workflow_run:
    workflows: ["CI", "E2E review relay", "E2E comment"]   # the names are inputs of the decision workflow (ci-workflow-name, relay-workflow-name, comment-workflow-name)
    types: [requested, completed]
  issue_comment:
    types: [edited]                          # the boxes of the control comment (suites started by hand)
  workflow_dispatch:
    inputs:
      number:
        description: Pull request number
        required: true

permissions: {}

jobs:
  decide:
    uses: osac-project/osac-ci/.github/workflows/e2e-pr-decide.yml@<sha>  # <tag>
    permissions:
      contents: read
      pull-requests: read
      checks: read
      actions: write
    secrets:
      OSAC_CI_APP_PRIVATE_KEY: ${{ secrets.OSAC_CI_APP_PRIVATE_KEY }}

  track:
    needs: decide
    if: needs.decide.outputs.start == 'true'
    uses: osac-project/osac-ci/.github/workflows/e2e-pr-track.yml@<sha>  # <tag>
    permissions:
      contents: read
    with:
      sha: ${{ needs.decide.outputs.sha }}
      suite-checks: ${{ needs.decide.outputs.suite-checks }}
    secrets:
      OSAC_CI_APP_PRIVATE_KEY: ${{ secrets.OSAC_CI_APP_PRIVATE_KEY }}

  # One job per suite, named after the suite id (the policy gives the suite its id and the name of its line).
  vmaas:
    needs: decide
    if: needs.decide.outputs.start == 'true' && contains(needs.decide.outputs.suites, '"vmaas"')
    permissions:
      contents: read
      id-token: write
    uses: ./.github/workflows/e2e-vmaas-full-install.yml
    with:
      repo: ${{ needs.decide.outputs.head-repo }}
      sha: ${{ needs.decide.outputs.sha }}
      pr-number: ${{ needs.decide.outputs.number }}
      fork-pr-author: ${{ needs.decide.outputs.fork-author }}
      fork-pr-author-association: ${{ needs.decide.outputs.fork-association }}

  finish:
    needs: [decide, track, vmaas]            # decide, track and every suite
    if: always() && needs.decide.outputs.start == 'true'
    uses: osac-project/osac-ci/.github/workflows/e2e-pr-finish.yml@<sha>  # <tag>
    permissions:
      contents: read
    with:
      sha: ${{ needs.decide.outputs.sha }}
      suites: ${{ needs.decide.outputs.suites }}
      suite-checks: ${{ needs.decide.outputs.suite-checks }}
      number: ${{ needs.decide.outputs.number }}
      manual: ${{ needs.decide.outputs.manual }}
      selected-checks: ${{ needs.decide.outputs.selected-checks }}
      results: ${{ toJSON(needs) }}          # the jobs that are not suites are listed in non-suite-jobs (default decide,track)
    secrets:
      OSAC_CI_APP_PRIVATE_KEY: ${{ secrets.OSAC_CI_APP_PRIVATE_KEY }}
```

The review relay (`e2e-review-relay.yml`) is a workflow named `E2E review relay` that does nothing on
`pull_request_review` and on `pull_request` `ready_for_review`/`converted_to_draft`; finishing wakes `e2e-pr.yml`.

## The caller of the early comment (`.github/workflows/e2e-comment.yml`)

```yaml
name: E2E comment

on:
  pull_request_target:        # runs from the default branch with the secret, and checks out nothing of the pull request
    types: [opened, reopened]

permissions: {}

jobs:
  comment:
    uses: osac-project/osac-ci/.github/workflows/e2e-pr-comment.yml@<sha>  # <tag>
    permissions: {}
    secrets:
      OSAC_CI_APP_PRIVATE_KEY: ${{ secrets.OSAC_CI_APP_PRIVATE_KEY }}
```

A comment cannot be moved, and CodeRabbit posts about nine seconds after a pull request opens. The decision's own early
job starts only when CI is requested (about seven seconds in) and then needs a few more, so this workflow, which starts
with the pull request, posts the placeholder sooner. The decision keeps its job as a second chance (a reopened pull
request, a missed event) and fills the placeholder in. When this run finishes, the `E2E comment` entry in the `workflow_run`
list of `e2e-pr.yml` wakes the decision, which deletes the run so its row does not stay on the pull request.

## Suites started by hand

The decision workflow keeps a comment on the pull request of a member of the organization (written by the OSAC CI app)
with a checkbox per suite; the caller above only needs the `issue_comment` trigger, the three-way `run-name` and the two
extra inputs of `finish`. The comment lists the suites of `.github/e2e-policy.yml`; a job with `manual_only: true` is
listed and can be started from the comment, but the automatic decision never chooses it and the `E2E` check does not wait
for it. Its suite job in `e2e-pr.yml` is written like the others (`if: ... contains(needs.decide.outputs.suites, '"<id>"')`).
The rules are in [`RULES.md`](RULES.md).

## The caller of the merge queue (`.github/workflows/e2e-queue.yml`)

`plan: uses: ...e2e-queue-plan.yml@<sha>` (job permissions: contents read, actions write), one suite job per suite with
`needs: plan` and `if: contains(needs.plan.outputs.suites, '"<id>"')` calling the repository's suite workflow with
`repo: ${{ github.repository }}` and `sha: ${{ github.event.merge_group.head_sha }}`, and
`finish: needs: [plan, <suites>]`, `if: always()`, `uses: ...e2e-queue-finish.yml@<sha>` with
`sha: ${{ github.event.merge_group.head_sha }}`, `results: ${{ toJSON(needs) }}` and the app key.

## The caller of the bot sweep (`.github/workflows/bot-approve.yml`)

`on: schedule: [{cron: '3,18,33,48 * * * *'}]` and `workflow_dispatch` with a `number` input, a `concurrency` group so one
sweep runs at a time, and `sweep: uses: ...bot-sweep.yml@<sha>` with `number: ${{ inputs.number }}`, optionally
`konflux-prep: true`, the app key, and `MERGE_QUEUE_TOKEN` only to keep the old token for the Konflux preparation (deprecated: the app does it now). Job permissions: contents
read, pull-requests read.

## How the shared workflows find their own actions

Inside a called workflow `uses: $/.github/actions/<name>` means "this workflow's own repository at the commit the caller
pinned", so the actions used are the ones reviewed with the workflow (a plain `./` resolves against the caller's
workspace). This was verified in a cross-repository call on 2026-10-17. actionlint does not know the syntax yet, so
`.github/actionlint.yaml` ignores that one message.
