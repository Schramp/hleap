"""Issue #4: one ALEAPP run per anchor trace, with every module the trace starts."""
import pytest
from hansken_extraction_plugin.api.data_context import DataContext

import plugin
from fake_trace import FakeTrace


@pytest.fixture
def runs(monkeypatch, tmp_path):
    """Replace the ALEAPP run: record the modules and the search scope per glob, return an empty report."""
    calls = []

    def fake_run(input_dir, out_dir, work_dir, modules, handlers=None):
        calls.append(list(modules))
        report = tmp_path / f'report{len(calls)}'
        report.mkdir()
        return str(report)

    monkeypatch.setattr(plugin, 'run_aleapp', fake_run)
    return calls


def process(path, raw=b'x'):
    trace = FakeTrace({'path': '/image' + path, 'file': {'path': path}}, raw)
    plugin.Plugin().process(trace, DataContext(data_type='raw', data_size=len(raw)), searcher=None)
    return trace


def test_matcher_comes_from_the_anchor_plan():
    assert plugin.Plugin().plugin_info().matcher == plugin.PLAN.matcher()
    assert "gmm_storage.db*'" in plugin.MATCHER and len(plugin.PLAN.kept) > 1000


def test_one_run_with_every_module_the_trace_starts(runs):
    process('/data/data/com.life360.android.safetymapd/cache/picasso-cache/journal')
    assert runs == [['life360CacheEmergencyContacts', 'life360CacheEntries', 'life360CacheMemberHistory']]


def test_traces_that_start_no_module_are_skipped(runs):
    process('/data/data/com.google.android.apps.maps/databases/gmm_storage.db-journal')
    process('/data/data/com.example/files/readme.txt')
    assert runs == []


def test_dropped_modules_do_not_run(runs):
    process('/data/data/com.life360.android.safetymapd/databases/messaging.db')  # get_Life360_chat_messages
    assert runs == []
