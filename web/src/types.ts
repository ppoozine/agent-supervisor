// Shapes of the API responses, matching the backend's `api/views.py`.

export interface Project {
  id: number
  name: string
  repo_path: string
  github_repo: string | null
  default_branch: string
  require_review: boolean
}

/** An agent type the daemon has; `usable` is as of the daemon's last check. */
export interface Agent {
  agent_type: string
  display_name: string
  usable: boolean
  reports_cost: boolean
}

/** Whether one tool (an agent or gh) can be used. Never contains a credential. */
export interface ToolStatus {
  usable: boolean
  installed: boolean
  version: string | null
  /** api_key, subscription, cloud_provider, account, other or none. */
  auth: string
  /** How it is signed in, for people, e.g. "claude.ai (max)". */
  auth_label: string | null
  /** Why it cannot be used, when it cannot. */
  detail: string | null
  /** Commands to run in a terminal to make it usable. */
  fix: string[]
  /** Something to know even though it can be used. */
  notice: string | null
}

export interface AgentStatus extends ToolStatus {
  agent_type: string
  display_name: string
}

/** The result of the daemon's last check of every agent and gh. */
export interface Setup {
  checked_at: string
  agents: AgentStatus[]
  github: ToolStatus
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
  feedback_attempts: number
  max_feedback_attempts: number
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

/** Token and cost totals over every task, archived ones included. */
export interface Usage {
  /** Only agents that report cost count toward it. */
  cost_usd: number
  tokens_in: number
  tokens_out: number
  has_unknown_cost: boolean
  usage_is_complete: boolean
}
