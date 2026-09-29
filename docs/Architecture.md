# Plan van aanpak — HLEAPP

## Goal
Hansken extraction plugin that runs ALEAPP on files Hansken already extracted.

## Choices
- **Input**: Hansken-based filesystem reconstruction. The plugin materialises
  matched Hansken traces into a temp dir at their `file.path` and runs ALEAPP
  with `-t fs` on it. ALEAPP code stays untouched (ALEAPP only reads files via
  its seekers, `ALEAPP/scripts/search_files.py`).
- **Matcher**: derived from the ALEAPP artifact `paths` globs in
  `__artifacts_v2__` (e.g. `*/system/build.prop` -> HQL on `file.path`).
  Start with one ALEAPP artifact.
- **Execution**: ALEAPP runs as a subprocess in a separate venv
  (`/opt/aleapp-venv`), because ALEAPP pins protobuf 5.x and the plugin SDK
  needs protobuf 7.x. An `.alprofile` limits ALEAPP to the selected module(s).
- **Output**: per input trace, child traces:
  - `ALEAPP report` — zipped ALEAPP output folder (HTML/TSV/LAVA)
  - one child per produced TSV (`<artifact name>`), with the TSV as raw data

  No mapping to native Hansken trace types yet.

## Phases
1. Single ALEAPP artifact `googleMapsGmm` (Google Maps Directions) on
   `gmm_storage.db`. Source of test data is the extracted tree in
   `testdata/practical_exercise`; starting point is
   `data/data/com.google.android.apps.maps/databases/gmm_storage.db`
   (ALEAPP finds 4 directions in it). `tools/tree_to_testdata.py` turns tree
   files into a `testdata/input` `.raw` + `.trace` pair (JSON with
   `file.name`, `file.path`, `file.extension`). The process plugin writes the
   trace to `tmp/<file.path>`, runs ALEAPP, attaches the output.
   Matcher is `file.name='gmm_storage.db'`; the glob's trailing `*`
   (sidecars `-journal`/`-wal`) is not handled yet.
2. Multi-file artifacts (sqlite sidecars, `settingsSecure`, Life360 dbs):
   deferred plugin using the searcher to fetch sibling traces into the
   reconstructed tree.
3. Generate matchers for all ALEAPP artifacts from `__artifacts_v2__`.
4. (Later) map selected artifacts to native Hansken trace types.

## Development setup
- Two venvs: plugin/tox (`requirements.txt`) and ALEAPP
  (`ALEAPP/requirements.txt`); point `ALEAPP_PYTHON` at the ALEAPP interpreter
  (tox passes `ALEAPP_*` through).
- SDK 0.10.0 `test_plugin` cannot parse 4-part Java versions
  (`21.0.12.1`); run `tox -- --skip-java-version-check`.

## Open issues
- ALEAPP globs vs Hansken `file.path` format (leading `/`, image prefix).
- Multi-file artifacts need a deferred plugin + searcher.
- ALEAPP report zip contains run timestamps (logs, `index.html`, LAVA dbs,
  zip mtimes), so tests run with `HLEAPP_REPORT=0` (set in `tox.ini`) and only
  check the TSV child traces.
- ALEAPP runtime deps (`sqlcipher3`, git dep `mister_skinnylegs`) in the slim image.
