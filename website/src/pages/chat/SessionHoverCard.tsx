import { useEffect, useRef, useState } from 'react'
import { CornerDownLeft, Loader2, ShieldCheck, MessageCircleQuestionMark, TriangleAlert } from 'lucide-react'
import { Popover, PopoverContent, PopoverAnchor } from '../../components/ui/popover'
import { i18nT } from '../../i18n/t'
import { sendTurn } from '../../chat-core/transport/sendTurn'
import { hoverStatus, type HoverStatusTone, type HoverStatusSlot } from './sessionHoverStatus'

/** The slot fields the card reads: a session key to send to, plus the status
 *  fields. Structural, so the wire `ChatSlot` and the sidebar `Slot` both fit. */
export type HoverCardSlot = HoverStatusSlot & { key: string }

/** How long the "Sent"/"Queued" confirmation stays before the input returns. */
const CONFIRM_MS = 1600

/** Maps the status tone onto the theme token used for its glyph + bold label.
 *  Tokens only (theming-contract): no literal colours. */
const TONE_COLOR: Record<HoverStatusTone, string> = {
  warn: 'var(--warn)',
  info: 'var(--info)',
  accent: 'var(--accent)',
  danger: 'var(--danger)',
  muted: 'var(--muted)',
}

function StatusGlyph({ tone, busy }: { tone: HoverStatusTone; busy: boolean }) {
  if (busy) return <Loader2 size={12} className="shrink-0 animate-spin" style={{ color: TONE_COLOR.accent }} aria-hidden />
  if (tone === 'warn') return <ShieldCheck size={12} className="shrink-0" style={{ color: TONE_COLOR.warn }} aria-hidden />
  if (tone === 'info') return <MessageCircleQuestionMark size={12} className="shrink-0" style={{ color: TONE_COLOR.info }} aria-hidden />
  if (tone === 'danger') return <TriangleAlert size={12} className="shrink-0" style={{ color: TONE_COLOR.danger }} aria-hidden />
  return null
}

export interface SessionHoverCardProps {
  slot: HoverCardSlot
  /** Resolved display title (falls back to the key upstream). */
  title: string
  /** Resolved agent display name, for the status line and the input label. */
  agentName: string
  open: boolean
  /** The DOM node the card anchors beside — the hovered row. */
  anchorEl: HTMLElement | null
  /** Open was reached via keyboard, so the card should take focus on open. */
  viaKeyboard: boolean
  /** The gateway is reachable. A send is impossible while disconnected. */
  connected: boolean
  /** This session accepts a message from here. False for a peer-owned
   *  (foreign) row or any read-only session: the input is hidden. */
  sendable: boolean
  /** Pointer/focus entered the card — keep it open. */
  onCardEnter: () => void
  /** Pointer left the card. */
  onCardLeave: () => void
  /** Close now (Escape, a completed send, the veto). */
  onClose: () => void
}

/**
 * A floating card revealed on hovering a sidebar session row. It lets the user
 * read what the session is waiting on and reply to it WITHOUT opening it — the
 * reply rides the same send path the chat view uses (`sendTurn`), so it starts
 * a turn on an idle session or queues on a busy one, exactly as typing in the
 * open chat would.
 *
 * Anchored beside the row with Radix's Popover so it escapes the sidebar's
 * `overflow` and clip. Open/close TIMING is owned by the caller's hover-intent
 * hook; this component only renders the open state and owns the reply.
 */
export default function SessionHoverCard({
  slot, title, agentName, open, anchorEl, viaKeyboard, connected, sendable,
  onCardEnter, onCardLeave, onClose,
}: SessionHoverCardProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  // A brief post-send acknowledgement: 'dispatched' started a turn, 'queued'
  // parked it behind the running one, 'error' could not deliver.
  const [confirm, setConfirm] = useState<'dispatched' | 'queued' | 'error' | null>(null)
  const confirmTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Reset the draft and any stale confirmation each time the card opens, so a
  // reopened row never shows the last row's leftovers.
  useEffect(() => {
    if (!open) return
    setConfirm(null)
    setSending(false)
    if (confirmTimer.current) { clearTimeout(confirmTimer.current); confirmTimer.current = null }
  }, [open])

  useEffect(() => () => { if (confirmTimer.current) clearTimeout(confirmTimer.current) }, [])

  const status = hoverStatus(slot, agentName)
  const canSend = sendable && connected && !sending
  const replyLabel = agentName
    ? i18nT('pages.chat.sessionHoverCard.reply_to_agent', { agent: agentName })
    : i18nT('pages.chat.sessionHoverCard.reply_placeholder')

  async function submit() {
    const text = draft.trim()
    if (!text || !canSend) return
    setSending(true)
    setConfirm(null)
    const receipt = await sendTurn({ message: text, slot: slot.key })
    setSending(false)
    if (receipt.status === 'dispatched' || receipt.status === 'queued') {
      setDraft('')
      setConfirm(receipt.status)
      if (confirmTimer.current) clearTimeout(confirmTimer.current)
      // Close shortly after acknowledging, so the card does not linger as a
      // modal the user now has to dismiss — the reply is a fire-and-read action.
      confirmTimer.current = setTimeout(() => { setConfirm(null); onClose() }, CONFIRM_MS)
    } else {
      // response-late / unknown / transport-error / refused: the text may or
      // may not have been delivered, so keep the draft for the user to decide
      // and surface that it did not confirm.
      setConfirm('error')
    }
  }

  if (!anchorEl) return null

  const titleId = `session-hover-title-${slot.key}`

  return (
    <Popover open={open} onOpenChange={o => { if (!o) onClose() }}>
      <PopoverAnchor virtualRef={{ current: anchorEl }} />
      <PopoverContent
          side="right"
          align="start"
          sideOffset={6}
          collisionPadding={8}
          role="dialog"
          aria-labelledby={titleId}
          data-testid="session-hover-card"
          // Hover must never yank focus from what the user is typing elsewhere;
          // only a keyboard-opened card takes focus (into the input when there
          // is one, else the card itself via the autoFocus fallback below).
          onOpenAutoFocus={e => {
            if (!viaKeyboard) { e.preventDefault(); return }
            if (sendable && connected) { e.preventDefault(); inputRef.current?.focus() }
          }}
          // The card is a hover affordance layered over the list; a click or
          // focus move OUTSIDE it should not steal focus or scroll — the
          // hover-intent timers own the close. Escape still closes (Radix).
          onInteractOutside={e => e.preventDefault()}
          onPointerEnter={onCardEnter}
          onPointerLeave={onCardLeave}
          onFocusCapture={onCardEnter}
          onBlurCapture={onCardLeave}
          // Width cap keeps it inside a 320px viewport; the wrapper already
          // supplies border / bg-bg-elevated / z / entry animation, and we tune
          // padding, radius and shadow for a compact hover card. `motion-reduce:`
          // drops the open animation under prefers-reduced-motion in CSS alone.
          className="max-w-[calc(100vw-16px)] rounded-lg p-3 shadow-lg motion-reduce:animate-none motion-reduce:data-[state=open]:animate-none"
        >
          {/* Title — the session title, clamped to ~2 lines. */}
          <div
            id={titleId}
            className="text-sm font-semibold leading-snug line-clamp-2 break-words"
            title={title}
          >
            {title}
          </div>

          {/* One muted status line: what the session is waiting on / its latest
              state. Bold leading phrase, optional muted trailing detail. */}
          <div className="mt-1.5 flex items-start gap-1.5 text-[13px] min-w-0" data-testid="session-hover-status">
            <span className="mt-[3px]"><StatusGlyph tone={status.tone} busy={status.busy} /></span>
            <span className="min-w-0 truncate">
              <span className="font-medium" style={status.tone === 'muted' ? undefined : { color: TONE_COLOR[status.tone] }}>
                {status.label}
              </span>
              {status.detail ? <span className="text-muted"> · {status.detail}</span> : null}
            </span>
          </div>

          {/* Reply input — hidden for a read-only / foreign session, where there
              is nothing to send to from here. */}
          {sendable && (
            <form
              className="mt-2.5"
              onSubmit={e => { e.preventDefault(); void submit() }}
            >
              <div className={`flex items-center gap-1.5 rounded-md border bg-bg px-2 py-1 transition-colors ${connected ? 'border-border focus-within:border-accent' : 'border-border opacity-60'}`}>
                <input
                  ref={inputRef}
                  type="text"
                  value={draft}
                  disabled={!connected || sending}
                  onChange={e => setDraft(e.target.value)}
                  onKeyDown={e => {
                    // Escape closes the card (and is not consumed by Radix's own
                    // dismiss, which also fires). Enter submits via the form.
                    if (e.key === 'Escape') { onClose() }
                  }}
                  placeholder={replyLabel}
                  aria-label={replyLabel}
                  className="min-w-0 flex-1 border-none bg-transparent text-[13px] text-text outline-hidden placeholder:text-muted"
                  // Keep hover-intent alive while the field is focused/holds text.
                  onFocus={onCardEnter}
                />
                {sending
                  ? <Loader2 size={13} className="shrink-0 animate-spin text-muted" aria-hidden />
                  : (
                    <button
                      type="submit"
                      disabled={!canSend || draft.trim() === ''}
                      aria-label={i18nT('pages.chat.sessionHoverCard.send')}
                      className="shrink-0 rounded border-none bg-transparent p-0.5 text-muted outline-hidden transition-colors enabled:hover:text-accent enabled:focus-visible:text-accent disabled:opacity-40"
                    >
                      <CornerDownLeft size={13} aria-hidden />
                    </button>
                  )}
              </div>
              {/* Brief confirmation / failure, announced politely. */}
              <div className="mt-1 h-[15px] text-[11px]" aria-live="polite">
                {confirm === 'dispatched' && <span className="text-ok">{i18nT('pages.chat.sessionHoverCard.sent')}</span>}
                {confirm === 'queued' && <span className="text-muted">{i18nT('pages.chat.sessionHoverCard.queued')}</span>}
                {confirm === 'error' && <span className="text-danger">{i18nT('pages.chat.sessionHoverCard.send_failed')}</span>}
              </div>
            </form>
          )}
        </PopoverContent>
    </Popover>
  )
}
