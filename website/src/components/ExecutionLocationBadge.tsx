import { Loader2, Server } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { ExecutionLocation } from '../types'

export function ExecutionLocationBadge({
  location,
}: {
  location?: ExecutionLocation
}) {
  const { t } = useTranslation()
  if (!location) return null

  const starting = location.state === 'starting'
  let label: string
  if (location.kind === 'coder') {
    label = location.workspace
      ? t('coder.badge_label', { workspace: location.workspace })
      : t('coder.badge_allocating')
  } else if (starting) {
    label = location.workspace
      ? t('execution_environment.badge_starting_named', {
          kind: location.kind,
          workspace: location.workspace,
        })
      : t('execution_environment.badge_starting', { kind: location.kind })
  } else {
    label = t('execution_environment.badge_running', {
      kind: location.kind,
      workspace: location.workspace,
    })
  }

  return (
    <span
      className="pointer-events-auto inline-flex max-w-[45vw] shrink-0 items-center gap-1 rounded-full border border-accent/30 bg-accent-subtle px-2 py-0.5 text-[11px] font-medium text-accent md:max-w-[28vw]"
      title={label}
      role={starting ? 'status' : undefined}
      aria-live={starting ? 'polite' : undefined}
    >
      {starting
        ? <Loader2 className="lucide-inline animate-spin motion-reduce:animate-none" />
        : <Server className="lucide-inline" />}
      <span className="truncate">{label}</span>
    </span>
  )
}
