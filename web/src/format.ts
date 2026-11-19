// 把資料排成畫面上的文字。全部是純函式。
import type { Card, EventRow, Project } from './types'

/** 花費或 token 數。agent 不回報花費時顯示 token 數；花費是估算值，前面加 `~`。 */
export function formatUsage(card: Pick<Card, 'cost_usd' | 'tokens' | 'usage_is_complete'>): string {
  const amount =
    card.cost_usd === null ? `${formatCount(card.tokens)} tok` : `~$${card.cost_usd.toFixed(2)}`
  // 有 run 的用量拿不到（例如被中止）時，實際的數字比這個高。
  return card.usage_is_complete ? amount : `${amount}+`
}

/** 把大的數字縮短：1234 變成 1.2k，1234567 變成 1.2M。 */
export function formatCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`
  return String(value)
}

/** 多久以前，例如 `8m ago`。 */
export function timeAgo(timestamp: string, now: Date = new Date()): string {
  const seconds = Math.max(0, Math.floor((now.getTime() - new Date(timestamp).getTime()) / 1000))
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

/** 當地時間的時分秒。 */
export function clockTime(timestamp: string): string {
  return new Date(timestamp).toLocaleTimeString('en-GB')
}

export type Tone = 'blue' | 'orange' | 'red' | 'yellow' | 'green' | 'gray'

const LABEL_TONES: Record<string, Tone> = {
  Working: 'blue',
  'Fixing CI': 'blue',
  'Addressing review': 'blue',
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
  Ready: 'green',
  Merged: 'green',
}

const COLUMN_TONES: Record<string, Tone> = {
  Working: 'blue',
  'Needs you': 'orange',
  'In review': 'yellow',
  'Ready to merge': 'green',
}

/** 狀態標籤用什麼顏色；不認得的標籤跟著欄位的顏色。只決定顏色，標籤與欄位本身一律來自 API。 */
export function labelTone(label: string, column: string): Tone {
  return LABEL_TONES[label] ?? columnTone(column)
}

export function columnTone(column: string): Tone {
  return COLUMN_TONES[column] ?? 'gray'
}

/** PR 在 GitHub 上的網址；不知道 repo 或還沒開 PR 時是 null。 */
export function pullRequestUrl(project: Project | undefined, card: Card): string | null {
  if (!project?.github_repo || card.pr_number === null) return null
  return `https://github.com/${project.github_repo}/pull/${card.pr_number}`
}

/** 把新收到的事件併進已有的清單：依編號排序，同一筆只留一份。 */
export function mergeEvents(existing: EventRow[], incoming: EventRow[]): EventRow[] {
  if (incoming.length === 0) return existing
  const byId = new Map(existing.map((event) => [event.id, event]))
  for (const event of incoming) byId.set(event.id, event)
  return [...byId.values()].sort((first, second) => first.id - second.id)
}

/** Log 分頁的一行。 */
export interface LogLine {
  id: number
  ts: string
  kind: 'message' | 'reasoning' | 'command' | 'output' | 'tool' | 'denied' | 'system' | 'error'
  text: string
}

const MAX_OUTPUT_CHARS = 2000

function text(value: unknown): string {
  if (typeof value === 'string') return value
  return value === null || value === undefined ? '' : JSON.stringify(value)
}

function clip(value: string): string {
  return value.length <= MAX_OUTPUT_CHARS ? value : `${value.slice(0, MAX_OUTPUT_CHARS)}\n…`
}

/** 把一筆事件變成 Log 分頁的一行；沒有東西可顯示的回傳 null。 */
export function toLogLine(event: EventRow): LogLine | null {
  const { payload } = event
  const line = (kind: LogLine['kind'], content: string): LogLine | null =>
    content.trim() === '' ? null : { id: event.id, ts: event.ts, kind, text: content }
  switch (event.type) {
    case 'agent_message':
      return line(payload.kind === 'reasoning' ? 'reasoning' : 'message', text(payload.text))
    case 'command':
      if (payload.phase === 'started') return line('command', `$ ${text(payload.command)}`)
      return line(payload.is_error ? 'error' : 'output', clip(text(payload.output)))
    case 'tool_call':
      if (payload.phase !== 'started') return null
      return line('tool', `${text(payload.tool)} ${clip(text(payload.input ?? payload.item ?? ''))}`)
    case 'permission_denied':
      return line('denied', `denied: ${text(payload.tool)} ${clip(text(payload.input))}`)
    case 'pushed':
      return line('system', `pushed ${text(payload.branch)} @ ${text(payload.head_sha).slice(0, 8)}`)
    case 'pr_opened':
      return line('system', `opened PR #${text(payload.number)}`)
    case 'ci_status':
      return line('system', `CI ${text(payload.ci_state)} · review ${text(payload.review_state)}`)
    case 'review_comment': {
      const where = payload.path ? ` on ${text(payload.path)}${payload.line ? `:${text(payload.line)}` : ''}` : ''
      const skipped = payload.ignored ? ` (not sent to the agent: ${text(payload.ignored)})` : ''
      const body = text(payload.body).trim() || `(${text(payload.review_state ?? 'no text')})`
      return line('system', `comment by ${text(payload.author)}${where}${skipped}: ${clip(body)}`)
    }
    case 'state_change': {
      const detail = payload.detail ? ` — ${text(payload.detail)}` : ''
      const outcome = payload.exit_status ? ` (${text(payload.exit_status)})` : ''
      return line('system', `${text(payload.change)}${outcome}${detail}`)
    }
    default:
      return null
  }
}
