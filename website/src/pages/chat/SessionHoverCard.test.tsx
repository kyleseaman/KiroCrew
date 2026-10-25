/**
 * The sidebar hover card's reply surface.
 *
 * The send path is the chat view's own `sendTurn`, so these pin the contract
 * that matters: Enter sends the typed text to THIS slot, the receipt drives the
 * brief confirmation ("Sent" for an immediate turn, "Queued" when the slot was
 * busy), a failure keeps the draft, and a read-only (foreign) session shows no
 * input at all.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { i18nT } from '../../i18n/t'

const sendTurn = vi.fn()
vi.mock('../../chat-core/transport/sendTurn', () => ({
  sendTurn: (...args: unknown[]) => sendTurn(...args),
}))

import SessionHoverCard from './SessionHoverCard'

function mountCard(over: Partial<React.ComponentProps<typeof SessionHoverCard>> = {}) {
  const anchor = document.createElement('div')
  document.body.appendChild(anchor)
  const onClose = vi.fn()
  const props: React.ComponentProps<typeof SessionHoverCard> = {
    slot: { key: 'chat-1', running: false, last_message: 'all done' },
    title: 'Fix the parser',
    agentName: 'coder',
    open: true,
    anchorEl: anchor,
    viaKeyboard: false,
    connected: true,
    sendable: true,
    onCardEnter: vi.fn(),
    onCardLeave: vi.fn(),
    onClose,
    ...over,
  }
  const utils = render(<SessionHoverCard {...props} />)
  return { ...utils, onClose, anchor }
}

describe('SessionHoverCard', () => {
  beforeEach(() => { sendTurn.mockReset() })

  it('renders the title and a status line', () => {
    mountCard()
    expect(screen.getByText('Fix the parser')).toBeTruthy()
    // Nothing pending and not running -> the muted last-message line.
    expect(screen.getByTestId('session-hover-status').textContent).toContain('all done')
  })

  it('shows what the session is waiting on when a question is pending', () => {
    mountCard({ slot: { key: 'chat-1', needs_input: true, last_message: 'thinking' } })
    expect(screen.getByTestId('session-hover-status').textContent)
      .toContain(i18nT('pages.chat.sessionHoverCard.status_agent_asked', { agent: 'coder' }))
  })

  it('Enter sends the typed text to this slot and confirms "Sent"', async () => {
    sendTurn.mockResolvedValue({ status: 'dispatched', body: {} })
    mountCard()
    const input = screen.getByRole('textbox') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'ship it' } })
    await act(async () => { fireEvent.submit(input.closest('form')!) })

    expect(sendTurn).toHaveBeenCalledWith({ message: 'ship it', slot: 'chat-1' })
    await waitFor(() => expect(screen.getByText(i18nT('pages.chat.sessionHoverCard.sent'))).toBeTruthy())
    // The input is cleared after a confirmed send.
    expect(input.value).toBe('')
  })

  it('a busy slot queues and confirms "Queued"', async () => {
    sendTurn.mockResolvedValue({ status: 'queued', body: {} })
    mountCard()
    const input = screen.getByRole('textbox') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'next step' } })
    await act(async () => { fireEvent.submit(input.closest('form')!) })

    expect(sendTurn).toHaveBeenCalledWith({ message: 'next step', slot: 'chat-1' })
    await waitFor(() => expect(screen.getByText(i18nT('pages.chat.sessionHoverCard.queued'))).toBeTruthy())
  })

  it('a failed send keeps the draft and surfaces the failure', async () => {
    sendTurn.mockResolvedValue({ status: 'refused', body: {}, reason: 'nope' })
    mountCard()
    const input = screen.getByRole('textbox') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'retry me' } })
    await act(async () => { fireEvent.submit(input.closest('form')!) })

    await waitFor(() => expect(screen.getByText(i18nT('pages.chat.sessionHoverCard.send_failed'))).toBeTruthy())
    expect(input.value).toBe('retry me') // not cleared, so the user can resend
  })

  it('does not send an empty / whitespace-only draft', async () => {
    mountCard()
    const input = screen.getByRole('textbox') as HTMLInputElement
    fireEvent.change(input, { target: { value: '   ' } })
    await act(async () => { fireEvent.submit(input.closest('form')!) })
    expect(sendTurn).not.toHaveBeenCalled()
  })

  it('a read-only (foreign) session shows no reply input', () => {
    mountCard({ sendable: false })
    expect(screen.queryByRole('textbox')).toBeNull()
    // Status is still readable.
    expect(screen.getByTestId('session-hover-status')).toBeTruthy()
  })

  it('a disconnected gateway disables the input (no send possible)', () => {
    mountCard({ connected: false })
    const input = screen.getByRole('textbox') as HTMLInputElement
    expect(input.disabled).toBe(true)
  })
})
