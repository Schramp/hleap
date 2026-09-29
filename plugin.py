import ast
import fnmatch
import io
import json
import os
import subprocess
import tempfile
import zipfile

from hansken_extraction_plugin.api.extraction_plugin import ExtractionPlugin
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


def read_artifact_paths(module, key):
    """Read the path globs of an ALEAPP artifact without importing it (ALEAPP deps live in its own venv)."""
    with open(os.path.join(ALEAPP_DIR, 'scripts', 'artifacts', f'{module}.py'), encoding='utf8') as source:
        tree = ast.parse(source.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', None) == '__artifacts_v2__':
            paths = ast.literal_eval(node.value)[key]['paths']
            return (paths,) if isinstance(paths, str) else tuple(paths)
    raise ValueError(f'no __artifacts_v2__ in ALEAPP artifact {module}')


def globs_to_matcher(globs):
    """Turn ALEAPP globs into a Hansken matcher on file name; the full path is checked again in process().

    A trailing '*' (sqlite sidecars like -journal/-wal) is dropped: only the main file triggers ALEAPP,
    fetching the sidecars needs a deferred plugin (phase 2).
    """
    names = sorted({glob.rsplit('/', 1)[-1].rstrip('*') for glob in globs})
    if any(set(name) & set('*?[') for name in names):
        raise ValueError(f'wildcards in file names not supported yet: {names}')
    return '(' + ' OR '.join(f"file.name='{name}'" for name in names) + ') AND $data.type=raw'


ARTIFACT_PATHS = read_artifact_paths(ARTIFACT_MODULE, ARTIFACT_KEY)


def reconstruct(trace, fs_dir):
    """Write the trace data into fs_dir at its file.path, so ALEAPP sees a normal extracted filesystem."""
    rel_path = (trace.get('file.path') or trace.get('file.name') or trace.get('name')).lstrip('/')
    target = os.path.join(fs_dir, rel_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with trace.open() as reader, open(target, 'wb') as writer:
        while chunk := reader.read(1024 * 1024):
            writer.write(chunk)
    return rel_path


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
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for dirpath, _, filenames in os.walk(folder):
            for filename in sorted(filenames):
                path = os.path.join(dirpath, filename)
                archive.write(path, os.path.relpath(path, folder))
    return buffer.getvalue()


class Plugin(ExtractionPlugin):

    def plugin_info(self):
        plugin_info = PluginInfo(
            id=PluginId(domain='github.com/Schramp', category='extract', name='HLEAPP'),
            version='0.0.1',
            description=f'Runs ALEAPP artifact {ARTIFACT_MODULE} on files reconstructed from Hansken traces',
            author=Author('Ruud Schramp', 'netwerkforens@gmail.com', 'NFI'),
            maturity=MaturityLevel.PROOF_OF_CONCEPT,
            webpage_url='https://github.com/Schramp/hleap',
            matcher=globs_to_matcher(ARTIFACT_PATHS),
            license='Apache License 2.0',
            resources=PluginResources(maximum_cpu=2, maximum_memory=1024, maximum_workers=4),
        )
        return plugin_info

    def process(self, trace, data_context):
        with tempfile.TemporaryDirectory(prefix='hleapp-') as work_dir:
            fs_dir = os.path.join(work_dir, 'fs')
            rel_path = reconstruct(trace, fs_dir)
            # the matcher only checks the file name, ALEAPP matches its globs against 'root/' + relative path
            if not any(fnmatch.fnmatch('root/' + rel_path, glob) for glob in ARTIFACT_PATHS):
                log.info(f'{rel_path} does not match {ARTIFACT_PATHS}, skipping')
                return
            log.info(f'running ALEAPP {ARTIFACT_KEY} on {rel_path}')
            report_dir = run_aleapp(fs_dir, os.path.join(work_dir, 'out'), work_dir)

            tsv_dir = os.path.join(report_dir, '_TSV Exports')
            for tsv in sorted(os.listdir(tsv_dir)) if os.path.isdir(tsv_dir) else []:
                with open(os.path.join(tsv_dir, tsv), 'rb') as tsv_file:
                    trace.child_builder(os.path.splitext(tsv)[0]).add_data('raw', tsv_file.read()).build()
            if HLEAPP_REPORT:
                trace.child_builder('ALEAPP report').add_data('raw', zip_folder(report_dir)).build()


if __name__ == '__main__':
    # Optional main method to run your plugin with Hansken.py
    # See detail at:
    #  https://netherlandsforensicinstitute.github.io/hansken-extraction-plugin-sdk-documentation/latest/dev/python/hanskenpy.html
    run_with_hanskenpy(Plugin)
