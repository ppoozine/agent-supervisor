// Format data as text for the screen. Pure functions only.
import type { Card, EventRow, Project, Run } from './types'

/** Cost or token count. Shows tokens when the agent does not report cost; cost is an estimate, prefixed with `~`. */
export function formatUsage(card: Pick<Card, 'cost_usd' | 'tokens' | 'usage_is_complete'>): string {
  const amount =
    card.cost_usd === null ? `${formatCount(card.tokens)} tok` : `~$${card.cost_usd.toFixed(2)}`
  // When some run's usage is unavailable (e.g. it was stopped), the real number is higher.
  return card.usage_is_complete ? amount : `${amount}+`
}

/** Shorten large numbers: 1234 becomes 1.2k, 1234567 becomes 1.2M. */
export function formatCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`
  return String(value)
}

/** How long ago, e.g. `8m ago`. */
export function timeAgo(timestamp: string, now: Date = new Date()): string {
  const seconds = Math.max(0, Math.floor((now.getTime() - new Date(timestamp).getTime()) / 1000))
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

/** Hours, minutes and seconds in local time. */
export function clockTime(timestamp: string): string {
  return new Date(timestamp).toLocaleTimeString('en-GB')
}

/** How long a run took, e.g. `2m 14s`; `running` while it has not ended. */
export function runDuration(run: Pick<Run, 'started_at' | 'ended_at'>): string {
  if (run.ended_at === null) return 'running'
  const seconds = Math.round(
    (new Date(run.ended_at).getTime() - new Date(run.started_at).getTime()) / 1000,
  )
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

const EXIT_SYMBOLS: Record<string, string> = {
  success: '✓',
  error: '✗',
  stopped: '■',
  limit_reached: '⏱',
  interrupted: '⚡',
}

/** A run's outcome with its symbol, e.g. `✓ success`; `running` while it has not ended. */
export function runOutcome(run: Pick<Run, 'exit_status'>): string {
  if (run.exit_status === null) return 'running'
  const symbol = EXIT_SYMBOLS[run.exit_status]
  return symbol ? `${symbol} ${run.exit_status.replace('_', ' ')}` : run.exit_status
}

/** A run's own usage: estimated cost, else tokens; empty when unknown. */
export function runUsage(run: Pick<Run, 'cost_usd' | 'tokens_in' | 'tokens_out'>): string {
  if (run.cost_usd !== null) return `~$${run.cost_usd.toFixed(2)}`
  if (run.tokens_in === null && run.tokens_out === null) return ''
  return `${formatCount((run.tokens_in ?? 0) + (run.tokens_out ?? 0))} tok`
}

export type Tone = 'blue' | 'orange' | 'red' | 'yellow' | 'green' | 'gray'

const LABEL_TONES: Record<string, Tone> = {
  Working: 'blue',
  'Fixing CI': 'blue',
  'Addressing review': 'blue',
  'Resolving conflict': 'blue',
  'Merge conflict': 'orange',
  'CI failed': 'red',
  'Agent error': 'red',
  'Changes requested': 'orange',
  'Over budget': 'orange',
  'Limit reached': 'orange',
  Stopped: 'orange',
  Interrupted: 'orange',
  'No changes': 'orange',
  'Review pending': 'yellow',
  'CI running': 'yellow',
  'No checks': 'yellow',
  'Checking mergeability': 'yellow',
  Ready: 'green',
  Merged: 'green',
}

const COLUMN_TONES: Record<string, Tone> = {
  Working: 'blue',
  'Needs you': 'orange',
  'In review': 'yellow',
  'Ready to merge': 'green',
}

/** The color of a status label; unknown labels use their column's color. Only the color is decided here: labels and columns always come from the API. */
export function labelTone(label: string, column: string): Tone {
  return LABEL_TONES[label] ?? columnTone(column)
}

export function columnTone(column: string): Tone {
  return COLUMN_TONES[column] ?? 'gray'
}

/** The PR's URL on GitHub; null when the repo is unknown or no PR is open yet. */
export function pullRequestUrl(project: Project | undefined, card: Card): string | null {
  if (!project?.github_repo || card.pr_number === null) return null
  return `https://github.com/${project.github_repo}/pull/${card.pr_number}`
}

/** Merge newly received events into the existing list: sorted by id, one copy per event. */
export function mergeEvents(existing: EventRow[], incoming: EventRow[]): EventRow[] {
  if (incoming.length === 0) return existing
  const byId = new Map(existing.map((event) => [event.id, event]))
  for (const event of incoming) byId.set(event.id, event)
  return [...byId.values()].sort((first, second) => first.id - second.id)
}

export type ArchiveOutcome = 'merged' | 'closed' | 'archived'

export const ARCHIVE_OUTCOME_LABELS: Record<ArchiveOutcome, string> = {
  merged: 'Merged',
  closed: 'PR closed',
  archived: 'Archived by you',
}

/** How an archived task ended: by its PR's result when the PR was merged or closed, else archived by you. */
export function archiveOutcome(card: Pick<Card, 'pr_state'>): ArchiveOutcome {
  if (card.pr_state === 'merged') return 'merged'
  if (card.pr_state === 'closed') return 'closed'
  return 'archived'
}

/** When an archived task finished: when it was archived, else when it last changed. */
export function finishedAt(card: Pick<Card, 'archived_at' | 'updated_at'>): string {
  return card.archived_at ?? card.updated_at
}

/** Whether a card matches an Archive search: title or branch contains the query, ignoring case. */
export function matchesSearch(card: Pick<Card, 'title' | 'branch'>, query: string): boolean {
  const needle = query.trim().toLowerCase()
  if (needle === '') return true
  return [card.title, card.branch ?? ''].some((value) => value.toLowerCase().includes(needle))
}

/** A preview of the branch name a new task gets; only an illustration, the daemon picks the real name. */
export function branchPreview(text: string): string {
  const slug = text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40)
    .replace(/-+$/, '')
  return slug ? `asv/task-…-${slug}` : 'asv/task-…'
}

/** Total time the agent ran across finished runs, e.g. `4m 12s`. */
export function totalRunTime(runs: Pick<Run, 'started_at' | 'ended_at'>[]): string {
  const seconds = runs.reduce((total, run) => {
    if (run.ended_at === null) return total
    return total + (new Date(run.ended_at).getTime() - new Date(run.started_at).getTime()) / 1000
  }, 0)
  const rounded = Math.round(seconds)
  if (rounded < 60) return `${rounded}s`
  if (rounded < 3600) return `${Math.floor(rounded / 60)}m ${rounded % 60}s`
  return `${Math.floor(rounded / 3600)}h ${Math.floor((rounded % 3600) / 60)}m`
}
