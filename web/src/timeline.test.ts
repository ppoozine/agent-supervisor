import { describe, expect, test } from 'vitest'

import { buildTimeline, type AgentItem, type TimelineItem } from './timeline'
import type { EventRow, Run } from './types'

function run(id: number, trigger: string, startedAt: string, fields: Partial<Run> = {}): Run {
  return {
    id,
    task_id: 7,
    trigger,
    input_text: `input of run ${id}`,
    source_head_sha: null,
    started_at: startedAt,
    ended_at: startedAt,
    exit_status: 'success',
    tokens_in: 10,
    tokens_out: 5,
    cost_usd: null,
    ...fields,
  }
}

function event(
  id: number,
  runId: number | null,
  ts: string,
  type: string,
  payload: Record<string, unknown>,
): EventRow {
  return { id, task_id: 7, run_id: runId, ts, type, payload }
}

const at = (minute: number, second = 0): string =>
  `2026-11-19T10:${String(minute).padStart(2, '0')}:${String(second).padStart(2, '0')}+00:00`

function kinds(items: TimelineItem[]): string[] {
  return items.map((item) => item.kind)
}

function agent(items: TimelineItem[], index = 0): AgentItem {
  return items.filter((item): item is AgentItem => item.kind === 'agent')[index]
}

describe('who each entry comes from', () => {
  test('the task description and follow-up messages are yours', () => {
    const items = buildTimeline(
      [run(1, 'initial', at(0)), run(2, 'user_message', at(5), { input_text: 'also add tests' })],
      [],
    )

    expect(kinds(items)).toEqual(['run', 'you', 'run', 'you'])
    expect(items[1]).toMatchObject({ kind: 'you', text: 'input of run 1' })
    expect(items[3]).toMatchObject({ kind: 'you', text: 'also add tests' })
    expect(items[2]).toMatchObject({ kind: 'run', number: 2, reason: 'Your message', source: 'you' })
  })

  test('instructions sent for failed CI, conflicts and PR comments are automatic', () => {
    const items = buildTimeline(
      [
        run(4, 'initial', at(0)),
        run(5, 'ci_failure', at(1), { source_head_sha: '3f2a9c1e55', input_text: 'CI failed\nlog' }),
        run(6, 'merge_conflict', at(2)),
        run(7, 'review_feedback', at(3)),
      ],
      [],
    )

    const autos = items.filter((item) => item.kind === 'auto')
    expect(autos.map((item) => item.kind === 'auto' && item.reason)).toEqual([
      'CI failed on 3f2a9c1e: sent the failing logs to the agent',
      'Merge conflict with the base branch: asked the agent to resolve it',
      'Sent new pull request comments to the agent',
    ])
    expect(autos[0]).toMatchObject({ text: 'CI failed\nlog' })
    const dividers = items.filter((item) => item.kind === 'run')
    expect(dividers.map((item) => item.kind === 'run' && [item.number, item.reason])).toEqual([
      [1, 'Task opened'],
      [2, 'Automatic: CI failed'],
      [3, 'Automatic: merge conflict'],
      [4, 'Automatic: PR comments'],
    ])
  })

  test('PR comments are from reviewers, with where they were left and why some were not sent', () => {
    const items = buildTimeline(
      [],
      [
        event(1, null, at(1), 'review_comment', {
          kind: 'inline_comment',
          author: 'alice',
          body: 'off by one',
          path: 'calc.py',
          line: 21,
          url: 'https://github.com/o/r/pull/1#c1',
        }),
        event(2, null, at(1), 'review_comment', { kind: 'comment', author: 'ci[bot]', body: 'coverage 90%', ignored: 'bot' }),
        event(3, null, at(1), 'review_comment', { kind: 'review', author: 'bob', body: '', review_state: 'approved', ignored: 'not_a_change_request' }),
      ],
    )

    expect(items).toMatchObject([
      { kind: 'reviewer', author: 'alice', commentKind: 'inline comment', where: 'calc.py:21', notSent: null },
      { kind: 'reviewer', author: 'ci[bot]', where: null, notSent: 'Not sent: posted by a bot' },
      { kind: 'reviewer', author: 'bob', body: '(approved)', notSent: 'Not sent: approval' },
    ])
  })

  test('pushes, the PR and CI results are system entries', () => {
    const items = buildTimeline(
      [],
      [
        event(1, 1, at(1), 'pushed', { branch: 'asv/task-7', head_sha: 'abcdef123456' }),
        event(2, 1, at(1), 'pr_opened', { number: 12, url: 'https://github.com/o/r/pull/12' }),
        event(3, null, at(2), 'ci_status', { ci_state: 'failed', review_state: 'none', merge_state: 'clean', head_sha: 'abcdef123456' }),
        event(4, null, at(3), 'ci_status', { ci_state: 'passed', review_state: 'none', merge_state: 'conflicting', head_sha: 'abcdef123456' }),
        event(5, null, at(4), 'ci_status', { pr_state: 'merged', ci_state: 'passed' }),
      ],
    )

    expect(items).toMatchObject([
      { kind: 'system', text: 'Pushed abcdef12 to asv/task-7', tone: 'neutral' },
      { kind: 'system', text: 'Opened PR #12', url: 'https://github.com/o/r/pull/12' },
      { kind: 'system', text: 'CI failed on abcdef12', tone: 'fail' },
      { kind: 'system', text: 'CI passed on abcdef12 · merge conflict with the base branch', tone: 'fail' },
      { kind: 'system', text: 'PR merged', tone: 'pass' },
    ])
  })
})

describe('agent activity', () => {
  test('consecutive agent events of one run form one entry; a command pairs with its result', () => {
    const items = buildTimeline(
      [run(1, 'initial', at(0))],
      [
        event(1, 1, at(0, 1), 'session_started', { session_id: 's' }),
        event(2, 1, at(0, 2), 'agent_message', { kind: 'reasoning', text: 'look first\nthen edit' }),
        event(3, 1, at(0, 3), 'command', { phase: 'started', tool_use_id: 't1', command: 'pytest' }),
        event(4, 1, at(0, 4), 'command', { phase: 'finished', tool_use_id: 't1', output: 'boom', exit_code: 1, is_error: true }),
        event(5, 1, at(0, 5), 'tool_call', { phase: 'started', tool_use_id: 't2', tool: 'Edit', input: { file_path: 'calc.py', old: 'a' } }),
        event(6, 1, at(0, 6), 'tool_call', { phase: 'finished', tool_use_id: 't2', is_error: false }),
        event(7, 1, at(0, 7), 'agent_message', { kind: 'message', text: 'Fixed it.' }),
      ],
    )

    expect(kinds(items)).toEqual(['run', 'you', 'agent'])
    expect(agent(items).parts).toMatchObject([
      { kind: 'action', type: 'thinking', title: 'Thinking', detail: 'look first\nthen edit', note: '2 lines' },
      { kind: 'action', type: 'command', title: '$ pytest', detail: 'boom', note: 'exit 1', failed: true },
      { kind: 'action', type: 'tool', title: 'Edit calc.py', failed: false },
      { kind: 'message', text: 'Fixed it.' },
    ])
  })

  test('a denied action is shown with the reason', () => {
    const items = buildTimeline(
      [run(1, 'initial', at(0))],
      [event(1, 1, at(0, 1), 'permission_denied', { tool: 'Bash', input: { command: 'git push' }, reason: 'not allowed' })],
    )

    expect(agent(items).parts).toMatchObject([
      { type: 'denied', title: 'Denied: Bash git push', detail: 'not allowed', failed: true },
    ])
  })

  test('empty agent messages are left out', () => {
    const items = buildTimeline(
      [run(1, 'initial', at(0))],
      [event(1, 1, at(0, 1), 'agent_message', { kind: 'reasoning', text: '  ' })],
    )

    expect(kinds(items)).toEqual(['run', 'you'])
  })

  test('a system entry in between starts a new agent entry, and a late result still finds its command', () => {
    const items = buildTimeline(
      [run(1, 'initial', at(0))],
      [
        event(1, 1, at(0, 1), 'command', { phase: 'started', tool_use_id: 't1', command: 'make' }),
        event(2, null, at(0, 2), 'ci_status', { ci_state: 'pending', head_sha: 'aaaa1111' }),
        event(3, 1, at(0, 3), 'command', { phase: 'finished', tool_use_id: 't1', output: 'ok', exit_code: 0 }),
        event(4, 1, at(0, 4), 'agent_message', { kind: 'message', text: 'done' }),
      ],
    )

    expect(kinds(items)).toEqual(['run', 'you', 'agent', 'system', 'agent'])
    expect(agent(items, 0).parts).toMatchObject([{ title: '$ make', detail: 'ok', note: '' }])
    expect(agent(items, 1).parts).toMatchObject([{ kind: 'message', text: 'done' }])
  })
})

describe('order and runs', () => {
  test('comments found between runs sit where they happened in time', () => {
    const items = buildTimeline(
      [run(1, 'initial', at(0)), run(2, 'review_feedback', at(10))],
      [
        event(1, 1, at(1), 'pushed', { branch: 'b', head_sha: '1111aaaa' }),
        event(2, null, at(5), 'review_comment', { kind: 'comment', author: 'alice', body: 'rename it' }),
        event(3, 2, at(11), 'agent_message', { kind: 'message', text: 'renamed' }),
      ],
    )

    expect(kinds(items)).toEqual(['run', 'you', 'system', 'reviewer', 'run', 'auto', 'agent'])
  })

  test('a run that went wrong says so; a successful end adds nothing', () => {
    const items = buildTimeline(
      [run(1, 'initial', at(0), { exit_status: 'error' })],
      [
        event(1, 1, at(1), 'state_change', { change: 'run_ended', exit_status: 'error', detail: 'git push failed\nmore' }),
        event(2, 1, at(1), 'state_change', { change: 'run_ended', exit_status: 'success' }),
      ],
    )

    expect(items.filter((item) => item.kind === 'system')).toMatchObject([
      { text: 'Run ended (error): git push failed', tone: 'fail' },
    ])
  })

  test('the latest run still going shows the agent working', () => {
    const items = buildTimeline([run(1, 'initial', at(0), { ended_at: null, exit_status: null })], [])

    expect(kinds(items)).toEqual(['run', 'you', 'working'])
  })

  test('a run without instructions shows only its divider', () => {
    const items = buildTimeline([run(1, 'ci_failure', at(0), { input_text: null })], [])

    expect(kinds(items)).toEqual(['run'])
  })
})
