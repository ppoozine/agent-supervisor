import { expect, test } from 'vitest'

import { formatUsage, mergeEvents, runDuration, runOutcome, runUsage, timeAgo } from './format'
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

test('a run shows how long it took, how it ended, and what it cost', () => {
  const run = {
    started_at: '2026-11-19T10:00:00+00:00',
    ended_at: '2026-11-19T10:02:14+00:00',
    exit_status: 'limit_reached',
    cost_usd: null,
    tokens_in: 1200,
    tokens_out: 300,
  }

  expect(runDuration(run)).toBe('2m 14s')
  expect(runOutcome(run)).toBe('⏱ limit reached')
  expect(runUsage(run)).toBe('1.5k tok')
  expect(runUsage({ ...run, cost_usd: 0.123 })).toBe('~$0.12')
  expect(runUsage({ ...run, tokens_in: null, tokens_out: null })).toBe('')
  expect(runDuration({ ...run, ended_at: null })).toBe('running')
  expect(runOutcome({ exit_status: null })).toBe('running')
})
