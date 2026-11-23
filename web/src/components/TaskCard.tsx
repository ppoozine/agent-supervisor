import { createContext, useContext } from 'react'

import { formatUsage, timeAgo } from '../format'
import type { Card } from '../types'
import { StatusLabel } from './StatusLabel'

/** Display names of agent types, from the daemon's agent list (adapters can be added by plugins). */
export const AgentNames = createContext<Record<string, string>>({})

/** Look up an agent type's display name; a type the daemon does not know is shown by its id. */
export function useAgentName(): (agentType: string) => string {
  const names = useContext(AgentNames)
  return (agentType) => names[agentType] ?? agentType
}

/** A card on the board; clicking it opens the task detail page. */
export function TaskCard({ card, projectName }: { card: Card; projectName?: string }) {
  const agentName = useAgentName()
  return (
    <a className="card" href={`#/tasks/${card.id}`}>
      <div className="card-head">
        <span className="truncate">
          {projectName && `${projectName} · `}#{card.id}
        </span>
        <span className="shrink-0">{agentName(card.agent_type)}</span>
      </div>
      <div className="card-title">{card.title}</div>
      {card.branch && <div className="branch">{card.branch}</div>}
      <div className="card-status">
        <StatusLabel label={card.label} column={card.column} active={card.has_open_run} />
        <span title={card.cost_usd === null ? 'This agent does not report cost' : 'Estimated'}>
          {formatUsage(card)} · {timeAgo(card.updated_at)}
        </span>
      </div>
      {card.pr_number !== null && (
        <div className="card-pr">
          <span>
            PR #{card.pr_number}
            {card.pr_state !== 'open' && card.pr_state && ` ${card.pr_state}`}
          </span>
          <Checks card={card} />
          <Attempts card={card} />
        </div>
      )}
    </a>
  )
}

/** Automatic rounds used, shown only once any were used; at the limit they turn to the Needs you color. */
export function Attempts({ card, always = false }: { card: Card; always?: boolean }) {
  const rounds = [
    ['fixes', card.ci_fix_attempts, card.max_ci_fix_attempts],
    ['comments', card.feedback_attempts, card.max_feedback_attempts],
  ] as const
  return (
    <>
      {rounds
        .filter(([, used]) => always || used > 0)
        .map(([name, used, limit]) => (
          <span
            key={name}
            className={used >= limit ? 'text-needs-you-text' : undefined}
            title="Automatic rounds used since you last stepped in"
          >
            {name} {used}/{limit}
          </span>
        ))}
    </>
  )
}

/** Shared CI, review and merge indicators, using only the API state. */
export function Checks({ card, detailed = false }: { card: Card; detailed?: boolean }) {
  return (
    <>
      {card.ci_state !== 'none' && (
        <span
          className={
            card.ci_state === 'passed'
              ? 'text-success'
              : card.ci_state === 'failed'
                ? 'text-danger'
                : 'text-in-review-text'
          }
        >
          {card.ci_state === 'passed' ? '✓' : card.ci_state === 'failed' ? '✗' : '◌'} CI{' '}
          {card.ci_state}
        </span>
      )}
      {card.review_state === 'approved' ? (
        <span className="text-success">✓ review</span>
      ) : (
        // A review waiting or asking for changes is worth showing; no review yet is not.
        detailed && card.review_state !== 'none' && <span>review {card.review_state}</span>
      )}
      {card.merge_state === 'conflicting' && <span className="text-needs-you-text">⚠ conflict</span>}
    </>
  )
}
