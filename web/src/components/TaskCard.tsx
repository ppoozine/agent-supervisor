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
export function TaskCard({ card }: { card: Card }) {
  const agentName = useAgentName()
  return (
    <a className="card" href={`#/tasks/${card.id}`}>
      <div className="card-head">
        <span className={`agent agent-${card.agent_type}`}>{agentName(card.agent_type)}</span>
        <span className="muted">#{card.id}</span>
      </div>
      <div className="card-title">{card.title}</div>
      {card.branch && <div className="branch">{card.branch}</div>}
      <div className="card-foot">
        <StatusLabel label={card.label} column={card.column} />
        <span className="muted" title={card.cost_usd === null ? 'This agent does not report cost' : 'Estimated'}>
          {formatUsage(card)} · {timeAgo(card.updated_at)}
        </span>
      </div>
      {card.pr_number !== null && (
        <div className="card-pr">
          PR #{card.pr_number} <span className={`pr-state pr-${card.pr_state}`}>{card.pr_state}</span>
          {card.ci_fix_attempts > 0 && (
            <span className="muted">
              {' '}
              · fixes {card.ci_fix_attempts}/{card.max_ci_fix_attempts}
            </span>
          )}
        </div>
      )}
    </a>
  )
}
