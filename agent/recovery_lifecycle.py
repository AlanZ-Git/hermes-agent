"""Recovery-only ownership of exact request objects and execution threads."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import RLock, Thread, current_thread
from typing import Callable, Iterator, TypeVar

T = TypeVar('T')
_current: ContextVar['RecoveryLifecycle | None'] = ContextVar('recovery_lifecycle', default=None)


@dataclass
class RequestRegistration:
    request: object
    thread: Thread
    finished: bool = False
    cleanup_ok: bool = False


class RecoveryLifecycle:
    def __init__(self, owner: Thread):
        self.owner = owner
        self._lock = RLock()
        self._requests: list[RequestRegistration] = []
        self._owner_finished = False
        self._owner_cleanup_ok = False
        self._unsupported: set[str] = set()

    @contextmanager
    def scope(self) -> Iterator[None]:
        token = _current.set(self)
        try:
            yield
        finally:
            _current.reset(token)

    def finish_owner(self, cleanup_ok: bool) -> None:
        with self._lock:
            self._owner_finished = True
            self._owner_cleanup_ok = cleanup_ok

    def unsupported(self, reason: str) -> None:
        with self._lock:
            self._unsupported.add(reason)

    def register(self, request: object, thread: Thread) -> RequestRegistration:
        with self._lock:
            registration = RequestRegistration(request, thread)
            self._requests.append(registration)
            return registration

    def observe(self) -> dict:
        with self._lock:
            active = sum(not r.finished or r.thread.is_alive() for r in self._requests)
            if self.owner.is_alive() or active:
                return {'outcome': 'running', 'reason': 'owned_execution_active', 'active_requests': active}
            if (not self._owner_finished or not self._owner_cleanup_ok or self._unsupported
                    or any(not r.cleanup_ok for r in self._requests)):
                return {'outcome': 'unknown', 'reason': 'owner_or_request_cleanup_unproven',
                        'unsupported': sorted(self._unsupported)}
            return {'outcome': 'exited_proven', 'reason': 'owner_and_registered_requests_exited',
                    'request_count': len(self._requests)}


def register_request(request: object) -> RequestRegistration | None:
    tracker = _current.get()
    return tracker.register(request, current_thread()) if tracker is not None else None


def finish_request(registration: RequestRegistration | None) -> None:
    if registration is not None:
        registration.cleanup_ok = True
        registration.finished = True


def request_target(request: object, target: Callable[[], T], thread: Thread | None = None) -> Callable[[], T]:
    tracker = _current.get()
    if tracker is None:
        return target
    registration = tracker.register(request, thread or current_thread())

    def run() -> T:
        with tracker.scope():
            try:
                value = target()
            except BaseException:
                with tracker._lock:
                    registration.finished = True
                raise
            else:
                with tracker._lock:
                    registration.cleanup_ok = True
                    registration.finished = True
                return value
    return run


def bind_request_thread(request: object, thread: Thread, target: Callable[[], T]) -> Callable[[], T]:
    return request_target(request, target, thread)


def request_thread(request: object, target: Callable[[], T], **kwargs) -> Thread:
    run = target
    thread = Thread(target=lambda: run(), **kwargs)
    run = bind_request_thread(request, thread, target)
    return thread


def unsupported(reason: str) -> None:
    tracker = _current.get()
    if tracker is not None:
        tracker.unsupported(reason)
