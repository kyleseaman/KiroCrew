import { i18nT } from '../../i18n/t'

/** The slot fields the hover status reads. Structural, so both the wire
 *  `ChatSlot` and the sidebar's narrowed `Slot` view satisfy it. */
export interface HoverStatusSlot {
  pending_approval?: boolean
  needs_input?: boolean
  interrupted?: boolean
  running?: boolean
  last_message?: string
}

/** Which coloured role the status line reads as — the same precedence the
 *  sidebar row's own status chain uses, collapsed to the handful the hover card
 *  shows. An owed decision (approval, then an unanswered question) outranks a
 *  "working" signal, which outranks a stalled turn, which outranks the quiet
 *  tail of the last thing the agent said. */
export type HoverStatusTone = 'warn' | 'info' | 'accent' | 'danger' | 'muted'

export interface HoverStatus {
  tone: HoverStatusTone
  /** The leading, bolded phrase — the state itself ("Needs approval"). */
  label: string
  /** Optional trailing muted detail (the last assistant line), already trimmed.
   *  Absent when the label stands alone (a bare question, where trailing the
   *  pre-ask message would read as the question itself). */
  detail?: string
  /** True while the agent is actively working, so the card can pulse its glyph
   *  and the caller can decide whether a send will queue. */
  busy: boolean
}

/** One-line condensation of the last thing the agent said, for the muted detail.
 *  Collapses whitespace and hard-caps the length; the card also truncates
 *  visually, this just keeps a pathological payload out of the DOM. */
function condense(text: string | undefined): string | undefined {
  if (!text) return undefined
  const line = text.replace(/\s+/g, ' ').trim()
  if (!line) return undefined
  return line.length > 200 ? `${line.slice(0, 199)}…` : line
}

/**
 * The hover card's single status line, derived from the fields the slot list
 * already carries — no new backend read. Mirrors the row's own ordering
 * (`pending_approval` > `needs_input` > `interrupted` > `running` > last
 * message), so the card never disagrees with the row it grew out of.
 *
 * `agentName` is the already-resolved display name, so the card can say
 * "<Agent> asked · …" the way the reference does. The slot's own `last_message`
 * is the "latest assistant summary" the card shows when nothing is pending.
 */
export function hoverStatus(slot: HoverStatusSlot, agentName: string): HoverStatus {
  const last = condense(slot.last_message)
  if (slot.pending_approval) {
    return { tone: 'warn', label: i18nT('pages.chat.sessionHoverCard.status_needs_approval'), detail: last, busy: false }
  }
  if (slot.needs_input) {
    // A card is a broadcast with no transcript row, so `last_message` is what
    // the agent said BEFORE the ask, not the question. Naming the agent that
    // asked is the signal; trailing the pre-ask line would read as the question.
    return {
      tone: 'info',
      label: agentName
        ? i18nT('pages.chat.sessionHoverCard.status_agent_asked', { agent: agentName })
        : i18nT('pages.chat.sessionHoverCard.status_needs_answer'),
      busy: false,
    }
  }
  if (slot.interrupted) {
    return { tone: 'danger', label: i18nT('pages.chat.sessionHoverCard.status_interrupted'), detail: last, busy: false }
  }
  if (slot.running) {
    return { tone: 'accent', label: i18nT('pages.chat.sessionHoverCard.status_working'), detail: last, busy: true }
  }
  if (last) {
    return { tone: 'muted', label: last, busy: false }
  }
  return { tone: 'muted', label: i18nT('pages.chat.sessionHoverCard.status_no_activity'), busy: false }
}
