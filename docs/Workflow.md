# Workflow — issues, branches and pull requests

Work on HLEAPP runs issue → branch → pull request → merge. `main` only
changes through merged pull requests and should always pass the tests.

## 1. Issues
- Every piece of work starts as a GitHub issue. The roadmap steps are issues
  #2–#8 (see [Architecture.md](Architecture.md#phases)); chores and upstream
  bugs go in their own issues.
- Describe the goal, a task list (`- [ ]`) and a **Done when** line that
  says how to verify it (usually a `tox` result).
- Keep issues small enough for one pull request. When an issue grows, split
  off a new issue and link it (`Follow-up of #N`).
- Findings along the way (e.g. test framework quirks, HQL vs HQL-Lite)
  go in an issue comment, and in `docs/` when they last.

## 2. Branches
- Branch from an up-to-date `main`, one branch per issue:
  ```shell
  git switch main && git pull
  git switch -c 2-fs-emulation
  ```
- Name: `<issue number>-<short-slug>`, e.g. `3-path-mapping`,
  `9-gitignore-profile`.
- Commit often; start the commit subject with what changed, and mention the
  issue in the body (`Part of #2`).
- Keep the branch up to date with `git pull --rebase origin main`, not merge
  commits.
- The `ALEAPP` submodule: only bump it in its own commit, with the ALEAPP
  version in the message.

## 3. Before opening a pull request
- Tests pass, twice in a row (catches non-reproducible results):
  ```shell
  export ALEAPP_PYTHON=~/.venvs/aleapp/bin/python
  tox -- --skip-java-version-check
  ```
- Changed plugin output? Regenerate and **review** the expected results
  before committing them:
  ```shell
  tox -e regenerate -- --skip-java-version-check
  git diff testdata/result
  ```
  Do not commit `testdata/result/*.profile.txt` (per-run timings).
- New test input comes from `tools/tree_to_testdata.py` run on
  `testdata/practical_exercise.zip`, so it keeps the original file times.
- `docs/Architecture.md` is updated when a choice, phase or open issue
  changed.

## 4. Pull requests
- Push the branch and open a pull request against `main`:
  ```shell
  git push -u origin 2-fs-emulation
  gh pr create --fill
  ```
- Description: what changed and why, how it was tested (the `tox` result),
  and what is left. End with `Closes #N` when the PR finishes the issue, or
  `Part of #N` when it does not.
- Open it as a draft (`gh pr create --draft`) for early feedback.
- Review: at least one look by someone other than the author when
  possible; the author answers or resolves every comment.

## 5. Merge
- Squash-merge, so `main` has one commit per pull request, and delete the
  branch:
  ```shell
  gh pr merge --squash --delete-branch
  ```
- `Closes #N` closes the issue on merge. Tick off the issue's task list, or
  move unfinished tasks to a new issue.
- Locally: `git switch main && git pull`, and remove the local branch.
