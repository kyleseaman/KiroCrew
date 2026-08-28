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
import type { ChatSlot, SessionEnvironmentProviderSummary } from '../types'
import { ExecutionLocationBadge } from './ExecutionLocationBadge'
import CoderLogo from './icons/CoderLogo'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from './ui/select'

const SELECTION_SEPARATOR = ':'

function selectionValue(provider: string, configuration: string): string {
  return `${provider}${SELECTION_SEPARATOR}${configuration}`
}

function parseSelection(value: string): { provider: string; configuration: string } {
  const separator = value.indexOf(SELECTION_SEPARATOR)
  return separator < 0
    ? { provider: value, configuration: '' }
    : { provider: value.slice(0, separator), configuration: value.slice(separator + 1) }
}

function ProviderIcon({ provider }: { provider?: SessionEnvironmentProviderSummary }) {
  return provider?.icon === 'coder'
    ? <CoderLogo height={11} />
    : <Server className="lucide-inline" />
}

export function SessionEnvironmentControl({
  slot,
  placement = 'header',
}: {
  slot?: ChatSlot
  placement?: 'header' | 'composer'
}) {
  const binding = sessionEnvironment(slot)
  const executionLocation = sessionExecutionLocation(slot)
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

  if (executionLocation) {
    if (placement === 'composer') return null
    const configuration = binding?.configuration || executionLocation.profile || ''
    const detailLabel = executionLocation.state !== 'starting'
      ? configuration
        ? i18nT('execution_environment.configuration_badge', { configuration })
        : i18nT('execution_environment.default_configuration')
      : undefined
    return (
      <ExecutionLocationBadge
        location={executionLocation}
        providerLabel={providerLabel || undefined}
        detailLabel={detailLabel}
      />
    )
  }
  if (placement === 'header') {
    if (!slot || slot.messages === 0 || !binding) return null
    const configuration = binding.configuration
      ? i18nT('execution_environment.configuration_badge', {
          configuration: binding.configuration,
        })
      : i18nT('execution_environment.default_configuration')
    return (
      <span className="pointer-events-auto inline-flex shrink-0 items-center gap-1 rounded-full border border-accent/30 bg-accent-subtle px-2 py-0.5 text-[11px] font-medium text-accent">
        <ProviderIcon provider={selectedProvider} />
        {providerLabel} · {configuration}
      </span>
    )
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
  const selectedOption = options.find(option => option.value === selectedValue)

  return (
    <div className="pointer-events-auto flex items-center justify-end gap-2 pt-1.5" data-testid="composer-execution-profile">
      <ProviderIcon provider={selectedOption?.provider} />
      <div className="w-[260px] max-w-[75vw] shrink-0">
        <Select
          value={selectedValue}
          disabled={slot.running || selectEnvironment.isPending}
          onValueChange={value => selectEnvironment.mutate(value)}
        >
          <SelectTrigger
            className="h-7 text-[11px]"
            aria-label={i18nT('execution_environment.session_configuration_label')}
          >
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
          <span className="sr-only" role="status">
            {i18nT('execution_environment.configuration_change_failed')}
          </span>
        )}
      </div>
    </div>
  )
}
