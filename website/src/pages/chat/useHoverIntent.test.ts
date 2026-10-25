/**
 * Open/close TIMING for the sidebar hover card's intent hook.
 *
 * The card and its trigger are two hover regions with a gap between them, so
 * "still hovering" is the union of four states and a close only fires when all
 * of them have been false for the grace window. These tests pin exactly that:
 * the open delay, the grace close, that moving into the card cancels the close,
 * and that the caller's veto forces the card shut and drops pending timers.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { useHoverIntent, HOVER_OPEN_DELAY_MS, HOVER_CLOSE_GRACE_MS } from './useHoverIntent'

describe('useHoverIntent', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => { vi.runOnlyPendingTimers(); vi.useRealTimers() })

  it('opens only after the open delay elapses', () => {
    const { result } = renderHook(() => useHoverIntent(false))
    expect(result.current.open).toBe(false)

    act(() => { result.current.onPointerEnter() })
    // Still closed one tick before the threshold.
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_DELAY_MS - 1) })
    expect(result.current.open).toBe(false)

    act(() => { vi.advanceTimersByTime(1) })
    expect(result.current.open).toBe(true)
    expect(result.current.viaKeyboard).toBe(false)
  })

  it('does not open if the pointer leaves before the delay', () => {
    const { result } = renderHook(() => useHoverIntent(false))
    act(() => { result.current.onPointerEnter() })
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_DELAY_MS - 10) })
    act(() => { result.current.onPointerLeave() })
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_DELAY_MS) })
    expect(result.current.open).toBe(false)
  })

  it('closes after the grace window once the pointer leaves', () => {
    const { result } = renderHook(() => useHoverIntent(false))
    act(() => { result.current.onPointerEnter() })
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_DELAY_MS) })
    expect(result.current.open).toBe(true)

    act(() => { result.current.onPointerLeave() })
    act(() => { vi.advanceTimersByTime(HOVER_CLOSE_GRACE_MS - 1) })
    expect(result.current.open).toBe(true) // still within grace

    act(() => { vi.advanceTimersByTime(1) })
    expect(result.current.open).toBe(false)
  })

  it('moving into the card cancels a pending close', () => {
    const { result } = renderHook(() => useHoverIntent(false))
    act(() => { result.current.onPointerEnter() })
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_DELAY_MS) })
    act(() => { result.current.onPointerLeave() })        // arm the close
    act(() => { result.current.onCardEnter() })           // pointer reached the card
    act(() => { vi.advanceTimersByTime(HOVER_CLOSE_GRACE_MS * 3) })
    expect(result.current.open).toBe(true)

    // Leaving the card then closes after the grace window.
    act(() => { result.current.onCardLeave() })
    act(() => { vi.advanceTimersByTime(HOVER_CLOSE_GRACE_MS) })
    expect(result.current.open).toBe(false)
  })

  it('a keyboard open marks viaKeyboard', () => {
    const { result } = renderHook(() => useHoverIntent(false))
    act(() => { result.current.onFocusVisible() })
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_DELAY_MS) })
    expect(result.current.open).toBe(true)
    expect(result.current.viaKeyboard).toBe(true)
  })

  it('close() shuts immediately and drops the pending open', () => {
    const { result } = renderHook(() => useHoverIntent(false))
    act(() => { result.current.onPointerEnter() })
    act(() => { result.current.close() })
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_DELAY_MS * 2) })
    expect(result.current.open).toBe(false)
  })

  it('the suppression veto forces it shut and blocks reopen', () => {
    const { result, rerender } = renderHook(({ s }) => useHoverIntent(s), { initialProps: { s: false } })
    act(() => { result.current.onPointerEnter() })
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_DELAY_MS) })
    expect(result.current.open).toBe(true)

    // Veto raised (e.g. a drag starts) — the card is forced shut.
    rerender({ s: true })
    expect(result.current.open).toBe(false)

    // A hover attempt while vetoed never opens, even after the delay.
    act(() => { result.current.onPointerEnter() })
    act(() => { vi.advanceTimersByTime(HOVER_OPEN_DELAY_MS * 2) })
    expect(result.current.open).toBe(false)

    // Lifting the veto does not release a stale open — the user must hover again.
    rerender({ s: false })
    expect(result.current.open).toBe(false)
  })
})
