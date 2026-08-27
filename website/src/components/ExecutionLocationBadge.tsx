import { useEffect, useRef, useState } from 'react'
import { Check, Copy, Loader2, Server } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { ExecutionLocation } from '../types'
import { copyToClipboard } from '../utils/clipboard'
import { IconButton } from './ui'

export function ExecutionLocationBadge({
  location,
}: {
  location?: ExecutionLocation
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
    <span
      className="pointer-events-auto inline-flex max-w-[45vw] shrink-0 items-center gap-1 rounded-full border border-accent/30 bg-accent-subtle px-2 py-0.5 text-[11px] font-medium text-accent md:max-w-[28vw]"
      title={label}
      role={starting ? 'status' : undefined}
      aria-live={starting ? 'polite' : undefined}
    >
      {starting ? (
        <Loader2 className="lucide-inline animate-spin motion-reduce:animate-none" />
      ) : (
        <>
          {ready ? (
            <span
              className="h-1.5 w-1.5 shrink-0 rounded-full bg-ok"
              data-testid="execution-location-ready"
              aria-hidden="true"
            />
          ) : null}
          <Server className="lucide-inline" />
        </>
      )}
      <span className="truncate">{label}</span>
      {location.workspace ? (
        <IconButton
          className="-my-1 -mr-1 ml-0.5 shrink-0"
          aria-label={copyLabel}
          aria-live="polite"
          title={copyLabel}
          onClick={copyWorkspace}
        >
          {copied
            ? <Check className="lucide-inline text-ok" />
            : <Copy className="lucide-inline" />}
        </IconButton>
      ) : null}
    </span>
  )
}
