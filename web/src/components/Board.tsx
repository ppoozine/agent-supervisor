import { useEffect, useState } from 'react'

import { columnTone } from '../format'
import type { Card, Project } from '../types'
import { TaskCard } from './TaskCard'

const COLUMNS = ['Working', 'Needs you', 'In review', 'Ready to merge']
const ARCHIVE = 'Archive'
const CLOCK_TICK_MILLISECONDS = 30_000

function newestFirst(cards: Card[]): Card[] {
  return [...cards].sort((first, second) => second.updated_at.localeCompare(first.updated_at))
}

/** The four-column board and the expandable Archive section below it. Cards go strictly in the API's `column`. */
export function Board({ cards, projects }: { cards: Card[]; projects: Project[] }) {
  const [isArchiveOpen, setIsArchiveOpen] = useState(false)
  const [, setTick] = useState(0)
  // Redraw periodically so "minutes ago" keeps moving forward.
  useEffect(() => {
    const timer = setInterval(() => setTick((tick) => tick + 1), CLOCK_TICK_MILLISECONDS)
    return () => clearInterval(timer)
  }, [])

  const inColumn = (column: string): Card[] =>
    newestFirst(cards.filter((card) => card.column === column))
  const archived = inColumn(ARCHIVE)

  if (projects.length === 0) {
    return (
      <p className="empty">No projects yet. Use "+ Add project" on the left to add a local repo.</p>
    )
  }
  return (
    <div className="board-area">
      <div className="board">
        {COLUMNS.map((column) => {
          const columnCards = inColumn(column)
          return (
            <section className="column" key={column} aria-label={column}>
              <h2 className={`column-title tone-${columnTone(column)}`}>
                <span className="dot" />
                {column}
                <span className="count">{columnCards.length}</span>
              </h2>
              <div className="column-cards">
                {columnCards.length === 0 && (
                  <p className="py-6 text-center text-xs text-text-muted">Nothing here</p>
                )}
                {columnCards.map((card) => (
                  <TaskCard
                    card={card}
                    projectName={projects.find((project) => project.id === card.project_id)?.name}
                    key={card.id}
                  />
                ))}
              </div>
            </section>
          )
        })}
      </div>
      <section className="archive" aria-label={ARCHIVE}>
        <button
          className="archive-toggle"
          aria-expanded={isArchiveOpen}
          onClick={() => setIsArchiveOpen((open) => !open)}
        >
          {isArchiveOpen ? '▾' : '▸'} Archive <span className="count">{archived.length}</span>
        </button>
        {isArchiveOpen && (
          <div className="archive-cards">
            {archived.map((card) => (
              <a className="archive-row" href={`#/tasks/${card.id}`} key={card.id}>
                <span className="truncate font-medium">{card.title}</span>
                <span className="text-archived">{card.label}</span>
                <time className="text-text-muted" dateTime={card.archived_at ?? card.updated_at}>
                  {new Date(card.archived_at ?? card.updated_at).toLocaleDateString()}
                </time>
              </a>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
