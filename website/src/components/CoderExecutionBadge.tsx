import { Server } from 'lucide-react'

import type { ChatSlot } from '../types'
import { i18nT } from '../i18n/t'

export function CoderExecutionBadge({
  location,
}: {
  location: ChatSlot['execution_location']
}) {
  if (!location || location.kind !== 'coder') return null
  const label = location.workspace
    ? i18nT('coder.badge_label', { workspace: location.workspace })
    : i18nT('coder.badge_allocating')
  return (
    <span
      className="pointer-events-auto inline-flex max-w-[45vw] shrink-0 items-center gap-1 rounded-full border border-accent/30 bg-accent-subtle px-2 py-0.5 text-[11px] font-medium text-accent md:max-w-[28vw]"
      title={location.workspace ? i18nT('coder.badge_title') : label}
    >
      <Server className="lucide-inline" />
      <span className="truncate">
        {label}
      </span>
    </span>
  )
}
