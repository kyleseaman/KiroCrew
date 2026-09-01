import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn() }))

import { copyToClipboard } from '../utils/clipboard'
import { ExecutionLocationBadge } from './ExecutionLocationBadge'

describe('ExecutionLocationBadge', () => {
  beforeEach(() => {
    vi.mocked(copyToClipboard).mockReset()
    vi.mocked(copyToClipboard).mockResolvedValue()
  })

  it('keeps the live remote workspace compact until its details are expanded', async () => {
    const user = userEvent.setup()
    render(
      <ExecutionLocationBadge
        configurationLabel="gpu"
        location={{
          kind: 'coder',
          workspace: 'crew-dogfood',
          remote_cwd: '/home/coder/private-project',
          state: 'running',
        }}
      />,
    )

    const control = screen.getByRole('button', { name: 'Session Environments' })
    expect(control).toHaveTextContent('Coder')
    expect(control).not.toHaveTextContent('crew-dogfood')
    expect(screen.getByTestId('execution-location-badge')).toHaveAttribute(
      'title',
      'Coder workspace · crew-dogfood',
    )
    expect(screen.getByTestId('execution-location-ready')).toBeInTheDocument()

    await user.click(control)

    expect(screen.getByText('Session Environment')).toBeInTheDocument()
    expect(screen.queryByText('Provider')).not.toBeInTheDocument()
    expect(screen.getByText('gpu')).toBeInTheDocument()
    expect(screen.getByText('crew-dogfood')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('/home/coder/private-project')
  })

  it('does not claim a retained workspace is currently awake', async () => {
    const user = userEvent.setup()
    render(
      <ExecutionLocationBadge
        location={{
          kind: 'coder',
          workspace: 'crew-retained',
          remote_cwd: '/workspace',
          state: 'retained',
        }}
      />,
    )

    expect(screen.queryByTestId('execution-location-ready')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Session Environments' }))
    expect(screen.getByText('crew-retained')).toBeInTheDocument()
  })

  it('renders nothing when the slot has no live remote location', () => {
    const { container } = render(<ExecutionLocationBadge location={undefined} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows a compact spinner while an existing environment reconnects', async () => {
    const user = userEvent.setup()
    render(
      <ExecutionLocationBadge
        location={{
          kind: 'test-sandbox',
          workspace: 'sandbox-opaque',
          remote_cwd: '/workspace',
          state: 'starting',
        }}
      />,
    )

    const control = screen.getByRole('button', { name: 'Session Environments' })
    expect(control).toHaveTextContent('test-sandbox')
    expect(control).not.toHaveTextContent('sandbox-opaque')
    expect(control.querySelector('.animate-spin')).toBeInTheDocument()
    await user.click(control)
    expect(screen.getByText('sandbox-opaque')).toBeInTheDocument()
  })

  it('falls back to the environment kind for non-Coder workspaces', async () => {
    const user = userEvent.setup()
    render(
      <ExecutionLocationBadge
        location={{
          kind: 'test-sandbox',
          workspace: 'sandbox-opaque',
          remote_cwd: '/workspace',
          state: 'running',
        }}
      />,
    )

    expect(screen.getByRole('button', { name: 'Session Environments' }))
      .toHaveTextContent('test-sandbox')
    await user.click(screen.getByRole('button', { name: 'Session Environments' }))
    expect(screen.getByText('sandbox-opaque')).toBeInTheDocument()
  })

  it('copies the generated workspace ID and confirms the completed action', async () => {
    const user = userEvent.setup()
    render(
      <ExecutionLocationBadge
        location={{
          kind: 'coder',
          workspace: 'crew-session-kyle-opaque',
          remote_cwd: '/workspace',
          state: 'running',
        }}
      />,
    )

    await user.click(screen.getByRole('button', { name: 'Session Environments' }))
    await user.click(screen.getByRole('button', { name: 'Copy workspace ID' }))

    expect(copyToClipboard).toHaveBeenCalledWith('crew-session-kyle-opaque')
    expect(screen.getByRole('button', { name: 'Workspace ID copied' })).toBeInTheDocument()
  })
})
