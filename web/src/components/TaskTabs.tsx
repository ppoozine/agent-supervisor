import { useMemo, useState } from 'react'

import { clockTime, formatCount, runDuration, runOutcome } from '../format'
import { runReason } from '../timeline'
import type { EventRow, Run } from '../types'

/** The audit log in write order, filterable by type. */
export function EventsTab({ events }: { events: EventRow[] }) {
  const [type, setType] = useState('')
  const types = useMemo(() => [...new Set(events.map((event) => event.type))].sort(), [events])
  const shown = type === '' ? events : events.filter((event) => event.type === type)

  return (
    <div className="events">
      <label className="events-filter">
        Type
        <select value={type} onChange={(event) => setType(event.target.value)}>
          <option value="">All ({events.length})</option>
          {types.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </label>
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Time</th>
            <th>Run</th>
            <th>Type</th>
            <th>Payload</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((event) => (
            <tr key={event.id}>
              <td>{event.id}</td>
              <td>{clockTime(event.ts)}</td>
              <td>{event.run_id ?? '–'}</td>
              <td>
                <code className={eventColor(event.type)}>{event.type}</code>
              </td>
              <td>
                <pre>{JSON.stringify(event.payload)}</pre>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/** Each run's trigger, timing, outcome and usage. */
export function RunsTab({
  runs,
  onSelectRun,
}: {
  runs: Run[]
  onSelectRun?: (number: number) => void
}) {
  return (
    <table className="runs">
      <thead>
        <tr>
          <th>Run</th>
          <th>Why</th>
          <th>Started</th>
          <th>Duration</th>
          <th>Result</th>
          <th>Tokens in / out</th>
          <th>Cost</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run, index) => {
          const { reason, source } = runReason(run.trigger)
          return (
            <tr key={run.id} onClick={() => onSelectRun?.(index + 1)}>
              <td>
                <button
                  className="text-accent"
                  aria-label={`Go to Run #${index + 1}`}
                  onClick={(event) => {
                    event.stopPropagation()
                    onSelectRun?.(index + 1)
                  }}
                >
                  #{index + 1}
                </button>
              </td>
              <td>
                <span className={`source-${source}`}>{reason}</span>
                {run.trigger === 'user_message' && run.input_text && (
                  <div className="muted run-input">{run.input_text}</div>
                )}
              </td>
              <td>{new Date(run.started_at).toLocaleString('en-GB')}</td>
              <td>{runDuration(run)}</td>
              <td>{runOutcome(run)}</td>
              <td>
                {run.tokens_in === null ? '–' : formatCount(run.tokens_in)} /{' '}
                {run.tokens_out === null ? '–' : formatCount(run.tokens_out)}
              </td>
              <td>{run.cost_usd === null ? '–' : `~$${run.cost_usd.toFixed(2)}`}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function eventColor(type: string): string {
  if (type === 'review_comment') return 'text-source-reviewer'
  if (type === 'permission_denied') return 'text-needs-you'
  if (['agent_message', 'command', 'tool_call'].includes(type)) return 'text-source-agent'
  return 'text-source-system'
}
