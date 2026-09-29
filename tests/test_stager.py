"""Issue #15: the plugin side of the on-demand seeker (glob -> Hansken query, Stager) with a fake searcher."""
import datetime
import io
import os
import re

import pytest

from anchors import load_modules
from plugin import ALEAPP_DIR, Stager, glob_query, materialize

GMM_GLOB = '*/com.google.android.apps.maps/databases/gmm_storage.db*'
DATABASES = '/data/data/com.google.android.apps.maps/databases'


class FakeTrace:
    """Stands in for both the matched ExtractionTrace and SearchTraces: get() on properties, open() on data."""

    def __init__(self, path, data=b'', properties=None):
        self._properties = {'file.path': path, 'data.raw.size': len(data) if data is not None else None}
        self._properties.update(properties or {})
        self._data = data
        self.opened = 0

    def get(self, key, default=None):
        return self._properties.get(key, default)

    def open(self):
        if self._data is None:
            raise ValueError('no raw data')
        self.opened += 1
        return io.BytesIO(self._data)


class FakeSearcher:
    """Returns every trace for every query, like a coarse query would; the Stager must filter exactly."""

    def __init__(self, traces):
        self.traces = traces
        self.queries = []

    def search(self, query, count=None):
        self.queries.append(query)
        return iter(self.traces)


@pytest.mark.parametrize('glob, hql_lite, expected', [
    (GMM_GLOB, True, "file.name='gmm_storage.db*' AND NOT type:deleted"),
    (GMM_GLOB, False, 'file.name:gmm_storage.db* AND NOT type:deleted'),
    ('**/com.openai.chatgpt/cache/files/*', True, "file.path='*/com.openai.chatgpt/cache/files/*' AND NOT type:deleted"),
    ('*/cache/*/[0-9a-f][0-9a-f].[01]', True, "file.name='??.?' AND NOT type:deleted"),
    ('*/app_webview/Default/Web Data*', False, 'file.name:"Web Data*" AND NOT type:deleted'),
    ('*', True, None),
    ('*/*/*', True, None),
])
def test_glob_query(glob, hql_lite, expected):
    assert glob_query(glob, hql_lite) == expected


def test_every_aleapp_glob_is_translated_or_has_no_literal_text():
    globs = {glob for paths in load_modules(ALEAPP_DIR).values() for glob in paths}
    for glob in globs:
        if glob_query(glob, hql_lite=True) is None:
            assert not re.sub(r'\[[^\]]*\]|[*?/]', '', glob), glob


@pytest.fixture
def out_dir(tmp_path):
    return tmp_path / 'out'


def stage(stager, out_dir, glob=GMM_GLOB, first_hit=False):
    return stager.search_and_stage(glob=glob, data_folder=str(out_dir / 'report' / 'data'), first_hit=first_hit)


def test_anchor_and_matching_search_results_are_staged(out_dir):
    anchor = FakeTrace(f'{DATABASES}/gmm_storage.db', b'db', {'file.modifiedOn': '2024-07-27T12:57:24.000Z'})
    searcher = FakeSearcher([
        FakeTrace(f'{DATABASES}/gmm_storage.db-journal'),
        FakeTrace('/data/data/com.google.android.apps.maps/files/gmm_storage.db-journal'),  # other directory
        FakeTrace(f'{DATABASES}/other.db', b'x'),
    ])
    staged = stage(Stager(anchor, searcher, out_dir, hql_lite=True), out_dir)

    assert [item['source_path'] for item in staged] == [
        'data/data/com.google.android.apps.maps/databases/gmm_storage.db',
        'data/data/com.google.android.apps.maps/databases/gmm_storage.db-journal']
    data = out_dir / 'report' / 'data'
    assert staged[0]['staged'] == str(data / staged[0]['source_path'])
    assert (data / staged[0]['source_path']).read_bytes() == b'db'
    expected = datetime.datetime(2024, 7, 27, 12, 57, 24, tzinfo=datetime.timezone.utc).timestamp()
    assert staged[0]['mtime'] == staged[0]['ctime'] == expected
    assert searcher.queries == ["file.name='gmm_storage.db*' AND NOT type:deleted"]


def test_first_hit_stops_after_one(out_dir):
    anchor = FakeTrace(f'{DATABASES}/gmm_storage.db', b'db')
    searcher = FakeSearcher([FakeTrace(f'{DATABASES}/gmm_storage.db-journal')])
    assert len(stage(Stager(anchor, searcher, out_dir), out_dir, first_hit=True)) == 1


def test_anchor_found_again_and_repeated_requests_are_staged_once(out_dir):
    anchor = FakeTrace(f'{DATABASES}/gmm_storage.db', b'db')
    stager = Stager(anchor, FakeSearcher([anchor]), out_dir)
    first = stage(stager, out_dir)
    second = stage(stager, out_dir)
    assert first == second and len(first) == 1
    assert anchor.opened == 1


def test_traces_without_raw_data_are_skipped(out_dir):
    anchor = FakeTrace('/data/data/other/anchor.db', b'x')
    searcher = FakeSearcher([FakeTrace(f'{DATABASES}/gmm_storage.db-shm', data=None)])
    assert stage(Stager(anchor, searcher, out_dir), out_dir) == []


def test_globs_without_literal_text_are_not_searched(out_dir):
    searcher = FakeSearcher([FakeTrace(f'{DATABASES}/gmm_storage.db', b'x')])
    # the anchor '/a' does not match either, so nothing is staged
    assert stage(Stager(FakeTrace('/a', b'x'), searcher, out_dir), out_dir, glob='*/*/*') == []
    assert searcher.queries == []


def test_data_folder_outside_the_output_folder_is_refused(out_dir, tmp_path):
    stager = Stager(FakeTrace(f'{DATABASES}/gmm_storage.db', b'db'), FakeSearcher([]), out_dir)
    with pytest.raises(ValueError, match='outside the ALEAPP output folder'):
        stager.search_and_stage(glob=GMM_GLOB, data_folder=str(tmp_path / 'elsewhere'))
    with pytest.raises(ValueError):
        stager.search_and_stage(glob=GMM_GLOB, data_folder=str(out_dir / '..' / 'elsewhere'))


def test_materialize_writes_data_and_times(tmp_path):
    trace = FakeTrace(f'{DATABASES}/gmm_storage.db', b'SQLite format 3\0',
                      {'file.modifiedOn': datetime.datetime(2024, 1, 26, 16, 47, 10, tzinfo=datetime.timezone.utc)})
    target = tmp_path / materialize(trace, str(tmp_path))
    assert target.read_bytes() == b'SQLite format 3\0'
    assert os.stat(target).st_mtime == trace.get('file.modifiedOn').timestamp()


def test_materialize_writes_empty_file_without_opening(tmp_path):
    trace = FakeTrace(f'{DATABASES}/gmm_storage.db-journal', b'')
    trace.open = None  # an empty trace must not be opened
    assert (tmp_path / materialize(trace, str(tmp_path))).read_bytes() == b''
