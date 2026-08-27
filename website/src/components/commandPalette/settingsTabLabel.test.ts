import { readFileSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { SETTINGS_REGISTRY } from './settingsRegistry.gen'
import { settingsTabLabel } from './settingsTabLabel'

describe('settingsTabLabel', () => {
  it('resolves a localized display label for EVERY tab in the registry', () => {
    // The guarantee this test exists for: no settings row can render a raw machine
    // key as its tab name. A new tab, or a tab whose catalog key moves, lands here
    // rather than in front of a user reading a non-English dashboard.
    const tabs = [...new Set(SETTINGS_REGISTRY.map(e => e.tab))].sort()
    expect(tabs.length).toBeGreaterThan(0)
    const unresolved = tabs.filter(tab => {
      const label = settingsTabLabel(tab)
      return !label || label === tab || label === `settings.tabs.${tab}.label`
    })
    expect(unresolved).toEqual([])
  })

  it('translates regular and irregular tabs', () => {
    expect(settingsTabLabel('browser')).toBe('Browser')
    // Kebab tab key, camelCase catalog segment.
    expect(settingsTabLabel('computer-use')).toBe('Computer Use')
    // Lives outside the settings tab block entirely.
    expect(settingsTabLabel('privacy')).toBe('Privacy')
    expect(settingsTabLabel('session-environments')).toBe('Session Environments')
    expect(settingsTabLabel('coder')).toBe('Session Environments')
  })

  it('leaves an unknown machine tab unchanged', () => {
    expect(settingsTabLabel('not-a-tab')).toBe('not-a-tab')
  })

  it('is the single source both palette surfaces read', () => {
    // The legacy provider used to capitalize the tab key, which rendered
    // "Computer-use" for `computer-use` and, in any non-English locale, the English
    // machine key for every tab. Two surfaces showing different names for one tab is
    // the drift this resolver exists to remove, so the sibling is asserted here
    // rather than left to be rediscovered.
    const src = readFileSync(
      path.join(__dirname, 'providers', 'settingsProvider.ts'),
      'utf-8',
    )
    expect(src).toContain('settingsTabLabel(entry.tab)')
    expect(src).not.toMatch(/entry\.tab\.charAt\(0\)\.toUpperCase\(\)/)
  })
})
