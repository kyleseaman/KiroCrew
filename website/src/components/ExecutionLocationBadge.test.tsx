import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ExecutionLocationBadge } from './ExecutionLocationBadge'

describe('ExecutionLocationBadge', () => {
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
})
