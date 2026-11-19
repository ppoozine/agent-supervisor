import { useEffect, useRef, useState } from 'react'

import { archiveTask, errorMessage, getTask, listEventsAfter, sendMessage, stopTask } from '../api'
import { formatCount, formatUsage, mergeEvents, pullRequestUrl } from '../format'
import type { Card, EventRow, Project, TaskDetail } from '../types'
import { EventsTab, LogTab, RunsTab } from './TaskTabs'
import { StatusLabel } from './StatusLabel'
import { agentName } from './TaskCard'

interface TaskPageProps {
  taskId: number
  /** 看板上這個任務最新的卡片；推送有更新時會換成新的物件。 */
  card: Card | undefined
  project: Project | undefined
  /** 連上 daemon 的次數。變了表示剛重連，要補抓斷線期間的資料。 */
  connectionCount: number
  subscribeToEvents: (listener: (event: EventRow) => void) => () => void
  /** 操作成功後回報任務最新的內容，讓看板上的卡片跟著更新。 */
  onTaskChanged: (task: TaskDetail) => void
}

const TABS = ['Log', 'Events', 'Runs'] as const
type Tab = (typeof TABS)[number]

/** 任務詳情頁：標題列、Log / Events / Runs 三個分頁、追加指令、Stop 與 Archive。 */
export function TaskPage(props: TaskPageProps) {
  const { taskId, card, project, connectionCount, subscribeToEvents, onTaskChanged } = props
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [events, setEvents] = useState<EventRow[]>([])
  const [tab, setTab] = useState<Tab>('Log')
  const [messageText, setMessageText] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busyAction, setBusyAction] = useState<string | null>(null)
  // 已經完整補抓到哪一筆。只有補抓成功才往前推；推送來的事件不算，
  // 否則補抓失敗時，中間那一段就再也不會補回來。
  const backfilledUpTo = useRef(0)

  const addEvents = (incoming: EventRow[]): void =>
    setEvents((current) => mergeEvents(current, incoming))

  // 卡片有變（例如 run 結束）就重新抓詳情，Runs 分頁才會跟上。
  useEffect(() => {
    let isCurrent = true
    getTask(taskId)
      .then((loaded) => isCurrent && setDetail(loaded))
      .catch((failure: unknown) => isCurrent && setError(errorMessage(failure)))
    return () => {
      isCurrent = false
    }
  }, [taskId, card, connectionCount])

  // 第一次開啟與每次重連：從已經有的最後一筆之後補抓。
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

  // 推送來的卡片比抓回來的詳情新，狀態以它為準。
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
            onClick={() => void perform('archive', () => archiveTask(taskId))}
            disabled={isArchived || busyAction !== null}
          >
            {busyAction === 'archive' ? 'Archiving…' : isArchived ? 'Archived' : 'Archive'}
          </button>
        </div>
      </div>
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
