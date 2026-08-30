import { useEffect, useRef, useState } from 'react'
import { Box, Check, ChevronDown, Copy, Layers3, Loader2, Server } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { ExecutionLocation } from '../types'
import { copyToClipboard } from '../utils/clipboard'
import { Btn, IconButton } from './ui'
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover'

export function ExecutionLocationBadge({
  configurationLabel,
  location,
  providerLabel,
}: {
  configurationLabel?: string
  location?: ExecutionLocation
  providerLabel?: string
}) {
  const { t } = useTranslation()
  const [copied, setCopied] = useState(false)
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => {
    if (resetTimer.current) clearTimeout(resetTimer.current)
  }, [])
  if (!location) return null

  const starting = location.state === 'starting'
  const ready = location.state === 'running'
  const kind = providerLabel || (location.kind === 'coder' ? t('coder.tab_label') : location.kind)
  const workspaceLabel = location.workspace || t('execution_environment.workspace_pending')
  const configurationTooltip = configurationLabel
    ? t('execution_environment.configuration_badge', { configuration: configurationLabel })
    : t('execution_environment.default_configuration')
  const tooltip = starting
    ? t('execution_environment.phase_connecting')
    : t('execution_environment.badge_running', { kind, workspace: workspaceLabel })

  const copyLabel = copied
    ? t('execution_environment.workspace_id_copied')
    : t('execution_environment.copy_workspace_id')
  const copyWorkspace = async () => {
    if (!location.workspace) return
    try {
      await copyToClipboard(location.workspace)
    } catch {
      return
    }
    setCopied(true)
    if (resetTimer.current) clearTimeout(resetTimer.current)
    resetTimer.current = setTimeout(() => setCopied(false), 1500)
  }

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Btn
          className="pointer-events-auto inline-flex h-7 min-w-0 max-w-[11rem] items-center gap-1.5 rounded-md border-none bg-transparent px-2.5 py-0 text-[12px] font-medium text-muted shadow-none transition-colors hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] hover:text-text"
          title={tooltip}
          aria-label={t('execution_environment.settings_tab_label')}
          aria-live={starting ? 'polite' : undefined}
          data-testid="execution-location-badge"
        >
          {starting ? (
            <Loader2 className="lucide-inline shrink-0 animate-spin text-accent motion-reduce:animate-none" />
          ) : (
            <Server className="lucide-inline shrink-0" />
          )}
          <span className="truncate text-text">{kind}</span>
          {ready ? (
            <span
              className="h-1.5 w-1.5 shrink-0 rounded-full bg-ok"
              data-testid="execution-location-ready"
              aria-hidden="true"
            />
          ) : null}
          <ChevronDown className="lucide-inline shrink-0" aria-hidden="true" />
        </Btn>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="end"
        className="w-[min(22rem,calc(100vw-2rem))] p-3"
      >
        <dl className="grid min-w-0 gap-2.5 text-[12px]">
          <div className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-x-2">
            <dt className="flex items-center gap-1.5 text-muted">
              <Server className="lucide-inline" />
              {t('execution_environment.provider_label')}
            </dt>
            <dd className="truncate text-right font-medium text-text" title={kind}>{kind}</dd>
          </div>
          <div className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] items-center gap-x-2">
            <dt className="flex items-center gap-1.5 text-muted">
              <Layers3 className="lucide-inline" />
              {t('execution_environment.configuration_label')}
            </dt>
            <dd className="truncate text-right font-medium text-text" title={configurationTooltip}>
              {configurationLabel || t('execution_environment.default_configuration')}
            </dd>
          </div>
          <div className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-2">
            <dt className="flex items-center gap-1.5 text-muted">
              <Box className="lucide-inline" />
              {t('execution_environment.workspace_label')}
            </dt>
            <dd className="truncate text-right font-medium text-text" title={workspaceLabel}>
              {workspaceLabel}
            </dd>
            {location.workspace ? (
              <IconButton
                aria-label={copyLabel}
                aria-live="polite"
                title={copyLabel}
                onClick={copyWorkspace}
              >
                {copied
                  ? <Check className="lucide-inline text-ok" />
                  : <Copy className="lucide-inline" />}
              </IconButton>
            ) : <span />}
          </div>
        </dl>
      </PopoverContent>
    </Popover>
  )
}
