"""Issue #15: hleapp_launcher.py swaps ALEAPP's FileSeekerDir for HanskenSeeker without changing ALEAPP.

The first test pins the ALEAPP internals the launcher relies on (fails on a submodule bump that renames them); the
second runs HanskenSeeker in the ALEAPP venv against a fake RPC client (skipped without ALEAPP_PYTHON).
"""
import ast
import os
import subprocess
import textwrap

import pytest

import plugin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse(*parts):
    with open(os.path.join(plugin.ALEAPP_DIR, *parts), encoding='utf8') as source:
        return ast.parse(source.read())


def test_aleapp_internals_the_launcher_relies_on():
    aleapp = parse('aleapp.py')
    star_imports = [node.module for node in ast.walk(aleapp)
                    if isinstance(node, ast.ImportFrom) and any(alias.name == '*' for alias in node.names)]
    assert 'scripts.search_files' in star_imports  # FileSeekerDir is looked up in the aleapp module
    seeker_calls = [node for node in ast.walk(aleapp) if isinstance(node, ast.Call)
                    and getattr(node.func, 'id', None) == 'FileSeekerDir']
    assert [len(call.args) for call in seeker_calls] == [2]  # FileSeekerDir(input_path, data_folder)
    assert any(isinstance(node, ast.FunctionDef) and node.name == 'main' for node in aleapp.body)

    classes = {node.name: node for node in parse('scripts', 'search_files.py').body if isinstance(node, ast.ClassDef)}
    assert 'FileSeekerBase' in classes
    init = next(node for node in classes['FileInfo'].body if isinstance(node, ast.FunctionDef)
                and node.name == '__init__')
    assert [arg.arg for arg in init.args.args] == ['self', 'source_path', 'creation_date', 'modification_date']


@pytest.mark.skipif(not os.path.isfile(plugin.ALEAPP_PYTHON), reason='ALEAPP_PYTHON not found')
def test_hansken_seeker_in_the_aleapp_venv(tmp_path):
    code = textwrap.dedent('''
        from hleapp_launcher import HanskenSeeker

        class FakeClient:
            def __init__(self):
                self.calls = []
            def call(self, method, **params):
                self.calls.append((method, params))
                if params['glob'] == 'none':
                    return []
                return [{'staged': '/out/data/a.db', 'source_path': 'data/a.db', 'ctime': 1.0, 'mtime': 2.0},
                        {'staged': '/out/data/b.db', 'source_path': 'data/b.db', 'ctime': 3.0, 'mtime': 4.0}]
            def close(self):
                self.closed = True

        client = FakeClient()
        seeker = HanskenSeeker('/empty', '/out/data', client)
        assert seeker.search('*/a.db*') == ['/out/data/a.db', '/out/data/b.db']
        assert seeker.search('*/a.db*') == ['/out/data/a.db', '/out/data/b.db'] and len(client.calls) == 1  # cached
        assert seeker.search('*/a.db*', return_on_first_hit=True) == '/out/data/a.db'
        assert seeker.search('none', return_on_first_hit=True) == []  # as FileSeekerDir: empty list, not None
        info = seeker.file_infos['/out/data/b.db']
        assert (info.source_path, info.creation_date, info.modification_date) == ('data/b.db', 3.0, 4.0)
        assert '*/a.db*' in seeker.searched
        assert client.calls[0] == ('search_and_stage', {'glob': '*/a.db*', 'data_folder': '/out/data',
                                                        'first_hit': False})
        seeker.cleanup()
        assert client.closed
    ''')
    result = subprocess.run([plugin.ALEAPP_PYTHON, '-c', code], cwd=plugin.ALEAPP_DIR, capture_output=True, text=True,
                            env=dict(os.environ, PYTHONPATH=ROOT), timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
