import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { CoderExecutionBadge } from './CoderExecutionBadge'

describe('CoderExecutionBadge', () => {
  it('identifies the live remote workspace without exposing its working directory', () => {
    render(
      <CoderExecutionBadge
        location={{
          kind: 'coder',
          workspace: 'crew-dogfood',
          remote_cwd: '/home/coder/private-project',
        }}
      />,
    )

    expect(screen.getByText('Coder workspace · crew-dogfood')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('/home/coder/private-project')
  })

  it('renders nothing when the slot has no live remote location', () => {
    const { container } = render(<CoderExecutionBadge location={undefined} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('labels a managed workspace while its binding is still being allocated', () => {
    render(
      <CoderExecutionBadge
        location={{
          kind: 'coder',
          workspace: '',
          remote_cwd: '/home/coder/workspace',
          state: 'allocating',
        }}
      />,
    )

    expect(screen.getByText('Allocating Coder workspace')).toBeInTheDocument()
  })
})
