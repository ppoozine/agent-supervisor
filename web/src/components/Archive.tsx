import { useState } from 'react'

import {
  ARCHIVE_OUTCOME_LABELS,
  archiveOutcome,
  finishedAt,
  formatUsage,
  matchesSearch,
  type ArchiveOutcome,
} from '../format'
import type { Card, Project } from '../types'
import { useAgentName } from './TaskCard'
import { countByProject, ViewHeader } from './ViewHeader'

const OUTCOMES: ArchiveOutcome[] = ['merged', 'closed', 'archived']

interface ArchiveProps {
  /** Every card; only archived ones are listed. */
  cards: Card[]
  projects: Project[]
  selectedProjectId: number | null
  onSelectProject: (projectId: number | null) => void
  onAddProject: () => void
}

/** Finished tasks: filtered by project, outcome and a search, newest first. */
export function Archive({
  cards,
  projects,
  selectedProjectId,
  onSelectProject,
  onAddProject,
}: ArchiveProps) {
  const agentName = useAgentName()
  const [outcome, setOutcome] = useState<ArchiveOutcome | null>(null)
  const [query, setQuery] = useState('')
  const selected = projects.find((project) => project.id === selectedProjectId)
  const archived = cards.filter((card) => card.column === 'Archive')
  const inProject = selected ? archived.filter((card) => card.project_id === selected.id) : archived
  const shown = inProject
    .filter((card) => outcome === null || archiveOutcome(card) === outcome)
    .filter((card) => matchesSearch(card, query))
    .sort((first, second) => finishedAt(second).localeCompare(finishedAt(first)))
  const open = (card: Card): void => {
    window.location.hash = `#/tasks/${card.id}`
  }

  return (
    <>
      <ViewHeader
        view="archive"
        projects={projects}
        selectedProjectId={selected?.id ?? null}
        counts={countByProject(archived)}
        total={archived.length}
        onSelect={onSelectProject}
        onAddProject={onAddProject}
      >
        <input
          type="search"
          className="search"
          aria-label="Search title or branch"
          placeholder="Search title or branch"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </ViewHeader>
      <div className="archive">
        <div className="filters" role="group" aria-label="Outcome">
          <button className="pill" aria-pressed={outcome === null} onClick={() => setOutcome(null)}>
            All <span className="count">{inProject.length}</span>
          </button>
          {OUTCOMES.map((name) => (
            <button
              key={name}
              className="pill"
              aria-pressed={outcome === name}
              onClick={() => setOutcome(name)}
            >
              {ARCHIVE_OUTCOME_LABELS[name]}{' '}
              <span className="count">
                {inProject.filter((card) => archiveOutcome(card) === name).length}
              </span>
            </button>
          ))}
        </div>
        <div className="table-wrap">
          <table className="table" aria-label="Archived tasks">
            <thead>
              <tr>
                <th>Task</th>
                <th>Project</th>
                <th>Agent</th>
                <th>Outcome</th>
                <th>PR</th>
                <th>Finished</th>
                <th>Usage</th>
              </tr>
            </thead>
            <tbody>
              {shown.length === 0 && (
                <tr>
                  <td colSpan={7} className="text-center text-text-muted">
                    No archived tasks match.
                  </td>
                </tr>
              )}
              {shown.map((card) => {
                const result = archiveOutcome(card)
                return (
                  <tr key={card.id} className="is-link" onClick={() => open(card)}>
                    <td className="max-w-80 truncate">
                      <a
                        href={`#/tasks/${card.id}`}
                        className="font-medium text-text"
                        onClick={(event) => event.stopPropagation()}
                      >
                        {card.title}
                      </a>
                    </td>
                    <td className="text-text-muted">
                      {projects.find((project) => project.id === card.project_id)?.name}
                    </td>
                    <td className="text-text-muted">{agentName(card.agent_type)}</td>
                    <td>
                      <span className="inline-flex items-center gap-1.5">
                        <span
                          className={`dot ${result === 'merged' ? 'bg-accent' : result === 'archived' ? 'dot-hollow' : ''}`}
                        />
                        {ARCHIVE_OUTCOME_LABELS[result]}
                      </span>
                    </td>
                    <td className="text-text-muted">
                      {card.pr_number === null ? '—' : `#${card.pr_number}`}
                    </td>
                    <td className="text-text-muted">
                      <time dateTime={finishedAt(card)}>
                        {new Date(finishedAt(card)).toLocaleDateString()}
                      </time>
                    </td>
                    <td className="text-text-muted">{formatUsage(card)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        <p className="text-xs text-text-muted">
          Opening an archived task shows its Chat, Diff and history read-only.
        </p>
      </div>
    </>
  )
}
