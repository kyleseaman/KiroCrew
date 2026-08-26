import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from '../test/helpers'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: {
    getCoderConfig: vi.fn(),
    chatSlotCoderProfile: vi.fn(),
  },
}))

import { api } from '../api/client'
import { CoderExecutionControl } from './CoderExecutionControl'

const slot: ChatSlot = {
  key: 'chat-1',
  title: 'Fresh session',
  messages: 0,
  running: false,
  coder_profile: '',
}

describe('CoderExecutionControl', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.getCoderConfig).mockResolvedValue({
      enabled: true,
      url: 'https://coder.example',
      template: 'kirocrew-arm',
      preset: '',
      profiles: {
        gpu: { template: 'kirocrew-gpu', preset: 'gpu-medium' },
      },
      remote_cwd: '/home/coder/workspace',
      runtime_warm_minutes: 5,
      stop_after_minutes: 30,
      delete_after_days: 30,
      max_running: 3,
      workspace_prefix: 'crew',
      token_configured: true,
      legacy_environment: false,
    })
    vi.mocked(api.chatSlotCoderProfile).mockResolvedValue({ ok: true, profile: 'gpu' })
  })

  it('lets a fresh session select a named profile', async () => {
    const user = userEvent.setup()
    renderWithProviders(<CoderExecutionControl slot={slot} />)

    const trigger = await screen.findByRole('combobox', { name: 'Coder profile for this session' })
    await user.click(trigger)
    await user.click(await screen.findByRole('option', { name: 'gpu' }))

    expect(api.chatSlotCoderProfile).toHaveBeenCalledWith('chat-1', 'gpu')
  })

  it('shows the live workspace instead of a mutable selector after allocation', async () => {
    renderWithProviders(
      <CoderExecutionControl
        slot={{
          ...slot,
          messages: 1,
          coder_profile: 'gpu',
          execution_location: {
            kind: 'coder',
            workspace: 'crew-opaque',
            remote_cwd: '/home/coder/workspace',
            state: 'running',
          },
        }}
      />,
    )

    expect(screen.getByText('Coder workspace · crew-opaque')).toBeInTheDocument()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('shows a starting workspace and the selected Coder profile', async () => {
    renderWithProviders(
      <CoderExecutionControl
        slot={{
          ...slot,
          messages: 1,
          coder_profile: 'gpu',
          execution_location: {
            kind: 'coder',
            workspace: '',
            remote_cwd: '/home/coder/workspace',
            state: 'starting',
          },
        }}
      />,
    )

    expect(screen.getByRole('status')).toHaveTextContent('Starting Coder workspace')
    expect(screen.getByText('Coder profile · gpu')).toBeInTheDocument()
  })

  it('does not fetch Coder settings for an already hosted session', async () => {
    renderWithProviders(
      <CoderExecutionControl
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
    expect(api.getCoderConfig).not.toHaveBeenCalled()
  })
})
