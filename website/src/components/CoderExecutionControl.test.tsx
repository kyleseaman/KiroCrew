import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from '../test/helpers'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: {
    getSessionEnvironments: vi.fn(),
    getSessionEnvironmentHealth: vi.fn(),
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
  afterEach(() => {
    vi.useRealTimers()
  })

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
    vi.mocked(api.getSessionEnvironmentHealth).mockResolvedValue({
      provider: 'coder',
      resource_name: 'crew-opaque',
      state: 'running',
      memory: {
        available_gb: 0.4,
        total_gb: 4,
        used_percent: 90,
        pressure: 'critical',
      },
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
    expect(api.getSessionEnvironmentHealth).not.toHaveBeenCalled()
    expect(screen.queryByText('Critical pressure')).not.toBeInTheDocument()

    await user.click(control)
    expect(screen.getByText('crew-opaque')).toBeInTheDocument()
    expect(screen.getByText('gpu')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy workspace ID' })).toBeInTheDocument()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    await waitFor(() => expect(api.getSessionEnvironmentHealth).toHaveBeenCalledWith('chat-1'))
    expect(screen.getByText('Critical pressure')).toBeInTheDocument()
    expect(screen.getByText('90% used · 0.4GB available of 4GB')).toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: 'Memory' })).toHaveAttribute(
      'aria-valuenow',
      '90',
    )
  })

  it('omits memory when the provider has no live sample', async () => {
    vi.mocked(api.getSessionEnvironmentHealth).mockResolvedValue({
      provider: 'coder',
      resource_name: 'crew-opaque',
      state: 'stopped',
    })
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

    await user.click(screen.getByRole('button', { name: 'Session Environments' }))
    expect(await screen.findByText('Stopped')).toBeInTheDocument()
    expect(screen.queryByText('Memory')).not.toBeInTheDocument()
  })

  it('stops health refreshes when the environment details close', async () => {
    vi.useFakeTimers()
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
    fireEvent.click(control)
    await act(async () => Promise.resolve())
    expect(api.getSessionEnvironmentHealth).toHaveBeenCalledTimes(1)

    fireEvent.click(control)
    act(() => vi.advanceTimersByTime(20_000))
    await act(async () => Promise.resolve())
    expect(api.getSessionEnvironmentHealth).toHaveBeenCalledTimes(1)
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
