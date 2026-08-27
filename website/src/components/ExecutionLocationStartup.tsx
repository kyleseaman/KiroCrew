import { Box, Layers3, MessageSquareText, Server } from 'lucide-react'
import { motion, useReducedMotion } from 'framer-motion'
import { useTranslation } from 'react-i18next'

import type { ExecutionLocation } from '../types'

export function ExecutionLocationStartup({
  location,
  profile,
}: {
  location: ExecutionLocation
  profile?: string
}) {
  const { t } = useTranslation()
  const reduceMotion = useReducedMotion()
  const provider = location.kind === 'coder' ? t('coder.tab_label') : location.kind
  const profileLabel = profile || location.profile
    || (location.kind === 'coder' ? t('coder.default_profile') : '')

  return (
    <div
      className="rounded-xl border border-accent/25 bg-accent-subtle p-3.5 shadow-sm sm:p-4"
      role="status"
      aria-live="polite"
      data-testid="execution-location-startup"
    >
      <div className="flex items-start gap-3">
        <span
          className="mt-0.5 inline-flex shrink-0 rounded-lg border border-accent/25 bg-card p-2 text-accent"
          aria-hidden="true"
        >
          <Server className="lucide-inline" />
        </span>
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-text-strong">
            {t('execution_environment.startup_title')}
          </h3>
          <p className="mt-0.5 text-[13px] leading-relaxed text-muted">
            {t('execution_environment.startup_description')}
          </p>
        </div>
      </div>

      <div
        className="mt-3 h-1.5 overflow-hidden rounded-full bg-accent/15"
        role="progressbar"
        aria-label={t('execution_environment.startup_progress_label')}
      >
        <motion.div
          className="h-full w-1/3 rounded-full bg-accent"
          initial={{ x: reduceMotion ? '0%' : '-100%' }}
          animate={{ x: reduceMotion ? '0%' : '300%' }}
          transition={reduceMotion ? undefined : {
            duration: 1.6,
            ease: 'easeInOut',
            repeat: Infinity,
            repeatType: 'loop',
          }}
        />
      </div>

      <dl className="mt-3 grid min-w-0 grid-cols-1 gap-2 sm:grid-cols-3">
        <div className="min-w-0 rounded-lg border border-border/70 bg-card/60 px-2.5 py-2">
          <dt className="flex items-center gap-1 text-[11px] font-medium text-muted">
            <Server className="lucide-inline" />
            {t('execution_environment.provider_label')}
          </dt>
          <dd className="mt-0.5 truncate text-[13px] font-medium text-text" title={provider}>
            {provider}
          </dd>
        </div>
        {profileLabel ? (
          <div className="min-w-0 rounded-lg border border-border/70 bg-card/60 px-2.5 py-2">
            <dt className="flex items-center gap-1 text-[11px] font-medium text-muted">
              <Layers3 className="lucide-inline" />
              {t('execution_environment.profile_label')}
            </dt>
            <dd className="mt-0.5 truncate text-[13px] font-medium text-text" title={profileLabel}>
              {profileLabel}
            </dd>
          </div>
        ) : null}
        <div className="min-w-0 rounded-lg border border-border/70 bg-card/60 px-2.5 py-2">
          <dt className="flex items-center gap-1 text-[11px] font-medium text-muted">
            <Box className="lucide-inline" />
            {t('execution_environment.workspace_label')}
          </dt>
          <dd
            className="mt-0.5 truncate text-[13px] font-medium text-text"
            title={location.workspace || undefined}
          >
            {location.workspace || t('execution_environment.workspace_pending')}
          </dd>
        </div>
      </dl>

      <p className="mt-3 flex items-start gap-1.5 text-[12px] leading-relaxed text-muted">
        <MessageSquareText className="lucide-inline mt-0.5 shrink-0" aria-hidden="true" />
        <span>{t('execution_environment.startup_queued')}</span>
      </p>
    </div>
  )
}
