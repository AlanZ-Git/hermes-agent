import json
import os
from pathlib import Path

import pytest

from agent import skill_utils as su
from tools import skill_manager_tool as sm
from tools import skills_tool as sk
from tools import steward_skill_execution as sx
from tools.skill_text_format import normalize_skill_text, encode_skill_text, patch_skill_bytes

TEXT = '---\nname: fixture\ndescription: Use when testing boundaries.\n---\n\nOriginal sentence.\n'


@pytest.mark.parametrize('bom,newline', [(False, '\n'), (True, '\n'), (False, '\r\n'), (True, '\r\n')])
def test_execute_receipt_and_unknown_no_retry(skill, monkeypatch, bom, newline):
    home, target = skill
    raw = (b'\xef\xbb\xbf' if bom else b'') + TEXT.replace('\n', newline).encode()
    target.write_bytes(raw)
    from tools import write_approval as wa
    state = home / 'state'
    (state / 'execution-plans').mkdir(parents=True)
    (state / 'execution-results').mkdir()
    delegation = {'enabled': True, 'home': str(home), 'state': str(state), 'cron_id': 'fixture',
                  'pending_roots': [str(home / 'pending/skills')], 'target_roots': [str(home / 'skills')]}
    (home / 'steward-delegation.json').write_text(json.dumps(delegation))
    monkeypatch.setattr('tools.approval_context._is_cron_approval_context', lambda: True)
    monkeypatch.setattr(wa, 'write_approval_enabled', lambda subsystem: True)
    record = wa.stage_write('skills', operation(), summary='fixture', origin='foreground')
    source = home / 'pending/skills' / (record['id'] + '.json')
    source_raw = source.read_bytes()
    digest = sx.sha(source_raw)
    identity = {'binding': {'source_token': sx.sha(sx.packed(['fixture', record['id'], str(source), digest]))}}
    monkeypatch.setattr(sx, 'review_identity', lambda *a: identity)
    targets, _ = sx.plan_targets(record)
    event = 'a' * 64
    plan = {'schema': 'steward-execution-plan/v1', 'event_id': event, 'identity': identity,
            'decision': 'approved', 'stage': {'id': record['id'], 'sha256': digest}, 'targets': targets,
            'sources': [{'scope': 'fixture', 'path': str(source), 'id': record['id'], 'sha256': digest,
                         'coverage': [{'ordinal': 0, 'operations': [0], 'basis': 'synthetic exact sentence'}]}],
            'outstanding': []}
    sx.store_new(state / 'execution-plans' / (event + '.json'), plan)
    writes = []
    real_write = sm._guarded_write

    def write(*args, **kwargs):
        writes.append(args[2])
        return real_write(*args, **kwargs)

    monkeypatch.setattr(sm, '_guarded_write', write)
    result = sx.execute(event, task_id='cron:fixture:run')
    assert target.read_bytes() == raw.replace(b'Original', b'Revised')
    assert len(writes) == 1 and source.read_bytes() == source_raw
    receipt = sx.load(sx.paths(state, event)[1])
    assert receipt['schema'] == 'steward-execution-result/v2'
    assert receipt['plan']['targets'][0]['format']['before']['preimage'] == sx.sha(raw)
    assert sx.execute(event, task_id='cron:fixture:run') == result and len(writes) == 1
    other = 'b' * 64
    sx.store_new(sx.paths(state, other)[0], dict(plan, event_id=other))
    sx.store_new(sx.paths(state, other)[1].with_suffix('.started'), {'synthetic': 'unknown'})
    with pytest.raises(ValueError, match='execution_result_unknown_do_not_retry'):
        sx.execute(other, task_id='cron:fixture:run')
    assert len(writes) == 1


@pytest.fixture
def skill(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    directory = home / 'skills/fixture'
    directory.mkdir(parents=True)
    target = directory / 'SKILL.md'
    target.write_text(TEXT)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_YOLO_MODE', '1')
    monkeypatch.setattr(sm, '_skills_dir', lambda: home / 'skills')
    monkeypatch.setattr(sk, '_skills_dir', lambda: home / 'skills')
    return home, target


def operation(old='Original sentence.', new='Revised sentence.'):
    return {'action': 'patch', 'name': 'fixture', 'old_string': old, 'new_string': new}


@pytest.mark.parametrize('bom', [False, True])
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_restricted_raw_forward_reverse(skill, bom, newline):
    _, target = skill
    before = (b'\xef\xbb\xbf' if bom else b'') + TEXT.replace('\n', newline).encode()
    target.write_bytes(before)
    payload = operation()
    record = {'payload': payload}
    targets, entries = sx.plan_targets(record)
    expected = before.replace(b'Original sentence.', b'Revised sentence.')
    result = json.loads(sm.apply_skill_pending(payload, restricted_targets=targets))
    assert result['success'], result
    assert target.read_bytes() == expected
    sx.current_targets(targets, post=True)
    receipt = {'schema': 'steward-execution-result/v2', 'operations': entries}
    assert sx._target_preimage(receipt, record, targets[0], expected) == before
    text, witness = normalize_skill_text(before)
    assert encode_skill_text(text, witness) == before
    assert normalize_skill_text(encode_skill_text(text, witness)) == (text, witness)
    targets[0]['format']['after']['bom'] = not bom
    with pytest.raises(ValueError, match='format_drift'):
        sx._target_preimage(receipt, record, targets[0], expected)


@pytest.mark.parametrize('bom', [False, True])
@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_metadata_zero_writer(skill, monkeypatch, bom, newline):
    _, target = skill
    raw = (b'\xef\xbb\xbf' if bom else b'') + TEXT.replace('\n', newline).encode()
    target.write_bytes(raw)
    payload = operation('description: Use when testing boundaries.', 'description: Changed metadata.')
    monkeypatch.setattr(sm, '_guarded_write', lambda *a, **k: pytest.fail('metadata reached writer'))
    with pytest.raises(ValueError, match='frontmatter_immutable'):
        sx.plan_targets({'payload': payload})
    targets = [{'path': str(target), 'preimage': sx.sha(raw)}]
    outcome = json.loads(sm.apply_skill_pending(payload, restricted_targets=targets))
    assert not outcome['success'] and 'frontmatter_immutable' in outcome['error']
    assert target.read_bytes() == raw


@pytest.mark.parametrize('raw', [b'one\r\ntwo\n', b'one\rtwo', b'\xff'])
def test_ambiguous_formats_rejected_before_writer(skill, monkeypatch, raw):
    _, target = skill
    target.write_bytes(raw)
    monkeypatch.setattr(sm, '_guarded_write', lambda *a, **k: pytest.fail('ambiguous bytes reached writer'))
    with pytest.raises(ValueError):
        sx.plan_targets({'payload': operation('one', 'ONE')})
    assert target.read_bytes() == raw


@pytest.mark.parametrize('bom,newline', [(False, '\n'), (True, '\n'), (False, '\r\n'), (True, '\r\n')])
def test_legacy_receipt_uses_raw_bytes(bom, newline):
    before = (b'\xef\xbb\xbf' if bom else b'') + TEXT.replace('\n', newline).encode()
    post = before.replace(b'Original', b'Revised')
    payload = operation('Original', 'Revised')
    path = '/synthetic/SKILL.md'
    target = {'path': path, 'preimage': sx.sha(before), 'postimage': sx.sha(post)}
    receipt = {'schema': 'steward-execution-result/v1', 'operations': [{'target': path}]}
    assert sx._target_preimage(receipt, {'payload': payload}, target, post) == before
    with pytest.raises(ValueError, match='postimage_drift'):
        sx._target_preimage(receipt, {'payload': payload}, target, b'\xef\xbb\xbf' + post)


def test_support_overwrite_not_reversible_chain(skill):
    _, target = skill
    support = target.parent / 'references/a.md'
    support.parent.mkdir()
    support.write_bytes(b'\xef\xbb\xbfOld\r\n')
    payload = {'action': 'write_file', 'name': 'fixture', 'file_path': 'references/a.md', 'file_content': 'New\n'}
    record = {'payload': payload}
    targets, entries = sx.plan_targets(record)
    outcome = json.loads(sm.apply_skill_pending(payload, restricted_targets=targets))
    assert outcome['success'], outcome
    assert support.read_bytes() == b'\xef\xbb\xbfNew\r\n'
    receipt = {'schema': 'steward-execution-result/v2', 'operations': entries}
    assert sx._target_preimage(receipt, record, targets[0], support.read_bytes()) is None
    with pytest.raises(ValueError, match='unprovable_successor_overwrite'):
        sx._target_preimage(receipt, record, targets[0], support.read_bytes(), chained=True)


def test_root_link_zero_write_and_dirty_rollback(skill, monkeypatch, tmp_path):
    home, target = skill
    real = tmp_path / 'linked'
    target.parent.rename(real)
    link = home / 'skills/fixture'
    link.symlink_to(real, target_is_directory=True)
    raw = b'\xef\xbb\xbf' + TEXT.replace('\n', '\r\n').encode()
    target.write_bytes(raw)
    aside = link.with_name('fixture.rollback-broken')
    aside.mkdir()
    (aside / 'keep').write_bytes(b'foreign')
    internal = real / 'references'
    internal.mkdir()
    (internal / 'alias.md').symlink_to('../SKILL.md')
    original_link = os.readlink(link)
    monkeypatch.setattr(sm, '_background_review_read_before_write_guard', lambda *a: {'success': False, 'error': 'fixture denied'})
    result = json.loads(sm.apply_skill_pending({'action': 'batch', 'operations': [operation()]}))
    assert not result['success'] and 'no restore needed' in result['error']
    assert link.is_symlink() and os.readlink(link) == original_link
    assert target.read_bytes() == raw
    monkeypatch.setattr(sm, '_background_review_read_before_write_guard', lambda *a: None)
    result = json.loads(sm.apply_skill_pending({'action': 'batch', 'operations': [operation(), operation('absent', 'other')]}))
    assert not result['success'] and 'rolled back' in result['error']
    assert target.read_bytes() == raw
    assert link.is_symlink() and os.readlink(link) == original_link
    assert (internal / 'alias.md').is_symlink()
    assert (aside / 'keep').read_bytes() == b'foreign'


def test_removed_internal_link_restored_without_touching_referent(skill):
    _, target = skill
    references = target.parent / 'references'
    references.mkdir()
    referent = references / 'original.md'
    raw = b'\xef\xbb\xbfUnchanged referent.\r\n'
    referent.write_bytes(raw)
    link = references / 'alias.md'
    link.symlink_to('original.md')
    result = json.loads(sm.apply_skill_pending({'action': 'batch', 'operations': [
        {'action': 'remove_file', 'name': 'fixture', 'file_path': 'references/alias.md'},
        operation('absent', 'other'),
    ]}))
    assert not result['success'] and 'rolled back' in result['error']
    assert link.is_symlink() and os.readlink(link) == 'original.md'
    assert referent.read_bytes() == raw


def test_foreign_drift_retained_on_rollback(skill, monkeypatch):
    _, target = skill
    original = sm._patch_skill
    calls = 0

    def patch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            target.write_bytes(b'foreign drift')
            return {'success': False, 'error': 'fixture failure'}
        return original(*args, **kwargs)

    monkeypatch.setattr(sm, '_patch_skill', patch)
    result = json.loads(sm.apply_skill_pending({'action': 'batch', 'operations': [operation(), operation('absent', 'other')]}))
    assert not result['success'] and 'ROLLBACK FAILED' in result['error'] and 'foreign drift' in result['error']
    assert target.read_bytes() == b'foreign drift'


@pytest.mark.parametrize('broken', ['skills:\n  disabled: 42\n', 'skills:\n  platform_disabled: [bad]\n', 'skills: ['])
def test_cold_bad_policy_denies_all_load_surfaces(skill, broken):
    home, _ = skill
    (home / 'config.yaml').write_text(broken)
    assert su.get_skill_policy().state == 'unavailable'
    for getter in (su.get_disabled_skill_names, su.get_hidden_skill_names):
        with pytest.raises(su.SkillPolicyUnavailable):
            getter()
    for result in (sk.skill_view('fixture'), sk.skills_list()):
        parsed = json.loads(result)
        assert parsed.get('success') is False or parsed.get('error')
        assert 'Original sentence.' not in result
    from agent.prompt_builder import build_skills_system_prompt
    assert 'Use when testing boundaries.' not in build_skills_system_prompt()


def test_lkg_profile_and_policy_source_isolation(skill, monkeypatch, tmp_path):
    home, _ = skill
    config = home / 'config.yaml'
    config.write_text('skills:\n  disabled: [fixture]\n')
    assert su.get_disabled_skill_names() == {'fixture'}
    config.write_text('skills: [')
    assert su.get_skill_policy().state == 'lkg'
    assert su.get_disabled_skill_names() == {'fixture'}
    monkeypatch.setenv('HERMES_PROFILE', 'another')
    assert su.get_skill_policy().state == 'unavailable'
    other = tmp_path / 'other-policy.yaml'
    other.write_text('skills: [')
    config.unlink()
    config.symlink_to(other)
    assert su.get_skill_policy().state == 'unavailable'
    other.write_text('skills:\n  disabled: []\n')
    assert su.get_skill_policy().state == 'valid'
    assert su.get_disabled_skill_names() == set()


def test_real_policy_read_error_and_recovery(skill):
    home, _ = skill
    config = home / 'config.yaml'
    config.mkdir()
    assert su.get_skill_policy().state == 'unavailable'
    config.rmdir()
    config.write_text('skills:\n  disabled: []\n')
    assert su.get_skill_policy().state == 'valid'


def test_discovery_real_permission_failure_recovers_without_ttl(skill):
    _, target = skill
    if os.geteuid() == 0:
        pytest.skip('chmod requires non-root')
    assert json.loads(sk.skills_list())['success']
    target.chmod(0)
    try:
        failed = json.loads(sk.skills_list())
        assert failed['success'] is False and failed['errors']
    finally:
        target.chmod(0o644)
    good = json.loads(sk.skills_list())
    assert good['success'] and 'fixture' in json.dumps(good)
    target.parent.chmod(0)
    try:
        failed = json.loads(sk.skills_list())
        assert failed['success'] is False and failed['errors']
    finally:
        target.parent.chmod(0o755)
    assert json.loads(sk.skills_list())['success']


def test_platform_explicit_env_contextvar_empty(skill, monkeypatch):
    home, _ = skill
    (home / 'config.yaml').write_text('skills:\n  platform_disabled:\n    telegram: [fixture]\n    discord: [other]\n')
    from gateway.session_context import _VAR_MAP
    _session_platform = _VAR_MAP['HERMES_SESSION_PLATFORM']
    monkeypatch.setenv('HERMES_SESSION_PLATFORM', 'telegram')
    token = _session_platform.set('')
    try:
        assert su.resolve_skill_platform() == ''
        assert su.get_disabled_skill_names() == set()
        from agent.prompt_builder import _current_session_platform_hint
        assert _current_session_platform_hint() == ''
        monkeypatch.setenv('HERMES_PLATFORM', 'discord')
        assert su.get_disabled_skill_names() == {'other'}
        assert su.get_disabled_skill_names('telegram') == {'fixture'}
    finally:
        _session_platform.reset(token)


@pytest.mark.parametrize('explicit,context,allowed', [
    (None, '', True), (None, 'telegram', False),
    ('cli', 'telegram', True), ('telegram', '', False),
])
def test_prompt_catalog_and_view_share_platform_precedence(skill, monkeypatch, explicit, context, allowed):
    home, _ = skill
    (home / 'config.yaml').write_text('skills:\n  platform_disabled:\n    telegram: [fixture]\n')
    monkeypatch.setenv('HERMES_SESSION_PLATFORM', 'telegram')
    if explicit is None:
        monkeypatch.delenv('HERMES_PLATFORM', raising=False)
    else:
        monkeypatch.setenv('HERMES_PLATFORM', explicit)
    from gateway.session_context import _VAR_MAP
    from agent.prompt_builder import build_skills_system_prompt
    variable = _VAR_MAP['HERMES_SESSION_PLATFORM']
    token = variable.set(context)
    try:
        prompt = build_skills_system_prompt()
        listing = json.loads(sk.skills_list())
        viewed = json.loads(sk.skill_view('fixture', preprocess=False))
        assert listing['success'] is True
        assert bool(listing['skills']) is allowed
        assert ('Use when testing boundaries.' in prompt) is allowed
        assert viewed['success'] is allowed
        assert ('Original sentence.' in json.dumps(viewed)) is allowed
        assert su.get_disabled_skill_names('') == set()
        assert su.get_disabled_skill_names('telegram') == {'fixture'}
    finally:
        variable.reset(token)


def test_external_missing_directory_not_cached_as_complete(skill, tmp_path):
    home, _ = skill
    external = tmp_path / 'external'
    (home / 'config.yaml').write_text(f'skills:\n  external_dirs: [{external}]\n')
    assert su.get_external_skills_dirs() == []
    external.mkdir()
    assert su.get_external_skills_dirs() == [external]


def test_partial_discovery_and_genuine_empty(skill):
    home, target = skill
    if os.geteuid() == 0:
        pytest.skip('chmod requires non-root')
    other = home / 'skills/other'
    other.mkdir()
    blocked = other / 'SKILL.md'
    blocked.write_text(TEXT.replace('fixture', 'other'))
    blocked.chmod(0)
    try:
        result = json.loads(sk.skills_list())
        assert result['success'] is False and result['partial'] is True and result['errors']
        assert any(row['name'] == 'fixture' for row in result['skills'])
        from agent.prompt_builder import build_skills_system_prompt
        assert 'discovery is incomplete' in build_skills_system_prompt()
    finally:
        blocked.chmod(0o644)
    assert json.loads(sk.skills_list())['success']
    blocked.unlink()
    target.unlink()
    result = json.loads(sk.skills_list())
    assert result['success'] is True and not result['skills']


def test_policy_permission_lkg_and_recovery(skill):
    home, _ = skill
    if os.geteuid() == 0:
        pytest.skip('chmod requires non-root')
    config = home / 'config.yaml'
    config.write_text('skills:\n  disabled: [fixture]\n')
    assert su.get_disabled_skill_names() == {'fixture'}
    config.chmod(0)
    try:
        policy = su.get_skill_policy()
        assert policy.state == 'lkg' and policy.errors
        assert su.get_disabled_skill_names() == {'fixture'}
    finally:
        config.chmod(0o644)
    config.write_text('skills:\n  disabled: []\n')
    assert su.get_skill_policy().state == 'valid'
    assert su.get_disabled_skill_names() == set()


@pytest.mark.parametrize('old,new,error', [
    ('Original sentence', 'Use when testing boundaries.', 'unprovable_patch_postimage'),
    ('Original  sentence.', 'Changed.', 'unique_patch_anchor'),
])
def test_restricted_exact_reversible_anchor_before_writer(skill, monkeypatch, old, new, error):
    _, target = skill
    raw = target.read_bytes()
    monkeypatch.setattr(sm, '_guarded_write', lambda *a, **k: pytest.fail('invalid anchor reached writer'))
    with pytest.raises(ValueError, match=error):
        sx.plan_targets({'payload': operation(old, new)})
    result = json.loads(sm.apply_skill_pending(operation(old, new), restricted_targets=[{'path': str(target), 'preimage': sx.sha(raw)}]))
    assert not result['success'] and error in result['error']
    assert target.read_bytes() == raw


def test_rollback_aside_failure_preserves_root_link_preimages(skill, monkeypatch):
    home, target = skill
    real = home / 'linked'
    target.parent.rename(real)
    target.parent.symlink_to(real, target_is_directory=True)
    raw = b'\xef\xbb\xbf' + TEXT.replace('\n', '\r\n').encode()
    target.write_bytes(raw)
    from tools import skill_manager_batch as batch
    original = batch.tempfile.mkdtemp

    def allocate(*args, **kwargs):
        if kwargs.get('prefix') == 'rollback-broken-':
            raise OSError('synthetic aside allocation failure')
        return original(*args, **kwargs)

    monkeypatch.setattr(batch.tempfile, 'mkdtemp', allocate)
    result = json.loads(sm.apply_skill_pending({'action': 'batch', 'operations': [operation(), operation('absent', 'new')]}))
    assert not result['success'] and 'ROLLBACK FAILED' in result['error']
    assert target.parent.is_symlink() and b'Revised sentence.' in target.read_bytes()
    import re
    snapshot = Path(re.search(r"assets preserved at '([^']+)'", result['error']).group(1))
    backups = list(snapshot.glob('preimage-*'))
    assert any(p.suffix != '.json' and p.read_bytes() == raw for p in backups)
    metadata = [json.loads(p.read_text()) for p in backups if p.suffix == '.json']
    assert any(row['path'] == str(real / 'SKILL.md') for row in metadata)


@pytest.mark.parametrize('bom,newline', [(False, '\n'), (True, '\n'), (False, '\r\n'), (True, '\r\n')])
def test_authenticated_raw_successor_chain(skill, monkeypatch, bom, newline):
    home, target = skill
    target.write_bytes((b'\xef\xbb\xbf' if bom else b'') + TEXT.replace('\n', newline).encode())
    from tools import write_approval as wa
    state = home / 'state'
    (state / 'execution-plans').mkdir(parents=True)
    (state / 'execution-results').mkdir()
    delegation = {'enabled': True, 'home': str(home), 'state': str(state), 'cron_id': 'fixture',
                  'pending_roots': [str(home / 'pending/skills')], 'target_roots': [str(home / 'skills')]}
    (home / 'steward-delegation.json').write_text(json.dumps(delegation))
    monkeypatch.setattr('tools.approval_context._is_cron_approval_context', lambda: True)
    monkeypatch.setattr(wa, 'write_approval_enabled', lambda subsystem: True)
    identities = {}
    monkeypatch.setattr(sx, 'review_identity', lambda state, event, *a: identities[event])
    events = []
    edits = [('Original sentence.', 'Revised sentence.'),
             ('Revised sentence.', 'Revised sentence.\nExtra one.\nExtra two.'),
             ('Extra two.', 'Extra two.\nFinal line.')]
    for index, (old, new) in enumerate(edits):
        record = wa.stage_write('skills', operation(old, new), summary='fixture', origin='foreground')
        source = home / 'pending/skills' / (record['id'] + '.json')
        digest = sx.sha(source.read_bytes())
        event = str(index + 1) * 64
        events.append(event)
        identities[event] = {'binding': {'source_token': sx.sha(sx.packed(['fixture', record['id'], str(source), digest]))}}
        targets, _ = sx.plan_targets(record)
        plan = {'schema': 'steward-execution-plan/v1', 'event_id': event, 'identity': identities[event],
                'decision': 'approved', 'stage': {'id': record['id'], 'sha256': digest}, 'targets': targets,
                'sources': [{'scope': 'fixture', 'path': str(source), 'id': record['id'], 'sha256': digest,
                             'coverage': [{'ordinal': 0, 'operations': [0], 'basis': 'synthetic exact addition'}]}],
                'outstanding': []}
        sx.store_new(sx.paths(state, event)[0], plan)
        before = target.read_bytes()
        result = sx.execute(event, task_id='cron:fixture:run')
        assert result['business_complete'] is True
        assert target.read_bytes() == before.replace(old.replace('\n', newline).encode(), new.replace('\n', newline).encode())
    for event in events:
        assert sx.verified_result(state, event)['business_complete'] is True
    target.write_bytes(target.read_bytes().replace(b'Extra one.', b'Lost.'))
    with pytest.raises(ValueError):
        sx.verified_result(state, events[0])
