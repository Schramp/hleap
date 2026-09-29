"""Unit tests for the filesystem emulation in plugin.py, with a fake searcher instead of Hansken."""
import datetime
import io
import os

import pytest

from plugin import ARTIFACT_PATHS, TraceFinder, emulate_fs, materialize

DATABASES = '/data/data/com.google.android.apps.maps/databases'


class FakeTrace:
    """Stands in for both the matched ExtractionTrace and SearchTraces: get() on properties, open() on data."""

    def __init__(self, path, data=b'', properties=None):
        self._properties = {'file.path': path, 'data.raw.size': len(data) if data is not None else None}
        self._properties.update(properties or {})
        self._data = data

    def get(self, key, default=None):
        return self._properties.get(key, default)

    def open(self):
        if self._data is None:
            raise ValueError('no raw data')
        return io.BytesIO(self._data)


class FakeSearcher:
    def __init__(self, traces):
        self.traces = traces
        self.queries = []

    def search(self, query, count=None):
        self.queries.append(query)
        return iter(self.traces)


@pytest.mark.parametrize('hql_lite, expected', [
    (True, 'file.name=gmm_storage.db* AND NOT type:deleted'),
    (False, 'file.name:gmm_storage.db* AND NOT type:deleted'),
])
def test_query_excludes_deleted_traces(hql_lite, expected):
    assert TraceFinder(FakeSearcher([]), hql_lite=hql_lite).query('gmm_storage.db*') == expected


def test_find_keeps_only_matching_files_in_the_directory():
    searcher = FakeSearcher([
        FakeTrace(f'{DATABASES}/gmm_storage.db-journal'),
        FakeTrace(f'{DATABASES}/gmm_storage.db-wal', b'wal'),
        # same name, other directory (ALEAPP's glob requires /databases/)
        FakeTrace('/data/data/com.google.android.apps.maps/files/gmm_storage.db-journal'),
        # same name, other user
        FakeTrace('/data/user/10/com.google.android.apps.maps/databases/gmm_storage.db-wal', b'wal'),
        FakeTrace(None),
    ])
    finder = TraceFinder(searcher, hql_lite=True)
    found = [path for path, _ in finder.find(DATABASES, ARTIFACT_PATHS[0])]
    assert found == [f'{DATABASES}/gmm_storage.db-journal', f'{DATABASES}/gmm_storage.db-wal']


def test_find_skips_traces_without_raw_data():
    searcher = FakeSearcher([FakeTrace(f'{DATABASES}/gmm_storage.db-shm', data=None)])
    assert list(TraceFinder(searcher, hql_lite=True).find(DATABASES, ARTIFACT_PATHS[0])) == []


def test_materialize_writes_data_and_times(tmp_path):
    modified = '2024-07-27T12:57:24.000Z'
    trace = FakeTrace(f'{DATABASES}/gmm_storage.db', b'SQLite format 3\0', {'file.modifiedOn': modified})
    rel_path = materialize(trace, str(tmp_path))
    target = tmp_path / rel_path
    assert rel_path == 'data/data/com.google.android.apps.maps/databases/gmm_storage.db'
    assert target.read_bytes() == b'SQLite format 3\0'
    expected = datetime.datetime(2024, 7, 27, 12, 57, 24, tzinfo=datetime.timezone.utc).timestamp()
    assert os.stat(target).st_mtime == expected


def test_materialize_accepts_datetime_times(tmp_path):
    modified = datetime.datetime(2024, 1, 26, 16, 47, 10, tzinfo=datetime.timezone.utc)
    trace = FakeTrace(f'{DATABASES}/gmm_storage.db', b'x', {'file.modifiedOn': modified})
    assert os.stat(tmp_path / materialize(trace, str(tmp_path))).st_mtime == modified.timestamp()


def test_materialize_writes_empty_file_without_opening(tmp_path):
    trace = FakeTrace(f'{DATABASES}/gmm_storage.db-journal', b'')
    trace.open = None  # an empty trace must not be opened
    assert (tmp_path / materialize(trace, str(tmp_path))).read_bytes() == b''


def test_emulate_fs_writes_anchor_and_sidecars_once(tmp_path):
    anchor = FakeTrace(f'{DATABASES}/gmm_storage.db', b'db')
    searcher = FakeSearcher([
        FakeTrace(f'{DATABASES}/gmm_storage.db', b'db'),  # the anchor itself is found again
        FakeTrace(f'{DATABASES}/gmm_storage.db-journal'),
    ])
    written = emulate_fs(anchor, TraceFinder(searcher, hql_lite=True), str(tmp_path))
    assert written == ['data/data/com.google.android.apps.maps/databases/gmm_storage.db',
                       'data/data/com.google.android.apps.maps/databases/gmm_storage.db-journal']
