import { expect, test } from 'vitest'

import { pageFromHash } from './App'

test('the url hash picks the page', () => {
  expect(pageFromHash('#/settings')).toEqual({ kind: 'settings' })
  expect(pageFromHash('#/tasks/12')).toEqual({ kind: 'task', taskId: 12 })
  expect(pageFromHash('#/')).toEqual({ kind: 'board' })
  expect(pageFromHash('')).toEqual({ kind: 'board' })
})
