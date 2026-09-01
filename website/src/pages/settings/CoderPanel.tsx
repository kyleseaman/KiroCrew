import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Copy, ExternalLink, Loader, Plus, TestTube2, Trash2 } from 'lucide-react'

import { api, type CoderConfigData, type CoderConfigSave } from '../../api/client'
import CoderLogo from '../../components/icons/CoderLogo'
import { SettingsCard, SettingsInput, SettingsSection, SettingsToggle } from '../../components/settings'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import { copyToClipboard } from '../../utils/clipboard'
import { CONTROL_PLANE_COMMAND, WORKSPACE_COMMAND } from './coderDeployExamples'

const CODER_CONFIG_QUERY = ['coder-config'] as const
const WORKSPACE_TEMPLATE_URL = 'https://github.com/kirodotdev/KiroCrew/tree/main/deploy/coder-aws/workspace'
const CONTROL_PLANE_TEMPLATE_URL = 'https://github.com/kirodotdev/KiroCrew/tree/main/deploy/coder-aws/control-plane'
const REMOTE_GUIDE_URL = 'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/remote-and-mobile.md'

type FormState = Omit<CoderConfigSave, 'token'>

function numericValue(value: string, fallback: number): number {
  const parsed = Number.parseInt(value, 10)
  return Number.isFinite(parsed) ? parsed : fallback
}

function CommandBlock({ command }: { command: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="mt-2 flex min-w-0 items-center gap-2 rounded-md border border-border bg-bg px-2.5 py-2">
      <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap text-[11px] text-text">{command}</code>
      <Btn
        type="button"
        className="shrink-0"
        aria-label={i18nT('coder.copy_command')}
        onClick={async () => {
          await copyToClipboard(command)
          setCopied(true)
        }}
      >
        {copied ? <Check className="lucide-inline" /> : <Copy className="lucide-inline" />}
        {copied
          ? i18nT('coder.copied')
          : i18nT('coder.copy')}
      </Btn>
    </div>
  )
}

function DeployDoc({
  title,
  description,
  command,
  href,
  linkLabel,
}: {
  title: string
  description: string
  command: string
  href: string
  linkLabel: string
}) {
  return (
    <details className="border-b border-border py-3 last:border-b-0">
      <summary className="cursor-pointer text-[13px] font-semibold text-text">{title}</summary>
      <div className="pt-2 text-[12px] leading-relaxed text-muted">
        <p>{description}</p>
        <CommandBlock command={command} />
        <a
          className="mt-2 inline-flex items-center gap-1 font-medium text-accent hover:underline"
          href={href}
          target="_blank"
          rel="noopener noreferrer"
        >
          {linkLabel} <ExternalLink className="lucide-inline" />
        </a>
      </div>
    </details>
  )
}

export function CoderPanel() {
  const queryClient = useQueryClient()
  const configQuery = useQuery({ queryKey: CODER_CONFIG_QUERY, queryFn: api.getCoderConfig })
  const [form, setForm] = useState<FormState>({
    enabled: false,
    url: '',
    template: '',
    preset: '',
    profiles: {},
    remote_cwd: '/home/coder/workspace',
    runtime_warm_minutes: 15,
    stop_after_minutes: 30,
    delete_after_days: 30,
    max_running: 3,
    workspace_prefix: 'crew',
  })
  const [token, setToken] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (!configQuery.data) return
    const {
      enabled,
      url,
      template,
      preset,
      profiles,
      remote_cwd,
      runtime_warm_minutes,
      stop_after_minutes,
      delete_after_days,
      max_running,
      workspace_prefix,
    } = configQuery.data
    setForm({
      enabled,
      url,
      template,
      preset,
      profiles,
      remote_cwd,
      runtime_warm_minutes,
      stop_after_minutes,
      delete_after_days,
      max_running,
      workspace_prefix,
    })
  }, [configQuery.data])

  const save = useMutation({
    mutationFn: (body: CoderConfigSave) => api.saveCoderConfig(body),
    onSuccess: result => {
      setToken('')
      setError('')
      setNotice(
        result.active_sessions_unchanged
          ? i18nT('coder.saved_active_sessions_unchanged')
          : i18nT('coder.saved'),
      )
      queryClient.setQueryData<CoderConfigData>(CODER_CONFIG_QUERY, old =>
        old
          ? {
              ...old,
              ...form,
              token_configured: result.token_configured,
              legacy_environment: false,
            }
          : old,
      )
    },
    onError: cause => {
      setNotice('')
      setError(cause instanceof Error ? cause.message : i18nT('coder.save_failed'))
    },
  })

  const testConnection = useMutation({
    mutationFn: () => api.testCoderConnection({
      url: form.url,
      template: form.template,
      preset: form.preset,
      profiles: form.profiles,
      remote_cwd: form.remote_cwd,
      runtime_warm_minutes: form.runtime_warm_minutes,
      stop_after_minutes: form.stop_after_minutes,
      delete_after_days: form.delete_after_days,
      max_running: form.max_running,
      workspace_prefix: form.workspace_prefix,
      token,
    }),
    onSuccess: result => {
      setError('')
      setNotice(i18nT('coder.connected_to', result))
    },
    onError: cause => {
      setNotice('')
      setError(cause instanceof Error ? cause.message : i18nT('coder.connection_failed'))
    },
  })

  if (configQuery.isLoading) {
    return (
      <SettingsSection title={i18nT('coder.title')} badge={<CoderLogo />}>
        <div className="text-[12px] text-muted">{i18nT('coder.loading')}</div>
      </SettingsSection>
    )
  }
  if (configQuery.isError) {
    return (
      <SettingsSection title={i18nT('coder.title')} badge={<CoderLogo />}>
        <div className="text-[12px] text-danger">{i18nT('coder.load_failed')}</div>
      </SettingsSection>
    )
  }

  const busy = save.isPending || testConnection.isPending
  const tokenConfigured = configQuery.data?.token_configured ?? false
  const limits = configQuery.data!.limits

  return (
    <>
      <SettingsSection title={i18nT('coder.title')} badge={<CoderLogo />}>
        <SettingsCard>
          <SettingsToggle
            label={i18nT('coder.enable_label')}
            description={i18nT('coder.enable_description')}
            checked={form.enabled}
            onChange={enabled => setForm(current => ({ ...current, enabled }))}
            disabled={busy}
            configKey="session.coder.enabled"
          />
          <SettingsInput
            label={i18nT('coder.url_label')}
            description={i18nT('coder.url_description')}
            value={form.url}
            onChange={url => setForm(current => ({ ...current, url }))}
            placeholder="https://coder.example.ts.net"
            disabled={busy}
            configKey="session.coder.url"
          />
          <SettingsInput
            label={i18nT('coder.workspace_template')}
            value={form.template}
            onChange={template => setForm(current => ({ ...current, template }))}
            placeholder="kirocrew-arm"
            disabled={busy}
            configKey="session.coder.template"
          />
          <SettingsInput
            label={i18nT('coder.preset_label')}
            value={form.preset}
            onChange={preset => setForm(current => ({ ...current, preset }))}
            placeholder={i18nT('coder.preset_placeholder')}
            disabled={busy}
            configKey="session.coder.preset"
          />
          <div className="border-t border-border pt-3">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-[13px] font-medium text-text">
                  {i18nT('coder.profiles_title')}
                </div>
                <div className="mt-0.5 text-[11px] leading-relaxed text-muted">
                  {i18nT('coder.profiles_description')}
                </div>
              </div>
              <Btn
                type="button"
                disabled={busy || Object.keys(form.profiles).length >= limits.max_profiles}
                onClick={() => setForm(current => {
                  let suffix = Object.keys(current.profiles).length + 1
                  while (current.profiles[`profile-${suffix}`]) suffix += 1
                  return {
                    ...current,
                    profiles: {
                      ...current.profiles,
                      [`profile-${suffix}`]: { template: current.template, preset: '' },
                    },
                  }
                })}
              >
                <Plus className="lucide-inline" /> {i18nT('coder.add_profile')}
              </Btn>
            </div>
            <div className="mt-3 space-y-3">
              {Object.entries(form.profiles).map(([name, profile], index) => (
                <div key={index} className="rounded-md border border-border bg-bg px-3 py-3">
                  <div className="grid gap-3 md:grid-cols-3">
                    <SettingsInput
                      label={i18nT('coder.profile_name_label')}
                      value={name}
                      onChange={nextName => setForm(current => {
                        const entries = Object.entries(current.profiles)
                        entries[index] = [nextName, entries[index][1]]
                        return { ...current, profiles: Object.fromEntries(entries) }
                      })}
                      disabled={busy}
                    />
                    <SettingsInput
                      label={i18nT('coder.profile_template_label', { name })}
                      value={profile.template}
                      onChange={template => setForm(current => ({
                        ...current,
                        profiles: { ...current.profiles, [name]: { ...profile, template } },
                      }))}
                      disabled={busy}
                    />
                    <SettingsInput
                      label={i18nT('coder.profile_preset_label', { name })}
                      value={profile.preset}
                      onChange={preset => setForm(current => ({
                        ...current,
                        profiles: { ...current.profiles, [name]: { ...profile, preset } },
                      }))}
                      disabled={busy}
                    />
                  </div>
                  <div className="mt-2 flex justify-end">
                    <Btn
                      type="button"
                      aria-label={i18nT('coder.remove_profile_named', { name })}
                      disabled={busy}
                      onClick={() => setForm(current => {
                        const profiles = { ...current.profiles }
                        delete profiles[name]
                        return { ...current, profiles }
                      })}
                    >
                      <Trash2 className="lucide-inline" /> {i18nT('coder.remove_profile')}
                    </Btn>
                  </div>
                </div>
              ))}
            </div>
          </div>
          <SettingsInput
            label={i18nT('coder.cwd_label')}
            description={i18nT('coder.cwd_description')}
            value={form.remote_cwd}
            onChange={remote_cwd => setForm(current => ({ ...current, remote_cwd }))}
            placeholder="/home/coder/workspace"
            disabled={busy}
            configKey="session.coder.remote_cwd"
          />
          <SettingsInput
            label={i18nT('coder.runtime_warm_label')}
            value={String(form.runtime_warm_minutes)}
            onChange={value => setForm(current => ({
              ...current,
              runtime_warm_minutes: numericValue(value, current.runtime_warm_minutes),
            }))}
            type="number"
            disabled={busy}
            configKey="session.coder.runtime_warm_minutes"
          />
          <SettingsInput
            label={i18nT('coder.stop_after_label')}
            value={String(form.stop_after_minutes)}
            onChange={value => setForm(current => ({
              ...current,
              stop_after_minutes: numericValue(value, current.stop_after_minutes),
            }))}
            type="number"
            disabled={busy}
            configKey="session.coder.stop_after_minutes"
          />
          <SettingsInput
            label={i18nT('coder.delete_after_label')}
            value={String(form.delete_after_days)}
            onChange={value => setForm(current => ({
              ...current,
              delete_after_days: numericValue(value, current.delete_after_days),
            }))}
            type="number"
            disabled={busy}
            configKey="session.coder.delete_after_days"
          />
          <SettingsInput
            label={i18nT('coder.max_running_label')}
            value={String(form.max_running)}
            onChange={value => setForm(current => ({
              ...current,
              max_running: numericValue(value, current.max_running),
            }))}
            type="number"
            disabled={busy}
            configKey="session.coder.max_running"
          />
          <SettingsInput
            label={i18nT('coder.workspace_label')}
            description={i18nT('coder.workspace_description')}
            value={form.workspace_prefix}
            onChange={workspace_prefix => setForm(current => ({ ...current, workspace_prefix }))}
            placeholder="crew"
            maxLength={limits.workspace_prefix_max_chars}
            disabled={busy}
            configKey="session.coder.workspace_prefix"
          />
          <SettingsInput
            label={i18nT('coder.token_label')}
            description={
              tokenConfigured
                ? i18nT('coder.token_stored')
                : i18nT('coder.token_missing')
            }
            value={token}
            onChange={setToken}
            type="password"
            placeholder={i18nT('coder.token_placeholder')}
            disabled={busy}
            autoComplete="new-password"
          />
          {configQuery.data?.legacy_environment && (
            <div className="mt-2 rounded-md border border-warn/30 bg-warn-subtle px-3 py-2 text-[12px] text-warn">
              {i18nT('coder.legacy_environment', {
                workspace: configQuery.data.static_workspace ?? '',
              })}
            </div>
          )}
          {(notice || error) && (
            <div className={`mt-2 text-[12px] ${error ? 'text-danger' : 'text-success'}`} role="status">
              {error || notice}
            </div>
          )}
          <div className="mt-3 flex flex-wrap justify-end gap-2">
            <Btn
              type="button"
              onClick={() => testConnection.mutate()}
              disabled={busy}
            >
              {testConnection.isPending
                ? <Loader className="lucide-inline animate-spin" />
                : <TestTube2 className="lucide-inline" />}
              {i18nT('coder.test_connection')}
            </Btn>
            <Btn
              primary
              type="button"
              onClick={() => save.mutate({ ...form, token })}
              disabled={busy}
            >
              {save.isPending && <Loader className="lucide-inline animate-spin" />}
              {i18nT('coder.save_configuration')}
            </Btn>
          </div>
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('coder.how_it_works')}>
        <SettingsCard index={1}>
          <div className="text-[12px] leading-relaxed text-muted">
            {i18nT('coder.architecture')}
          </div>
          <div className="mt-2 text-[12px] leading-relaxed text-muted">
            {i18nT('coder.lifecycle', {
              stopMinutes: form.stop_after_minutes,
              deleteDays: form.delete_after_days,
            })}
          </div>
          <a
            className="mt-2 inline-flex items-center gap-1 text-[12px] font-medium text-accent hover:underline"
            href={REMOTE_GUIDE_URL}
            target="_blank"
            rel="noopener noreferrer"
          >
            {i18nT('coder.open_full_guide')} <ExternalLink className="lucide-inline" />
          </a>
        </SettingsCard>
      </SettingsSection>

      <SettingsSection title={i18nT('coder.deploy_templates')}>
        <SettingsCard index={2}>
          <DeployDoc
            title={i18nT('coder.workspace_template')}
            description={i18nT('coder.workspace_template_description')}
            command={WORKSPACE_COMMAND}
            href={WORKSPACE_TEMPLATE_URL}
            linkLabel={i18nT('coder.open_workspace_template')}
          />
          <DeployDoc
            title={i18nT('coder.control_plane_template')}
            description={i18nT('coder.control_plane_template_description')}
            command={CONTROL_PLANE_COMMAND}
            href={CONTROL_PLANE_TEMPLATE_URL}
            linkLabel={i18nT('coder.open_control_plane_template')}
          />
        </SettingsCard>
      </SettingsSection>
    </>
  )
}
