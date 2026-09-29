"""Issue #6: run the real ALEAPP through Plugin.process() and check the 'ALEAPP report' child trace.

The report child is one zip blob with ALEAPP's whole output folder (HTML, TSV, LAVA); Hansken unpacks zips and
parses the sqlite inside. tox runs the SDK test framework with HLEAPP_REPORT=0 because the zip holds run timestamps,
so this test is what covers the report blob.

Needs ALEAPP_PYTHON pointing at a Python with ALEAPP/requirements.txt installed, skipped otherwise.
"""
import glob
import io
import json
import os
import zipfile

import pytest
from hansken_extraction_plugin.api.data_context import DataContext

import plugin
from fake_trace import FakeTrace

INPUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'testdata', 'input',
                     'data_data_com_google_android_apps_maps_databases_gmm_storage_db')

pytestmark = pytest.mark.skipif(not os.path.isfile(plugin.ALEAPP_PYTHON),
                                reason=f'ALEAPP_PYTHON ({plugin.ALEAPP_PYTHON}) not found')


def load_trace(base):
    with open(base + '.trace', encoding='utf8') as trace_file, open(base + '.raw', 'rb') as raw_file:
        return FakeTrace(json.load(trace_file)['trace'], raw_file.read())


class FakeSearcher:
    """Serves <input>/searchtraces/ like the SDK test framework; TraceFinder filters directory and glob itself."""

    def __init__(self, search_dir):
        self.traces = [load_trace(path[:-len('.trace')])
                       for path in sorted(glob.glob(os.path.join(search_dir, '*.trace')))]
        self.queries = []

    def search(self, query, count=None, **kwargs):
        self.queries.append(query)
        return self.traces[:count]


@pytest.fixture(scope='module')
def searcher():
    return FakeSearcher(os.path.join(INPUT, 'searchtraces'))


@pytest.fixture(scope='module')
def processed_trace(searcher):
    trace = load_trace(INPUT)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(plugin, 'HLEAPP_REPORT', True)
        plugin.Plugin().process(trace, DataContext(data_type='raw', data_size=len(trace.raw)), searcher)
    return trace


def test_process_searches_the_sidecars(searcher, processed_trace):
    assert searcher.queries, 'process() did not use the searcher to find the sqlite sidecars'


@pytest.fixture(scope='module')
def report_zip(processed_trace):
    return zipfile.ZipFile(io.BytesIO(processed_trace.child('ALEAPP report').data['raw']))


def test_process_adds_artifact_and_report_children(processed_trace):
    assert sorted(processed_trace.tree()) == ['ALEAPP report', 'Google Maps Directions']


def test_report_blob_is_a_valid_zip_with_the_aleapp_output(report_zip):
    assert report_zip.testzip() is None
    names = set(report_zip.namelist())
    assert {plugin.LAVA_JSON, plugin.LAVA_DB, 'index.html'} <= names
    assert any(name.endswith('Google Maps Directions.tsv') for name in names)


def test_report_blob_is_stored_uncompressed(report_zip):
    assert {info.compress_type for info in report_zip.infolist()} == {zipfile.ZIP_STORED}


def test_report_blob_holds_the_artifact_rows(report_zip, tmp_path):
    report_zip.extractall(tmp_path)

    artifacts = {name: rows for _, name, _, rows in plugin.read_lava(tmp_path)}

    assert len(artifacts['Google Maps Directions']) == 4  # issue #1: ALEAPP finds 4 directions in gmm_storage.db


def test_report_blob_paths_are_portable(report_zip):
    # built on any OS, unpacked by Hansken: zip entries must use '/' and stay inside the report folder
    for name in report_zip.namelist():
        assert '\\' not in name and not name.startswith('/') and '..' not in name.split('/'), name


def test_report_blob_keeps_the_staged_evidence(report_zip):
    # ALEAPP's data/ (copies of the input) stays in the report; the loop it caused is stopped in process() (#21)
    assert any(name.startswith('data/') and name.endswith('gmm_storage.db') for name in report_zip.namelist())


@pytest.mark.parametrize('path', [
    '/example/data/data/com.google.android.apps.maps/databases/gmm_storage.db/ALEAPP report/_HTML/media/x.db',
    ['example', 'gmm_storage.db', 'ALEAPP report', 'data', 'gmm_storage.db'],
])
def test_traces_inside_a_report_are_skipped(path, searcher):
    # issue #21: Hansken unpacks the report zip; its data/data/.../gmm_storage.db matched the plugin again
    trace = FakeTrace({'path': path, 'file': {'path': '/data/data/com.google.android.apps.maps/databases/'
                                                      'gmm_storage.db'}}, b'x')
    plugin.Plugin().process(trace, DataContext(data_type='raw', data_size=1), searcher)
    assert trace.tree() == {}
