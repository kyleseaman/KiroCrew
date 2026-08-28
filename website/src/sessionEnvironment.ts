import { i18nT } from './i18n/t'
import type {
  ExecutionLocation,
  SessionEnvironmentBinding,
  SessionEnvironmentProviderSummary,
} from './types'

interface SessionEnvironmentSlot {
  environment?: SessionEnvironmentBinding | null
  coder_profile?: string
  coder_workspace?: string
  execution_location?: ExecutionLocation
}

/** One compatibility seam for gateway payloads written before generic bindings. */
export function sessionEnvironment(
  slot?: SessionEnvironmentSlot,
): SessionEnvironmentBinding | undefined {
  if (slot?.environment?.provider) return slot.environment
  if (slot?.coder_workspace || slot?.coder_profile) {
    return {
      provider: 'coder',
      configuration: slot.coder_profile || '',
      resource_name: slot.coder_workspace || '',
    }
  }
  if (slot?.execution_location?.kind) {
    return {
      provider: slot.execution_location.kind,
      configuration: slot.execution_location.profile || '',
      resource_name: slot.execution_location.workspace || '',
    }
  }
  return undefined
}

export function sessionExecutionLocation(slot?: SessionEnvironmentSlot): ExecutionLocation | undefined {
  if (slot?.execution_location) return slot.execution_location
  const environment = sessionEnvironment(slot)
  if (!environment?.resource_name) return undefined
  return {
    kind: environment.provider,
    workspace: environment.resource_name,
    remote_cwd: '',
    state: 'retained',
    profile: environment.configuration || undefined,
  }
}

export function environmentProvider(
  providers: SessionEnvironmentProviderSummary[] | undefined,
  providerId: string,
): SessionEnvironmentProviderSummary | undefined {
  return providers?.find(provider => provider.id === providerId)
}

export function environmentProviderLabel(
  providerId: string,
  provider?: SessionEnvironmentProviderSummary,
): string {
  if (provider?.name) return provider.name
  if (providerId === 'coder') return i18nT('coder.tab_label')
  return providerId
}
