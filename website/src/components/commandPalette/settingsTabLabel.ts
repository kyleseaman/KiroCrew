/**
 * Localized display name for a settings tab key.
 *
 * `SettingEntry.tab` is a machine key (`browser`, `computer-use`), so rendering it
 * raw puts an untranslated internal id in front of the user. The settings page
 * already localizes every tab name; this resolves the same catalog key so a tab
 * name reads the same wherever it appears.
 *
 * Most tabs follow `settings.tabs.<key>.label`. A few do not, and a mechanical
 * derivation would silently render the key itself for those tabs, so they are
 * translated explicitly. `settingsTabLabel.test.ts` pins every tab in
 * SETTINGS_REGISTRY to a resolved label, so adding a tab or moving its key fails
 * the test instead of shipping a raw id.
 */
import { i18nT } from '../../i18n/t'

/**
 * Registry tabs are finite, so keep every catalog key literal and discoverable.
 * Functions defer translation until render time without assembling catalog keys.
 */
const SETTINGS_TAB_LABEL: Record<string, () => string> = {
  browser: () => i18nT('settings.tabs.browser.label'),
  channels: () => i18nT('settings.tabs.channels.label'),
  chat: () => i18nT('settings.tabs.chat.label'),
  'computer-use': () => i18nT('settings.tabs.computerUse.label'),
  developer: () => i18nT('settings.tabs.developer.label'),
  display: () => i18nT('settings.tabs.display.label'),
  notifications: () => i18nT('settings.tabs.notifications.label'),
  privacy: () => i18nT('privacyDisclosure.settingsLabel'),
  security: () => i18nT('settings.tabs.security.label'),
  'session-environments': () => i18nT('execution_environment.settings_tab_label'),
  shortcuts: () => i18nT('settings.tabs.shortcuts.label'),
  skills: () => i18nT('settings.tabs.skills.label'),
  voice: () => i18nT('settings.tabs.voice.label'),
  // Compatibility for saved command-palette entries from the vendor-named route.
  coder: () => i18nT('execution_environment.settings_tab_label'),
}

/** Localized display name for a settings tab key. */
export function settingsTabLabel(tab: string): string {
  return SETTINGS_TAB_LABEL[tab]?.() ?? tab
}

/**
 * Subtitle for a settings row: what tells two rows apart.
 *
 * The tab name alone is not enough. The registry holds entries with the SAME label
 * in the SAME tab -- two `Speed` selects under Voice -- distinguished only by their
 * description, so a tab-only subtitle leaves those rows identical. The composition
 * is a catalog entry, so a locale can reorder the parts or change the separator.
 * `description` itself is not in the catalog, so that detail stays English until the
 * registry carries a key for it -- still better than two rows a user cannot choose
 * between.
 */
export function settingsSubtitle(entry: { tab: string; description?: string }): string {
  const tab = settingsTabLabel(entry.tab)
  if (!entry.description) return tab
  return i18nT('components.commandPalette.settings_subtitle', {
    tab,
    detail: entry.description,
  })
}
