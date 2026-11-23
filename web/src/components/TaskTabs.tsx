import { useMemo, useState } from 'react'

import {
  clockTime,
  formatCount,
  formatUsage,
  runDuration,
  runOutcome,
  runUsage,
  totalRunTime,
} from '../format'
import { eventSource, eventSummary, runReason, type Source } from '../timeline'
import type { Card, EventRow, Run } from '../types'

const SOURCE_FILTERS: { source: Source | null; label: string }[] = [
  { source: null, label: 'All' },
  { source: 'agent', label: 'Agent' },
  { source: 'you', label: 'You' },
  { source: 'reviewer', label: 'Reviewer' },
  { source: 'auto', label: 'Automatic' },
  { source: 'system', label: 'System' },
]

/** Run numbers as Chat shows them: the first run of the task is Run 1. */
function runNumbers(runs: Run[]): Map<number, number> {
  return new Map(
    [...runs].sort((first, second) => first.id - second.id).map((run, index) => [run.id, index + 1]),
  )
}

/** The audit log, newest first, filterable by source, type and run; a row opens to its payload. */
export function EventsTab({ events, runs = [] }: { events: EventRow[]; runs?: Run[] }) {
  const [source, setSource] = useState<Source | null>(null)
  const [type, setType] = useState('')
  const [runId, setRunId] = useState('')
  const [openId, setOpenId] = useState<number | null>(null)
  const [copied, setCopied] = useState('')
  const numbers = useMemo(() => runNumbers(runs), [runs])
  const sources = useMemo(() => {
    const runsById = new Map(runs.map((run) => [run.id, run]))
    return new Map(events.map((event) => [event.id, eventSource(event, runsById)]))
  }, [events, runs])
  const types = useMemo(() => [...new Set(events.map((event) => event.type))].sort(), [events])
  const shown = events
    .filter((event) => source === null || sources.get(event.id) === source)
    .filter((event) => type === '' || event.type === type)
    .filter((event) => runId === '' || String(event.run_id) === runId)
    .reverse()

  const copy = async (event: EventRow): Promise<void> => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(event.payload, null, 2))
      setCopied('Copied')
    } catch {
      setCopied('Could not copy')
    }
  }

  return (
    <div className="panel-section">
      <div className="filters" role="group" aria-label="Source">
        {SOURCE_FILTERS.map((filter) => (
          <button
            key={filter.label}
            className="pill"
            aria-pressed={source === filter.source}
            onClick={() => setSource(filter.source)}
          >
            {filter.label}
          </button>
        ))}
      </div>
      <div className="filters text-xs text-text-muted">
        <label className="flex items-center gap-1.5">
          Type
          <select className="select" value={type} onChange={(event) => setType(event.target.value)}>
            <option value="">All types</option>
            {types.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1.5">
          Run
          <select className="select" value={runId} onChange={(event) => setRunId(event.target.value)}>
            <option value="">All runs</option>
            {[...numbers].map(([id, number]) => (
              <option key={id} value={String(id)}>
                Run {number}
              </option>
            ))}
          </select>
        </label>
        <span className="ml-auto">
          {shown.length} of {events.length}
        </span>
      </div>
      {shown.length === 0 ? (
        <p className="muted text-xs">No events match.</p>
      ) : (
        <ul className="event-list" aria-label="Events">
          {shown.map((event) => {
            const isOpen = openId === event.id
            return (
              <li key={event.id}>
                <button
                  className="event-row"
                  aria-expanded={isOpen}
                  onClick={() => {
                    setOpenId(isOpen ? null : event.id)
                    setCopied('')
                  }}
                >
                  <span className="event-time">{clockTime(event.ts)}</span>
                  <span
                    className={`event-type source-${sources.get(event.id)}${event.type === 'permission_denied' ? ' is-denied' : ''}`}
                  >
                    {event.type}
                  </span>
                  <span className="event-summary">{eventSummary(event)}</span>
                </button>
                {isOpen && (
                  <div className="event-detail">
                    <div className="event-detail-head">
                      <code className="text-text">{event.type}</code>
                      {event.run_id !== null && <span>Run {numbers.get(event.run_id) ?? '?'}</span>}
                      <span>{new Date(event.ts).toLocaleString('en-GB')}</span>
                      <span role="status" className="ml-auto">
                        {copied}
                      </span>
                      <button className="btn h-6 px-2 text-xs" onClick={() => void copy(event)}>
                        Copy JSON
                      </button>
                    </div>
                    <pre>{JSON.stringify(event.payload, null, 2)}</pre>
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

const RESULT_TONES: Record<string, string> = {
  success: 'text-success',
  error: 'text-danger',
}

/** The commit a run worked from or pushed, from the run itself or its `pushed` event. */
function runCommit(run: Run, events: EventRow[]): string | null {
  const pushed = events.find((event) => event.type === 'pushed' && event.run_id === run.id)
  const sha = pushed?.payload.head_sha ?? run.source_head_sha
  return typeof sha === 'string' ? sha.slice(0, 7) : null
}

interface RunsTabProps {
  runs: Run[]
  events?: EventRow[]
  card?: Card
  onSelectRun?: (number: number) => void
}

/** Totals for the task, then each run newest first; selecting one jumps to it in Chat. */
export function RunsTab({ runs, events = [], card, onSelectRun }: RunsTabProps) {
  const numbered = runs.map((run, index) => ({ run, number: index + 1 })).reverse()
  return (
    <div className="panel-section">
      {card && (
        <div className="stats">
          <Stat label="Tokens" value={formatCount(card.tokens)} title={formatUsage(card)} />
          <Stat label="Agent time" value={totalRunTime(runs)} />
          <Stat
            label="Automatic fixes"
            value={`${card.ci_fix_attempts}/${card.max_ci_fix_attempts}`}
          />
          <Stat
            label="Comment rounds"
            value={`${card.feedback_attempts}/${card.max_feedback_attempts}`}
          />
        </div>
      )}
      <p className="text-xs text-text-muted">Select a run to jump to it in Chat.</p>
      {numbered.map(({ run, number }) => {
        const { reason, source } = runReason(run.trigger)
        const usage = runUsage(run)
        const commit = runCommit(run, events)
        return (
          <button
            key={run.id}
            className="run-card"
            title={run.trigger === 'user_message' ? (run.input_text ?? undefined) : undefined}
            onClick={() => onSelectRun?.(number)}
          >
            <span className="run-card-head">
              <span className="font-semibold">Run {number}</span>
              <span className={`dot source-${source}`} />
              <span className={`toned source-${source}`}>{reason}</span>
              <span
                className={`ml-auto ${run.exit_status === null ? 'text-working-text' : (RESULT_TONES[run.exit_status] ?? 'text-needs-you-text')}`}
              >
                {run.exit_status === null ? '● Running…' : runOutcome(run)}
              </span>
            </span>
            <span className="run-card-meta">
              <span>{new Date(run.started_at).toLocaleString('en-GB')}</span>
              <span>{runDuration(run)}</span>
              {usage && <span>{usage}</span>}
              <span className="font-mono">
                {commit ?? (run.exit_status === null ? '' : 'no changes')}
              </span>
            </span>
          </button>
        )
      })}
    </div>
  )
}

function Stat({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className="stat" title={title}>
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
    </div>
  )
}
