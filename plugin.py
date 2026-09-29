import ast
import csv
import datetime
import fnmatch
import io
import json
import os
import pathlib
import posixpath
import sqlite3
import subprocess
import tempfile
import zipfile

from hansken_extraction_plugin.api.extraction_plugin import DeferredExtractionPlugin
from hansken_extraction_plugin.api.plugin_info import Author, MaturityLevel, PluginId, PluginInfo, PluginResources
from hansken_extraction_plugin.runtime.extraction_plugin_runner import run_with_hanskenpy
from logbook import Logger

log = Logger(__name__)

# ALEAPP runs in its own interpreter: it pins protobuf 5.x, the plugin SDK needs protobuf 7.x
ALEAPP_DIR = os.environ.get('ALEAPP_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ALEAPP'))
ALEAPP_PYTHON = os.environ.get('ALEAPP_PYTHON', '/opt/aleapp-venv/bin/python')
ALEAPP_TIMEOUT = int(os.environ.get('ALEAPP_TIMEOUT', '600'))
# the zipped report holds run timestamps, tests turn it off to get reproducible results
HLEAPP_REPORT = os.environ.get('HLEAPP_REPORT', '1') != '0'

# the single ALEAPP artifact this plugin runs (phase 1): module in scripts/artifacts, key in __artifacts_v2__
ARTIFACT_MODULE = 'googleMapsGmm'
ARTIFACT_KEY = 'get_googleMapsGmm'

REPORT_FOLDER = 'report'
REPORT_CHILD = 'ALEAPP report'
# LAVA output of ALEAPP: artifact metadata (json) and one sqlite table per artifact
LAVA_JSON = '_lava_data.lava'
LAVA_DB = '_lava_artifacts.db'


def read_artifact_paths(module, key):
    """Read the path globs of an ALEAPP artifact without importing it (ALEAPP deps live in its own venv)."""
    with open(os.path.join(ALEAPP_DIR, 'scripts', 'artifacts', f'{module}.py'), encoding='utf8') as source:
        tree = ast.parse(source.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', None) == '__artifacts_v2__':
            paths = ast.literal_eval(node.value)[key]['paths']
            return (paths,) if isinstance(paths, str) else tuple(paths)
    raise ValueError(f'no __artifacts_v2__ in ALEAPP artifact {module}')


# step 1 (issue #2): hard-coded matcher, the files ALEAPP needs next to it are found with the searcher
MATCHER = "file.name='gmm_storage.db' AND $data.type=raw"
SEARCH_LIMIT = 100

ARTIFACT_PATHS = read_artifact_paths(ARTIFACT_MODULE, ARTIFACT_KEY)


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


class TraceFinder:
    """Finds the traces of files in one directory whose name matches an ALEAPP glob.

    Hansken evaluates searcher queries as HQL, the SDK standalone test framework as HQL-Lite (the matcher
    language). This wrapper only sends a coarse name query in the dialect at hand and does the exact filtering
    (same directory, ALEAPP glob) here, so both give the same result. Tests set HLEAPP_TEST_SEARCH=1 (tox.ini).
    """

    def __init__(self, searcher, hql_lite=os.environ.get('HLEAPP_TEST_SEARCH', '0') == '1'):
        self._searcher = searcher
        self._hql_lite = hql_lite

    def query(self, name_glob):
        # search results do not carry trace types, so deleted files can only be left out in the query
        if self._hql_lite:
            return f'file.name={name_glob} AND NOT type:deleted'
        # TODO verify on a real Hansken: wildcard, NOT, escaping of names with spaces or HQL characters
        return f'file.name:{name_glob} AND NOT type:deleted'

    def find(self, directory, glob):
        for found in self._searcher.search(self.query(posixpath.basename(glob)), count=SEARCH_LIMIT):
            path = found.get('file.path')
            # the name query covers the whole image, keep only what ALEAPP would match in this directory
            if not path or posixpath.dirname(path) != directory or not fnmatch.fnmatch('root' + path, glob):
                continue
            if found.get('data.raw.size') is None:
                log.info(f'{path} has no raw data, not written to the emulated filesystem')
                continue
            yield path, found


def emulate_fs(trace, finder, fs_dir):
    """Rebuild the part of the filesystem ALEAPP needs: the matched trace plus the files next to it
    that match the artifact globs (sqlite sidecars like -journal/-wal/-shm). Returns the written paths."""
    anchor_path = trace.get('file.path')
    written = {anchor_path: materialize(trace, fs_dir)}
    for glob in ARTIFACT_PATHS:
        for path, found in finder.find(posixpath.dirname(anchor_path), glob):
            if path not in written:
                written[path] = materialize(found, fs_dir)
    return sorted(written.values())


def run_aleapp(fs_dir, out_dir, work_dir):
    os.makedirs(out_dir, exist_ok=True)
    profile = os.path.join(work_dir, 'hleapp.alprofile')
    with open(profile, 'w') as profile_file:
        json.dump({'leapp': 'aleapp', 'format_version': 1, 'plugins': [ARTIFACT_KEY]}, profile_file)
    command = [ALEAPP_PYTHON, os.path.join(ALEAPP_DIR, 'aleapp.py'), '-t', 'fs', '-i', fs_dir, '-o', out_dir,
               '-m', profile, '--custom_output_folder', REPORT_FOLDER]
    result = subprocess.run(command, cwd=ALEAPP_DIR, capture_output=True, text=True, timeout=ALEAPP_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(f'ALEAPP failed ({result.returncode}): {result.stdout[-2000:]}{result.stderr[-2000:]}')
    return os.path.join(out_dir, REPORT_FOLDER)


def zip_folder(folder):
    # entries are stored, not compressed (compression level 0)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_STORED) as archive:
        for dirpath, _, filenames in os.walk(folder):
            for filename in sorted(filenames):
                path = os.path.join(dirpath, filename)
                archive.write(path, os.path.relpath(path, folder))
    return buffer.getvalue()


def read_lava(report_dir):
    """Yield (category, artifact name, header, rows) for every non-empty artifact in an ALEAPP LAVA report."""
    with open(os.path.join(report_dir, LAVA_JSON), encoding='utf8') as lava_file:
        lava = json.load(lava_file)
    db = sqlite3.connect(pathlib.Path(report_dir, LAVA_DB).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        for category, artifacts in sorted(lava.get('artifacts', {}).items()):
            for artifact in artifacts:
                table = artifact['tablename'].replace('"', '""')
                cursor = db.execute(f'SELECT * FROM "{table}"')
                rows = cursor.fetchall()
                if rows:
                    # LAVA stores sanitized sql column names, column_map gives the original ALEAPP headers back
                    column_map = artifact.get('column_map') or {}
                    header = [column_map.get(column[0], column[0]) for column in cursor.description]
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
        if category not in categories:
            categories[category] = trace.child_builder(category)
            categories[category].build()  # the SDK requires a parent to be built before its children
        categories[category].child_builder(name).add_data('raw', to_tsv(header, rows)).build()
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
            description=f'Runs ALEAPP artifact {ARTIFACT_MODULE} on a filesystem emulated from Hansken traces',
            author=Author('Ruud Schramp', 'netwerkforens@gmail.com', 'NFI'),
            maturity=MaturityLevel.PROOF_OF_CONCEPT,
            webpage_url='https://github.com/Schramp/hleap',
            matcher=MATCHER,
            license='Apache License 2.0',
            resources=PluginResources(maximum_cpu=2, maximum_memory=1024, maximum_workers=4),
        )
        return plugin_info

    def process(self, trace, data_context, searcher):
        if inside_report(trace):
            # Hansken unpacks the report zip, which holds ALEAPP's copies of the evidence (data/, media/): they
            # must not start a new run on the plugin's own output
            log.info(f'{trace.get("path")} lies inside an {REPORT_CHILD}, skipping')
            return
        rel_path = (trace.get('file.path') or '').lstrip('/')
        # the matcher only checks the file name, ALEAPP matches its globs against 'root/' + relative path
        if not any(fnmatch.fnmatch('root/' + rel_path, glob) for glob in ARTIFACT_PATHS):
            log.info(f'{rel_path} does not match {ARTIFACT_PATHS}, skipping')
            return
        with tempfile.TemporaryDirectory(prefix='hleapp-') as work_dir:
            fs_dir = os.path.join(work_dir, 'fs')
            files = emulate_fs(trace, TraceFinder(searcher), fs_dir)
            log.info(f'running ALEAPP {ARTIFACT_KEY} on {files}')
            report_dir = run_aleapp(fs_dir, os.path.join(work_dir, 'out'), work_dir)

            tsv_dir = os.path.join(report_dir, '_TSV Exports')
            for tsv in sorted(os.listdir(tsv_dir)) if os.path.isdir(tsv_dir) else []:
                with open(os.path.join(tsv_dir, tsv), 'rb') as tsv_file:
                    trace.child_builder(os.path.splitext(tsv)[0]).add_data('raw', tsv_file.read()).build()
            if HLEAPP_REPORT:
                trace.child_builder(REPORT_CHILD).add_data('raw', zip_folder(report_dir)).build()


if __name__ == '__main__':
    # Optional main method to run your plugin with Hansken.py
    # See detail at:
    #  https://netherlandsforensicinstitute.github.io/hansken-extraction-plugin-sdk-documentation/latest/dev/python/hanskenpy.html
    run_with_hanskenpy(Plugin)
