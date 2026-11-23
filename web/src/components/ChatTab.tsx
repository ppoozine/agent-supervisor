import { useEffect, useMemo, useRef, useState } from 'react'

import { clockTime, runDuration, runOutcome, runUsage } from '../format'
import {
  buildTimeline,
  type AgentAction,
  type AgentItem,
  type AutoItem,
  type ReviewerItem,
  type RunItem,
  type SystemItem,
  type TimelineItem,
} from '../timeline'
import type { EventRow, Run } from '../types'

const NEAR_BOTTOM_PIXELS = 80

interface ChatTabProps {
  runs: Run[]
  events: EventRow[]
  agentName: string
  visible?: boolean
  jump?: { number: number } | null
}

/**
 * The task as a conversation: each run, who started it (you or agent-supervisor), what the agent
 * did, and what happened on GitHub. Follows new activity while scrolled to the bottom; while
 * reading older entries, a button offers to jump to the new ones instead.
 */
export function ChatTab({ runs, events, agentName, jump, visible = true }: ChatTabProps) {
  const items = useMemo(() => buildTimeline(runs, events), [runs, events])
  const container = useRef<HTMLDivElement>(null)
  const isFollowing = useRef(true)
  const [hasUnseen, setHasUnseen] = useState(false)

  useEffect(() => {
    const element = container.current
    if (element === null) return
    if (isFollowing.current) element.scrollTop = element.scrollHeight
    else setHasUnseen(true)
  }, [items])

  useEffect(() => {
    // A hidden pane has no scroll height; resume following when it becomes visible again.
    const element = container.current
    if (visible && element && isFollowing.current) element.scrollTop = element.scrollHeight
  }, [visible])

  useEffect(() => {
    if (!jump) return
    const target = container.current?.querySelector<HTMLElement>(`[data-run="${jump.number}"]`)
    if (target) {
      isFollowing.current = false
      target.scrollIntoView?.({ block: 'start' })
      target.focus({ preventScroll: true })
    }
  }, [jump])

  const jumpToLatest = (): void => {
    const element = container.current
    if (element) element.scrollTop = element.scrollHeight
    isFollowing.current = true
    setHasUnseen(false)
  }

  return (
    <div
      className="chat"
      ref={container}
      role="log"
      aria-label="Chat"
      onScroll={(event) => {
        const element = event.currentTarget
        isFollowing.current =
          element.scrollHeight - element.scrollTop - element.clientHeight < NEAR_BOTTOM_PIXELS
        if (isFollowing.current) setHasUnseen(false)
      }}
    >
      {items.length === 0 && <p className="empty self-center">Nothing yet.</p>}
      {items.map((item) => (
        <ChatItem item={item} agentName={agentName} key={item.key} />
      ))}
      {hasUnseen && (
        <button type="button" className="chat-new-activity" onClick={jumpToLatest}>
          ↓ New activity
        </button>
      )}
    </div>
  )
}

function ChatItem({ item, agentName }: { item: TimelineItem; agentName: string }) {
  switch (item.kind) {
    case 'run':
      return <RunDivider item={item} />
    case 'you':
      // Your words are on the right; everyone else's are on the left.
      return (
        <div className="msg msg-you source-you" data-source="you">
          <div className="bubble bubble-you">
            <div className="bubble-head">
              <span className="muted">{clockTime(item.ts)}</span> You
            </div>
            <div className="chat-body">{item.text}</div>
          </div>
          <span className="avatar" aria-hidden="true">
            YO
          </span>
        </div>
      )
    case 'auto':
      return <AutoEntry item={item} />
    case 'reviewer':
      return <ReviewerEntry item={item} />
    case 'agent':
      return <AgentEntry item={item} agentName={agentName} />
    case 'system':
      return <SystemEntry item={item} />
    case 'working':
      return (
        <div className="chat-working source-agent" data-source="agent">
          <span className="avatar" aria-hidden="true">
            {initials(agentName)}
          </span>
          <span>Working…</span>
        </div>
      )
  }
}

function RunDivider({ item }: { item: RunItem }) {
  const usage = runUsage(item.run)
  return (
    <div className="chat-run" data-run={item.number} tabIndex={-1}>
      <b>Run {item.number}</b>
      <span className={`toned source-${item.source}`}>{item.reason}</span>
      <span>{clockTime(item.run.started_at)}</span>
      {item.run.ended_at !== null && <span>{runDuration(item.run)}</span>}
      {usage && <span>{usage}</span>}
      <span>{runOutcome(item.run)}</span>
    </div>
  )
}

function AutoEntry({ item }: { item: AutoItem }) {
  const lines = item.text.split('\n').length
  return (
    <details className="chat-centre chat-auto flex-col" data-source="auto">
      <summary>
        Automatic · {item.reason} · {clockTime(item.ts)}{' '}
        <span className="opacity-75">
          (show what was sent · {lines} {lines === 1 ? 'line' : 'lines'})
        </span>
      </summary>
      <pre className="text-left text-text">{item.text}</pre>
    </details>
  )
}

function ReviewerEntry({ item }: { item: ReviewerItem }) {
  return (
    <div className="msg source-reviewer" data-source="reviewer">
      <span className="avatar" aria-hidden="true">
        {initials(item.author)}
      </span>
      <div className={`bubble bubble-reviewer${item.notSent ? ' is-ignored' : ''}`}>
        <div className="bubble-head">
          @{item.author}
          <span className="muted">
            · reviewer · {item.commentKind} · {clockTime(item.ts)}
          </span>
        </div>
        {(item.where || item.url) && (
          <div className="chat-where">
            {item.where}
            {item.where && item.url && ' · '}
            {item.url && (
              <a href={item.url} target="_blank" rel="noreferrer">
                view on GitHub ↗
              </a>
            )}
          </div>
        )}
        <div className="chat-body">{item.body}</div>
        {item.notSent && <div className="chat-not-sent">{item.notSent}</div>}
      </div>
    </div>
  )
}

function AgentEntry({ item, agentName }: { item: AgentItem; agentName: string }) {
  return (
    <div className="msg source-agent" data-source="agent">
      <span className="avatar" aria-hidden="true">
        {initials(agentName)}
      </span>
      <div className="bubble bubble-agent">
        <div className="bubble-head">
          {agentName} <span className="muted">· {clockTime(item.ts)}</span>
        </div>
        {item.parts.map((part) =>
          part.kind === 'message' ? (
            <div className="chat-body" key={part.key}>
              {part.text}
            </div>
          ) : (
            <ActionRow action={part} key={part.key} />
          ),
        )}
      </div>
    </div>
  )
}

function ActionRow({ action }: { action: AgentAction }) {
  const className = `chat-action action-${action.type}${action.failed ? ' is-failed' : ''}`
  const summary = (
    <>
      <span className="chat-action-title">{action.title}</span>
      {action.note && <span className="chat-action-note">{action.note}</span>}
    </>
  )
  if (action.detail.trim() === '') return <div className={className}>{summary}</div>
  return (
    <details className={className}>
      <summary>{summary}</summary>
      <pre>{action.detail}</pre>
    </details>
  )
}

function SystemEntry({ item }: { item: SystemItem }) {
  return (
    <div className={`chat-centre chat-system tone-${item.tone}`} data-source="system">
      <span className="chat-system-symbol">{item.symbol}</span>
      {item.url ? (
        <a href={item.url} target="_blank" rel="noreferrer">
          {item.text}
        </a>
      ) : (
        item.text
      )}
      <span>· {clockTime(item.ts)}</span>
    </div>
  )
}

/** Up to two letters for the agent's stamp: `Claude Code` becomes `CC`, `Codex` becomes `CO`. */
export function initials(name: string): string {
  const words = name.split(/\s+/).filter(Boolean)
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase()
  return name.slice(0, 2).toUpperCase()
}
