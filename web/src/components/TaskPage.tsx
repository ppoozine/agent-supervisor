import { useEffect, useRef, useState, type CSSProperties } from 'react'

import { archiveTask, errorMessage, getTask, listEventsAfter, sendMessage, stopTask } from '../api'
import { formatCount, formatUsage, mergeEvents, pullRequestUrl } from '../format'
import { currentSubmitShortcutLabel, submitOnShortcut } from '../keys'
import type { Card, EventRow, Project, TaskDetail } from '../types'
import { Dialog } from './Dialog'
import { DiffTab } from './DiffTab'
import { ChatTab } from './ChatTab'
import { EventsTab, RunsTab } from './TaskTabs'
import { StatusLabel } from './StatusLabel'
import { Attempts, Checks, useAgentName } from './TaskCard'
import { readPreference, savePreference } from '../preferences'

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

const TABS = ['Chat', 'Diff', 'Shell', 'Events', 'Runs'] as const
type Tab = (typeof TABS)[number]

/** Task detail page: header, the Chat / Diff / Events / Runs tabs, follow-up messages, Stop and Archive. */
export function TaskPage(props: TaskPageProps) {
  const { taskId, card, project, connectionCount, subscribeToEvents, onTaskChanged } = props
  const agentName = useAgentName()
  const shortcutLabel = currentSubmitShortcutLabel()
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [events, setEvents] = useState<EventRow[]>([])
  const [tab, setTab] = useState<Tab>('Chat')
  const [panel, setPanel] = useState<Tab>(() => {
    const saved = readPreference('task-panel')
    return saved && TABS.includes(saved as Tab) && saved !== 'Chat' ? (saved as Tab) : 'Diff'
  })
  const [wide, setWide] = useState(
    () => window.matchMedia?.('(min-width: 1100px)').matches ?? false,
  )
  const [panelWidth, setPanelWidth] = useState(42)
  const workspace = useRef<HTMLDivElement>(null)
  const [jump, setJump] = useState<{ number: number } | null>(null)
  const [copyStatus, setCopyStatus] = useState('')
  useEffect(() => {
    const query = window.matchMedia?.('(min-width: 1100px)')
    if (!query) return
    const update = (): void => setWide(query.matches)
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])
  const selectTab = (name: Tab): void => {
    setTab(name)
    if (name !== 'Chat') {
      setPanel(name)
      savePreference('task-panel', name)
    }
  }
  const resizePanel = (clientX: number): void => {
    const rect = workspace.current?.getBoundingClientRect()
    if (rect?.width)
      setPanelWidth(Math.max(30, Math.min(65, ((rect.right - clientX) / rect.width) * 100)))
  }
  const selected = wide ? panel : tab
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
      <div className="skeleton" aria-busy="true">
        <span className="sr-only">Loading…</span>
        <span className="w-1/3" />
        <span className="w-2/3" />
        <span className="w-1/2" />
      </div>
    )
  }
  const prUrl = pullRequestUrl(project, current)
  const isArchived = current.archived_at !== null
  const hasOpenPullRequest = current.pr_number !== null && current.pr_state === 'open'
  // A closed PR may have been reopened on GitHub, which the daemon checks when a message is sent,
  // so only archived tasks and merged PRs rule out messages here.
  const closedReason = isArchived
    ? 'This task is archived, so it cannot run again.'
    : current.pr_state === 'merged'
      ? 'The pull request was merged, so this task cannot run again.'
      : null
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
      <header className="task-header">
        <div className="task-title-row">
          <a className="crumb" href="#/">
            Board <span aria-hidden="true">/</span>
          </a>
          <h1 className="task-title">{current.title}</h1>
          <StatusLabel
            label={current.label}
            column={current.column}
            active={current.has_open_run}
          />
          <div className="task-actions">
            <button
              className="btn"
              onClick={requestArchive}
              disabled={isArchived || busyAction !== null}
            >
              {busyAction === 'archive' ? 'Archiving…' : isArchived ? 'Archived' : 'Archive'}
            </button>
            <button
              className="btn btn-danger"
              onClick={() => void perform('stop', () => stopTask(taskId))}
              disabled={!current.has_open_run || busyAction !== null}
              title={current.has_open_run ? undefined : 'No run is active'}
            >
              {busyAction === 'stop' ? 'Stopping…' : 'Stop'}
            </button>
          </div>
        </div>
        <div className="task-meta">
          <span>
            {project?.name} · {agentName(current.agent_type)}
          </span>
          {current.branch && (
            <span className="inline-flex min-w-0 items-center gap-1">
              <span className="branch">{current.branch}</span>
              <button
                className="ghost px-1 py-0"
                aria-label="Copy branch name"
                title="Copy branch name"
                onClick={() => {
                  void (async () => {
                    try {
                      await navigator.clipboard.writeText(current.branch!)
                      setCopyStatus('Branch copied')
                    } catch {
                      setCopyStatus('Could not copy branch name')
                    }
                  })()
                }}
              >
                ⧉
              </button>
              <span role="status" className="text-xs">
                {copyStatus}
              </span>
            </span>
          )}
          {current.pr_number !== null && (
            <span>
              {prUrl ? (
                <a href={prUrl} target="_blank" rel="noreferrer">
                  PR #{current.pr_number}
                </a>
              ) : (
                `PR #${current.pr_number}`
              )}
              {current.pr_state !== 'open' && current.pr_state && ` ${current.pr_state}`}
            </span>
          )}
          <Checks card={current} detailed />
          <Attempts card={current} always />
          <span
            title={`All runs: ${formatCount(detail.tokens_in)} in · ${formatCount(detail.tokens_out)} out. Cost is an estimate; agents that do not report cost show tokens only`}
          >
            {formatUsage(current)}
            {current.cost_usd !== null && ` · ${formatCount(current.tokens)} tok`}
          </span>
        </div>
      </header>
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
            PR #{current.pr_number} is still open. After archiving, CI failures are no longer fixed
            automatically and you cannot send messages. Archiving cannot be undone.
          </p>
          <p>Merge or close the PR on GitHub.</p>
        </Dialog>
      )}
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      <div
        className="task-workspace"
        ref={workspace}
        style={{ '--panel-width': `${panelWidth}%` } as CSSProperties}
      >
        {!wide && (
          <div className="tabs page-tabs" role="tablist">
            {TABS.map((name) => (
              <button
                key={name}
                role="tab"
                aria-selected={tab === name}
                className={tab === name ? 'tab is-active' : 'tab'}
                onClick={() => selectTab(name)}
              >
                {name}
                {name === 'Runs' && <span className="count">{detail.runs.length}</span>}
                {name === 'Events' && (
                  <span className="count" aria-hidden="true">
                    {events.length}
                  </span>
                )}
              </button>
            ))}
          </div>
        )}
        <section className="chat-pane" data-visible={wide || tab === 'Chat'}>
          <div className="tab-body">
            <ChatTab
              runs={detail.runs}
              events={events}
              agentName={agentName(current.agent_type)}
              jump={jump}
              visible={wide || tab === 'Chat'}
            />
          </div>
          {closedReason !== null ? (
            <p className="composer-closed">{closedReason}</p>
          ) : (
            <form
              className="composer"
              onSubmit={(event) => {
                event.preventDefault()
                void send()
              }}
            >
              <div className="composer-box">
                <textarea
                  value={messageText}
                  onChange={(event) => setMessageText(event.target.value)}
                  onKeyDown={submitOnShortcut}
                  placeholder="Send a follow-up to the same session…"
                  rows={2}
                  aria-label="Message"
                  aria-keyshortcuts="Meta+Enter Control+Enter"
                />
                <div className="composer-foot">
                  <span>
                    {current.has_open_run
                      ? 'The agent is working. Stop it to send a message now.'
                      : 'Continues the same agent session.'}
                  </span>
                  <button
                    type="submit"
                    className="primary"
                    disabled={
                      messageText.trim() === '' || busyAction !== null || current.has_open_run
                    }
                  >
                    {busyAction === 'send' ? 'Sending…' : 'Send'}
                    <kbd className="key-hint" aria-hidden="true">
                      {shortcutLabel}
                    </kbd>
                  </button>
                </div>
              </div>
            </form>
          )}
        </section>
        <div
          className="panel-resizer"
          role="separator"
          aria-label="Resize detail panel"
          aria-orientation="vertical"
          aria-valuemin={30}
          aria-valuemax={65}
          aria-valuenow={Math.round(panelWidth)}
          tabIndex={0}
          onPointerDown={(event) => event.currentTarget.setPointerCapture(event.pointerId)}
          onPointerMove={(event) => {
            if (event.currentTarget.hasPointerCapture(event.pointerId)) resizePanel(event.clientX)
          }}
          onPointerUp={(event) => event.currentTarget.releasePointerCapture(event.pointerId)}
          onKeyDown={(event) => {
            if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
              event.preventDefault()
              setPanelWidth((width) =>
                Math.max(30, Math.min(65, width + (event.key === 'ArrowLeft' ? 2 : -2))),
              )
            }
          }}
        />
        <section
          className="detail-panel"
          data-visible={wide || tab !== 'Chat'}
          aria-label="Details"
        >
          {wide && (
            <div className="tabs panel-tabs" role="tablist">
              {TABS.filter((name) => name !== 'Chat').map((name) => (
                <button
                  key={name}
                  role="tab"
                  aria-selected={panel === name}
                  className={panel === name ? 'tab is-active' : 'tab'}
                  onClick={() => selectTab(name)}
                >
                  {name}
                  {name === 'Runs' && <span className="count">{detail.runs.length}</span>}
                  {name === 'Events' && (
                    <span className="count" aria-hidden="true">
                      {events.length}
                    </span>
                  )}
                </button>
              ))}
            </div>
          )}
          <div className="tab-body">
            {selected === 'Diff' && (
              <DiffTab
                taskId={taskId}
                headSha={current.head_sha}
                connectionCount={connectionCount}
              />
            )}
            {selected === 'Shell' && (
              <p className="shell-placeholder">
                {isArchived
                  ? 'The worktree was removed when the task was archived.'
                  : "A terminal in this task's worktree. Coming in 0.4."}
              </p>
            )}
            {selected === 'Events' && <EventsTab events={events} runs={detail.runs} />}
            {selected === 'Runs' && (
              <RunsTab
                runs={detail.runs}
                events={events}
                card={current}
                onSelectRun={(number) => {
                  setTab('Chat')
                  setJump({ number })
                }}
              />
            )}
          </div>
        </section>
      </div>
    </div>
  )
}
