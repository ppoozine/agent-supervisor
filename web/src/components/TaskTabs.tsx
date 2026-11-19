import { useEffect, useMemo, useRef, useState } from 'react'

import { clockTime, formatCount, toLogLine } from '../format'
import type { EventRow, Run } from '../types'

const NEAR_BOTTOM_PIXELS = 80

/** agent 的輸出，新的在下面。停在底部時會跟著捲動；往上看舊內容時不會被拉走。 */
export function LogTab({ events, prompt }: { events: EventRow[]; prompt: string }) {
  const lines = useMemo(
    () => events.map(toLogLine).filter((line) => line !== null),
    [events],
  )
  const container = useRef<HTMLDivElement>(null)
  const isFollowing = useRef(true)

  useEffect(() => {
    const element = container.current
    if (element && isFollowing.current) element.scrollTop = element.scrollHeight
  }, [lines.length])

  return (
    <div
      className="log"
      ref={container}
      onScroll={(event) => {
        const element = event.currentTarget
        isFollowing.current =
          element.scrollHeight - element.scrollTop - element.clientHeight < NEAR_BOTTOM_PIXELS
      }}
    >
      <div className="log-line log-prompt">
        <span className="log-time">task</span>
        <pre>{prompt}</pre>
      </div>
      {lines.map((line) => (
        <div className={`log-line log-${line.kind}`} key={line.id}>
          <span className="log-time">{clockTime(line.ts)}</span>
          <pre>{line.text}</pre>
        </div>
      ))}
      {lines.length === 0 && <p className="empty">還沒有輸出。</p>}
    </div>
  )
}

/** 稽核紀錄，依寫入順序，可依類型篩選。 */
export function EventsTab({ events }: { events: EventRow[] }) {
  const [type, setType] = useState('')
  const types = useMemo(() => [...new Set(events.map((event) => event.type))].sort(), [events])
  const shown = type === '' ? events : events.filter((event) => event.type === type)

  return (
    <div className="events">
      <label className="filter">
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
                <code>{event.type}</code>
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

function duration(run: Run): string {
  if (run.ended_at === null) return 'running'
  const seconds = Math.round(
    (new Date(run.ended_at).getTime() - new Date(run.started_at).getTime()) / 1000,
  )
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`
}

/** 每一次 run 的觸發原因、時間、結果與用量。 */
export function RunsTab({ runs }: { runs: Run[] }) {
  return (
    <table className="runs">
      <thead>
        <tr>
          <th>Run</th>
          <th>Trigger</th>
          <th>Started</th>
          <th>Duration</th>
          <th>Result</th>
          <th>Tokens in / out</th>
          <th>Cost</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => (
          <tr key={run.id}>
            <td>{run.id}</td>
            <td>
              <code>{run.trigger}</code>
              {run.trigger === 'user_message' && run.input_text && (
                <div className="muted run-input">{run.input_text}</div>
              )}
            </td>
            <td>{new Date(run.started_at).toLocaleString('en-GB')}</td>
            <td>{duration(run)}</td>
            <td>{run.exit_status ?? 'running'}</td>
            <td>
              {run.tokens_in === null ? '–' : formatCount(run.tokens_in)} /{' '}
              {run.tokens_out === null ? '–' : formatCount(run.tokens_out)}
            </td>
            <td>{run.cost_usd === null ? '–' : `~$${run.cost_usd.toFixed(2)}`}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
