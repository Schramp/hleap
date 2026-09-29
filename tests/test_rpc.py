"""Issue #14: RPC channel between the plugin and the ALEAPP child process, tested with fake children."""
import os
import sys
import textwrap
import threading
import time

import pytest

from hleapp_rpc import RpcServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_child(code, handlers, timeout=20):
    """Run a fake ALEAPP: a python child that imports hleapp_rpc like the launcher will."""
    child = textwrap.dedent('''
        import sys
        from hleapp_rpc import RpcClient, RpcError
        client = RpcClient.from_env()
    ''') + textwrap.dedent(code)
    env = dict(os.environ, PYTHONPATH=ROOT)
    return RpcServer(handlers).run([sys.executable, '-c', child], timeout, env=env)


def test_request_and_reply():
    calls = []

    def stage(glob, first_hit=False):
        calls.append((glob, first_hit))
        return [f'staged/{glob}']

    code = '''
        assert client.call('stage', glob='*/gmm_storage.db*', first_hit=True) == ['staged/*/gmm_storage.db*']
        assert client.call('stage', glob='x') == ['staged/x']
    '''
    assert run_child(code, {'stage': stage}) == 0
    assert calls == [('*/gmm_storage.db*', True), ('x', False)]


def test_handlers_run_on_the_serving_thread():
    """The Hansken searcher is only valid on the thread running process()."""
    threads = []
    run_child("client.call('where')", {'where': lambda: threads.append(threading.get_ident())})
    assert threads == [threading.get_ident()]


def test_handler_error_is_reported_to_the_child():
    def broken():
        raise ValueError('no such trace')

    code = '''
        try:
            client.call('broken')
        except RpcError as error:
            assert 'ValueError: no such trace' in str(error), error
            sys.exit(3)
    '''
    assert run_child(code, {'broken': broken}) == 3


def test_unknown_method_is_an_error_reply():
    code = '''
        try:
            client.call('nope')
        except RpcError as error:
            assert "unknown method 'nope'" in str(error), error
            sys.exit(4)
    '''
    assert run_child(code, {}) == 4


def test_large_reply():
    paths = [f'/data/data/app/cache/{index:06d}.0' for index in range(20000)]
    code = f'''
        assert len(client.call('many')) == {len(paths)}
    '''
    assert run_child(code, {'many': lambda: paths}) == 0


def test_child_without_requests_and_crashing_child():
    assert run_child('pass', {}) == 0
    assert run_child('sys.exit(1)', {}) == 1


def test_timeout_kills_the_child():
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        run_child('import time; time.sleep(60)', {}, timeout=1)
    assert time.monotonic() - started < 10


def test_timeout_while_the_child_keeps_calling():
    with pytest.raises(TimeoutError):
        run_child("while True: client.call('ping')", {'ping': lambda: None}, timeout=1)


def test_child_outside_the_plugin_gets_a_clear_error():
    from hleapp_rpc import RpcClient, RpcError, ENV_FD
    os.environ.pop(ENV_FD, None)
    with pytest.raises(RpcError, match='not started by the HLEAPP plugin'):
        RpcClient.from_env()


def test_no_file_descriptors_leak_on_any_exit_path():
    before = len(os.listdir('/proc/self/fd'))
    run_child("client.call('ping')", {'ping': lambda: None})
    run_child('sys.exit(1)', {})
    with pytest.raises(TimeoutError):
        run_child('import time; time.sleep(60)', {}, timeout=1)
    assert len(os.listdir('/proc/self/fd')) == before
