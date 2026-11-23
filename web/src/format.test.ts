import { expect, test } from 'vitest'

import {
  formatSeconds,
  formatUsage,
  mergeEvents,
  runDuration,
  runOutcome,
  runUsage,
  settingRows,
  timeAgo,
} from './format'
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

test('lengths of time use the largest unit that reads naturally', () => {
  expect(formatSeconds(30)).toBe('30 s')
  expect(formatSeconds(1800)).toBe('30 min')
  expect(formatSeconds(90)).toBe('1.5 min')
  expect(formatSeconds(5400)).toBe('1.5 h')
})

test('the limits read as label and value, with the built-in ones marked', () => {
  const rows = settingRows({
    config_path: '/c.toml',
    limits: {
      task_cost_limit_usd: 1000,
      task_token_limit: 500_000_000,
      max_turns_per_run: 200,
      run_timeout_seconds: 5400,
      max_ci_fix_attempts: 3,
      max_feedback_attempts: 5,
    },
    poll_interval_seconds: 30,
    notify_needs_you: false,
    permission: { sandbox_level: 'read-only', allow_network: false, tool_rules: { denied_commands: [] } },
  })
  const value = (label: string) => rows.find((row) => row.label === label)

  expect(value('Cost limit per task')?.value).toBe('$1000.00')
  expect(value('Token limit per task')?.value).toBe('500,000,000')
  expect(value('Time per run')?.value).toBe('1.5 h')
  expect(value('Notify when a task needs you')?.value).toBe('off')
  expect(value('Network for agents')?.value).toBe('not allowed')
  expect(value('Denied commands')?.value).toBe('none')
  // An adapter-specific rule that is not there is left out rather than shown empty.
  expect(value('Allowed tools')).toBeUndefined()
  expect(rows.filter((row) => row.fixed).map((row) => row.label)).toEqual([
    'Automatic fixes per task',
    'Comment rounds per task',
  ])
})

