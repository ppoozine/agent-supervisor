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
      <div className="card-title">{card.title}</div>
      <div className="card-head">
        {projectName && <span className="text-text-muted">{projectName}</span>}
        <span className={`agent agent-${card.agent_type}`}>{agentName(card.agent_type)}</span>
        <span className="muted">#{card.id}</span>
      </div>
      {card.branch && <div className="branch">{card.branch}</div>}
      <div className="card-foot">
        <StatusLabel label={card.label} column={card.column} active={card.has_open_run} />
        <span
          className="muted"
          title={card.cost_usd === null ? 'This agent does not report cost' : 'Estimated'}
        >
          {formatUsage(card)} · {timeAgo(card.updated_at)}
        </span>
      </div>
      {card.pr_number !== null && (
        <div className="card-pr">
          PR #{card.pr_number}{' '}
          <span className={`pr-state pr-${card.pr_state}`}>{card.pr_state}</span>
        </div>
      )}
      {(card.ci_state !== 'none' ||
        card.review_state === 'approved' ||
        card.merge_state === 'conflicting' ||
        card.ci_fix_attempts > 0) && (
        <div className="flex flex-wrap gap-2 text-xs">
          <Checks card={card} />
          {card.ci_fix_attempts > 0 && (
            <span
              className={
                card.ci_fix_attempts >= card.max_ci_fix_attempts
                  ? 'text-needs-you'
                  : 'text-text-muted'
              }
            >
              fixes {card.ci_fix_attempts}/{card.max_ci_fix_attempts}
            </span>
          )}
        </div>
      )}
    </a>
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
                : 'text-in-review'
          }
        >
          {card.ci_state === 'passed' ? '✓' : card.ci_state === 'failed' ? '✗' : '◌'} CI{' '}
          {card.ci_state}
        </span>
      )}
      {card.review_state === 'approved' ? (
        <span className="text-success">✓ review</span>
      ) : (
        detailed && <span>review {card.review_state === 'none' ? '—' : card.review_state}</span>
      )}
      {card.merge_state === 'conflicting' ? (
        <span className="text-needs-you">⚠ conflict</span>
      ) : (
        detailed && (
          <span>{card.merge_state === 'clean' ? 'no conflict' : 'mergeability unknown'}</span>
        )
      )}
    </>
  )
}
