"""Issue #4: one ALEAPP run per anchor trace, with every module the trace starts."""
import json
import sqlite3

import pytest
from hansken_extraction_plugin.api.data_context import DataContext

import plugin
from anchors import load_profile
from fake_trace import FakeTrace


@pytest.fixture
def runs(monkeypatch, tmp_path):
    """Replace the ALEAPP run: record the modules and the search scope per glob, return an empty LAVA report."""
    calls = []

    def fake_run(input_dir, out_dir, work_dir, modules, handlers=None):
        calls.append(list(modules))
        report = tmp_path / f'report{len(calls)}'
        report.mkdir()
        (report / plugin.LAVA_JSON).write_text(json.dumps({'artifacts': {}}), encoding='utf8')
        sqlite3.connect(report / plugin.LAVA_DB).close()
        return str(report)

    monkeypatch.setattr(plugin, 'run_aleapp', fake_run)
    return calls


def process(path, raw=b'x'):
    trace = FakeTrace({'path': '/image' + path, 'file': {'path': path}}, raw)
    plugin.Plugin().process(trace, DataContext(data_type='raw', data_size=len(raw)), searcher=None)
    return trace


def test_matcher_comes_from_the_anchor_plan():
    assert plugin.Plugin().plugin_info().matcher == plugin.PLAN.matcher()
    assert "gmm_storage.db*'" in plugin.MATCHER


def test_the_profile_selects_the_modules():
    selected = load_profile(plugin.PROFILE, plugin._MODULES)
    assert set(plugin.PLAN.kept) == set(selected) - set(plugin.PLAN.dropped)
    assert 'get_googleMapsGmm' in plugin.PLAN.kept and 'get_Life360_chat_messages' not in plugin.PLAN.kept
    assert plugin.MATCHER.count(' OR ') + 1 == len({anchor.hql_lite() for anchor in plugin.PLAN.kept.values()})


def test_modules_outside_the_profile_do_not_run(runs):
    process('/data/data/com.whatsapp/databases/msgstore.db')  # a WhatsApp module anchor, not in the profile
    assert runs == []


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
