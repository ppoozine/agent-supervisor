// Arrange a task's runs and events into the Chat timeline. Pure functions only.
// The layout and wording follow docs/design/ui.md ("Chat").
import type { EventRow, Run } from './types'

/** Who an entry came from. */
export type Source = 'you' | 'reviewer' | 'auto' | 'agent' | 'system'

/** The divider that opens each run. */
export interface RunItem {
  kind: 'run'
  key: string
  /** 1 for the task's first run, counting in start order. */
  number: number
  run: Run
  /** Why the run started, e.g. `Automatic: CI failed`. */
  reason: string
  source: 'you' | 'auto'
}

/** What a person typed: the task description or a follow-up message. */
export interface YouItem {
  kind: 'you'
  key: string
  ts: string
  text: string
}

/** Instructions agent-supervisor sent on its own (failed CI, a merge conflict, PR comments). */
export interface AutoItem {
  kind: 'auto'
  key: string
  ts: string
  /** One line saying why, e.g. `CI failed on 3f2a9c1e`. */
  reason: string
  /** Exactly what the agent was given. */
  text: string
}

/** A comment on the pull request, as found by polling. */
export interface ReviewerItem {
  kind: 'reviewer'
  key: string
  ts: string
  author: string
  /** `comment`, `review`, or `inline comment`. */
  commentKind: string
  /** `path:line` for inline comments; null otherwise. */
  where: string | null
  url: string | null
  body: string
  /** Why it was not passed to the agent; null when it was. */
  notSent: string | null
}

/** One thing the agent did inside its turn. */
export interface AgentAction {
  kind: 'action'
  key: string
  type: 'command' | 'tool' | 'thinking' | 'plan' | 'denied'
  /** The one-line summary that is always shown. */
  title: string
  /** Shown when the action is opened; empty when there is nothing more to show. */
  detail: string
  /** A short note on the right, e.g. `exit 1`. */
  note: string
  failed: boolean
}

export interface AgentMessage {
  kind: 'message'
  key: string
  text: string
}

/** A stretch of consecutive agent activity within one run. */
export interface AgentItem {
  kind: 'agent'
  key: string
  ts: string
  parts: (AgentMessage | AgentAction)[]
}

/** Something agent-supervisor, GitHub or the run itself reported, e.g. a push or a CI result. */
export interface SystemItem {
  kind: 'system'
  key: string
  ts: string
  symbol: string
  text: string
  url: string | null
  tone: 'neutral' | 'pass' | 'fail'
}

/** Shown at the bottom while the latest run is still going. */
export interface WorkingItem {
  kind: 'working'
  key: string
}

export type TimelineItem =
  | RunItem
  | YouItem
  | AutoItem
  | ReviewerItem
  | AgentItem
  | SystemItem
  | WorkingItem

const RUN_REASONS: Record<string, { reason: string; source: 'you' | 'auto' }> = {
  initial: { reason: 'Task opened', source: 'you' },
  user_message: { reason: 'Your message', source: 'you' },
  review_feedback: { reason: 'Automatic: PR comments', source: 'auto' },
  ci_failure: { reason: 'Automatic: CI failed', source: 'auto' },
  merge_conflict: { reason: 'Automatic: merge conflict', source: 'auto' },
}

const NOT_SENT: Record<string, string> = {
  bot: 'Not sent: posted by a bot',
  untrusted_author: 'Not sent: the author is not a collaborator',
  empty: 'Not sent: no text',
  not_a_change_request: 'Not sent: approval',
}

const COMMENT_KINDS: Record<string, string> = {
  comment: 'comment',
  review: 'review',
  inline_comment: 'inline comment',
}

const AGENT_EVENT_TYPES = new Set(['agent_message', 'command', 'tool_call', 'permission_denied'])

/** Longest one-line summary of a tool's input. */
const MAX_TITLE_CHARS = 120
/** Longest output kept for an opened action. */
const MAX_DETAIL_CHARS = 20_000

function text(value: unknown): string {
  if (typeof value === 'string') return value
  return value === null || value === undefined ? '' : JSON.stringify(value, null, 2)
}

function clip(value: string, limit: number): string {
  return value.length <= limit ? value : `${value.slice(0, limit)}…`
}

function shortSha(value: unknown): string {
  return text(value).slice(0, 8)
}

function firstLine(value: string): string {
  return value.split('\n', 1)[0]
}

/** The run's reason and who it counts as coming from; unknown triggers count as automatic. */
export function runReason(trigger: string): { reason: string; source: 'you' | 'auto' } {
  return RUN_REASONS[trigger] ?? { reason: `Automatic: ${trigger}`, source: 'auto' }
}

/** How a tool's input reads on one line: the file or command if there is one, otherwise the input itself. */
function summarizeInput(input: unknown): string {
  if (input !== null && typeof input === 'object') {
    const fields = input as Record<string, unknown>
    for (const name of ['file_path', 'path', 'command', 'pattern', 'url']) {
      if (typeof fields[name] === 'string') return fields[name] as string
    }
  }
  return clip(firstLine(text(input)).trim(), MAX_TITLE_CHARS)
}

function autoReason(run: Run): string {
  const sha = run.source_head_sha ? ` on ${shortSha(run.source_head_sha)}` : ''
  if (run.trigger === 'ci_failure') return `CI failed${sha}: sent the failing logs to the agent`
  if (run.trigger === 'merge_conflict') {
    return `Merge conflict with the base branch${sha}: asked the agent to resolve it`
  }
  if (run.trigger === 'review_feedback') return 'Sent new pull request comments to the agent'
  return `Started automatically (${run.trigger})`
}

function systemItem(event: EventRow): SystemItem | null {
  const { payload } = event
  const base = { kind: 'system' as const, key: `e${event.id}`, ts: event.ts, url: null }
  switch (event.type) {
    case 'pushed': {
      const replaced = payload.forced ? `, replacing ${shortSha(payload.replaced_sha)}` : ''
      return {
        ...base,
        symbol: '↑',
        text: `Pushed ${shortSha(payload.head_sha)} to ${text(payload.branch)}${replaced}`,
        tone: 'neutral',
      }
    }
    case 'pr_opened':
      return {
        ...base,
        symbol: '⎇',
        text: `Opened PR #${text(payload.number)}`,
        url: typeof payload.url === 'string' ? payload.url : null,
        tone: 'neutral',
      }
    case 'ci_status':
      return ciStatusItem(event)
    case 'state_change': {
      // A successful end is already on the run's divider; only say more when it went wrong.
      if (payload.change !== 'run_ended' || payload.exit_status === 'success') return null
      const detail = payload.detail ? `: ${firstLine(text(payload.detail))}` : ''
      return {
        ...base,
        symbol: payload.exit_status === 'stopped' ? '■' : '✗',
        text: `Run ended (${text(payload.exit_status)})${detail}`,
        tone: payload.exit_status === 'stopped' ? 'neutral' : 'fail',
      }
    }
    default:
      return null
  }
}

function ciStatusItem(event: EventRow): SystemItem | null {
  const { payload } = event
  const base = { kind: 'system' as const, key: `e${event.id}`, ts: event.ts, url: null }
  if (payload.pr_state === 'merged') return { ...base, symbol: '✓', text: 'PR merged', tone: 'pass' }
  if (payload.pr_state === 'closed') return { ...base, symbol: '■', text: 'PR closed', tone: 'neutral' }
  const sha = payload.head_sha ? ` on ${shortSha(payload.head_sha)}` : ''
  const parts: string[] = []
  let tone: SystemItem['tone'] = 'neutral'
  if (payload.ci_state === 'failed') {
    parts.push(`CI failed${sha}`)
    tone = 'fail'
  } else if (payload.ci_state === 'passed') {
    parts.push(`CI passed${sha}`)
    tone = 'pass'
  } else if (payload.ci_state === 'pending') {
    parts.push(`CI running${sha}`)
  }
  if (payload.review_state === 'approved') parts.push('review approved')
  if (payload.review_state === 'changes_requested') parts.push('changes requested')
  if (payload.merge_state === 'conflicting') {
    parts.push('merge conflict with the base branch')
    tone = 'fail'
  }
  if (parts.length === 0) return null
  const symbol = tone === 'fail' ? '✗' : tone === 'pass' ? '✓' : '◌'
  return { ...base, symbol, text: parts.join(' · '), tone }
}

function reviewerItem(event: EventRow): ReviewerItem {
  const { payload } = event
  const where = payload.path
    ? `${text(payload.path)}${payload.line ? `:${text(payload.line)}` : ''}`
    : null
  const body = text(payload.body).trim() || `(${text(payload.review_state) || 'no text'})`
  const ignored = typeof payload.ignored === 'string' ? payload.ignored : null
  return {
    kind: 'reviewer',
    key: `e${event.id}`,
    ts: event.ts,
    author: text(payload.author) || 'someone',
    commentKind: COMMENT_KINDS[text(payload.kind)] ?? text(payload.kind),
    where,
    url: typeof payload.url === 'string' ? payload.url : null,
    body,
    notSent: ignored === null ? null : (NOT_SENT[ignored] ?? `Not sent: ${ignored}`),
  }
}

/** Builds agent items, pairing each command or tool call's start with its finish. */
class AgentParts {
  private readonly byToolUse = new Map<string, AgentAction>()

  /** Add the event to `item`; returns false when it adds nothing visible. */
  add(item: AgentItem, event: EventRow): boolean {
    const { payload } = event
    const key = `e${event.id}`
    switch (event.type) {
      case 'agent_message': {
        const body = text(payload.text).trim()
        if (body === '') return false
        if (payload.kind === 'reasoning' || payload.kind === 'plan') {
          const isPlan = payload.kind === 'plan'
          item.parts.push({
            kind: 'action',
            key,
            type: isPlan ? 'plan' : 'thinking',
            title: isPlan ? 'Plan' : 'Thinking',
            detail: body,
            note: lineCount(body),
            failed: false,
          })
        } else {
          item.parts.push({ kind: 'message', key, text: body })
        }
        return true
      }
      case 'command':
        return this.addToolUse(item, event, 'command', `$ ${text(payload.command)}`)
      case 'tool_call':
        return this.addToolUse(
          item,
          event,
          'tool',
          `${text(payload.tool)} ${summarizeInput(payload.input ?? payload.item)}`.trim(),
        )
      case 'permission_denied':
        item.parts.push({
          kind: 'action',
          key,
          type: 'denied',
          title: `Denied: ${text(payload.tool)} ${summarizeInput(payload.input)}`.trim(),
          detail: text(payload.reason),
          note: 'blocked by policy',
          failed: true,
        })
        return true
      default:
        return false
    }
  }

  private addToolUse(
    item: AgentItem,
    event: EventRow,
    type: 'command' | 'tool',
    title: string,
  ): boolean {
    const { payload } = event
    const toolUseId = typeof payload.tool_use_id === 'string' ? payload.tool_use_id : null
    const known = toolUseId === null ? undefined : this.byToolUse.get(toolUseId)
    if (payload.phase !== 'finished' || known === undefined) {
      const action: AgentAction = {
        kind: 'action',
        key: `e${event.id}`,
        type,
        title: clip(title, MAX_TITLE_CHARS),
        detail: type === 'tool' ? text(payload.input ?? payload.item) : '',
        note: '',
        failed: false,
      }
      if (toolUseId !== null) this.byToolUse.set(toolUseId, action)
      item.parts.push(action)
      if (payload.phase !== 'finished') return true
      this.finish(action, event)
      return true
    }
    this.finish(known, event)
    return false
  }

  private finish(action: AgentAction, event: EventRow): void {
    const { payload } = event
    action.failed = payload.is_error === true
    if (action.type === 'command') {
      action.detail = clip(text(payload.output), MAX_DETAIL_CHARS)
      const exitCode = payload.exit_code
      action.note =
        typeof exitCode === 'number' && exitCode !== 0 ? `exit ${exitCode}` : action.failed ? 'failed' : ''
    } else if (action.failed) {
      action.note = 'failed'
    }
  }
}

function lineCount(value: string): string {
  const lines = value.split('\n').length
  return lines === 1 ? '1 line' : `${lines} lines`
}

interface Anchor {
  time: number
  /** Runs open before the events written at the same instant. */
  order: number
  id: number
  run?: Run
  event?: EventRow
}

/**
 * The Chat timeline: each run opens with a divider and what it was given (by you or
 * automatically), followed by what happened during it. Events outside any run, such as PR
 * comments and CI results, sit where they happened in time.
 */
export function buildTimeline(runs: Run[], events: EventRow[]): TimelineItem[] {
  const orderedRuns = [...runs].sort((first, second) => first.id - second.id)
  const numbers = new Map(orderedRuns.map((run, index) => [run.id, index + 1]))
  const anchors: Anchor[] = [
    ...orderedRuns.map((run) => ({ time: Date.parse(run.started_at), order: 0, id: run.id, run })),
    ...events.map((event) => ({ time: Date.parse(event.ts), order: 1, id: event.id, event })),
  ].sort((first, second) => first.time - second.time || first.order - second.order || first.id - second.id)

  const items: TimelineItem[] = []
  const agentParts = new AgentParts()
  for (const anchor of anchors) {
    if (anchor.run) {
      items.push(...runOpening(anchor.run, numbers.get(anchor.run.id) ?? 0))
      continue
    }
    const event = anchor.event as EventRow
    if (AGENT_EVENT_TYPES.has(event.type)) {
      const last = items[items.length - 1]
      const sameRun = last?.kind === 'agent' && last.key.startsWith(`r${event.run_id}:`)
      const item: AgentItem = sameRun
        ? (last as AgentItem)
        : { kind: 'agent', key: `r${event.run_id}:e${event.id}`, ts: event.ts, parts: [] }
      if (agentParts.add(item, event) && !sameRun) items.push(item)
      continue
    }
    if (event.type === 'review_comment') {
      items.push(reviewerItem(event))
      continue
    }
    const system = systemItem(event)
    if (system) items.push(system)
  }

  const latest = orderedRuns[orderedRuns.length - 1]
  if (latest && latest.ended_at === null) items.push({ kind: 'working', key: 'working' })
  return items
}

function runOpening(run: Run, number: number): TimelineItem[] {
  const { reason, source } = runReason(run.trigger)
  const opening: TimelineItem[] = [{ kind: 'run', key: `run${run.id}`, number, run, reason, source }]
  const input = (run.input_text ?? '').trim()
  if (input === '') return opening
  if (source === 'you') {
    opening.push({ kind: 'you', key: `in${run.id}`, ts: run.started_at, text: input })
  } else {
    opening.push({
      kind: 'auto',
      key: `in${run.id}`,
      ts: run.started_at,
      reason: autoReason(run),
      text: input,
    })
  }
  return opening
}

/** Who an event came from, by the same rules as the Chat: agent actions, reviewer comments, the start of a run by whoever triggered it, everything else the system. */
export function eventSource(event: EventRow, runsById: Map<number, Run>): Source {
  if (AGENT_EVENT_TYPES.has(event.type)) return 'agent'
  if (event.type === 'review_comment') return 'reviewer'
  if (event.type === 'session_started' && event.run_id !== null) {
    const run = runsById.get(event.run_id)
    if (run) return runReason(run.trigger).source
  }
  return 'system'
}

/** One line saying what an event was, e.g. `CI passed on f2e2268` or `$ npm test · exit 0`; never raw JSON. */
export function eventSummary(event: EventRow): string {
  const { payload } = event
  const failed = payload.is_error === true ? ' · failed' : ''
  switch (event.type) {
    case 'agent_message': {
      const body = firstLine(text(payload.text).trim())
      if (payload.kind === 'reasoning') return clip(`Thinking: ${body}`, MAX_TITLE_CHARS)
      if (payload.kind === 'plan') return clip(`Plan: ${body}`, MAX_TITLE_CHARS)
      return clip(body, MAX_TITLE_CHARS)
    }
    case 'command': {
      const command = clip(`$ ${firstLine(text(payload.command))}`, MAX_TITLE_CHARS)
      if (payload.phase !== 'finished') return command
      const exitCode = typeof payload.exit_code === 'number' ? ` · exit ${payload.exit_code}` : ''
      return `${command}${exitCode || failed}`
    }
    case 'tool_call': {
      const title = `${text(payload.tool)} ${summarizeInput(payload.input ?? payload.item)}`.trim()
      return `${clip(title, MAX_TITLE_CHARS)}${failed}`
    }
    case 'permission_denied': {
      const title = `Denied: ${text(payload.tool)} ${summarizeInput(payload.input)}`.trim()
      const reason = payload.reason ? ` · ${firstLine(text(payload.reason))}` : ''
      return clip(`${title}${reason}`, MAX_TITLE_CHARS)
    }
    case 'review_comment': {
      const item = reviewerItem(event)
      const where = item.where ? ` on ${item.where}` : ''
      return clip(`@${item.author}${where}: ${firstLine(item.body)}`, MAX_TITLE_CHARS)
    }
    case 'session_started':
      return payload.session_id ? `Session ${text(payload.session_id)}` : 'Session started'
    case 'state_change': {
      const ended = systemItem(event)
      if (ended) return ended.text
      const change = text(payload.change).replace(/_/g, ' ')
      const status = payload.exit_status ? ` (${text(payload.exit_status)})` : ''
      return `${change.charAt(0).toUpperCase()}${change.slice(1)}${status}`
    }
    default:
      return systemItem(event)?.text ?? event.type.replace(/_/g, ' ')
  }
}

/** Several of the agent's actions in a row, shown as one line that opens to list them. */
export interface ActionGroup {
  kind: 'actions'
  key: string
  actions: AgentAction[]
}

/** Fold runs of at least `minimum` consecutive actions into groups; messages stay as they are. */
export function groupParts(
  parts: AgentItem['parts'],
  minimum = 3,
): Array<AgentItem['parts'][number] | ActionGroup> {
  const grouped: Array<AgentItem['parts'][number] | ActionGroup> = []
  let pending: AgentAction[] = []
  const flush = (): void => {
    if (pending.length >= minimum) {
      grouped.push({ kind: 'actions', key: `group-${pending[0].key}`, actions: pending })
    } else grouped.push(...pending)
    pending = []
  }
  for (const part of parts) {
    if (part.kind === 'action') pending.push(part)
    else {
      flush()
      grouped.push(part)
    }
  }
  flush()
  return grouped
}

const ACTION_NOUNS: Record<AgentAction['type'], [string, string]> = {
  command: ['command', 'commands'],
  tool: ['tool call', 'tool calls'],
  thinking: ['thought', 'thoughts'],
  plan: ['plan', 'plans'],
  denied: ['denied action', 'denied actions'],
}

/** The one line a group shows, e.g. `23 actions · 18 commands, 5 tool calls`, and how many failed. */
export function actionSummary(actions: AgentAction[]): { text: string; failed: number } {
  const counts = new Map<AgentAction['type'], number>()
  for (const action of actions) counts.set(action.type, (counts.get(action.type) ?? 0) + 1)
  const kinds = [...counts].map(([type, count]) => `${count} ${ACTION_NOUNS[type][count === 1 ? 0 : 1]}`)
  return {
    text: `${actions.length} actions · ${kinds.join(', ')}`,
    failed: actions.filter((action) => action.failed).length,
  }
}

