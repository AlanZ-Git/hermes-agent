"""Publishing and expiry are distinct: useful prompts notify, expired ones cannot consent."""
from tools import approval
from tools import approval_gateway_wait as wait


def test_nonzero_deadline_publishes_but_slow_callback_cannot_approve(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(wait.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(wait._ctx, '_get_approval_timeout', lambda: 5)
    monkeypatch.setattr(wait._ctx, '_fire_approval_hook', lambda *a, **kw: None)
    notified = []

    def notify(data):
        notified.append(data)
        now[0] = 16.0
        assert approval.resolve_gateway_approval('fixture-notify-deadline', 'once') == 0

    result = wait._await_gateway_decision('fixture-notify-deadline', notify,
        {'command': 'fixture', 'description': 'fixture', 'pattern_keys': ['fixture']})
    assert len(notified) == 1
    assert notified[0]['timeout_ms'] == 5000
    assert result['resolved'] is False and result['choice'] is None
    assert 'fixture-notify-deadline' not in approval._gateway_queues
