"""Exact-byte terminal preservation, not approval or proof of content adoption.

Local same-user audit records are not signatures. Only historical authentication
may use this store; new write admission must continue to require active pending.
"""
from contextlib import contextmanager

import os
from pathlib import Path
from tools.steward_skill_execution import safe, read, load, sha, store_new, require, PID, HEX


def coordinates(path, pending_id, digest):
    p = safe(path)
    require(isinstance(pending_id, str) and PID.fullmatch(pending_id) and
            isinstance(digest, str) and HEX.fullmatch(digest), 'pending_history_identity')
    require(p.name == pending_id + '.json' and p.parent.name == 'skills' and
            p.parent.parent.name == 'pending', 'pending_history_original_path')
    home = p.parent.parent.parent
    directory = safe(home / 'pending-archive/skills' / pending_id / digest)
    return p, directory


def expected(path, pending_id, digest, disposition):
    require(disposition in ('rejected', 'approved', 'applied_closed'), 'pending_disposition')
    return {'schema': 'pending-disposition/v1', 'path': str(path), 'id': pending_id,
            'subsystem': 'skills', 'sha256': digest, 'disposition': disposition}


def preserve_new(path, raw):
    """Exclusive immutable write; interrupted partial writes remain fail-closed."""
    safe(path)
    if path.exists():
        require(read(path) == raw, 'pending_preservation_conflict')
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    sync_dir(path.parent)
    require(read(path) == raw, 'pending_preservation_readback')


def sync_dir(path):
    fd = os.open(safe(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def record_once(path, value):
    if safe(path).exists():
        require(load(path) == value, 'pending_receipt_conflict')
    else:
        store_new(path, value)


@contextmanager
def locked_archive(home):
    import fcntl
    directory = safe(Path(home) / 'pending-archive/skills')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield directory
    finally:
        os.close(fd)


def archived(path, pending_id, digest):
    """Only exact origin + terminal receipt; no arbitrary preservation path lookup."""
    p, directory = coordinates(path, pending_id, digest)
    receipt = load(directory / 'receipt.json')
    want = expected(p, pending_id, digest, receipt.get('disposition'))
    if receipt.get('disposition') == 'applied_closed':
        binding = load(directory / 'closeout.json')
        want['closeout_sha256'] = sha(read(directory / 'closeout.json'))
        require(isinstance(binding, dict) and binding, 'pending_closeout_binding')
    require(receipt == dict(want, status='removed'), 'pending_terminal_receipt')
    require(load(directory / 'intent.json') == dict(want, status='prepared'), 'pending_intent_binding')
    raw = read(directory / 'source.json')
    require(sha(raw) == digest, 'pending_archive_drift')
    data = load(directory / 'source.json')
    require(data.get('id') == pending_id and data.get('subsystem') == 'skills', 'pending_archive_identity')
    return raw


def historical_read(path, pending_id, digest):
    p, _ = coordinates(path, pending_id, digest)
    # safe() rejects dangling symlinks too. Only absence at the active origin
    # permits fallback: permission, hash, identity or parse failures cannot hide.
    try:
        raw = read(p)
    except FileNotFoundError:
        raw = archived(p, pending_id, digest)
    require(sha(raw) == digest, 'pending_historical_drift')
    return raw


@contextmanager
def approval_attempt(home, pending_id, record):
    """Public approve writes a durable no-replay marker before its writer call."""
    require(isinstance(pending_id, str) and PID.fullmatch(pending_id), 'pending_id')
    p = safe(Path(home) / 'pending/skills' / (pending_id + '.json'))
    with locked_archive(home) as root:
        marker = safe(root / pending_id / 'apply.started')
        require(not marker.exists(), 'write_attempt_unknown_do_not_reapply')
        raw = read(p)
        from tools.steward_skill_execution import decode
        require(decode(raw) == record, 'pending_approval_input_drift')
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        store_new(marker, {'schema': 'pending-approval-attempt/v1', 'id': pending_id,
                          'path': str(p), 'sha256': sha(raw), 'status': 'started'})
        yield


def discard(home, pending_id, disposition='rejected'):
    require(disposition in ('approved', 'rejected'), 'public_disposition_only')
    require(isinstance(pending_id, str) and PID.fullmatch(pending_id), 'pending_id')
    p = safe(Path(home) / 'pending/skills' / (pending_id + '.json'))
    with locked_archive(home) as root:
        try:
            raw = read(p)
        except FileNotFoundError:
            # Recover only a unique original transaction. Never recreate pending,
            # infer adoption, or select an arbitrary previous version by ID.
            transaction = safe(root / pending_id)
            if not transaction.exists():
                return False
            entries = list(transaction.iterdir())
            versions = []
            marker = None
            for entry in entries:
                entry = safe(entry)
                if entry.name == 'apply.started':
                    marker = entry
                else:
                    require(HEX.fullmatch(entry.name) and entry.is_dir(), 'pending_archive_entry')
                    versions.append(entry)
            if not entries:
                return False
            require(len(versions) == 1, 'ambiguous_pending_disposition')
            digest = versions[0].name
            p, directory = coordinates(p, pending_id, digest)
            if marker is not None:
                require(load(marker) == {'schema': 'pending-approval-attempt/v1', 'id': pending_id,
                                         'path': str(p), 'sha256': digest, 'status': 'started'},
                        'pending_approval_attempt_binding')
            want = expected(p, pending_id, digest, disposition)
            require(load(directory / 'intent.json') == dict(want, status='prepared'), 'pending_intent_binding')
            require(sha(read(directory / 'source.json')) == digest, 'pending_archive_drift')
            record_once(directory / 'receipt.json', dict(want, status='removed'))
            archived(p, pending_id, digest)
            sync_dir(p.parent)
            return True
        digest = sha(raw)
        p, directory = coordinates(p, pending_id, digest)
        data = load(p)
        require(data.get('id') == pending_id and data.get('subsystem') == 'skills', 'pending_identity')
        want = expected(p, pending_id, digest, disposition)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        require(not safe(directory / 'receipt.json').exists(), 'terminal_pending_reappeared')
        preserve_new(directory / 'source.json', raw)
        record_once(directory / 'intent.json', dict(want, status='prepared'))
        sync_dir(directory.parent)
        sync_dir(directory.parent.parent)
        # Recheck immediately before unlink. Source staging never overwrites IDs;
        # same-user malicious file replacement is outside receipt authenticity.
        require(read(p) == raw, 'pending_changed_before_exit')
        p.unlink()
        sync_dir(p.parent)
        record_once(directory / 'receipt.json', dict(want, status='removed'))
        archived(p, pending_id, digest)
        return True


def close_applied(home, pending_id, digest, binding, *, precommit=None):
    """Archive already-authenticated adoption; never call the content writer."""
    from tools.steward_skill_execution import decode
    p, directory = coordinates(Path(home) / 'pending/skills' / (pending_id + '.json'), pending_id, digest)
    require(isinstance(binding, dict) and binding, 'pending_closeout_binding')
    with locked_archive(home):
        raw = None
        try:
            raw = read(p)
        except FileNotFoundError:
            require(directory.exists(), 'pending_closeout_missing_transaction')
        if raw is not None:
            require(sha(raw) == digest, 'pending_closeout_source_drift')
            data = decode(raw)
            require(data.get('id') == pending_id and data.get('subsystem') == 'skills', 'pending_identity')
            require(not (directory / 'receipt.json').exists(), 'terminal_pending_reappeared')
            require(not safe(directory.parent / 'apply.started').exists(), 'write_attempt_unknown_do_not_close')
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            preserve_new(directory / 'source.json', raw)
            record_once(directory / 'closeout.json', binding)
            want = dict(expected(p, pending_id, digest, 'applied_closed'),
                        closeout_sha256=sha(read(directory / 'closeout.json')))
            record_once(directory / 'intent.json', dict(want, status='prepared'))
            sync_dir(directory.parent)
            sync_dir(directory.parent.parent)
            if precommit is not None:
                precommit()
            require(read(p) == raw, 'pending_changed_before_exit')
            p.unlink()
            sync_dir(p.parent)
        else:
            require(load(directory / 'closeout.json') == binding, 'pending_closeout_recovery_binding')
            want = dict(expected(p, pending_id, digest, 'applied_closed'),
                        closeout_sha256=sha(read(directory / 'closeout.json')))
            require(load(directory / 'intent.json') == dict(want, status='prepared'), 'pending_intent_binding')
            require(sha(read(directory / 'source.json')) == digest, 'pending_archive_drift')
            if precommit is not None:
                precommit()
        record_once(directory / 'receipt.json', dict(want, status='removed'))
        archived(p, pending_id, digest)
        require(not p.exists(), 'pending_exit_readback')
        return load(directory / 'receipt.json')
