import { expect, test } from 'vitest'

import { isSubmitShortcut, submitShortcutLabel, type KeyPress } from './keys'

const enter: KeyPress = { key: 'Enter', metaKey: false, ctrlKey: false, isComposing: false }

test('Cmd+Enter and Ctrl+Enter submit', () => {
  expect(isSubmitShortcut({ ...enter, metaKey: true })).toBe(true)
  expect(isSubmitShortcut({ ...enter, ctrlKey: true })).toBe(true)
})

test('plain Enter and other keys do not submit', () => {
  expect(isSubmitShortcut(enter)).toBe(false)
  expect(isSubmitShortcut({ ...enter, key: 'a', metaKey: true })).toBe(false)
})

test('nothing submits while an input method is composing', () => {
  expect(isSubmitShortcut({ ...enter, metaKey: true, isComposing: true })).toBe(false)
})

test('the label follows the platform', () => {
  expect(submitShortcutLabel('MacIntel')).toBe('⌘↵')
  expect(submitShortcutLabel('Linux x86_64')).toBe('Ctrl+↵')
  expect(submitShortcutLabel('')).toBe('Ctrl+↵')
})
