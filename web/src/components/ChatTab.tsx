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
      {items.length === 0 && <p className="empty">Nothing yet.</p>}
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
      return (
        <div className="chat-you" data-source="you">
          <div className="chat-source">
            You <span className="muted">· {clockTime(item.ts)}</span>
          </div>
          <div className="chat-bubble">{item.text}</div>
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
        <div className="chat-working" data-source="agent">
          <span className="chat-stamp">{initials(agentName)}</span>
          <span>Working…</span>
        </div>
      )
  }
}

function RunDivider({ item }: { item: RunItem }) {
  const facts = [runOutcome(item.run)]
  if (item.run.ended_at !== null) facts.push(runDuration(item.run))
  const usage = runUsage(item.run)
  if (usage) facts.push(usage)
  return (
    <div className="chat-run" data-run={item.number} tabIndex={-1}>
      <span>
        <b>Run #{item.number}</b> ·{' '}
        <span className={`chat-why source-${item.source}`}>{item.reason}</span> ·{' '}
        {facts.join(' · ')}
      </span>
    </div>
  )
}

function AutoEntry({ item }: { item: AutoItem }) {
  const lines = item.text.split('\n').length
  return (
    <details className="chat-auto" data-source="auto">
      <summary>
        <span className="chat-source">
          ⚙ Automatic{' '}
          <span className="muted">· sent by agent-supervisor · {clockTime(item.ts)}</span>
        </span>
        <span>
          {item.reason}.{' '}
          <span className="muted">
            Show what was sent · {lines} {lines === 1 ? 'line' : 'lines'}
          </span>
        </span>
      </summary>
      <pre>{item.text}</pre>
    </details>
  )
}

function ReviewerEntry({ item }: { item: ReviewerItem }) {
  return (
    <div className={`chat-reviewer${item.notSent ? ' is-ignored' : ''}`} data-source="reviewer">
      <div className="chat-source">
        Reviewer{' '}
        <span className="muted">
          · @{item.author} · {item.commentKind} · {clockTime(item.ts)}
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
  )
}

function AgentEntry({ item, agentName }: { item: AgentItem; agentName: string }) {
  return (
    <div className="chat-agent" data-source="agent">
      <span className="chat-stamp">{initials(agentName)}</span>
      <div className="chat-agent-body">
        <div className="chat-source">
          Agent{' '}
          <span className="muted">
            · {agentName} · {clockTime(item.ts)}
          </span>
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
    <div className={`chat-system tone-${item.tone}`} data-source="system">
      <span className="chat-system-symbol">{item.symbol}</span>
      {item.url ? (
        <a href={item.url} target="_blank" rel="noreferrer">
          {item.text}
        </a>
      ) : (
        item.text
      )}
      <span className="muted">· {clockTime(item.ts)}</span>
    </div>
  )
}

/** Up to two letters for the agent's stamp: `Claude Code` becomes `CC`, `Codex` becomes `CO`. */
export function initials(name: string): string {
  const words = name.split(/\s+/).filter(Boolean)
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase()
  return name.slice(0, 2).toUpperCase()
}
