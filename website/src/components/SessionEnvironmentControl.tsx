import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Server } from 'lucide-react'

import { api } from '../api/client'
import { compareText } from '../i18n/format'
import { i18nT } from '../i18n/t'
import {
  environmentProvider,
  environmentProviderLabel,
  sessionEnvironment,
  sessionExecutionLocation,
} from '../sessionEnvironment'
import type { ChatSlot } from '../types'
import { ExecutionLocationBadge } from './ExecutionLocationBadge'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from './ui/select'

const SELECTION_SEPARATOR = ':'
const HEALTH_REFETCH_INTERVAL_MS = 30_000
const HEALTH_STALE_TIME_MS = 25_000

function selectionValue(provider: string, configuration: string): string {
  return `${provider}${SELECTION_SEPARATOR}${configuration}`
}

function parseSelection(value: string): { provider: string; configuration: string } {
  const separator = value.indexOf(SELECTION_SEPARATOR)
  return separator < 0
    ? { provider: value, configuration: '' }
    : { provider: value.slice(0, separator), configuration: value.slice(separator + 1) }
}

function ProviderIcon() {
  return <Server className="lucide-inline" />
}

export function SessionEnvironmentControl({
  hasCompletedTurn = false,
  slot,
  placement = 'header',
}: {
  hasCompletedTurn?: boolean
  slot?: ChatSlot
  placement?: 'header' | 'composer'
}) {
  const [detailsOpen, setDetailsOpen] = useState(false)
  const binding = sessionEnvironment(slot)
  const executionLocation = sessionExecutionLocation(slot)
  // Slot summaries arrive before full chat history on a reload. Two persisted
  // messages prove this is not an untouched first turn, so they keep a runtime
  // reconnect compact even while completed-turn history is still loading.
  const environmentWasReady = hasCompletedTurn || (slot?.messages ?? 0) > 1
  const catalog = useQuery({
    queryKey: ['session-environments'],
    queryFn: api.getSessionEnvironments,
    enabled: placement === 'composer' || Boolean(binding),
  })
  const selectedProvider = environmentProvider(catalog.data?.providers, binding?.provider || '')
  const providerLabel = binding
    ? environmentProviderLabel(binding.provider, selectedProvider)
    : ''
  const selectEnvironment = useMutation({
    mutationFn: (value: string) => {
      const selection = parseSelection(value)
      return api.chatSlotEnvironment(
        slot?.key ?? '', selection.provider, selection.configuration,
      )
    },
  })
  const health = useQuery({
    queryKey: ['session-environment-health', slot?.key],
    queryFn: () => api.getSessionEnvironmentHealth(slot?.key ?? ''),
    enabled: detailsOpen && Boolean(slot?.key && executionLocation?.workspace),
    refetchInterval: detailsOpen ? HEALTH_REFETCH_INTERVAL_MS : false,
    staleTime: HEALTH_STALE_TIME_MS,
    refetchIntervalInBackground: false,
    retry: false,
  })

  if (executionLocation) {
    if (placement === 'header') return null
    if (executionLocation.state === 'starting' && !environmentWasReady) return null
    const configuration = binding?.configuration || executionLocation.profile || ''
    return (
      <div className="pointer-events-auto min-w-0 shrink-0">
        <ExecutionLocationBadge
          location={executionLocation}
          providerLabel={providerLabel || undefined}
          configurationLabel={configuration || undefined}
          health={health.data}
          healthError={health.isError}
          healthLoading={health.isPending && health.fetchStatus === 'fetching'}
          onOpenChange={setDetailsOpen}
        />
      </div>
    )
  }
  if (placement === 'header') {
    return null
  }

  const providers = catalog.data?.providers ?? []
  if (!slot || providers.length === 0 || slot.messages > 0) return null
  const options = providers.flatMap(provider => provider.configurations.map(configuration => ({
    provider,
    configuration,
    value: selectionValue(provider.id, configuration.id),
  }))).sort((a, b) => compareText(
    `${a.provider.name} ${a.configuration.name}`,
    `${b.provider.name} ${b.configuration.name}`,
  ))
  if (options.length === 0) return null
  const selectedValue = binding
    ? selectionValue(binding.provider, binding.configuration)
    : options[0].value
  return (
    <div
      className="pointer-events-auto flex min-w-0 shrink items-center"
      data-testid="composer-execution-profile"
    >
      <div className="w-[180px] max-w-[36vw] min-w-0">
        <Select
          value={selectedValue}
          disabled={slot.running || selectEnvironment.isPending}
          onValueChange={value => selectEnvironment.mutate(value)}
        >
          <SelectTrigger
            className="h-7 gap-1.5 border-none bg-transparent px-2.5 py-0 text-[12px] text-muted shadow-none hover:border-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] hover:text-text"
            aria-label={i18nT('execution_environment.session_configuration_label')}
          >
            <ProviderIcon />
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {options.map(({ provider, configuration, value }) => (
              <SelectItem key={value} value={value}>
                {provider.name} · {configuration.id
                  ? configuration.name
                  : i18nT('execution_environment.default_configuration_short')}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {selectEnvironment.isError && (
          <span className="mt-1 block text-[12px] text-danger" role="status">
            {i18nT('execution_environment.configuration_change_failed')}
          </span>
        )}
      </div>
    </div>
  )
}
