"""RPC between the plugin (plugin venv) and ALEAPP (its own venv), issue #14.

ALEAPP runs as a child process in another venv (protobuf conflict, see docs/Architecture.md) and asks the plugin
for files while it runs. The Hansken searcher is only valid on the thread running process(), so the plugin serves
requests on that thread while it waits for ALEAPP.

Transport: one end of a socketpair is passed to the child (pass_fds, fd number in HLEAPP_RPC_FD), so there is no
socket file and nothing else can connect. Messages are JSON (no pickle: the venvs may differ in Python version),
framed with a 4-byte big-endian length. Only control messages go over the channel; file data is written by the
plugin straight into ALEAPP's folders (shared disk).

Request {"id", "method", "params"} -> reply {"id", "result"} or {"id", "error": {"type", "message"}}.

Standard library only: this module is imported in both venvs.
"""
import json
import os
import select
import socket
import struct
import subprocess
import time

ENV_FD = 'HLEAPP_RPC_FD'
MAX_MESSAGE = 64 * 1024 * 1024
POLL_INTERVAL = 0.2


class RpcError(Exception):
    """A request failed on the other side, or the channel broke."""


def _send(sock, message):
    data = json.dumps(message).encode('utf8')
    sock.sendall(struct.pack('>I', len(data)) + data)


def _recv_exactly(sock, size):
    chunks = []
    while size:
        chunk = sock.recv(min(size, 1024 * 1024))
        if not chunk:
            raise EOFError('RPC channel closed')
        chunks.append(chunk)
        size -= len(chunk)
    return b''.join(chunks)


def _recv(sock):
    size, = struct.unpack('>I', _recv_exactly(sock, 4))
    if size > MAX_MESSAGE:
        raise RpcError(f'RPC message of {size} bytes exceeds {MAX_MESSAGE}')
    return json.loads(_recv_exactly(sock, size).decode('utf8'))


class RpcServer:
    """Plugin side: start a child with one end of the channel and serve its requests until it exits.

    handlers maps method names to callables taking the request params as keyword arguments; they run on the
    thread that calls serve().
    """

    def __init__(self, handlers, log=None):
        self._handlers = handlers
        self._log = log

    def run(self, command, timeout, **popen_kwargs):
        """Start command with the channel, serve it, return the child's exit code.

        Raises TimeoutError (after killing the child) when it runs longer than timeout seconds.
        """
        parent_end, child_end = socket.socketpair()
        try:
            env = dict(popen_kwargs.pop('env', None) or os.environ)
            env[ENV_FD] = str(child_end.fileno())
            process = subprocess.Popen(command, env=env, pass_fds=(child_end.fileno(),), **popen_kwargs)
            child_end.close()  # only the child holds it now: EOF on parent_end means the child is gone
            return self._serve(process, parent_end, time.monotonic() + timeout, timeout)
        finally:
            parent_end.close()
            child_end.close()

    def _serve(self, process, sock, deadline, timeout):
        channel_open = True
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise TimeoutError(f'ALEAPP did not finish within {timeout} seconds')
            if channel_open:
                readable, _, _ = select.select([sock], [], [], min(POLL_INTERVAL, remaining))
                if readable:
                    try:
                        request = _recv(sock)
                    except EOFError:
                        channel_open = False
                    else:
                        _send(sock, self._handle(request))
                    continue
            else:
                time.sleep(min(POLL_INTERVAL, remaining))
            if process.poll() is not None:
                return process.returncode

    def _handle(self, request):
        request_id = request.get('id')
        method = request.get('method')
        try:
            handler = self._handlers[method]
        except KeyError:
            return {'id': request_id, 'error': {'type': 'KeyError', 'message': f'unknown method {method!r}'}}
        try:
            return {'id': request_id, 'result': handler(**(request.get('params') or {}))}
        except Exception as error:  # reported to the child, which decides whether its run fails
            if self._log:
                self._log.warning(f'RPC {method} failed: {error!r}')
            return {'id': request_id, 'error': {'type': type(error).__name__, 'message': str(error)}}


class RpcClient:
    """Child side: call the plugin over the inherited end of the channel."""

    def __init__(self, sock):
        self._sock = sock
        self._next_id = 0

    @classmethod
    def from_env(cls):
        fd = os.environ.get(ENV_FD)
        if fd is None:
            raise RpcError(f'{ENV_FD} not set: not started by the HLEAPP plugin')
        return cls(socket.socket(fileno=int(fd)))

    def call(self, method, **params):
        self._next_id += 1
        try:
            _send(self._sock, {'id': self._next_id, 'method': method, 'params': params})
            reply = _recv(self._sock)
        except (EOFError, OSError) as error:
            raise RpcError(f'RPC channel broken during {method}: {error}') from error
        if reply.get('id') != self._next_id:
            raise RpcError(f'RPC reply id {reply.get("id")} does not match request {self._next_id}')
        if 'error' in reply:
            raise RpcError(f'{method} failed in the plugin: {reply["error"]["type"]}: {reply["error"]["message"]}')
        return reply.get('result')

    def close(self):
        self._sock.close()
