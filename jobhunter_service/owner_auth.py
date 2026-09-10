"""Local, peer-authorized access to the owner's current Hermes runtime grant.

Only this owner process accesses Hermes auth state. The JobHunter process gets
the effective inference credential in memory, never OAuth refresh tokens or the
credential pool. The wire protocol has no caller-selected provider or file path.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import os
import pwd
from pathlib import Path
import secrets
import socket
import stat
import struct
import sys
import time
from urllib.parse import urlsplit


MAX_REQUEST = 1024
MAX_RESPONSE = 65536
RUNTIME_FIELDS = frozenset({'model', 'provider', 'api_mode', 'base_url', 'api_key'})
API_MODES = frozenset({'chat_completions', 'anthropic_messages', 'codex_responses'})
PROVIDERS = frozenset({'openai', 'openai-api', 'openai-codex', 'openrouter',
                       'anthropic', 'gemini', 'nous', 'custom'})
FAILURE = 'The shared Hermes provider is unavailable.'


class OwnerAuthError(RuntimeError):
    """Deliberately carries no upstream exception or credential metadata."""


class _Discard:
    def write(self, value):
        return len(value)

    def flush(self):
        pass


@contextlib.contextmanager
def quiet_provider():
    # Hermes/SDK exceptions can contain tokens. This dedicated broker never
    # forwards third-party output or logging, even on import or refresh errors.
    previous = logging.root.manager.disable
    logging.disable(sys.maxsize)
    try:
        with contextlib.redirect_stdout(_Discard()), contextlib.redirect_stderr(_Discard()):
            yield
    finally:
        logging.disable(previous)


def validate_runtime(value):
    if not isinstance(value, dict) or not RUNTIME_FIELDS <= value.keys():
        raise OwnerAuthError(FAILURE)
    result = {key: value[key] for key in RUNTIME_FIELDS}
    limits = {'model': 256, 'provider': 64, 'api_mode': 64, 'base_url': 2048, 'api_key': 32768}
    if any(not isinstance(result[key], str) or not result[key].strip()
           or len(result[key]) > limit or any(ord(char) < 32 for char in result[key])
           for key, limit in limits.items()):
        raise OwnerAuthError(FAILURE)
    if result['provider'] not in PROVIDERS or result['api_mode'] not in API_MODES:
        raise OwnerAuthError(FAILURE)
    try:
        url = urlsplit(result['base_url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError()
        if result['provider'] == 'openai-codex' and (
                result['api_mode'] != 'codex_responses' or url.hostname != 'chatgpt.com'
                or url.port not in (None, 443) or url.path.rstrip('/') != '/backend-api/codex'
                or url.query):
            raise ValueError()
    except ValueError:
        raise OwnerAuthError(FAILURE) from None
    return result


def _receive(sock, limit):
    data = bytearray()
    while len(data) <= limit:
        chunk = sock.recv(min(4096, limit + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if b'\n' in chunk:
            break
    if len(data) > limit or not data.endswith(b'\n') or b'\n' in data[:-1]:
        raise OwnerAuthError(FAILURE)
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError):
        raise OwnerAuthError(FAILURE) from None
    if not isinstance(value, dict):
        raise OwnerAuthError(FAILURE)
    return value


def _send(sock, value, limit):
    data = json.dumps(value, separators=(',', ':')).encode() + b'\n'
    if len(data) > limit:
        raise OwnerAuthError(FAILURE)
    sock.sendall(data)


def peer_uid(sock):
    if hasattr(socket, 'SO_PEERCRED'):
        _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return uid
    if hasattr(sock, 'getpeereid'):
        return sock.getpeereid()[0]
    if sys.platform == 'darwin':
        # CPython on macOS does not expose getpeereid on every build.
        import ctypes
        function = ctypes.CDLL(None, use_errno=True).getpeereid
        function.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        function.restype = ctypes.c_int
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if function(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) == 0:
            return uid.value
    raise OwnerAuthError(FAILURE)


class OwnerAuthClient:
    def __init__(self, socket_path, *, timeout=45, owner_uid=None):
        self.socket_path = Path(socket_path).absolute()
        self.timeout = timeout
        self.owner_uid = owner_uid

    def resolve(self, *, refresh_ticket=None):
        request = {'operation': 'resolve'}
        if refresh_ticket is not None:
            request['refresh_ticket'] = refresh_ticket
        try:
            metadata = self.socket_path.lstat()
            parent = self.socket_path.parent.lstat()
            if (not stat.S_ISSOCK(metadata.st_mode) or metadata.st_mode & 0o007
                    or not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o022
                    or metadata.st_uid != parent.st_uid
                    or self.owner_uid is not None and metadata.st_uid != self.owner_uid):
                raise OwnerAuthError(FAILURE)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(str(self.socket_path))
                if peer_uid(client) != metadata.st_uid:
                    raise OwnerAuthError(FAILURE)
                _send(client, request, MAX_REQUEST)
                response = _receive(client, MAX_RESPONSE)
            if set(response) != {'runtime', 'refresh_ticket'}:
                raise OwnerAuthError(FAILURE)
            runtime = validate_runtime(response['runtime'])
            ticket = response['refresh_ticket']
            if not isinstance(ticket, str) or len(ticket) != 43:
                raise OwnerAuthError(FAILURE)
            return {**runtime, 'refresh_ticket': ticket}
        except Exception:
            raise OwnerAuthError(FAILURE) from None


class HermesOwnerRuntime:
    """Initialize once in an owner-only process, resolve config on every call."""
    def __init__(self, owner_home, hermes_source):
        self.owner_home = Path(owner_home).resolve(strict=True)
        self.source = Path(hermes_source).resolve(strict=True)
        if not self.owner_home.is_dir() or not (self.source / 'hermes_cli/runtime_provider.py').is_file():
            raise OwnerAuthError(FAILURE)
        os.environ['HERMES_HOME'] = str(self.owner_home)
        os.environ['PYTHON_DOTENV_DISABLED'] = '1'
        sys.path.insert(0, str(self.source))
        with quiet_provider():
            from hermes_cli.config import load_config
            from hermes_cli.runtime_provider import resolve_runtime_provider
        self.load_config = load_config
        self.resolve_runtime_provider = resolve_runtime_provider

    def __call__(self):
        # The owner process may be supplied its existing .env through systemd.
        # Never load it in the service or child, or accept paths over the socket.
        config = self.load_config()
        model = config.get('model') if isinstance(config, dict) else None
        if not isinstance(model, dict) or not isinstance(model.get('default'), str):
            raise OwnerAuthError(FAILURE)
        if model.get('openai_runtime') == 'codex_app_server':
            raise OwnerAuthError(FAILURE)
        provider = model.get('provider')
        if not isinstance(provider, str) or provider not in PROVIDERS | {'auto'}:
            raise OwnerAuthError(FAILURE)
        runtime = self.resolve_runtime_provider(requested=provider, target_model=model['default'])
        return {**runtime, 'model': model['default']}


class OwnerAuthBroker:
    def __init__(self, resolver, allowed_uids, *, clock=time.monotonic):
        self.resolver = resolver
        self.allowed_uids = frozenset(allowed_uids)
        if not self.allowed_uids or any(type(uid) is not int or uid < 0 for uid in self.allowed_uids):
            raise ValueError('At least one valid local service UID is required.')
        self.clock = clock
        self.tickets = {}

    def dispatch(self, uid, request):
        try:
            if uid not in self.allowed_uids or not isinstance(request, dict):
                raise OwnerAuthError(FAILURE)
            if (set(request) not in ({'operation'}, {'operation', 'refresh_ticket'})
                    or request.get('operation') != 'resolve'):
                raise OwnerAuthError(FAILURE)
            current = self.clock()
            self.tickets = {key: value for key, value in self.tickets.items() if value['expires'] > current}
            old = None
            if 'refresh_ticket' in request:
                ticket = request['refresh_ticket']
                if not isinstance(ticket, str) or len(ticket) != 43:
                    raise OwnerAuthError(FAILURE)
                old = self.tickets.get(ticket)
                if old is None or old['uid'] != uid:
                    raise OwnerAuthError(FAILURE)
                del self.tickets[ticket]
            with quiet_provider():
                # Reload owner state first: another gateway process may already
                # have rotated the single-use refresh token since the failed call.
                raw = self.resolver()
                runtime = validate_runtime(raw)
                pool = raw.get('credential_pool')
                credential_id = pool.entry_id_for_api_key(runtime['api_key']) if pool else None
                fingerprint = hashlib.sha256(runtime['api_key'].encode()).digest()
                if (old and old['provider'] == runtime['provider'] and old['model'] == runtime['model']
                        and old['fingerprint'] == fingerprint and old['credential_id'] == credential_id
                        and runtime['provider'] in {'openai-codex', 'nous'} and pool and credential_id):
                    refreshed = pool.try_refresh_matching(credential_id=credential_id,
                                                         api_key_hint=runtime['api_key'])
                    if refreshed is None:
                        raise OwnerAuthError(FAILURE)
                    # The resolver applies the provider's exact API mode/base and
                    # reloads the token persisted by Hermes under its auth lock.
                    raw = self.resolver()
                    runtime = validate_runtime(raw)
                    pool = raw.get('credential_pool')
                    credential_id = pool.entry_id_for_api_key(runtime['api_key']) if pool else None
                    fingerprint = hashlib.sha256(runtime['api_key'].encode()).digest()
                    if fingerprint == old['fingerprint']:
                        raise OwnerAuthError(FAILURE)
            ticket = secrets.token_urlsafe(32)
            if len(self.tickets) >= 128:
                del self.tickets[next(iter(self.tickets))]
            self.tickets[ticket] = {'uid': uid, 'expires': current + 300, 'provider': runtime['provider'],
                                    'model': runtime['model'], 'fingerprint': fingerprint,
                                    'credential_id': credential_id}
            return {'runtime': runtime, 'refresh_ticket': ticket}
        except Exception:
            raise OwnerAuthError(FAILURE) from None

    def handle(self, connection):
        connection.settimeout(45)
        try:
            uid = peer_uid(connection)
            if uid not in self.allowed_uids:
                raise OwnerAuthError(FAILURE)
            response = self.dispatch(uid, _receive(connection, MAX_REQUEST))
        except Exception:
            response = {'error': FAILURE}
        try:
            _send(connection, response, MAX_RESPONSE)
        except (OSError, OwnerAuthError):
            pass


def serve(socket_path, broker):
    path = Path(socket_path).absolute()
    parent = path.parent.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o027
            or parent.st_uid != os.getuid()):
        raise OwnerAuthError('The owner broker requires its private runtime directory.')
    # Do not replace regular files or symlinks. A stale socket may be removed
    # only in the owner-controlled, non-group-writable runtime directory.
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise OwnerAuthError(FAILURE)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                raise OwnerAuthError('The owner broker is already running.')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        previous_umask = os.umask(0o117)
        try:
            listener.bind(str(path))
        finally:
            os.umask(previous_umask)
        os.chmod(path, 0o660)
        listener.listen(8)
        try:
            while True:
                connection, _ = listener.accept()
                with connection:
                    broker.handle(connection)
        finally:
            path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    command = parser.add_subparsers(dest='command', required=True).add_parser('serve')
    command.add_argument('--socket', required=True)
    command.add_argument('--owner-home', required=True)
    command.add_argument('--hermes-source', required=True)
    command.add_argument('--allowed-uid', type=int, action='append', default=[])
    command.add_argument('--allowed-user', action='append', default=[])
    args = parser.parse_args(argv)
    try:
        with quiet_provider():
            resolver = HermesOwnerRuntime(args.owner_home, args.hermes_source)
        allowed = set(args.allowed_uid) | {pwd.getpwnam(name).pw_uid for name in args.allowed_user}
        if not allowed:
            raise OwnerAuthError(FAILURE)
        serve(args.socket, OwnerAuthBroker(resolver, allowed | {os.getuid()}))
    except (Exception, KeyboardInterrupt):
        print(FAILURE, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
