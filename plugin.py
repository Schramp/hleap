import csv
import datetime
import fnmatch
import functools
import io
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import tempfile
import time
import zipfile

from hansken_extraction_plugin.api.extraction_plugin import DeferredExtractionPlugin
from hansken_extraction_plugin.api.plugin_info import Author, MaturityLevel, PluginId, PluginInfo, PluginResources
from hansken_extraction_plugin.runtime.extraction_plugin_runner import run_with_hanskenpy
from logbook import Logger

from anchors import CONFLICT_DROPS, AnchorPlan, load_modules, load_profile
from hleapp_rpc import RpcServer

log = Logger(__name__)

# ALEAPP runs in its own interpreter: it pins protobuf 5.x, the plugin SDK needs protobuf 7.x
ALEAPP_DIR = os.environ.get('ALEAPP_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ALEAPP'))
ALEAPP_PYTHON = os.environ.get('ALEAPP_PYTHON', '/opt/aleapp-venv/bin/python')
LAUNCHER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hleapp_launcher.py')
ALEAPP_TIMEOUT = int(os.environ.get('ALEAPP_TIMEOUT', '600'))
# the zipped report holds run timestamps, tests turn it off to get reproducible results
HLEAPP_REPORT = os.environ.get('HLEAPP_REPORT', '1') != '0'

REPORT_FOLDER = 'report'
REPORT_CHILD = 'ALEAPP report'
# LAVA output of ALEAPP: artifact metadata (json) and one sqlite table per artifact
LAVA_JSON = '_lava_data.lava'
LAVA_DB = '_lava_artifacts.db'


# which ALEAPP modules run (the ALEAPP profile hleapp.alprofile, #24; all modules when it is absent), started by
# which traces (anchors.py, #13); conflicting modules are dropped
PROFILE = os.environ.get('HLEAPP_PROFILE', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hleapp.alprofile'))
_MODULES = load_modules(ALEAPP_DIR)
PLAN = AnchorPlan(_MODULES, drop=CONFLICT_DROPS,
                  select=load_profile(PROFILE, _MODULES) if os.path.isfile(PROFILE) else None)
MATCHER = PLAN.matcher()
SEARCH_LIMIT = 100


def timestamp(value):
    """Hansken dates arrive as datetime or ISO string, return epoch seconds or None."""
    if isinstance(value, str):
        value = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value.timestamp() if isinstance(value, datetime.datetime) else None


def materialize(trace, fs_dir):
    """Write the raw data of a trace into fs_dir at its file.path, with its Hansken timestamps.

    ALEAPP's FileSeekerDir reports st_ctime/st_mtime of the files it finds, so the modification time is carried
    over; the creation time cannot be set on Linux.
    """
    rel_path = trace.get('file.path').lstrip('/')
    target = os.path.join(fs_dir, rel_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, 'wb') as writer:
        # an empty file is still written: whether e.g. a -journal exists is part of the filesystem state
        if trace.get('data.raw.size') != 0:
            with trace.open() as reader:
                while chunk := reader.read(1024 * 1024):
                    writer.write(chunk)
    modified = timestamp(trace.get('file.modifiedOn'))
    if modified is not None:
        accessed = timestamp(trace.get('file.accessedOn')) or modified
        os.utime(target, (accessed, modified))
    return rel_path


def glob_query(glob, hql_lite):
    """A Hansken query that finds at least every trace an ALEAPP glob matches, or None when the glob has no literal
    text to select on. Callers filter the results exactly with fnmatch('root' + file.path, glob).

    Hansken evaluates searcher queries as HQL, the SDK standalone test framework as HQL-Lite (the matcher language);
    tests set HLEAPP_TEST_SEARCH=1 (tox.ini). Search results carry no trace types, so deleted files can only be left
    out in the query.
    """
    pattern = re.sub(r'\[[^\]]*\]', '?', glob)  # character classes widen to '?'
    pattern = re.sub(r'\*{2,}', '*', pattern)
    leaf = pattern.rstrip('/').rsplit('/', 1)[-1]
    if re.sub(r'[*?]', '', leaf):
        field, value = 'file.name', leaf
    elif re.sub(r'[*?/]', '', pattern):
        # the name is only wildcards ('.../cache/files/*'): select on the path, Hansken paths start with '/'
        field, value = 'file.path', pattern
    else:
        return None
    if hql_lite:
        return f"{field}='{value}' AND NOT type:deleted"
    # TODO #10 verify on a real Hansken: wildcards, NOT, quoting of names with spaces or HQL characters
    value = f'"{value}"' if ' ' in value else value
    return f'{field}:{value} AND NOT type:deleted'


class Stager:
    """Serves ALEAPP's seeker over RPC: finds the traces matching an ALEAPP glob and writes them into ALEAPP's
    data folder, one copy per file (see hleapp_launcher.HanskenSeeker).

    The anchor trace (the one process() got) is matched here directly: under the deferred constraint a search never
    returns it. scope(glob) gives the ALEAPP style folder a glob's results are limited to, or None for the whole
    image (AnchorPlan.search_scope).
    """

    def __init__(self, anchor, searcher, out_dir, scope=None, anchor_is_dir=False,
                 hql_lite=os.environ.get('HLEAPP_TEST_SEARCH', '0') == '1'):
        self._anchor = anchor
        self._searcher = searcher
        self._out_dir = os.path.realpath(out_dir)
        self._scope = scope or (lambda glob: None)
        self._anchor_is_dir = anchor_is_dir
        self._hql_lite = hql_lite
        self._staged = {}

    @property
    def staged_paths(self):
        return sorted(item['source_path'] for item in self._staged.values())

    def search_and_stage(self, glob, data_folder, first_hit=False):
        data_folder = os.path.realpath(data_folder)
        # the request comes from another process: only ever write inside ALEAPP's own output folder
        if os.path.commonpath([self._out_dir, data_folder]) != self._out_dir:
            raise ValueError(f'data folder {data_folder} is outside the ALEAPP output folder')
        candidates = [self._anchor]
        query = glob_query(glob, self._hql_lite)
        if query:
            found = list(self._searcher.search(query, count=SEARCH_LIMIT))
            log.debug(f'ALEAPP glob {glob}: search {query!r} returned {len(found)} traces')
            candidates += found
        else:
            log.info(f'ALEAPP glob {glob} has no literal text to search on, skipped')
        folder = self._scope(glob)
        results, seen = [], set()
        for trace in candidates:
            path = trace.get('file.path')
            if not path or path in seen or not fnmatch.fnmatch('root' + path, glob):
                log.debug(f'ALEAPP glob {glob}: {path!r} does not match or was seen already')
                continue
            if folder and not ('root' + path).startswith(folder + '/'):
                log.debug(f'ALEAPP glob {glob}: {path} is outside the search scope {folder}')
                continue  # belongs to another anchor's run of the same module
            if trace is not self._anchor and trace.get('data.raw.size') is None:
                log.info(f'{path} has no raw data, not staged')
                continue
            seen.add(path)
            key = (data_folder, path)
            if key not in self._staged:
                if trace is self._anchor and self._anchor_is_dir:
                    rel_path = path.lstrip('/')
                    os.makedirs(os.path.join(data_folder, rel_path), exist_ok=True)
                else:
                    rel_path = materialize(trace, data_folder)
                modified = timestamp(trace.get('file.modifiedOn')) or 0
                self._staged[key] = {'staged': os.path.join(data_folder, rel_path), 'source_path': rel_path,
                                     'ctime': timestamp(trace.get('file.createdOn')) or modified, 'mtime': modified}
            results.append(self._staged[key])
            if first_hit:
                break
        log.info(f'ALEAPP glob {glob}: staged {[item["source_path"] for item in results]}')
        return results


def run_aleapp(input_dir, out_dir, work_dir, modules, handlers=None):
    """Run ALEAPP in its own venv through hleapp_launcher.py and serve its RPC requests (handlers, see hleapp_rpc)
    on this thread."""
    os.makedirs(out_dir, exist_ok=True)
    profile = os.path.join(work_dir, 'hleapp.alprofile')
    with open(profile, 'w') as profile_file:
        json.dump({'leapp': 'aleapp', 'format_version': 1, 'plugins': list(modules)}, profile_file)
    command = [ALEAPP_PYTHON, LAUNCHER, '-t', 'fs', '-i', input_dir, '-o', out_dir,
               '-m', profile, '--custom_output_folder', REPORT_FOLDER]
    # ALEAPP output goes to a file: a pipe nobody reads while requests are served could fill up and block ALEAPP
    aleapp_log = os.path.join(work_dir, 'aleapp.log')
    log.info(f'starting ALEAPP: {command}')
    started = time.monotonic()
    with open(aleapp_log, 'w') as log_file:
        returncode = RpcServer(handlers or {}, log).run(command, ALEAPP_TIMEOUT, cwd=ALEAPP_DIR,
                                                        stdout=log_file, stderr=subprocess.STDOUT)
    log.info(f'ALEAPP exited with {returncode} after {time.monotonic() - started:.1f}s')
    with open(aleapp_log, errors='replace') as log_file:
        output = log_file.read()
    log_aleapp_output(output)
    if returncode != 0:
        raise RuntimeError(f'ALEAPP failed ({returncode}): {output[-4000:]}')
    return os.path.join(out_dir, REPORT_FOLDER)


# ALEAPP output lines worth seeing at info level: ALEAPP catches the error of a module, logs it and still exits 0
ALEAPP_NOTABLE = re.compile(r'error|traceback|exception|failed|no file found|artifact (started|completed)',
                            re.IGNORECASE)
ALEAPP_LOG_TAIL = 20000


def log_aleapp_output(output):
    """Log ALEAPP's own output: the notable lines at info, the tail of the whole output at debug."""
    lines = output.splitlines()
    notable = [line for line in lines if ALEAPP_NOTABLE.search(line)]
    log.info(f'ALEAPP output: {len(lines)} lines, {len(notable)} notable')
    for line in notable:
        log.info(f'ALEAPP | {line}')
    log.debug(f'ALEAPP output (last {ALEAPP_LOG_TAIL} characters):\n{output[-ALEAPP_LOG_TAIL:]}')


def folder_size(path):
    return sum(os.path.getsize(os.path.join(folder, name)) for folder, _, names in os.walk(path) for name in names)


def log_report(report_dir):
    """Log what ALEAPP left in its report folder and the status per module it recorded in the LAVA json."""
    if not os.path.isdir(report_dir):
        log.error(f'ALEAPP report folder {report_dir} does not exist')
        return
    for name in sorted(os.listdir(report_dir)):
        path = os.path.join(report_dir, name)
        if os.path.isdir(path):
            log.info(f'ALEAPP report: {name}/ {folder_size(path)} bytes')
        else:
            log.info(f'ALEAPP report: {name} {os.path.getsize(path)} bytes')
    lava_json = os.path.join(report_dir, LAVA_JSON)
    if not os.path.isfile(lava_json):
        log.error(f'ALEAPP wrote no {LAVA_JSON}, so there are no LAVA artifacts to add')
        return
    try:
        with open(lava_json, encoding='utf8') as lava_file:
            lava = json.load(lava_file)
    except (OSError, ValueError) as error:
        log.error(f'cannot read {lava_json}: {error!r}')
        return
    log.info(f'LAVA processing_status: {lava.get("processing_status")}')
    # 'Complete', 'No files found' or 'Error' per module; an 'Error' is only explained in ALEAPP's output
    for module in lava.get('modules', []):
        log.info(f'LAVA module {module.get("module_name")} ({module.get("artifact_name")}): '
                 f'{module.get("module_status")}, {module.get("file_count")} files')
    artifacts = lava.get('artifacts', {})
    if not artifacts:
        log.warning('LAVA lists no artifacts: no module returned a table')
    for category, entries in sorted(artifacts.items()):
        for artifact in entries:
            log.info(f'LAVA artifact {category} / {artifact.get("name")}: table {artifact.get("tablename")}, '
                     f'{artifact.get("record_count")} records')


def zip_folder(folder):
    # entries are stored, not compressed (compression level 0)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_STORED) as archive:
        for dirpath, _, filenames in os.walk(folder):
            for filename in sorted(filenames):
                path = os.path.join(dirpath, filename)
                archive.write(path, os.path.relpath(path, folder))
    return buffer.getvalue()


def lava_datetime(value):
    """LAVA stores a 'datetime' column as whole epoch seconds (UTC), ALEAPP's TSV prints str(datetime)."""
    if isinstance(value, (int, float)):
        return str(datetime.datetime.fromtimestamp(value, tz=datetime.timezone.utc))
    return value


def lava_media(db, value):
    """LAVA stores a 'media' column as a media reference id (a JSON list for several), ALEAPP's TSV prints the
    extraction path of the media file in the report ('media/<id>.<ext>'), several joined by ' | '."""
    if not value:
        return ''
    ids = json.loads(value) if isinstance(value, str) and value.startswith('[') else [value]
    paths = []
    for ref_id in ids:
        found = db.execute('SELECT i.extraction_path FROM _lava_media_references r '
                           'JOIN _lava_media_items i ON r.media_item_id = i.id WHERE r.id = ?', (ref_id,)).fetchone()
        if found and found[0]:
            paths.append(found[0])
    return ' | '.join(paths)


def read_lava(report_dir):
    """Yield (category, artifact name, header, rows) for every non-empty artifact in an ALEAPP LAVA report.

    Values are rendered as ALEAPP's own TSV export does, except that LAVA keeps no sub-second part of timestamps.
    """
    with open(os.path.join(report_dir, LAVA_JSON), encoding='utf8') as lava_file:
        lava = json.load(lava_file)
    db = sqlite3.connect(pathlib.Path(report_dir, LAVA_DB).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        for category, artifacts in sorted(lava.get('artifacts', {}).items()):
            for artifact in artifacts:
                table = artifact['tablename'].replace('"', '""')
                cursor = db.execute(f'SELECT * FROM "{table}"')
                rows = cursor.fetchall()
                log.debug(f'LAVA table {artifact["tablename"]} ({category} / {artifact["name"]}): {len(rows)} rows')
                if rows:
                    columns = [column[0] for column in cursor.description]
                    # LAVA stores sanitized sql column names, column_map gives the original ALEAPP headers back
                    column_map = artifact.get('column_map') or {}
                    header = [column_map.get(column, column) for column in columns]
                    # typed columns ('datetime', 'media', ...) are listed in object_columns, the rest is TEXT
                    types = {column['name']: column['type'] for column in artifact.get('object_columns') or ()}
                    converters = [
                        lava_datetime if types.get(column) == 'datetime'
                        else functools.partial(lava_media, db) if types.get(column) == 'media'
                        else None
                        for column in columns]
                    if any(converters):
                        rows = [tuple(convert(value) if convert else value for convert, value in zip(converters, row))
                                for row in rows]
                    yield category, artifact['name'], header, rows
    finally:
        db.close()


def to_tsv(header, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter='\t', lineterminator='\n')
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode('utf8')


def add_lava_children(trace, report_dir):
    """Add a child per artifact category and below it a child per artifact (raw = TSV), returns the artifact count."""
    categories = {}
    count = 0
    for category, name, header, rows in read_lava(report_dir):
        tsv = b''
        try:
            if category not in categories:
                log.debug(f'building category child {category!r}')
                categories[category] = trace.child_builder(category)
                categories[category].build()  # the SDK requires a parent to be built before its children
            tsv = to_tsv(header, rows)
            log.debug(f'building artifact child {category!r} / {name!r}: {len(rows)} rows, {len(tsv)} bytes')
            categories[category].child_builder(name).add_data('raw', tsv).build()
        except Exception:
            log.exception(f'adding LAVA child {category!r} / {name!r} ({len(rows)} rows, {len(tsv)} bytes) failed')
            raise
        log.info(f'added LAVA child {category} / {name}: {len(rows)} rows, {len(tsv)} bytes')
        count += 1
    return count


def inside_report(trace):
    """True when the trace lies inside an ALEAPP report child of this plugin (its trace path has one)."""
    path = trace.get('path') or ''
    names = path if isinstance(path, (list, tuple)) else str(path).split('/')
    return REPORT_CHILD in names


class Plugin(DeferredExtractionPlugin):

    def plugin_info(self):
        plugin_info = PluginInfo(
            id=PluginId(domain='github.com/Schramp', category='extract', name='HLEAPP'),
            version='0.0.1',
            description=f'Runs the {len(PLAN.kept)} ALEAPP modules started by their anchor traces, '
                        'fetching their files from Hansken on demand',
            author=Author('Ruud Schramp', 'netwerkforens@gmail.com', 'NFI'),
            maturity=MaturityLevel.PROOF_OF_CONCEPT,
            webpage_url='https://github.com/Schramp/hleap',
            matcher=MATCHER,
            license='Apache License 2.0',
            resources=PluginResources(maximum_cpu=2, maximum_memory=1024, maximum_workers=4),
        )
        return plugin_info

    def process(self, trace, data_context, searcher):
        log.info(f'process: trace {trace.get("id")} path={trace.get("path")!r} file.path={trace.get("file.path")!r} '
                 f'{data_context.data_type} data of {data_context.data_size} bytes')
        try:
            self._process(trace, searcher)
        except Exception:
            log.exception(f'process failed for {trace.get("file.path")!r}')
            raise

    def _process(self, trace, searcher):
        if inside_report(trace):
            # Hansken unpacks the report zip, which holds ALEAPP's copies of the evidence (data/, media/): they
            # must not start a new run on the plugin's own output
            log.info(f'{trace.get("path")} lies inside an {REPORT_CHILD}, skipping')
            return
        rel_path = (trace.get('file.path') or '').lstrip('/')
        # the matcher may select more than the anchors (e.g. character classes widen to '?'): check exactly,
        # ALEAPP style ('root/' + path); a folder trace can only start directory anchors
        anchor_path = 'root/' + rel_path
        is_dir = False
        modules = PLAN.triggered_by(anchor_path)
        if not modules:
            modules = PLAN.triggered_by(anchor_path, is_dir=True)
            is_dir = bool(modules)
        if not modules:
            log.info(f'{rel_path} is no anchor of an ALEAPP module, skipping')
            return
        with tempfile.TemporaryDirectory(prefix='hleapp-') as work_dir:
            # ALEAPP wants an existing input folder; its files come on demand through the Stager instead
            input_dir = os.path.join(work_dir, 'input')
            os.makedirs(input_dir)
            out_dir = os.path.join(work_dir, 'out')
            stager = Stager(trace, searcher, out_dir, anchor_is_dir=is_dir,
                            scope=lambda glob: PLAN.search_scope(modules, glob, anchor_path, is_dir))
            log.info(f'running ALEAPP {modules} for {rel_path}')
            report_dir = run_aleapp(input_dir, out_dir, work_dir, modules,
                                    {'search_and_stage': stager.search_and_stage})
            log.info(f'ALEAPP {modules} staged {stager.staged_paths}')
            log_report(report_dir)

            # the LAVA report holds the same rows as _TSV Exports, plus category and table metadata
            count = add_lava_children(trace, report_dir)
            log.info(f'ALEAPP {modules} produced {count} non-empty artifacts')
            if HLEAPP_REPORT:
                report_zip = zip_folder(report_dir)
                log.info(f'adding {REPORT_CHILD} child: {len(report_zip)} bytes')
                trace.child_builder(REPORT_CHILD).add_data('raw', report_zip).build()
                log.info(f'added {REPORT_CHILD} child')


if __name__ == '__main__':
    # Optional main method to run your plugin with Hansken.py
    # See detail at:
    #  https://netherlandsforensicinstitute.github.io/hansken-extraction-plugin-sdk-documentation/latest/dev/python/hanskenpy.html
    run_with_hanskenpy(Plugin)
