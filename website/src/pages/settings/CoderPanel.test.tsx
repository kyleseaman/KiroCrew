import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from '../../test/helpers'
import type { CoderConfigData } from '../../api/client'

vi.mock('../../api/client', () => ({
  api: {
    getCoderConfig: vi.fn(),
    saveCoderConfig: vi.fn(),
    testCoderConnection: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { CoderPanel } from './CoderPanel'

function snapshot(overrides: Partial<CoderConfigData> = {}): CoderConfigData {
  return {
    enabled: true,
    url: 'https://coder.example',
    template: 'kirocrew-arm',
    preset: 'arm-small',
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
    limits: {
      max_profiles: 16,
      workspace_prefix_max_chars: 21,
    },
    ...overrides,
  }
}

async function renderPanel(data: CoderConfigData = snapshot()) {
  vi.mocked(api.getCoderConfig).mockResolvedValue(data)
  vi.mocked(api.saveCoderConfig).mockResolvedValue({
    ok: true,
    token_configured: true,
    active_sessions_unchanged: true,
  })
  vi.mocked(api.testCoderConnection).mockResolvedValue({
    ok: true,
    owner: 'kyleseaman',
    template: data.template,
  })
  const utils = renderWithProviders(<CoderPanel />)
  await screen.findByDisplayValue(data.template)
  return utils
}

describe('CoderPanel', () => {
  beforeEach(() => vi.clearAllMocks())

  it('keeps the Coder brand mark visible while configuration loads', () => {
    vi.mocked(api.getCoderConfig).mockReturnValue(new Promise(() => {}))

    renderWithProviders(<CoderPanel />)

    expect(screen.getByTestId('coder-logo')).toHaveAttribute('aria-hidden', 'true')
  })

  it('shows the Coder brand mark with the loaded configuration', async () => {
    await renderPanel()

    expect(screen.getByTestId('coder-logo')).toHaveAttribute('aria-hidden', 'true')
  })

  it('keeps the Coder brand mark visible when configuration fails', async () => {
    vi.mocked(api.getCoderConfig).mockRejectedValue(new Error('unavailable'))

    renderWithProviders(<CoderPanel />)

    await screen.findByText('Could not load Coder settings')
    expect(screen.getByTestId('coder-logo')).toHaveAttribute('aria-hidden', 'true')
  })

  it('shows token presence without receiving or rendering the bearer', async () => {
    await renderPanel()

    expect(screen.getByText('Token stored securely')).toBeInTheDocument()
    expect(screen.getByLabelText('Coder session token')).toHaveValue('')
    expect(document.body.textContent).not.toContain('coder-secret')
  })

  it('saves the split configuration, clears the candidate token, and explains scope', async () => {
    const user = userEvent.setup()
    await renderPanel()

    const token = screen.getByLabelText('Coder session token')
    await user.type(token, 'candidate-secret')
    await user.click(screen.getByRole('button', { name: 'Save configuration' }))

    await waitFor(() => expect(api.saveCoderConfig).toHaveBeenCalledWith({
      enabled: true,
      url: 'https://coder.example',
      template: 'kirocrew-arm',
      preset: 'arm-small',
      profiles: {
        gpu: { template: 'kirocrew-gpu', preset: 'gpu-medium' },
      },
      remote_cwd: '/home/coder/workspace',
      runtime_warm_minutes: 5,
      stop_after_minutes: 30,
      delete_after_days: 30,
      max_running: 3,
      workspace_prefix: 'crew',
      token: 'candidate-secret',
    }))
    expect(token).toHaveValue('')
    expect(screen.getByText(/Active sessions keep their current location/)).toBeInTheDocument()
  })

  it('tests unsaved coordinates and token without first persisting them', async () => {
    await renderPanel()
    fireEvent.change(screen.getByLabelText('Coder URL'), {
      target: { value: 'https://candidate.example' },
    })
    fireEvent.change(screen.getByLabelText('Coder session token'), {
      target: { value: 'candidate-secret' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Test connection' }))

    await waitFor(() => expect(api.testCoderConnection).toHaveBeenCalledWith({
      url: 'https://candidate.example',
      template: 'kirocrew-arm',
      preset: 'arm-small',
      profiles: {
        gpu: { template: 'kirocrew-gpu', preset: 'gpu-medium' },
      },
      remote_cwd: '/home/coder/workspace',
      runtime_warm_minutes: 5,
      stop_after_minutes: 30,
      delete_after_days: 30,
      max_running: 3,
      workspace_prefix: 'crew',
      token: 'candidate-secret',
    }))
    expect(await screen.findByText('Connected as kyleseaman; template kirocrew-arm is available')).toBeInTheDocument()
  })

  it('explains that each session gets an isolated retained workspace', async () => {
    await renderPanel()

    expect(screen.getByText(/Each new parent session gets its own workspace/)).toBeInTheDocument()
    expect(screen.getByText(/stopped workspaces are deleted after 30 inactive days/)).toBeInTheDocument()
    expect(screen.queryByDisplayValue('crew-dogfood')).not.toBeInTheDocument()
  })

  it('edits named template profiles for per-session selection', async () => {
    const user = userEvent.setup()
    await renderPanel()

    expect(screen.getByLabelText('Template for gpu')).toHaveValue('kirocrew-gpu')
    await user.click(screen.getByRole('button', { name: 'Add profile' }))
    expect(screen.getAllByLabelText('Profile name')).toHaveLength(2)
    await user.click(screen.getByRole('button', { name: 'Save configuration' }))

    await waitFor(() => expect(api.saveCoderConfig).toHaveBeenCalledWith(
      expect.objectContaining({
        profiles: {
          gpu: { template: 'kirocrew-gpu', preset: 'gpu-medium' },
          'profile-2': { template: 'kirocrew-arm', preset: '' },
        },
      }),
    ))
  })

  it('includes expandable setup docs and links for both deploy templates', async () => {
    await renderPanel()

    expect(screen.getAllByText('Workspace template')).toHaveLength(2)
    expect(screen.getByText('Full AWS control plane')).toBeInTheDocument()
    expect(
      screen.getByText(
        'coder templates push kirocrew-arm --yes --directory deploy/coder-aws/workspace',
      ),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open workspace template' })).toHaveAttribute(
      'href',
      expect.stringContaining('/deploy/coder-aws/workspace'),
    )
    expect(screen.getByRole('link', { name: 'Open control-plane template' })).toHaveAttribute(
      'href',
      expect.stringContaining('/deploy/coder-aws/control-plane'),
    )
  })
})
