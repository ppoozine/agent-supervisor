// API 回應的形狀，對應後端的 `api/views.py`。

export interface Project {
  id: number
  name: string
  repo_path: string
  github_repo: string | null
  default_branch: string
  require_review: boolean
}

export interface Agent {
  agent_type: string
  reports_cost: boolean
}

/** 看板上的一張卡片。`column` 與 `label` 由後端推導，前端只照著顯示。 */
export interface Card {
  id: number
  project_id: number
  title: string
  agent_type: string
  branch: string | null
  column: string
  label: string
  has_open_run: boolean
  pr_number: number | null
  pr_state: string | null
  ci_state: string
  review_state: string
  ci_fix_attempts: number
  max_ci_fix_attempts: number
  /** `null` 表示這種 agent 不回報花費：花費未知，不是零。 */
  cost_usd: number | null
  tokens: number
  usage_is_complete: boolean
  archived_at: string | null
  created_at: string
  updated_at: string
}

export interface Run {
  id: number
  task_id: number
  trigger: string
  input_text: string | null
  source_head_sha: string | null
  started_at: string
  ended_at: string | null
  exit_status: string | null
  tokens_in: number | null
  tokens_out: number | null
  cost_usd: number | null
}

export interface TaskDetail extends Card {
  prompt: string
  model: string | null
  session_id: string | null
  worktree_path: string | null
  head_sha: string | null
  tokens_in: number
  tokens_out: number
  runs: Run[]
}

/** 一筆稽核紀錄。`payload` 的內容依 `type` 而不同。 */
export interface EventRow {
  id: number
  task_id: number
  run_id: number | null
  ts: string
  type: string
  payload: Record<string, unknown>
}

export interface EventPage {
  events: EventRow[]
  next_after: number | null
}
