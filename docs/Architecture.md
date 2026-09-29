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
1. Single-file artifact `build.py` (`build.prop`). Test data = `build.prop` +
   `.trace` JSON with `file.path`/`file.name`. Process plugin writes the file to
   `tmp/<file.path>`, runs ALEAPP, attaches the output.
2. Multi-file artifacts (e.g. `settingsSecure`, sqlite + `-wal`): deferred
   plugin using the searcher to fetch sibling traces into the reconstructed tree.
3. Generate matchers for all ALEAPP artifacts from `__artifacts_v2__`.
4. (Later) map selected artifacts to native Hansken trace types.

## Open issues
- ALEAPP globs vs Hansken `file.path` format (leading `/`, image prefix).
- Multi-file artifacts need a deferred plugin + searcher.
- ALEAPP runtime deps (`sqlcipher3`, git dep `mister_skinnylegs`) in the slim image.
