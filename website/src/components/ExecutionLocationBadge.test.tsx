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

  it('identifies the live remote workspace without exposing its working directory', () => {
    render(
      <ExecutionLocationBadge
        location={{
          kind: 'coder',
          workspace: 'crew-dogfood',
          remote_cwd: '/home/coder/private-project',
          state: 'running',
        }}
      />,
    )

    expect(screen.getByText('Coder workspace · crew-dogfood')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('/home/coder/private-project')
  })

  it('renders nothing when the slot has no live remote location', () => {
    const { container } = render(<ExecutionLocationBadge location={undefined} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows a spinner and generated name while an environment starts', () => {
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

    const status = screen.getByRole('status')
    expect(status).toHaveTextContent('Starting test-sandbox workspace · sandbox-opaque')
    expect(status.querySelector('.animate-spin')).toBeInTheDocument()
  })

  it('falls back to the environment kind for non-Coder workspaces', () => {
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

    expect(screen.getByText('test-sandbox workspace · sandbox-opaque')).toBeInTheDocument()
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

    await user.click(screen.getByRole('button', { name: 'Copy workspace ID' }))

    expect(copyToClipboard).toHaveBeenCalledWith('crew-session-kyle-opaque')
    expect(screen.getByRole('button', { name: 'Workspace ID copied' })).toBeInTheDocument()
  })
})
