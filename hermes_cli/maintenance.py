"""Explicit offline maintenance entry for a locally managed paired release.

No normal CLI, scheduler or channel is imported before a durable release fence.
The operator supplies an owned private request; this is not a remote control API.
"""
import hashlib
import http.server
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time


def fingerprint(path):
    path = Path(path)
    if path.is_symlink():
        info = path.lstat()
        return {'type': 'link', 'value': os.readlink(path), 'mode': info.st_mode & 0o777, 'uid': info.st_uid, 'gid': info.st_gid}
    if not path.exists():
        return {'type': 'absent'}
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('regular maintenance target required')
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'type': 'file', 'sha256': digest, 'mode': stat.S_IMODE(info.st_mode), 'bytes': info.st_size, 'uid': info.st_uid, 'gid': info.st_gid}


def read_private(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError('private owned regular file required')
    with path.open() as stream:
        return json.load(stream)


def write_once(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare(request, role, root, writes):
    if role == 'gateway':
        from hermes_state import SessionDB
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from hermes_cli.maintenance_state import prepare_home
        def migrate(stage):
            token = set_hermes_home_override(stage)
            try:
                with SessionDB(stage / 'state.db'):
                    pass
            finally:
                reset_hermes_home_override(token)
        for name in request['homes']:
            home = Path(name)
            if not home.is_absolute() or not home.is_dir() or home.is_symlink():
                raise RuntimeError('invalid explicit home')
            prepare_home(home, root, request, writes, fingerprint, migrate)
    return writes


def wait(role, root, request, writes):
    stopped = False
    def stop(_signal, _frame):
        nonlocal stopped
        stopped = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    status = {'status': 'maintenance_ready', 'attempt': request['attempt'], 'nonce': request['nonce'],
              'role': role, 'pid': os.getpid(), 'pair': request['pair'], 'business_started': False,
              'start_time': Path('/proc/self/stat').read_text().rsplit(') ', 1)[1].split()[19], 'writes': writes, 'declared_paths': request['write_sets'][role]}
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == '/health/cutover' else 503)
            self.end_headers()
            if self.path == '/health/cutover':
                self.wfile.write(json.dumps(status).encode())
        def log_message(self, format, *args):
            pass
    with http.server.HTTPServer(('127.0.0.1', request['ports'][role]), Handler) as server:
        server.timeout = .1
        write_once(root / (role + '.ready.json'), status)
        while not stopped:
            if (root / 'release.json').exists():
                release = read_private(root / 'release.json')
                fence = read_private(root / 'business-fence.json')
                if any(release.get(k) != request[k] or fence.get(k) != request[k] for k in ['attempt', 'nonce']) or fence.get('status') != 'BUSINESS_MAY_HAVE_STARTED':
                    raise RuntimeError('release lacks matching durable fence')
                write_once(root / (role + '.activated.json'), {'attempt': request['attempt'], 'nonce': request['nonce'], 'pid': os.getpid(), 'status': 'activating_normal_entry'})
                return
            server.handle_request()
    raise SystemExit(0)


def main():
    role, attempt = sys.argv[1:]
    if role not in ('gateway', 'dashboard'):
        raise ValueError('invalid role')
    root = Path(attempt).absolute()
    if any(p.is_symlink() for p in [root, *root.parents]) or root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise RuntimeError('private owned attempt required')
    request = read_private(root / 'request.json')
    if request['version'] != 1 or not request['nonce']:
        raise RuntimeError('invalid request')
    pair = Path(request['pair']).resolve()
    if not Path(__file__).resolve().is_relative_to(pair / 'core/source'):
        raise RuntimeError('foreign Core source')
    activated = root / (role + '.activated.json')
    if activated.exists():
        value = read_private(activated)
        fence = read_private(root / 'business-fence.json')
        if any(value.get(k) != request[k] or fence.get(k) != request[k] for k in ['attempt', 'nonce']) or fence.get('status') != 'BUSINESS_MAY_HAVE_STARTED':
            raise RuntimeError('foreign restart identity')
    else:
        if (root / (role + '.ready.json')).exists():
            raise RuntimeError('unreleased restart requires recovery')
        writes = {}
        try:
            prepare(request, role, root, writes)
            wait(role, root, request, writes)
        except Exception as exc:
            write_once(root / (role + '.failed.json'), {'attempt': request['attempt'], 'nonce': request['nonce'], 'pair': request['pair'], 'role': role, 'error_class': type(exc).__name__, 'status': 'maintenance_failed', 'writes': writes, 'declared_paths': request['write_sets'][role]})
            raise
    python = str(pair / 'core/runtime/bin/python')
    args = [python, '-B', '-m', 'hermes_cli.main']
    if role == 'gateway':
        args += ['gateway', 'run']
    else:
        args += ['dashboard', '--skip-build', '--no-open', '--host', request.get('business_hosts', {}).get('dashboard', '0.0.0.0'), '--port', str(request['business_ports']['dashboard'])]
    os.execve(python, args, dict(os.environ))


if __name__ == '__main__':
    main()
