import { expect, test } from 'vitest'

import { formatUsage, mergeEvents, timeAgo, toLogLine } from './format'
import { makeCard, makeEvent } from './testing'

test('cost is shown as an estimate', () => {
  expect(formatUsage(makeCard({ cost_usd: 0.4239 }))).toBe('~$0.42')
})

test('an agent that does not report cost shows tokens instead of zero dollars', () => {
  expect(formatUsage(makeCard({ cost_usd: null, tokens: 86494 }))).toBe('86.5k tok')
})

test('incomplete usage is marked as a lower bound', () => {
  expect(formatUsage(makeCard({ cost_usd: 0.1, usage_is_complete: false }))).toBe('~$0.10+')
})

test('time ago uses the largest fitting unit', () => {
  const now = new Date('2026-11-19T12:00:00Z')
  expect(timeAgo('2026-11-19T11:59:40Z', now)).toBe('just now')
  expect(timeAgo('2026-11-19T11:38:00Z', now)).toBe('22m ago')
  expect(timeAgo('2026-11-19T09:00:00Z', now)).toBe('3h ago')
  expect(timeAgo('2026-11-17T12:00:00Z', now)).toBe('2d ago')
})

test('events fetched again after a reconnect are not duplicated', () => {
  const first = makeEvent(1, 'pushed', {})
  const second = makeEvent(2, 'pushed', {})
  const third = makeEvent(3, 'pushed', {})

  const merged = mergeEvents([first, third], [second, third])

  expect(merged.map((event) => event.id)).toEqual([1, 2, 3])
})

test('agent messages with no text are left out of the log', () => {
  expect(toLogLine(makeEvent(1, 'agent_message', { kind: 'reasoning', text: '' }))).toBeNull()
  expect(toLogLine(makeEvent(2, 'agent_message', { kind: 'message', text: 'done' }))?.text).toBe(
    'done',
  )
})

test('a command shows what was run and then its output', () => {
  const started = toLogLine(makeEvent(1, 'command', { phase: 'started', command: 'pytest' }))
  const failed = toLogLine(
    makeEvent(2, 'command', { phase: 'finished', is_error: true, output: 'boom' }),
  )

  expect(started).toMatchObject({ kind: 'command', text: '$ pytest' })
  expect(failed).toMatchObject({ kind: 'error', text: 'boom' })
})

test('a review comment shows who wrote it, where, and whether it was skipped', () => {
  const inline = toLogLine(
    makeEvent(1, 'review_comment', {
      author: 'alice',
      body: 'off by one',
      path: 'calc.py',
      line: 21,
      ignored: null,
    }),
  )
  const bot = toLogLine(
    makeEvent(2, 'review_comment', { author: 'ci[bot]', body: 'coverage 90%', ignored: 'bot' }),
  )

  expect(inline?.text).toBe('comment by alice on calc.py:21: off by one')
  expect(bot?.text).toBe('comment by ci[bot] (not sent to the agent: bot): coverage 90%')
})

test('a status change mentions a merge conflict only when there is one', () => {
  const status = { ci_state: 'passed', review_state: 'none' }

  expect(toLogLine(makeEvent(1, 'ci_status', { ...status, merge_state: 'clean' }))?.text).toBe(
    'CI passed · review none',
  )
  expect(
    toLogLine(makeEvent(2, 'ci_status', { ...status, merge_state: 'conflicting' }))?.text,
  ).toBe('CI passed · review none · merge conflict')
})
