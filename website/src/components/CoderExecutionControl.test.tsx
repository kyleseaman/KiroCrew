import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from '../test/helpers'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: {
    getSessionEnvironments: vi.fn(),
    chatSlotEnvironment: vi.fn(),
  },
}))

import { api } from '../api/client'
import { SessionEnvironmentControl } from './SessionEnvironmentControl'

const slot: ChatSlot = {
  key: 'chat-1',
  title: 'Fresh session',
  messages: 0,
  running: false,
  environment: { provider: 'coder', configuration: '', resource_name: '' },
}

describe('SessionEnvironmentControl', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.getSessionEnvironments).mockResolvedValue({
      providers: [{
        id: 'coder',
        name: 'Coder',
        icon: 'coder',
        configurations: [
          { id: '', name: 'default' },
          { id: 'gpu', name: 'gpu' },
        ],
      }],
    })
    vi.mocked(api.chatSlotEnvironment).mockResolvedValue({
      ok: true,
      environment: { provider: 'coder', configuration: 'gpu', resource_name: '' },
    })
  })

  it('lets a fresh session select a provider configuration', async () => {
    const user = userEvent.setup()
    const { container } = renderWithProviders(
      <SessionEnvironmentControl slot={slot} placement="composer" />,
    )

    const trigger = await screen.findByRole('combobox', {
      name: 'Environment configuration for this session',
    })
    expect(container.querySelector('.lucide-server')).toBeInTheDocument()
    expect(screen.queryByTestId('coder-logo')).not.toBeInTheDocument()
    await user.click(trigger)
    await user.click(await screen.findByRole('option', { name: 'Coder · gpu' }))

    expect(api.chatSlotEnvironment).toHaveBeenCalledWith('chat-1', 'coder', 'gpu')
  })

  it('keeps a fresh-session selector out of the session header', async () => {
    renderWithProviders(<SessionEnvironmentControl slot={slot} placement="header" />)

    await new Promise(resolve => setTimeout(resolve, 20))
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('shows one compact environment control beside the composer after allocation', async () => {
    const user = userEvent.setup()
    renderWithProviders(
      <SessionEnvironmentControl
        placement="composer"
        slot={{
          ...slot,
          messages: 1,
          environment: { provider: 'coder', configuration: 'gpu', resource_name: 'crew-opaque' },
          execution_location: {
            kind: 'coder',
            workspace: 'crew-opaque',
            remote_cwd: '/home/coder/workspace',
            state: 'running',
          },
        }}
      />,
    )

    const control = screen.getByRole('button', { name: 'Session Environments' })
    expect(control).toHaveTextContent('Coder')
    expect(control).not.toHaveTextContent('crew-opaque')
    expect(screen.queryByText('Environment configuration · gpu')).not.toBeInTheDocument()

    await user.click(control)
    expect(screen.getByText('crew-opaque')).toBeInTheDocument()
    expect(screen.getByText('gpu')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy workspace ID' })).toBeInTheDocument()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('keeps the durable workspace ID visible after the live runtime idles', async () => {
    renderWithProviders(
      <SessionEnvironmentControl
        placement="composer"
        slot={{
          ...slot,
          messages: 1,
          environment: {
            provider: 'coder',
            configuration: 'gpu',
            resource_name: 'crew-session-kyle-retained',
          },
        }}
      />,
    )

    expect(await screen.findByRole('button', { name: 'Session Environments' })).toHaveTextContent(
      'Coder',
    )
    expect(screen.queryByText('crew-session-kyle-retained')).not.toBeInTheDocument()
  })

  it('uses a compact reconnecting control after a prior turn', async () => {
    const { container } = renderWithProviders(
      <SessionEnvironmentControl
        placement="composer"
        slot={{
          ...slot,
          messages: 2,
          environment: { provider: 'coder', configuration: 'gpu', resource_name: '' },
          execution_location: {
            kind: 'coder',
            workspace: 'crew-opaque',
            remote_cwd: '/home/coder/workspace',
            state: 'starting',
          },
        }}
      />,
    )

    const control = screen.getByRole('button', { name: 'Session Environments' })
    expect(control).toHaveTextContent('Coder')
    expect(container.querySelector('.lucide-loader-circle')).toBeInTheDocument()
  })

  it('uses provider catalog metadata for an already hosted session', async () => {
    renderWithProviders(
      <SessionEnvironmentControl
        placement="composer"
        slot={{
          ...slot,
          messages: 1,
          execution_location: {
            kind: 'test-sandbox',
            workspace: 'sandbox-opaque',
            remote_cwd: '/workspace',
            state: 'running',
          },
        }}
      />,
    )

    await new Promise(resolve => setTimeout(resolve, 20))
    expect(api.getSessionEnvironments).toHaveBeenCalledTimes(1)
  })

  it('renders another provider from the same catalog contract', async () => {
    vi.mocked(api.getSessionEnvironments).mockResolvedValue({
      providers: [{
        id: 'kubernetes',
        name: 'Kubernetes',
        icon: 'server',
        configurations: [{ id: 'standard', name: 'Standard pod' }],
      }],
    })

    renderWithProviders(
      <SessionEnvironmentControl
        placement="composer"
        slot={{
          ...slot,
          messages: 1,
          environment: {
            provider: 'kubernetes',
            configuration: 'standard',
            resource_name: 'crew-pod-opaque',
          },
        }}
      />,
    )

    expect(await screen.findByText('Kubernetes')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Session Environments' })).toHaveTextContent(
      'Kubernetes',
    )
  })
})
