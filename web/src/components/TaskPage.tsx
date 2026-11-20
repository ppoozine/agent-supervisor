import { useEffect, useRef, useState } from 'react'

import { archiveTask, errorMessage, getTask, listEventsAfter, sendMessage, stopTask } from '../api'
import { formatCount, formatUsage, mergeEvents, pullRequestUrl } from '../format'
import type { Card, EventRow, Project, TaskDetail } from '../types'
import { Dialog } from './Dialog'
import { DiffTab } from './DiffTab'
import { EventsTab, LogTab, RunsTab } from './TaskTabs'
import { StatusLabel } from './StatusLabel'
import { agentName } from './TaskCard'

interface TaskPageProps {
  taskId: number
  /** This task's latest card on the board; replaced with a new object when a push updates it. */
  card: Card | undefined
  project: Project | undefined
  /** How many times the daemon was connected. A change means a reconnect: catch up on what was missed. */
  connectionCount: number
  subscribeToEvents: (listener: (event: EventRow) => void) => () => void
  /** Report the task's latest state after a successful action, so its card on the board updates. */
  onTaskChanged: (task: TaskDetail) => void
}

const TABS = ['Log', 'Diff', 'Events', 'Runs'] as const
type Tab = (typeof TABS)[number]

/** Task detail page: header, the Log / Diff / Events / Runs tabs, follow-up messages, Stop and Archive. */
export function TaskPage(props: TaskPageProps) {
  const { taskId, card, project, connectionCount, subscribeToEvents, onTaskChanged } = props
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [events, setEvents] = useState<EventRow[]>([])
  const [tab, setTab] = useState<Tab>('Log')
  const [messageText, setMessageText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  const [isConfirmingArchive, setIsConfirmingArchive] = useState(false)
  // The last event up to which catching up is complete. Only a successful catch-up moves it
  // forward; pushed events do not count, otherwise a failed catch-up would leave a gap that is
  // never filled.
  const backfilledUpTo = useRef(0)

  const addEvents = (incoming: EventRow[]): void =>
    setEvents((current) => mergeEvents(current, incoming))

  // Refetch the details when the card changes (e.g. a run ends) so the Runs tab keeps up.
  useEffect(() => {
    let isCurrent = true
    getTask(taskId)
      .then((loaded) => isCurrent && setDetail(loaded))
      .catch((failure: unknown) => isCurrent && setError(errorMessage(failure)))
    return () => {
      isCurrent = false
    }
  }, [taskId, card, connectionCount])

  // On first open and every reconnect: catch up from after the last event we already have.
  useEffect(() => {
    let isCurrent = true
    listEventsAfter(taskId, backfilledUpTo.current)
      .then((loaded) => {
        if (!isCurrent) return
        if (loaded.length > 0) backfilledUpTo.current = loaded[loaded.length - 1].id
        addEvents(loaded)
      })
      .catch((failure: unknown) => isCurrent && setError(errorMessage(failure)))
    return () => {
      isCurrent = false
    }
  }, [taskId, connectionCount])

  useEffect(
    () => subscribeToEvents((event) => event.task_id === taskId && addEvents([event])),
    [taskId, subscribeToEvents],
  )

  const perform = async (name: string, action: () => Promise<TaskDetail>): Promise<boolean> => {
    setBusyAction(name)
    setError(null)
    try {
      const updated = await action()
      setDetail(updated)
      onTaskChanged(updated)
      return true
    } catch (failure) {
      setError(errorMessage(failure))
      return false
    } finally {
      setBusyAction(null)
    }
  }

  const send = async (): Promise<void> => {
    if (await perform('send', () => sendMessage(taskId, messageText.trim()))) setMessageText('')
  }

  // The pushed card is newer than the fetched details, so its state wins.
  const current: Card | null = card ?? detail
  if (current === null || detail === null) {
    return error ? (
      <div className="alert" role="alert">
        {error}
      </div>
    ) : (
      <p className="empty">Loading…</p>
    )
  }
  const prUrl = pullRequestUrl(project, current)
  const isArchived = current.archived_at !== null
  const hasOpenPullRequest = current.pr_number !== null && current.pr_state === 'open'
  const archive = async (): Promise<void> => {
    await perform('archive', () => archiveTask(taskId))
    setIsConfirmingArchive(false)
  }
  // Archiving does not close the PR and cannot be undone, so ask first while the PR is open.
  const requestArchive = (): void => {
    if (hasOpenPullRequest) setIsConfirmingArchive(true)
    else void archive()
  }

  return (
    <div className="task-page">
      <div className="task-header">
        <div className="task-heading">
          <h2>{current.title}</h2>
          <div className="task-meta">
            <StatusLabel label={current.label} column={current.column} />
            <span className="muted">{current.column}</span>
            <span className={`agent agent-${current.agent_type}`}>{agentName(current.agent_type)}</span>
            {current.branch && <span className="branch">{current.branch}</span>}
            {current.pr_number !== null &&
              (prUrl ? (
                <a href={prUrl} target="_blank" rel="noreferrer">
                  PR #{current.pr_number} {current.pr_state}
                </a>
              ) : (
                <span>
                  PR #{current.pr_number} {current.pr_state}
                </span>
              ))}
            <span className="muted" title="花費是估算值；這種 agent 不回報花費時只有 token 數">
              {formatUsage(current)}
              {current.cost_usd !== null && ` · ${formatCount(current.tokens)} tok`}
              {` (in ${formatCount(detail.tokens_in)} / out ${formatCount(detail.tokens_out)})`}
            </span>
          </div>
        </div>
        <div className="task-actions">
          <button
            onClick={() => void perform('stop', () => stopTask(taskId))}
            disabled={!current.has_open_run || busyAction !== null}
            title={current.has_open_run ? undefined : '沒有 run 在執行'}
          >
            {busyAction === 'stop' ? 'Stopping…' : 'Stop'}
          </button>
          <button
            onClick={requestArchive}
            disabled={isArchived || busyAction !== null}
          >
            {busyAction === 'archive' ? 'Archiving…' : isArchived ? 'Archived' : 'Archive'}
          </button>
        </div>
      </div>
      {isConfirmingArchive && (
        <Dialog
          title="Archive this task?"
          submitLabel="Archive"
          isBusy={busyAction === 'archive'}
          error={null}
          onSubmit={() => void archive()}
          onClose={() => setIsConfirmingArchive(false)}
        >
          <p>
            PR #{current.pr_number} is still open. After archiving, CI failures are no longer
            fixed automatically and you cannot send messages. Archiving cannot be undone.
          </p>
          <p>Merge or close the PR on GitHub.</p>
        </Dialog>
      )}
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      <div className="tabs" role="tablist">
        {TABS.map((name) => (
          <button
            key={name}
            role="tab"
            aria-selected={tab === name}
            className={tab === name ? 'tab is-active' : 'tab'}
            onClick={() => setTab(name)}
          >
            {name}
            {name === 'Runs' && <span className="count">{detail.runs.length}</span>}
          </button>
        ))}
      </div>
      <div className="tab-body">
        {tab === 'Log' && <LogTab events={events} prompt={detail.prompt} />}
        {tab === 'Diff' && (
          <DiffTab taskId={taskId} headSha={current.head_sha} connectionCount={connectionCount} />
        )}
        {tab === 'Events' && <EventsTab events={events} />}
        {tab === 'Runs' && <RunsTab runs={detail.runs} />}
      </div>
      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault()
          void send()
        }}
      >
        <textarea
          value={messageText}
          onChange={(event) => setMessageText(event.target.value)}
          placeholder="對同一個 session 追加指令…"
          rows={2}
          aria-label="Message"
        />
        <button
          type="submit"
          className="primary"
          disabled={messageText.trim() === '' || busyAction !== null}
        >
          {busyAction === 'send' ? 'Sending…' : 'Send'}
        </button>
      </form>
    </div>
  )
}
