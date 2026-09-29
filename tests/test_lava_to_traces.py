"""Issue #6: turn an ALEAPP LAVA report (_lava_data.lava + _lava_artifacts.db) into Hansken child traces.

The LAVA report is made by hand, in the layout ALEAPP's scripts/lavafuncs.py writes, so no ALEAPP run is needed.
"""
import json
import sqlite3

import pytest

from fake_trace import FakeTrace
from plugin import LAVA_DB, LAVA_JSON, add_lava_children, read_lava


def make_lava_report(report_dir, artifacts):
    """artifacts: {category: [(name, table, {sql column: original header}, rows)]}"""
    lava = {'artifacts': {}}
    db = sqlite3.connect(report_dir / LAVA_DB)
    for category, entries in artifacts.items():
        for name, table, column_map, rows in entries:
            columns = ', '.join(f'"{column}" TEXT' for column in column_map)
            db.execute(f'CREATE TABLE "{table}" ({columns})')
            db.executemany(f'INSERT INTO "{table}" VALUES ({", ".join("?" * len(column_map))})', rows)
            lava['artifacts'].setdefault(category, []).append(
                {'artifact_key': table, 'name': name, 'tablename': table, 'module': table, 'column_map': column_map})
    db.commit()
    db.close()
    (report_dir / LAVA_JSON).write_text(json.dumps(lava), encoding='utf8')
    return report_dir


@pytest.fixture
def report_dir(tmp_path):
    return make_lava_report(tmp_path, {
        'Installed Apps': [
            ('packageGplinks', 'packageGplinks', {'bundle_id': 'Bundle ID', 'link': 'Possible Google Play Store Link'},
             [('com.whatsapp', 'https://play.google.com/store/apps/details?id=com.whatsapp'),
              ('com.life360.android.safetymapd',
               'https://play.google.com/store/apps/details?id=com.life360.android.safetymapd')]),
        ],
        'GEO Location': [
            ('Google Maps Directions', 'googleMapsGmm',
             {'directions_url': 'Directions URL', 'latitude': 'Latitude', 'longitude': 'Longitude'},
             [('https://google.com/maps/dir/38.9000223,-77.02807', '38.9000223', '-77.02807')]),
            # ALEAPP creates a table for every artifact that ran, also when it found nothing
            ('Google Maps Searches', 'googleMapsSearches', {'query': 'Query'}, []),
        ],
    })


def test_read_lava_skips_empty_artifacts_and_restores_headers(report_dir):
    artifacts = list(read_lava(report_dir))

    assert [(category, name) for category, name, _, _ in artifacts] == [
        ('GEO Location', 'Google Maps Directions'),
        ('Installed Apps', 'packageGplinks'),
    ]
    _, _, header, rows = artifacts[0]
    assert header == ['Directions URL', 'Latitude', 'Longitude']
    assert rows == [('https://google.com/maps/dir/38.9000223,-77.02807', '38.9000223', '-77.02807')]


def test_lava_report_becomes_category_and_artifact_traces(report_dir):
    trace = FakeTrace({'name': 'gmm_storage.db', 'file': {'path': '/data/data/x/gmm_storage.db'}})

    count = add_lava_children(trace, report_dir)

    assert count == 2
    assert trace.tree() == {
        'GEO Location': {
            'Google Maps Directions':
                b'Directions URL\tLatitude\tLongitude\n'
                b'https://google.com/maps/dir/38.9000223,-77.02807\t38.9000223\t-77.02807\n',
        },
        'Installed Apps': {
            'packageGplinks':
                b'Bundle ID\tPossible Google Play Store Link\n'
                b'com.whatsapp\thttps://play.google.com/store/apps/details?id=com.whatsapp\n'
                b'com.life360.android.safetymapd\t'
                b'https://play.google.com/store/apps/details?id=com.life360.android.safetymapd\n',
        },
    }


def test_values_with_tabs_and_newlines_stay_one_tsv_cell(tmp_path):
    report_dir = make_lava_report(tmp_path, {
        'Chats': [('Life360 - Chat Messages', 'life360Chat', {'message': 'Message', 'sender': 'Sender'},
                   [('see you\tat 5\nok?', 'mom')])],
    })
    trace = FakeTrace({'name': 'messaging.db'})

    add_lava_children(trace, report_dir)

    tsv = trace.child('Chats').child('Life360 - Chat Messages').data['raw']
    assert tsv == b'Message\tSender\n"see you\tat 5\nok?"\tmom\n'


def test_empty_report_adds_no_children(tmp_path):
    report_dir = make_lava_report(tmp_path, {'GEO Location': [('Google Maps Searches', 'searches', {'q': 'Query'}, [])]})
    trace = FakeTrace({'name': 'gmm_storage.db'})

    assert add_lava_children(trace, report_dir) == 0
    assert trace.children == []
