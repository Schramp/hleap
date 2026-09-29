"""Anchor rules for running ALEAPP modules from a deferred Hansken plugin (issue #13).

Deferred constraint: traces matched by a deferred plugin are not indexed, so searcher.search() never finds them.
Every file an ALEAPP module needs, except the anchor trace it is started with, must therefore stay outside the
plugin matcher.

Rules:
- the anchor of a module is its first glob; sqlite sidecars (-wal, -shm, -journal) never are anchors, so
  'gmm_storage.db*' anchors on gmm_storage.db and leaves gmm_storage.db-journal searchable;
- a glob ending in '/*' (or '/*.*') anchors on the directory;
- anchors without literal text to select on ('*.jpg', '*/*-wal', '*/*.cnt') are not eligible, they would make
  large parts of an image unsearchable;
- a module that needs, via search, a file some kept module anchors on is dropped (conflict).

ALEAPP globs are matched against 'root/' + path relative to the extraction root (ALEAPP scripts/search_files.py),
so a Hansken file.path '/data/...' is matched as 'root/data/...'.
"""
import ast
import fnmatch
import json
import os
import re

SIDECAR_SUFFIXES = ('-wal', '-shm', '-journal')

# Modules dropped for conflicts found by tools/anchor_check.py on the test trees. In Hansken there is no tree to
# check against, so these are kept here; tests/test_anchors.py fails when a test tree shows a conflict missing here.
CONFLICT_DROPS = {
    'get_chromeAutofill': 'needs app_webview/Default/Web Data, an anchor of the Mister Skinnylegs modules',
    'get_chromeAutofillProfiles': 'needs app_webview/Default/Web Data, an anchor of the Mister Skinnylegs modules',
    'get_chromeCreditCards': 'needs app_webview/Default/Web Data, an anchor of the Mister Skinnylegs modules',
    'get_chromePaymentsCustomerData': 'needs app_webview/Default/Web Data, an anchor of the Mister Skinnylegs modules',
    'get_Life360_chat_messages': 'needs cache/picasso-cache/journal, an anchor of the Life360 API cache modules',
}
WILDCARDS = re.compile(r'[*?\[]')


def load_modules(aleapp_dir):
    """Read {module key: path globs} from all ALEAPP artifacts, without importing them (ALEAPP deps live in its
    own venv). Modules without paths are left out."""
    modules = {}
    artifacts_dir = os.path.join(aleapp_dir, 'scripts', 'artifacts')
    for filename in sorted(os.listdir(artifacts_dir)):
        if not filename.endswith('.py'):
            continue
        with open(os.path.join(artifacts_dir, filename), encoding='utf8') as source:
            try:
                tree = ast.parse(source.read())
            except SyntaxError:
                continue
        for node in tree.body:
            if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', None) == '__artifacts_v2__':
                try:
                    artifacts = ast.literal_eval(node.value)
                except ValueError:
                    continue
                for key, artifact in artifacts.items():
                    paths = artifact.get('paths') or ()
                    paths = (paths,) if isinstance(paths, str) else tuple(paths)
                    if paths:
                        modules[key] = paths
    return modules


def load_profile(path, modules):
    """The module keys an ALEAPP profile (.alprofile) selects, checked against the known modules."""
    with open(path, encoding='utf8') as profile_file:
        profile = json.load(profile_file)
    if profile.get('leapp') != 'aleapp':
        raise ValueError(f'{path} is not an ALEAPP profile')
    selected = profile.get('plugins') or []
    unknown = sorted(set(selected) - set(modules))
    if unknown:
        raise ValueError(f'{path} names unknown ALEAPP modules: {", ".join(unknown)}')
    return selected


def write_profile(path, selected):
    with open(path, 'w', encoding='utf8') as profile_file:
        json.dump({'leapp': 'aleapp', 'format_version': 1, 'plugins': sorted(selected)}, profile_file, indent=2)
        profile_file.write('\n')


def is_sidecar(path):
    return path.endswith(SIDECAR_SUFFIXES)


class Anchor:
    """The anchor of one ALEAPP module: the first glob, as a file or a directory anchor."""

    def __init__(self, glob):
        parent, _, leaf = glob.rstrip('/').rpartition('/')
        if parent and leaf in ('*', '*.*'):
            self.glob, self.kind = parent, 'dir'
        else:
            self.glob, self.kind = glob, 'file'

    @property
    def base(self):
        """The folder pattern of the anchor: the anchor itself for a directory anchor, else its parent."""
        return self.glob if self.kind == 'dir' else self.glob.rpartition('/')[0]

    @property
    def eligible(self):
        """True when the anchor has literal text to select on: a fully literal path segment, or a name that is
        more than a wildcard plus an extension or suffix ('*.jpg', '*-wal', '*.realm' are not eligible,
        '*threads_db2' and '*vlc_media.db*' are)."""
        segments = [segment for segment in self.glob.split('/') if segment]
        if any(not WILDCARDS.search(segment) for segment in segments):
            return True
        literal = re.sub(r'\*|\?|\[[^\]]*\]', '', segments[-1]) if segments else ''
        return len(literal) >= 4 and literal[0] not in '.-'

    def matches(self, path, is_dir=False):
        """path is ALEAPP style ('root/...')."""
        if is_dir != (self.kind == 'dir'):
            return False
        return fnmatch.fnmatch(path, self.glob) and not (self.kind == 'file' and is_sidecar(path))

    def hql_lite(self):
        """HQL-Lite matcher clause for this anchor, on file.path (Hansken paths start with '/', ALEAPP's with
        'root/'). Character classes become '?': a matcher may only select more, the plugin checks exactly.
        TODO #10: check quoting and folder traces ('type:folder') on a real Hansken."""
        pattern = re.sub(r'\[[^\]]*\]', '?', self.glob)
        pattern = re.sub(r'^\*\*/', '*/', pattern)
        pattern = re.sub(r'^root/', '/', pattern)
        clause = f"file.path='{pattern}'"
        if self.kind == 'dir':
            return f'({clause} AND type:folder)'
        sidecars = ' '.join(f"AND NOT file.name='*{suffix}'" for suffix in SIDECAR_SUFFIXES)
        return f'({clause} AND $data.type=raw {sidecars})'


class AnchorPlan:
    """Kept and dropped modules for a set of ALEAPP modules, optionally checked against a concrete file tree."""

    def __init__(self, modules, drop=None, select=None):
        """select: module keys to use (e.g. from load_profile()), None for all."""
        if select is not None:
            modules = {key: paths for key, paths in modules.items() if key in set(select)}
        self.modules = modules
        self.anchors = {key: Anchor(paths[0]) for key, paths in modules.items()}
        self.dropped = {key: 'anchor without literal text' for key, anchor in self.anchors.items()
                        if not anchor.eligible}
        self.dropped.update({key: reason for key, reason in (drop or {}).items() if key in modules})
        self.conflicts = []

    @property
    def kept(self):
        return {key: anchor for key, anchor in self.anchors.items() if key not in self.dropped}

    def triggered_by(self, path, is_dir=False):
        """Kept modules started by a trace at this ALEAPP style path."""
        return sorted(key for key, anchor in self.kept.items() if anchor.matches(path, is_dir))

    def check(self, files, dirs=()):
        """Drop kept modules that need, via search, a file a kept module anchors on. files/dirs are ALEAPP style
        paths. Dropping a needer does not change the anchors, so one pass is enough. Returns the conflicts as
        (file, needing module, anchoring modules)."""
        anchored = {path: self.triggered_by(path) for path in files}
        anchored.update({path: self.triggered_by(path, is_dir=True) for path in dirs})
        anchored = {path: keys for path, keys in anchored.items() if keys}
        for key in list(self.kept):
            anchor = self.anchors[key]
            for path, owners in anchored.items():
                if path in dirs or anchor.matches(path):
                    continue  # a module's own anchors are handed to it as trace, not searched
                if any(fnmatch.fnmatch(path, glob) for glob in self.modules[key]):
                    self.conflicts.append((path, key, owners))
                    self.dropped[key] = f'needs {path[len("root/"):]}, anchor of {", ".join(owners[:2])}'
                    break
        return self.conflicts

    def search_scope(self, modules, glob, anchor_path, is_dir=False):
        """The folder (ALEAPP style) that searches for glob are limited to in a run started by anchor_path, or None
        for the whole image.

        A glob that starts with the folder pattern of a started module's anchor (e.g. the sqlite sidecars, or
        cache entries next to a cache journal) is limited to the anchor's concrete folder, so modules with several
        anchors on a device do not stage the same files once per anchor. Other globs (files elsewhere in the app,
        or in other apps) search the whole image. When modules disagree, the whole image wins.
        """
        folder = anchor_path if is_dir else anchor_path.rpartition('/')[0]
        scopes = set()
        for key in modules:
            if glob in self.modules[key]:
                scopes.add(folder if glob.startswith(self.anchors[key].base + '/') else None)
        return folder if scopes == {folder} else None

    def matcher(self):
        clauses = sorted({anchor.hql_lite() for anchor in self.kept.values()})
        return ' OR '.join(clauses)


def tree_paths(source):
    """ALEAPP style ('root/...') file and directory paths of an extracted tree (directory or zip)."""
    import zipfile
    if zipfile.is_zipfile(source):
        names = [info.filename for info in zipfile.ZipFile(source).infolist()]
        files = {'root/' + name for name in names if not name.endswith('/')}
    else:
        files = set()
        for dirpath, _, filenames in os.walk(source):
            for filename in filenames:
                files.add('root/' + os.path.relpath(os.path.join(dirpath, filename), source).replace(os.sep, '/'))
    dirs = {path.rsplit('/', depth)[0] for path in files for depth in range(1, path.count('/'))}
    return files, dirs
