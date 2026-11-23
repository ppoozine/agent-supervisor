import { useEffect, useState } from 'react'

import { columnTone } from '../format'
import type { Card, Project } from '../types'
import { TaskCard } from './TaskCard'
import { countByProject, ViewHeader } from './ViewHeader'

export const BOARD_COLUMNS = ['Working', 'Needs you', 'In review', 'Ready to merge']
const CLOCK_TICK_MILLISECONDS = 30_000

/** What an empty column says while a project is chosen, e.g. "Nothing running in web-app". */
const EMPTY_VERBS: Record<string, string> = {
  Working: 'running',
  'Needs you': 'waiting for you',
  'In review': 'in review',
  'Ready to merge': 'ready to merge',
}

function newestFirst(cards: Card[]): Card[] {
  return [...cards].sort((first, second) => second.updated_at.localeCompare(first.updated_at))
}

interface BoardProps {
  /** Every card, including archived ones; the board shows only those in its four columns. */
  cards: Card[]
  projects: Project[]
  selectedProjectId?: number | null
  onSelectProject?: (projectId: number | null) => void
  onAddProject?: () => void
  onNewTask?: () => void
  newTaskBlocker?: string | null
}

/** The board: its header with the project filter, and four columns. Cards go strictly in the API's `column`. */
export function Board({
  cards,
  projects,
  selectedProjectId = null,
  onSelectProject = () => undefined,
  onAddProject = () => undefined,
  onNewTask = () => undefined,
  newTaskBlocker = null,
}: BoardProps) {
  const [, setTick] = useState(0)
  // Redraw periodically so "minutes ago" keeps moving forward.
  useEffect(() => {
    const timer = setInterval(() => setTick((tick) => tick + 1), CLOCK_TICK_MILLISECONDS)
    return () => clearInterval(timer)
  }, [])

  const selected = projects.find((project) => project.id === selectedProjectId)
  const active = cards.filter((card) => BOARD_COLUMNS.includes(card.column))
  const shown = selected ? active.filter((card) => card.project_id === selected.id) : active
  const needYou = shown.filter((card) => card.column === 'Needs you').length
  const projectName = (card: Card): string | undefined =>
    projects.find((project) => project.id === card.project_id)?.name

  return (
    <>
      <ViewHeader
        view="board"
        projects={projects}
        selectedProjectId={selected?.id ?? null}
        counts={countByProject(active)}
        total={active.length}
        onSelect={onSelectProject}
        onAddProject={onAddProject}
        summary={projects.length > 0 ? `${shown.length} active · ${needYou} need you` : undefined}
      >
        <button
          className="primary"
          onClick={onNewTask}
          disabled={newTaskBlocker !== null}
          title={newTaskBlocker ?? undefined}
        >
          {selected ? `New task in ${selected.name}` : 'New task'}
        </button>
      </ViewHeader>
      {projects.length === 0 ? (
        <div className="empty flex flex-col items-start gap-3">
          <p>No projects yet. Add a local git repository to start opening tasks in it.</p>
          <button className="primary" onClick={onAddProject}>
            Add project
          </button>
        </div>
      ) : (
        <div className="board">
          {BOARD_COLUMNS.map((column) => {
            const columnCards = newestFirst(shown.filter((card) => card.column === column))
            return (
              <section className="column" key={column} aria-label={column}>
                <h2 className={`column-title tone-${columnTone(column)}`}>
                  <span className="dot" />
                  {column}
                  <span className="count">{columnCards.length}</span>
                </h2>
                {columnCards.length === 0 && (
                  <p className="column-empty">
                    {selected ? `Nothing ${EMPTY_VERBS[column]} in ${selected.name}` : 'Nothing here'}
                  </p>
                )}
                {columnCards.map((card) => (
                  <TaskCard card={card} projectName={projectName(card)} key={card.id} />
                ))}
              </section>
            )
          })}
        </div>
      )}
    </>
  )
}
