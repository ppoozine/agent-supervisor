// Shapes of the API responses, matching the backend's `api/views.py`.

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

/** A card on the board. `column` and `label` are derived by the backend; the frontend only displays them. */
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
  head_sha: string | null
  ci_state: string
  review_state: string
  merge_state: string
  ci_fix_attempts: number
  max_ci_fix_attempts: number
  /** `null` means this kind of agent does not report cost: the cost is unknown, not zero. */
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

/** One audit log entry. The contents of `payload` depend on `type`. */
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

export interface TaskDiff {
  diff: string
  is_truncated: boolean
  base_branch: string
}
