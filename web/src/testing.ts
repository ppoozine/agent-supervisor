// 測試共用的假資料。
import type { Card, EventRow, TaskDetail } from './types'

export function makeCard(fields: Partial<Card> = {}): Card {
  return {
    id: 7,
    project_id: 1,
    title: 'Add a power function',
    agent_type: 'claude_code',
    branch: 'asv/task-7-add-power',
    column: 'Working',
    label: 'Working',
    has_open_run: true,
    pr_number: null,
    pr_state: null,
    ci_state: 'none',
    review_state: 'none',
    merge_state: 'clean',
    ci_fix_attempts: 0,
    max_ci_fix_attempts: 3,
    cost_usd: 0.42,
    tokens: 12345,
    usage_is_complete: true,
    archived_at: null,
    created_at: '2026-11-19T10:00:00+00:00',
    updated_at: '2026-11-19T10:00:00+00:00',
    ...fields,
  }
}

export function makeDetail(fields: Partial<TaskDetail> = {}): TaskDetail {
  return {
    ...makeCard(),
    prompt: 'add power(base, exponent)',
    model: null,
    session_id: 's1',
    worktree_path: '/tmp/worktree',
    head_sha: null,
    tokens_in: 12000,
    tokens_out: 345,
    runs: [],
    ...fields,
  }
}

export function makeEvent(id: number, type: string, payload: Record<string, unknown>): EventRow {
  return { id, task_id: 7, run_id: 1, ts: '2026-11-19T10:00:00+00:00', type, payload }
}
