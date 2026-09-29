"""Issue #13: anchor rules for ALEAPP modules under the deferred constraint."""
import os

import pytest

from anchors import Anchor, AnchorPlan, load_modules, tree_paths

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TREE = os.path.join(ROOT, 'testdata', 'practical_exercise.zip')
GMM = 'root/data/data/com.google.android.apps.maps/databases/gmm_storage.db'


@pytest.mark.parametrize('path, expected', [
    (GMM, True),
    (GMM + '-journal', False),
    (GMM + '-wal', False),
    (GMM + '-shm', False),
    ('root/data/data/com.google.android.apps.maps/files/gmm_storage.db', False),
])
def test_file_anchor_leaves_sidecars_searchable(path, expected):
    assert Anchor('*/com.google.android.apps.maps/databases/gmm_storage.db*').matches(path) is expected


def test_glob_ending_in_star_anchors_on_the_directory():
    anchor = Anchor('**/com.openai.chatgpt/cache/files/*')
    assert (anchor.glob, anchor.kind) == ('**/com.openai.chatgpt/cache/files', 'dir')
    assert anchor.matches('root/data/data/com.openai.chatgpt/cache/files', is_dir=True)
    assert not anchor.matches('root/data/data/com.openai.chatgpt/cache/files')


@pytest.mark.parametrize('glob, eligible', [
    ('*/com.google.android.apps.maps/databases/gmm_storage.db*', True),
    ('*/cmh.db*', True),
    ('*/*threads_db2', True),
    ('*vlc_media.db*', True),
    ('*/*-wal', False),
    ('*.[jJ][pP][gG]', False),
    ('*/*.torrent', False),
    ('*.realm', False),
    ('*/*clipboard/*/*', False),
])
def test_generic_anchors_are_not_eligible(glob, eligible):
    assert Anchor(glob).eligible is eligible


def test_hql_lite_clauses():
    assert Anchor('*/com.google.android.apps.maps/databases/gmm_storage.db*').hql_lite() == (
        "(file.path='*/com.google.android.apps.maps/databases/gmm_storage.db*' AND $data.type=raw "
        "AND NOT file.name='*-wal' AND NOT file.name='*-shm' AND NOT file.name='*-journal')")
    assert Anchor('**/com.x/cache/files/*').hql_lite() == "(file.path='*/com.x/cache/files' AND type:folder)"
    # character classes widen to '?': the matcher may select more, never less
    assert "file.path='*/cache/??.db'" in Anchor('*/cache/[0-9][0-9].db').hql_lite()


@pytest.fixture(scope='module')
def plan():
    plan = AnchorPlan(load_modules(os.path.join(ROOT, 'ALEAPP')))
    files, dirs = tree_paths(TREE)
    plan.check(files, dirs)
    return plan, files, dirs


def test_conflicting_and_generic_modules_are_dropped(plan):
    plan, _, _ = plan
    assert {'get_walStrings', 'get_chromeAutofill', 'get_chromeAutofillProfiles', 'get_chromeCreditCards',
            'get_chromePaymentsCustomerData', 'get_Life360_chat_messages'} <= set(plan.dropped)
    assert 'get_googleMapsGmm' in plan.kept


def test_no_conflicts_left_after_drops(plan):
    plan, files, dirs = plan
    # the invariant: checking only the kept modules again finds nothing to drop
    kept_again = AnchorPlan({key: plan.modules[key] for key in plan.kept})
    assert kept_again.check(files, dirs) == []


def test_anchor_traces_trigger_their_modules(plan):
    plan, _, _ = plan
    assert plan.triggered_by(GMM) == ['get_googleMapsGmm']
    assert plan.triggered_by(GMM + '-journal') == []
