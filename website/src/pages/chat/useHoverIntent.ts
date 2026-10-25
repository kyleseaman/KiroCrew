import { useCallback, useEffect, useRef, useState } from 'react'

/** Open delay: long enough that a pointer merely crossing the list on its way
 *  somewhere else never trips a card, short enough that a deliberate pause
 *  reads as "yes, this one". The reference sits in the 400–500ms band. */
export const HOVER_OPEN_DELAY_MS = 450
/** Close grace: the pointer leaving the row has to be able to travel INTO the
 *  card (there is a gap between them), so the close is held briefly rather than
 *  firing the instant the row's own `mouseleave` does. */
export const HOVER_CLOSE_GRACE_MS = 150

export interface HoverIntent {
  open: boolean
  /** Whether the latest open was reached through keyboard focus rather than the
   *  pointer. The card uses it to decide whether to pull focus into itself. */
  viaKeyboard: boolean
  /** Pointer entered an eligible surface (the row). Starts the open timer. */
  onPointerEnter: () => void
  /** Pointer left an eligible surface. Starts the close timer. */
  onPointerLeave: () => void
  /** Keyboard focus landed on the row via `:focus-visible` (a real keyboard
   *  user, not a mouse click that also focuses). Opens after the same delay. */
  onFocusVisible: () => void
  /** Focus left the row-and-card subtree for good. */
  onBlur: () => void
  /** Pointer/focus is inside the card itself — cancels any pending close. */
  onCardEnter: () => void
  /** Pointer left the card. */
  onCardLeave: () => void
  /** Close immediately and cancel every pending timer (Escape, send, a click
   *  that navigates). */
  close: () => void
}

/**
 * Delayed-open / grace-close intent for a hover surface and the floating card
 * it reveals.
 *
 * The card and its trigger are two separate hover regions with a gap between
 * them, so "still hovering" is the union of four states — pointer on the row,
 * pointer on the card, focus on the row, focus inside the card — and a close is
 * only real when ALL four are false and stays false for the grace window. That
 * is why open/close are timers over a single boolean rather than a raw
 * `onMouseEnter`/`onMouseLeave` pair: the latter would flicker the card shut
 * every time the pointer crossed the gap.
 *
 * `suppressed` is the caller's veto (a drag in flight, a context menu open, the
 * active row). While true the card can neither open nor stay open: it is forced
 * shut and every pending timer is dropped, so lifting the veto does not release
 * a stale open the user never asked for.
 */
export function useHoverIntent(suppressed: boolean): HoverIntent {
  const [open, setOpen] = useState(false)
  const viaKeyboardRef = useRef(false)
  const [viaKeyboard, setViaKeyboard] = useState(false)
  const openTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const clearTimers = useCallback(() => {
    if (openTimer.current) { clearTimeout(openTimer.current); openTimer.current = null }
    if (closeTimer.current) { clearTimeout(closeTimer.current); closeTimer.current = null }
  }, [])

  const close = useCallback(() => {
    clearTimers()
    setOpen(false)
  }, [clearTimers])

  const scheduleOpen = useCallback((keyboard: boolean) => {
    if (suppressed) return
    if (closeTimer.current) { clearTimeout(closeTimer.current); closeTimer.current = null }
    if (open || openTimer.current) {
      // Already open or already arming — a keyboard arrival still upgrades the
      // focus-intent flag so the card knows to take focus.
      if (keyboard) { viaKeyboardRef.current = true; setViaKeyboard(true) }
      return
    }
    openTimer.current = setTimeout(() => {
      openTimer.current = null
      viaKeyboardRef.current = keyboard
      setViaKeyboard(keyboard)
      setOpen(true)
    }, HOVER_OPEN_DELAY_MS)
  }, [open, suppressed])

  const scheduleClose = useCallback(() => {
    if (openTimer.current) { clearTimeout(openTimer.current); openTimer.current = null }
    if (closeTimer.current) clearTimeout(closeTimer.current)
    closeTimer.current = setTimeout(() => {
      closeTimer.current = null
      setOpen(false)
    }, HOVER_CLOSE_GRACE_MS)
  }, [])

  // The veto forces the card shut and drops pending timers, so lifting it never
  // releases an open the user walked away from.
  useEffect(() => {
    if (suppressed) close()
  }, [suppressed, close])

  useEffect(() => clearTimers, [clearTimers])

  return {
    open: open && !suppressed,
    viaKeyboard,
    onPointerEnter: () => scheduleOpen(false),
    onPointerLeave: scheduleClose,
    onFocusVisible: () => scheduleOpen(true),
    onBlur: scheduleClose,
    onCardEnter: () => { if (closeTimer.current) { clearTimeout(closeTimer.current); closeTimer.current = null } },
    onCardLeave: scheduleClose,
    close,
  }
}
