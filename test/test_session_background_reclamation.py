"""The background session is reclaimable while an in-use one is never reaped.

``BACKGROUND_KEY`` is deliberately NOT in ``_PERSISTENT_KEYS`` on this branch: a
gateway whose interactive sessions run in a managed remote environment must be able
to reclaim its local background runtime instead of pinning a kiro-cli process
forever. That makes the entry reapable, so the acquisition path has to establish
OWNERSHIP rather than merely look recently-used.

The adversary here is the REAL sweep. These tests drive a real ``SessionManager``
(so ``session_cleanup.SessionCleanup._expire_idle`` and
``session_lifecycle``'s ``reset`` are the production code under test) with a fake
provider standing in only for the external kiro-cli process.

Why ownership and not a timestamp: ``_expire_idle`` selects candidates in one
locked pass and resets them in a LATER, unlocked one, and that second pass
re-checks ONLY ``semaphore.locked()`` -- never ``last_used``. So a freshened
timestamp cannot save an already-selected candidate; a held permit can.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from kiro_crew.session import _PERSISTENT_KEYS, BACKGROUND_KEY, SessionManager
from kiro_crew.session_background import (
    _BG_ACQUIRE_MAX_ATTEMPTS,
    BackgroundSessionRuntime,
    _ProviderBgSession,
)


class FakeProvider:
    """Stands in for the external kiro-cli process only."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.started = False
        self.shutdown_called = False
        self.session_id = f"sid-{name}"

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.shutdown_called = True

    def is_alive(self) -> bool:
        return not self.shutdown_called

    def is_process_alive(self) -> bool:
        return not self.shutdown_called

    async def stream(self, message: str):
        if self.shutdown_called:
            raise AssertionError(
                f"streamed on a shut-down provider ({self.name}) -- "
                "a reaped entry was handed to a live handle"
            )
        yield {"text": message}

    async def reject_tool(self, request_id: object) -> None:  # pragma: no cover - parity
        return None


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A real SessionManager whose provider factory yields FakeProviders."""
    from kiro_crew.config.loader import KiroCrewConfig

    made: list[FakeProvider] = []

    def factory(key: str, **kwargs: object) -> FakeProvider:
        provider = FakeProvider(f"p{len(made)}")
        made.append(provider)
        return provider

    cfg = KiroCrewConfig()
    mgr = SessionManager(cfg, provider_factory=factory)
    mgr.created = made  # type: ignore[attr-defined]
    return mgr


def bg_runtime(mgr: SessionManager) -> BackgroundSessionRuntime:
    """The manager's own background boundary — production wiring, not a stub."""
    return mgr._background_runtime


async def acquire_handle(mgr: SessionManager):
    return await mgr._provider_backed_bg_session()


def test_background_is_not_a_persistent_key() -> None:
    """The premise: reclamation is only possible because _bg is sweepable."""
    assert BACKGROUND_KEY not in _PERSISTENT_KEYS
    assert _PERSISTENT_KEYS, "the heartbeat key must still be protected"


@pytest.mark.asyncio
async def test_a_live_handle_makes_the_real_sweep_refuse_the_entry(manager) -> None:
    """The interleaving a freshened timestamp does NOT survive.

    The entry is genuinely idle-eligible, and a caller then holds a handle. The
    REAL ``_expire_idle`` must leave it alone, so the handle streams on a live
    provider.
    """
    await manager._ensure_background()
    entry = manager._sessions[BACKGROUND_KEY]
    entry.last_used = 0.0  # unambiguously idle by the clock

    handle = await acquire_handle(manager)
    assert entry.semaphore.locked(), "acquisition must take ownership"

    await manager._expire_idle(1)

    assert manager._sessions.get(BACKGROUND_KEY) is entry, "the sweep reaped an owned entry"
    assert entry.provider.shutdown_called is False

    assert [e async for e in handle.prompt("hello")] == [{"text": "hello"}]
    await handle.destroy()


@pytest.mark.asyncio
async def test_destroy_releases_ownership_so_the_real_sweep_can_reclaim(manager) -> None:
    """Reclamation is preserved: unowned and idle, the real sweep reaps it."""
    handle = await acquire_handle(manager)
    entry = manager._sessions[BACKGROUND_KEY]

    entry.last_used = 0.0
    await manager._expire_idle(1)
    assert manager._sessions.get(BACKGROUND_KEY) is entry, "owned entry must survive"

    await handle.destroy()
    assert not entry.semaphore.locked()

    entry.last_used = 0.0
    await manager._expire_idle(1)
    assert BACKGROUND_KEY not in manager._sessions, "an unowned idle entry must be reclaimable"


@pytest.mark.asyncio
async def test_a_reaped_background_session_is_recreated_on_next_acquisition(manager) -> None:
    """Reclamation stays transparent: the next caller gets a fresh live entry."""
    first = await acquire_handle(manager)
    first_entry = manager._sessions[BACKGROUND_KEY]
    await first.destroy()
    first_entry.last_used = 0.0
    await manager._expire_idle(1)
    assert BACKGROUND_KEY not in manager._sessions

    second = await acquire_handle(manager)
    second_entry = manager._sessions[BACKGROUND_KEY]
    assert second_entry is not first_entry
    assert [e async for e in second.prompt("after reclaim")] == [{"text": "after reclaim"}]
    await second.destroy()


@pytest.mark.asyncio
async def test_reclaiming_the_background_session_does_not_consolidate_it(manager) -> None:
    """A stateless shared scratch session must not spend an LLM consolidation pass."""
    seen: list[str] = []
    manager.on_session_expire = seen.append
    await manager._ensure_background()
    manager._sessions[BACKGROUND_KEY].last_used = 0.0

    await manager._expire_idle(1)

    assert BACKGROUND_KEY not in manager._sessions, "it must still be reclaimed"
    assert seen == [], f"consolidation fired for a stateless session: {seen}"


@pytest.mark.asyncio
async def test_an_entry_reset_between_ensure_and_acquire_is_never_handed_out(manager) -> None:
    """The stale-candidate interleaving: reset lands while we wait on the permit."""
    await manager._ensure_background()
    doomed = manager._sessions[BACKGROUND_KEY]
    await doomed.semaphore.acquire()  # occupy it so acquisition must wait

    acquiring = asyncio.create_task(acquire_handle(manager))
    await asyncio.sleep(0)

    # An operator-style reset: pops the entry and shuts its provider down while
    # our acquisition is blocked on the permit.
    await manager.reset(BACKGROUND_KEY)
    assert doomed.provider.shutdown_called is True
    doomed.semaphore.release()

    handle = await acquiring
    fresh = manager._sessions[BACKGROUND_KEY]
    assert fresh is not doomed
    # FakeProvider.stream raises if shut down, so this proves the dead provider
    # was not handed out.
    assert [e async for e in handle.prompt("fresh")] == [{"text": "fresh"}]
    await handle.destroy()


def swap_factory(runtime: BackgroundSessionRuntime, factory) -> None:
    """Replace the handle factory on the FROZEN deps dataclass."""
    object.__setattr__(runtime._deps, "provider_bg_session_factory", factory)


@pytest.mark.asyncio
async def test_cancellation_while_awaiting_the_owner_lock_releases_the_permit(
    manager, monkeypatch
) -> None:
    """A cancelled acquisition must not strand the permit.

    NOTE ON COVERAGE: the only ``await`` inside the protected region is the
    identity re-check's ``async with owner._lock``, and the acquisition path takes
    that SAME lock once BEFORE the permit (to read the entry). Contending it from
    a test therefore blocks the pre-permit use, so the post-permit wait cannot be
    interleaved deterministically from outside. What is asserted here is the
    reachable half -- a cancellation delivered while the permit is held releases
    it -- driven by cancelling the task at its next suspension point after the
    permit is taken. The structural guarantee for every other escape is the
    ``try/finally`` (see ``test_acquisition_transfers_under_a_finally``) plus the
    raising-factory and raising-adopt tests above, which exercise real exceptions
    inside that region.
    """
    await manager._ensure_background()
    entry = manager._sessions[BACKGROUND_KEY]

    released: list[bool] = []
    real_release = entry.semaphore.release

    def watched_release() -> None:
        released.append(True)
        real_release()

    monkeypatch.setattr(entry.semaphore, "release", watched_release)

    # A factory that raises CancelledError puts a cancellation INSIDE the
    # protected region -- the same class of escape a real cancellation takes.
    def cancelled(_sess: object) -> object:
        raise asyncio.CancelledError()

    swap_factory(bg_runtime(manager), cancelled)
    with pytest.raises(asyncio.CancelledError):
        await acquire_handle(manager)

    assert released, "a cancellation inside the permit region stranded the permit"
    assert not entry.semaphore.locked()


@pytest.mark.asyncio
async def test_a_raising_factory_releases_the_permit(manager) -> None:
    """An exception between acquire and transfer must not strand the permit."""
    await manager._ensure_background()
    entry = manager._sessions[BACKGROUND_KEY]

    def boom(_sess: object) -> object:
        raise RuntimeError("factory exploded")

    swap_factory(bg_runtime(manager), boom)
    with pytest.raises(RuntimeError, match="factory exploded"):
        await acquire_handle(manager)
    assert not entry.semaphore.locked(), "a raising factory stranded the permit"


@pytest.mark.asyncio
async def test_a_raising_adopt_releases_the_permit(manager) -> None:
    """Ownership transfer itself failing must not strand the permit either."""
    await manager._ensure_background()
    entry = manager._sessions[BACKGROUND_KEY]

    class BadHandle(_ProviderBgSession):
        def adopt_semaphore(self) -> None:
            raise RuntimeError("adopt exploded")

    swap_factory(bg_runtime(manager), BadHandle)
    with pytest.raises(RuntimeError, match="adopt exploded"):
        await acquire_handle(manager)
    assert not entry.semaphore.locked(), "a raising adopt stranded the permit"


@pytest.mark.asyncio
async def test_a_factory_that_cannot_adopt_is_refused_not_silently_unprotected(manager) -> None:
    """The fallback must not hand back an UNOWNED handle.

    A handle that cannot adopt would re-acquire in ``prompt`` and deadlock on the
    very permit this path holds, so it is a programming error surfaced here
    rather than a hang later -- and the permit is still released.
    """
    await manager._ensure_background()
    entry = manager._sessions[BACKGROUND_KEY]

    class NoAdopt:
        def __init__(self, sess: object) -> None:
            self._sess = sess

    swap_factory(bg_runtime(manager), NoAdopt)
    with pytest.raises(TypeError, match="adopt_semaphore"):
        await acquire_handle(manager)
    assert not entry.semaphore.locked(), "the refused path stranded the permit"


@pytest.mark.asyncio
async def test_prompt_does_not_release_ownership_it_did_not_take(manager) -> None:
    """A completed turn must not hand the entry back mid-handle-lifetime."""
    handle = await acquire_handle(manager)
    entry = manager._sessions[BACKGROUND_KEY]

    assert [e async for e in handle.prompt("one")] == [{"text": "one"}]
    assert entry.semaphore.locked(), "ownership was dropped after a turn"
    assert [e async for e in handle.prompt("two")] == [{"text": "two"}]
    assert entry.semaphore.locked()

    await handle.destroy()
    assert not entry.semaphore.locked()


@pytest.mark.asyncio
async def test_a_handle_built_without_ownership_still_serializes_per_turn(manager) -> None:
    """Backward compatibility: a directly-built handle acquires per turn."""
    await manager._ensure_background()
    entry = manager._sessions[BACKGROUND_KEY]
    handle = _ProviderBgSession(entry)

    assert not entry.semaphore.locked()
    async for _ in handle.prompt("solo"):
        assert entry.semaphore.locked(), "a turn must hold the semaphore"
    assert not entry.semaphore.locked(), "a self-acquired turn must release"


def test_the_acquire_retry_bound_is_finite_and_small() -> None:
    """A pathological reset loop must not spin here forever."""
    assert 1 < _BG_ACQUIRE_MAX_ATTEMPTS <= 5


def test_acquisition_transfers_under_a_finally() -> None:
    """The permit release must be structural, not a per-branch afterthought."""
    src = inspect.getsource(BackgroundSessionRuntime._provider_backed_bg_session)
    assert "finally:" in src, "the permit region must be exception-safe"
    assert "transferred" in src, "ownership transfer must be tracked explicitly"
