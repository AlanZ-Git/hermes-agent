"""Stage one home's declared SQLite maintenance files, then publish confirmed deltas.

Migration code never writes the live home. Unwritten paths retain their frozen
preimage even on an exception; a partial publication records only completed CAS
replacements. An interrupted/unconfirmed replacement requires manual preservation.
"""
import os
from pathlib import Path
import shutil
import uuid


def prepare_home(home, root, request, writes, fingerprint, migrate):
    paths = [Path(p) for p in request['write_sets']['gateway'] if Path(p).parent == home or Path(p) == home / 'cache/schema_columns.json']
    before = {str(p): fingerprint(p) for p in paths}
    stage = root / ('core-maintenance-' + uuid.uuid4().hex)
    stage.mkdir(mode=0o700)
    for path in paths:
        value = before[str(path)]
        if value['type'] == 'file':
            target = stage / path.relative_to(home)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(value['mode'])
        elif value['type'] != 'absent':
            raise RuntimeError('SQLite maintenance requires regular files')
    migrate(stage)
    # Unknown quarantine/repair artifacts are not silently published or deleted.
    declared = {p.relative_to(home) for p in paths}
    if any(p.relative_to(stage) not in declared for p in stage.rglob('*') if not p.is_dir()):
        raise RuntimeError('maintenance produced undeclared artifacts; preserve')
    changes = {}
    for path in paths:
        value = fingerprint(stage / path.relative_to(home))
        original = before[str(path)]
        if {k:v for k,v in value.items() if k not in ('uid','gid')} != {k:v for k,v in original.items() if k not in ('uid','gid')}:
            changes[str(path)] = value
    for name, original in before.items():
        if fingerprint(name) != original:
            raise RuntimeError('unknown target drift: ' + name)
    for path in paths:
        name = str(path)
        if name not in changes:
            continue
        for other, original in before.items():
            if fingerprint(other) != writes.get(other, original):
                raise RuntimeError('unknown target drift: ' + other)
        value = changes[name]
        if value['type'] == 'absent':
            path.rename(stage / ('retained-' + path.name))
            intended = value
        else:
            temp = path.with_name('.maintenance-' + uuid.uuid4().hex)
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(stage / path.relative_to(home), temp)
            original = before[name]
            uid = original.get('uid', os.getuid()); gid = original.get('gid', os.getgid())
            os.chown(temp, uid, gid)
            temp.chmod(value['mode'])
            with temp.open('rb') as stream:os.fsync(stream.fileno())
            intended = {**value, 'uid':uid, 'gid':gid}
            os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        if fingerprint(path) != intended:
            raise RuntimeError('maintenance publication readback differs')
        writes[name] = intended
