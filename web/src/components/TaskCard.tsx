import { formatUsage, timeAgo } from '../format'
import type { Card } from '../types'
import { StatusLabel } from './StatusLabel'

const AGENT_NAMES: Record<string, string> = { claude_code: 'Claude Code', codex: 'Codex' }

/** Display names of agents; an unknown type is shown by its id. */
export function agentName(agentType: string): string {
  return AGENT_NAMES[agentType] ?? agentType
}

/** A card on the board; clicking it opens the task detail page. */
export function TaskCard({ card }: { card: Card }) {
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
        <span className="muted" title={card.cost_usd === null ? '這種 agent 不回報花費' : '估算值'}>
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
