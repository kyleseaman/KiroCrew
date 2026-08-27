import { useMutation, useQuery } from '@tanstack/react-query'
import { Server } from 'lucide-react'

import { api } from '../api/client'
import { compareText } from '../i18n/format'
import { i18nT } from '../i18n/t'
import type { ChatSlot, ExecutionLocation } from '../types'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from './ui/select'
import { ExecutionLocationBadge } from './ExecutionLocationBadge'
import CoderLogo from './icons/CoderLogo'

const DEFAULT_PROFILE_VALUE = '__coder_default__'

export function CoderExecutionControl({
  slot,
  placement = 'header',
}: {
  slot?: ChatSlot
  placement?: 'header' | 'composer'
}) {
  const executionLocation: ExecutionLocation | undefined = slot?.execution_location
    ?? (slot?.coder_workspace
      ? {
          kind: 'coder',
          workspace: slot.coder_workspace,
          remote_cwd: '',
          state: 'retained',
        }
      : undefined)
  const config = useQuery({
    queryKey: ['coder-config'],
    queryFn: api.getCoderConfig,
    enabled: placement === 'composer' && !executionLocation,
  })
  const selectProfile = useMutation({
    mutationFn: (profile: string) => api.chatSlotCoderProfile(slot?.key ?? '', profile),
  })

  if (executionLocation) {
    if (placement === 'composer') return null
    const profileLabel = slot?.coder_profile
      ? i18nT('coder.profile_badge', { profile: slot.coder_profile })
      : i18nT('coder.default_profile')
    return (
      <div className="pointer-events-auto flex min-w-0 shrink items-center gap-1">
        <ExecutionLocationBadge location={executionLocation} />
        {executionLocation.kind === 'coder'
          && executionLocation.state !== 'starting' && (
          <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-accent/30 bg-accent-subtle px-2 py-0.5 text-[11px] font-medium text-accent">
            <Server className="lucide-inline" />
            {profileLabel}
          </span>
        )}
      </div>
    )
  }
  if (placement === 'header') {
    if (!slot || slot.messages === 0 || !slot.coder_profile) return null
    return (
      <span className="pointer-events-auto inline-flex shrink-0 items-center gap-1 rounded-full border border-accent/30 bg-accent-subtle px-2 py-0.5 text-[11px] font-medium text-accent">
        <Server className="lucide-inline" />
        {i18nT('coder.profile_badge', { profile: slot.coder_profile })}
      </span>
    )
  }
  const profiles = config.data?.profiles ?? {}
  if (!slot || !config.data?.enabled || Object.keys(profiles).length === 0) return null
  if (slot.messages > 0) return null

  return (
    <div className="pointer-events-auto flex items-center justify-end gap-2 pt-1.5" data-testid="composer-execution-profile">
      <CoderLogo height={11} />
      <div className="w-[220px] max-w-[70vw] shrink-0">
        <Select
          value={slot.coder_profile || DEFAULT_PROFILE_VALUE}
          disabled={slot.running || selectProfile.isPending}
          onValueChange={value => selectProfile.mutate(
            value === DEFAULT_PROFILE_VALUE ? '' : value,
          )}
        >
          <SelectTrigger
            className="h-7 text-[11px]"
            aria-label={i18nT('coder.session_profile_label')}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={DEFAULT_PROFILE_VALUE}>
              {i18nT('coder.default_profile')}
            </SelectItem>
            {Object.keys(profiles).sort(compareText).map(name => (
              <SelectItem key={name} value={name}>{name}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        {selectProfile.isError && (
          <span className="sr-only" role="status">{i18nT('coder.profile_change_failed')}</span>
        )}
      </div>
    </div>
  )
}
