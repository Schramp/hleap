# Plan van aanpak — HLEAPP

## Goal
Hansken extraction plugin that runs ALEAPP on files Hansken already extracted.

## Choices
- **Input**: Hansken-based filesystem reconstruction. The plugin materialises
  matched Hansken traces into a temp dir at their `file.path` and runs ALEAPP
  with `-t fs` on it. ALEAPP code stays untouched (ALEAPP only reads files via
  its seekers, `ALEAPP/scripts/search_files.py`).
- **File access** (#15, replaced the up-front emulation of #2): the plugin is
  a deferred plugin; ALEAPP gets its files on demand, see *On-demand file
  access* below. Files are written with `file.modifiedOn`/`file.accessedOn`
  as file times (ALEAPP reports `st_mtime`). Deleted traces are left out in
  the query (`NOT type:deleted`; search results carry no trace types), traces
  without raw data are skipped, empty files are still written (whether a
  `-journal` exists is filesystem state).
- **Search dialects**: Hansken evaluates searcher queries as HQL, the SDK
  standalone test framework as HQL-Lite. `glob_query()` in `plugin.py` builds
  a coarse query in the right dialect (`HLEAPP_TEST_SEARCH=1` in `tox.ini`
  selects HQL-Lite) and the `Stager` filters exactly with the ALEAPP glob.
- **Matcher**: generated from the module anchors (`PLAN.matcher()` in
  `plugin.py`, #4/#13, see Target design). `process()` checks the trace path
  exactly against the anchors and runs every module it starts in one ALEAPP
  run (multi-module `.alprofile`).
- **Module selection** (#24): `hleapp.alprofile` (ALEAPP's own profile
  format, so ALEAPP's GUI can edit it too) selects the modules the plugin
  uses; path overridable with `HLEAPP_PROFILE`, all modules when the file is
  absent, unknown module names are an error. It now holds the 54 modules the
  example tree starts (Google Maps, Life360, AirTag, Mister Skinnylegs browser
  modules, `imagemngCache`): matcher 2.2 KB (13 clauses) instead of ~118 KB
  for all 1255 modules.
- **Execution**: ALEAPP runs as a subprocess in a separate venv
  (`/opt/aleapp-venv`), because ALEAPP pins protobuf 5.x and the plugin SDK
  needs protobuf 7.x. An `.alprofile` limits ALEAPP to the selected module(s).
- **Output**: per input trace, child traces:
  - `ALEAPP report` — zipped ALEAPP output folder (HTML/TSV/LAVA)
  - from the LAVA report (`_lava_data.lava` + `_lava_artifacts.db`, #11): a
    child per artifact category (`GEO Location`) and below it a child per
    non-empty artifact (`Google Maps Directions`), raw data = TSV of its
    LAVA table with the original ALEAPP headers. Typed columns
    (`object_columns` in the `.lava`) are rendered as ALEAPP's TSV export
    does: `datetime` (whole epoch seconds in LAVA) as `2024-04-23
    12:52:34+00:00`, `media` (a reference id) as its `media/<id>.<ext>`
    path in the report. Checked on 14 Google Maps/Life360 artifacts of
    `practical_exercise` (~31k rows): identical to `_TSV Exports` except
    for the sub-second part of timestamps, which LAVA does not keep.

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
  Implemented in `anchors.py`; `python tools/anchor_check.py
  testdata/practical_exercise.zip [--matcher]` prints the plan. Current
  ALEAPP: 1269 modules, 1255 kept, 14 dropped:
  - generic anchor: `get_walStrings` (`*/*-wal`), `get_c2paProvenance`
    (`*.jpg` …), `exoplayerCachedMedia`, `realmUndecodedStores`,
    `get_offlinePages`, `get_TorrentData`, `get_torrentinfo`,
    `get_torrentResumeinfo`, `clipboard`;
  - conflict on the test tree: `get_chromeAutofill`,
    `get_chromeAutofillProfiles`, `get_chromeCreditCards`,
    `get_chromePaymentsCustomerData` (`app_webview/Default/Web Data`),
    `get_Life360_chat_messages` (`cache/picasso-cache/journal`, anchor of the
    Life360 API cache modules).

  Conflicts depend on the files present, so the drop list grows with the
  trees it is checked against. The generated HQL-Lite matcher (~118 KB) parses
  in the SDK test framework and matches only `gmm_storage.db` of the test
  inputs; quoting, `type:folder` and matcher size on a real Hansken are #10.
  In Hansken there is no tree to find conflicts in, so the conflict drops are
  kept in `anchors.CONFLICT_DROPS`; a test fails when a test tree shows a
  conflict missing there.
- **Search scope per run** (#4): a module can have several anchors on one
  device (e.g. the 9 `cache/*/journal` files of the Life360 API cache
  modules). A glob that starts with the folder pattern of a started module's
  anchor (sidecars, cache entries next to their journal) is limited to that
  anchor's concrete folder; every other glob searches the whole image
  (`AnchorPlan.search_scope()`). On the test tree this removes all 1722
  duplicate stagings and loses no file. Limiting every glob to the anchor's
  folder would break the 220 modules that read files elsewhere (e.g.
  `get_ChessComAccount`: `shared_prefs` and `databases`); limiting to the app
  folder removes no duplicates.
- **On-demand file access** (#15): the plugin starts `hleapp_launcher.py` in
  the ALEAPP venv (with an empty `-i` folder). It replaces ALEAPP's
  `FileSeekerDir` by `HanskenSeeker` for that run only (`aleapp.py` does
  `from scripts.search_files import *`) and calls `aleapp.main()`; the ALEAPP
  checkout is not changed. Each `seeker.search(glob)` becomes the RPC
  `search_and_stage`, served by `Stager` in `plugin.py`:
  - `glob_query()`: `file.name` wildcard when the glob's name part has
    literal text, else a `file.path` wildcard; character classes become `?`;
    globs without any literal text are not searched;
  - the anchor `trace` is matched directly (a search never returns it);
  - results are filtered exactly with `fnmatch('root' + file.path, glob)` and
    written straight into ALEAPP's data folder (one copy per file, #12);
    requests for a folder outside ALEAPP's output folder are refused;
  - a glob that matches a folder stages nothing inside it, as
    `FileSeekerDir` does.
  No full listing up front. `tests/test_launcher.py` pins the ALEAPP
  internals the launcher relies on.
- **RPC channel** (#14, `hleapp_rpc.py`, standard library only, imported in
  both venvs): one end of a `socketpair` is passed to the ALEAPP child
  (`pass_fds`, fd number in `HLEAPP_RPC_FD`), so there is no socket file and
  nothing else can connect. Length-prefixed JSON requests/replies, no pickle.
  `run_aleapp()` serves requests on the `process()` thread (the searcher is
  only valid there) while polling ALEAPP, kills it on `ALEAPP_TIMEOUT`, and
  sends handler errors back as error replies. ALEAPP's output goes to
  `aleapp.log` in the work dir instead of a pipe, so it cannot block while
  requests are served.

## Roadmap
Tracked as GitHub issues with priority labels, worked on as described in
[Workflow.md](Workflow.md).

Done:
- #1 single artifact `googleMapsGmm` (Google Maps Directions) on
  `data/data/com.google.android.apps.maps/databases/gmm_storage.db` from
  `testdata/practical_exercise` (ALEAPP finds 4 directions).
- #2 filesystem emulation with a hard-coded matcher: deferred plugin that also
  fetches the sidecars (`gmm_storage.db-journal`) via the searcher.
- #11 LAVA child traces in `process()`, replacing the `_TSV Exports` children.

**P1 — critical path**, in this order (#10 runs alongside when a Hansken is
available, and must be done before production use):
1. Done (#13): anchor rules and conflict checker.
2. Done (#14): RPC channel between plugin and ALEAPP venv.
3. Done (#15): HanskenSeeker, on-demand file access (the same 4 rows from
   `gmm_storage.db`; ALEAPP now asks for the `-journal` itself).
4. Done (#4): one ALEAPP run per anchor trace with all modules it starts,
   matcher from the anchor plan, search scope per run.
5. #10 Verify the HQL query forms on a real Hansken (`file.name`/`file.path`
   wildcards, `NOT type:deleted`, names with spaces, time per search, folder
   traces as anchors).

**P2 — before production use**:
- #3 Map Hansken `file.path` to ALEAPP root paths.
- #7 Docker image and integration test.
- #12 Temp disk and memory use.

**P3 — later**:
- #6 Richer output child traces.
- #8 Map selected artifacts to native Hansken trace types.
- #9 Upstream bugs and repo hygiene.

Dropped: #5 (generate matchers from all globs) conflicts with the deferred
constraint; replaced by #13.

## Development setup
- Regenerate the module selection for a tree (the modules it starts):
  `python tools/anchor_check.py testdata/practical_exercise.zip --write-profile hleapp.alprofile`;
  check a selection with `--profile hleapp.alprofile [--matcher]`.
- Two venvs: plugin/tox (`requirements.txt`) and ALEAPP
  (`ALEAPP/requirements.txt`); point `ALEAPP_PYTHON` at the ALEAPP interpreter
  (tox passes `ALEAPP_*` through).
- SDK 0.10.0 `test_plugin` cannot parse 4-part Java versions
  (`21.0.12.1`); run `tox -- --skip-java-version-check`.
- `tox -e unit` runs the unit tests in `tests/` (pytest, e.g. the `Stager`
  against a fake searcher, the launcher in the ALEAPP venv); `tox -e py3` the
  SDK test framework.
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
  check the LAVA child traces.
- ALEAPP's LAVA stores `datetime` columns as whole seconds
  (`scripts/lavafuncs.py`, `_prepare_datetime_value`), so the child traces
  lose the milliseconds ALEAPP's HTML/TSV show. Fix in the Schramp/ALEAPP
  fork (store a float epoch) or upstream, #9.
- ALEAPP runtime deps (`sqlcipher3`, git dep `mister_skinnylegs`) in the slim image.
