import type { KeyboardEvent } from 'react'

/** The parts of a key press that decide whether it is the submit shortcut. */
export interface KeyPress {
  key: string
  metaKey: boolean
  ctrlKey: boolean
  /** True while an input method (e.g. Zhuyin, Pinyin, Kana) is still composing text. */
  isComposing: boolean
}

/**
 * True for Cmd+Enter or Ctrl+Enter outside an input method composition.
 *
 * Both modifiers are accepted on every platform, so no platform detection is needed. Plain
 * Enter is left alone so it keeps inserting a newline.
 */
export function isSubmitShortcut(press: KeyPress): boolean {
  if (press.isComposing) return false
  return press.key === 'Enter' && (press.metaKey || press.ctrlKey)
}

/** The shortcut as shown next to a submit button: ⌘↵ on macOS, Ctrl+↵ elsewhere. */
export function submitShortcutLabel(platform: string): string {
  return /mac|iphone|ipad/i.test(platform) ? '⌘↵' : 'Ctrl+↵'
}

/** The shortcut label for the browser this page runs in. */
export function currentSubmitShortcutLabel(): string {
  const platform = typeof navigator === 'undefined' ? '' : navigator.platform || navigator.userAgent
  return submitShortcutLabel(platform)
}

/**
 * Submit the textarea's form on Cmd/Ctrl+Enter, as if its submit button were clicked.
 *
 * Nothing happens while that button is disabled, so the shortcut follows the same rules as the
 * button (empty text, a request in flight).
 */
export function submitOnShortcut(event: KeyboardEvent<HTMLTextAreaElement>): void {
  const press: KeyPress = {
    key: event.key,
    metaKey: event.metaKey,
    ctrlKey: event.ctrlKey,
    isComposing: event.nativeEvent.isComposing,
  }
  if (!isSubmitShortcut(press)) return
  event.preventDefault()
  const form = event.currentTarget.form
  const button = form?.querySelector<HTMLButtonElement>('button[type="submit"]')
  if (form == null || button == null || button.disabled) return
  form.requestSubmit(button)
}
