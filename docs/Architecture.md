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
  file times (ALEAPP reports `st_mtime`).
- **Search wrapper**: Hansken evaluates searcher queries as HQL, the SDK
  standalone test framework as HQL-Lite. `TraceFinder` in `plugin.py` sends a
  coarse name query in the right dialect (`HLEAPP_TEST_SEARCH=1` in `tox.ini`
  selects HQL-Lite) and filters directory and glob in Python.
- **Matcher**: hard-coded for step 1
  (`file.name='gmm_storage.db' AND $data.type=raw`); later derived from the
  ALEAPP artifact `paths` globs in `__artifacts_v2__` (issue #5).
- **Execution**: ALEAPP runs as a subprocess in a separate venv
  (`/opt/aleapp-venv`), because ALEAPP pins protobuf 5.x and the plugin SDK
  needs protobuf 7.x. An `.alprofile` limits ALEAPP to the selected module(s).
- **Output**: per input trace, child traces:
  - `ALEAPP report` — zipped ALEAPP output folder (HTML/TSV/LAVA)
  - one child per produced TSV (`<artifact name>`), with the TSV as raw data

  No mapping to native Hansken trace types yet.

## Phases
Tracked as GitHub issues, worked on as described in [Workflow.md](Workflow.md).
0. Done (#1): single artifact `googleMapsGmm` (Google Maps Directions) on
   `data/data/com.google.android.apps.maps/databases/gmm_storage.db` from
   `testdata/practical_exercise` (ALEAPP finds 4 directions).
1. #2 Filesystem emulation with a hard-coded matcher: deferred plugin that
   also fetches the sidecars (`gmm_storage.db-journal`) via the searcher.
2. #3 Map Hansken `file.path` to ALEAPP root paths (image/partition prefixes).
3. #4 One ALEAPP run per app directory instead of per file.
4. #5 Generate matchers from `__artifacts_v2__` globs.
5. #6 Richer output child traces.
6. #7 Docker image and integration test.
7. #8 Map selected artifacts to native Hansken trace types.

## Development setup
- Two venvs: plugin/tox (`requirements.txt`) and ALEAPP
  (`ALEAPP/requirements.txt`); point `ALEAPP_PYTHON` at the ALEAPP interpreter
  (tox passes `ALEAPP_*` through).
- SDK 0.10.0 `test_plugin` cannot parse 4-part Java versions
  (`21.0.12.1`); run `tox -- --skip-java-version-check`.
- Test data for a deferred plugin: files the plugin should find go in
  `testdata/input/<input name>/searchtraces/`. Their `.trace` needs `id` and
  `data.raw.size`, otherwise the test framework search fails with only
  `RuntimeError: Failed to fetch new search results`. The test framework also
  runs these files as test inputs of their own.
- Generate test data from the zip, not the checked-out tree (keeps the
  original file times):
  `python tools/tree_to_testdata.py testdata/practical_exercise.zip testdata/input '*/com.google.android.apps.maps/databases/gmm_storage.db' --siblings 'gmm_storage.db*'`

## Open issues
- ALEAPP globs vs Hansken `file.path` format (leading `/`, image prefix).
- Full-HQL name query (`file.name:<glob>`) is not yet verified on a real
  Hansken (wildcards, names with spaces such as `Web Data`).
- ALEAPP report zip contains run timestamps (logs, `index.html`, LAVA dbs,
  zip mtimes), so tests run with `HLEAPP_REPORT=0` (set in `tox.ini`) and only
  check the TSV child traces.
- ALEAPP runtime deps (`sqlcipher3`, git dep `mister_skinnylegs`) in the slim image.
