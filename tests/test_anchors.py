"""Issue #13: anchor rules for ALEAPP modules under the deferred constraint."""
import os

import pytest

from anchors import CONFLICT_DROPS, Anchor, AnchorPlan, load_modules, tree_paths

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


def test_conflict_drops_cover_every_conflict_in_the_test_tree():
    """In Hansken there is no tree to check: the conflicts found here must all be in CONFLICT_DROPS."""
    plan = AnchorPlan(load_modules(os.path.join(ROOT, 'ALEAPP')), drop=CONFLICT_DROPS)
    files, dirs = tree_paths(TREE)
    assert plan.check(files, dirs) == []


LIFE360_CACHE = 'root/data/data/com.life360.android.safetymapd/cache'


@pytest.fixture(scope='module')
def dropped_plan():
    return AnchorPlan(load_modules(os.path.join(ROOT, 'ALEAPP')), drop=CONFLICT_DROPS)


def test_globs_next_to_the_anchor_are_limited_to_its_folder(dropped_plan):
    anchor = f'{LIFE360_CACHE}/picasso-cache/journal'
    modules = dropped_plan.triggered_by(anchor)
    assert modules == ['life360CacheEmergencyContacts', 'life360CacheEntries', 'life360CacheMemberHistory']
    for glob in dropped_plan.modules[modules[0]]:
        assert dropped_plan.search_scope(modules, glob, anchor) == f'{LIFE360_CACHE}/picasso-cache'
    gmm = 'root/data/data/com.google.android.apps.maps/databases/gmm_storage.db'
    assert dropped_plan.search_scope(['get_googleMapsGmm'], dropped_plan.modules['get_googleMapsGmm'][0], gmm) == \
        'root/data/data/com.google.android.apps.maps/databases'


def test_globs_elsewhere_search_the_whole_image():
    plan = AnchorPlan({'chess': ('*/com.chess/shared_prefs/*', '*/com.chess/databases/chess.db*'),
                       'media': ('*/com.other/databases/x.db', '*/com.chess/shared_prefs/*')})
    folder = 'root/data/data/com.chess/shared_prefs'
    # next to the (directory) anchor: limited; elsewhere in the app: whole image
    assert plan.search_scope(['chess'], '*/com.chess/shared_prefs/*', folder, is_dir=True) == folder
    assert plan.search_scope(['chess'], '*/com.chess/databases/chess.db*', folder, is_dir=True) is None
    # a glob no started module declares (searched from module code) is not limited
    assert plan.search_scope(['chess'], '*/packages.xml', folder, is_dir=True) is None
    # modules started together that disagree: the whole image wins
    anchor = 'root/data/data/com.other/databases/x.db'
    assert plan.search_scope(['media'], '*/com.chess/shared_prefs/*', anchor) is None
    assert plan.search_scope(['chess', 'media'], '*/com.chess/shared_prefs/*', folder, is_dir=True) is None
