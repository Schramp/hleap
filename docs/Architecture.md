# Plan van aanpak — HLEAPP

## Goal
Hansken extraction plugin that runs ALEAPP on files Hansken already extracted.

## Choices
- **Input**: Hansken-based filesystem reconstruction. The plugin materialises
  matched Hansken traces into a temp dir at their `file.path` and runs ALEAPP
  with `-t fs` on it. ALEAPP code stays untouched (ALEAPP only reads files via
  its seekers, `ALEAPP/scripts/search_files.py`).
- **Filesystem emulation** (step 1, issue #2): the plugin is a deferred
  plugin. Besides the matched trace it searches the files next to it that
  match the artifact globs (sqlite sidecars `-journal`/`-wal`/`-shm`) and
  writes them into the tree too, with `file.modifiedOn`/`file.accessedOn` as
  file times (ALEAPP reports `st_mtime`). Deleted traces are left out in the
  query (`NOT type:deleted`; search results carry no trace types), traces
  without raw data are skipped, empty files are still written (whether a
  `-journal` exists is filesystem state).
- **Search wrapper**: Hansken evaluates searcher queries as HQL, the SDK
  standalone test framework as HQL-Lite. `TraceFinder` in `plugin.py` sends a
  coarse name query in the right dialect (`HLEAPP_TEST_SEARCH=1` in `tox.ini`
  selects HQL-Lite) and filters directory and glob in Python.
- **Matcher**: hard-coded for step 1
  (`file.name='gmm_storage.db' AND $data.type=raw`); later generated from the
  module anchors (#13, see Target design).
- **Execution**: ALEAPP runs as a subprocess in a separate venv
  (`/opt/aleapp-venv`), because ALEAPP pins protobuf 5.x and the plugin SDK
  needs protobuf 7.x. An `.alprofile` limits ALEAPP to the selected module(s).
- **Output**: per input trace, child traces:
  - `ALEAPP report` — zipped ALEAPP output folder (HTML/TSV/LAVA)
  - one child per produced TSV (`<artifact name>`), with the TSV as raw data

  No mapping to native Hansken trace types yet.

## Target design
- **Deferred constraint (hard rule)**: traces matched by a deferred plugin are
  not indexed, so `searcher.search()` never finds them. Every file ALEAPP
  needs, except the anchor passed in as `trace`, must be outside the matcher.
  The SDK test framework always serves `searchtraces/`, so tests do not catch
  a violation; the conflict checker (#13) does.
- **Anchors** (#13): each ALEAPP module is started by its first glob
  (trailing sidecar `*` stripped; a folder when the glob ends in `/*`). The
  matcher is the OR of the anchors of the kept modules; a trace triggers every
  module whose anchor matches it, and those run together in one ALEAPP run
  (#4). Conflicting modules are dropped, e.g. `get_walStrings` (anchor
  `*/*-wal` covers every WAL other modules need) and `get_chromeAutofill*` /
  `get_chromeCreditCards` (need `app_webview/Default/Web Data`, an anchor of
  the Mister Skinnylegs modules); also anchors without literal text
  (`*.jpg`, `*/*.cnt`, …).
- **On-demand file access** (#14, #15): instead of emulating the filesystem up
  front, a launcher in the ALEAPP venv replaces ALEAPP's `FileSeekerDir` by a
  `HanskenSeeker`. Each `seeker.search(glob)` becomes an RPC to the plugin
  (`multiprocessing.connection`, Unix socket, JSON), which turns the glob into
  a Hansken query, filters exactly with ALEAPP's glob, and writes the hits
  straight into ALEAPP's data folder (one copy, #12). No full listing up
  front. The anchor is staged from `trace`; a folder hit fetches its subtree.
  ALEAPP itself stays untouched.

## Roadmap
Tracked as GitHub issues with priority labels, worked on as described in
[Workflow.md](Workflow.md).

Done:
- #1 single artifact `googleMapsGmm` (Google Maps Directions) on
  `data/data/com.google.android.apps.maps/databases/gmm_storage.db` from
  `testdata/practical_exercise` (ALEAPP finds 4 directions).
- #2 filesystem emulation with a hard-coded matcher: deferred plugin that also
  fetches the sidecars (`gmm_storage.db-journal`) via the searcher.

**P1 — critical path**, in this order (#10 runs alongside when a Hansken is
available, and must be done before production use):
1. #13 Anchor rules and conflict checker.
2. #14 RPC channel between plugin and ALEAPP venv.
3. #15 HanskenSeeker: on-demand file access (acceptance: the same 4 rows from
   `gmm_storage.db`).
4. #10 Verify the HQL query forms on a real Hansken (`file.name`/`file.path`
   wildcards, `NOT type:deleted`, names with spaces, time per search, folder
   traces as anchors).

**P2 — before production use**:
- #4 One ALEAPP run per anchor trace with all modules it triggers.
- #3 Map Hansken `file.path` to ALEAPP root paths.
- #11 Use LAVA child traces in `process()`.
- #7 Docker image and integration test.
- #12 Temp disk and memory use.

**P3 — later**:
- #6 Richer output child traces.
- #8 Map selected artifacts to native Hansken trace types.
- #9 Upstream bugs and repo hygiene.

Dropped: #5 (generate matchers from all globs) conflicts with the deferred
constraint; replaced by #13.

## Development setup
- Two venvs: plugin/tox (`requirements.txt`) and ALEAPP
  (`ALEAPP/requirements.txt`); point `ALEAPP_PYTHON` at the ALEAPP interpreter
  (tox passes `ALEAPP_*` through).
- SDK 0.10.0 `test_plugin` cannot parse 4-part Java versions
  (`21.0.12.1`); run `tox -- --skip-java-version-check`.
- `tox -e unit` runs the unit tests in `tests/` (pytest, e.g. the FS
  emulation against a fake searcher); `tox -e py3` the SDK test framework.
- Test data for a deferred plugin: files the plugin should find go in
  `testdata/input/<input name>/searchtraces/`. Their `.trace` needs `id` and
  `data.raw.size`, otherwise the test framework search fails with only
  `RuntimeError: Failed to fetch new search results`. The test framework also
  runs these files as test inputs of their own. A search trace is marked
  deleted with an (empty) `"deleted": {}` object; traces without a `.raw`
  file are not served at all.
- Generate test data from the zip, not the checked-out tree (keeps the
  original file times):
  `python tools/tree_to_testdata.py testdata/practical_exercise.zip testdata/input '*/com.google.android.apps.maps/databases/gmm_storage.db' --siblings 'gmm_storage.db*'`

## Open issues
- ALEAPP globs vs Hansken `file.path` format (leading `/`, image prefix), #3.
- Full-HQL queries are not yet verified on a real Hansken, #10.
- ALEAPP report zip contains run timestamps (logs, `index.html`, LAVA dbs,
  zip mtimes), so tests run with `HLEAPP_REPORT=0` (set in `tox.ini`) and only
  check the TSV child traces.
- ALEAPP runtime deps (`sqlcipher3`, git dep `mister_skinnylegs`) in the slim image.
