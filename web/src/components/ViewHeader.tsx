import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'

import type { Project } from '../types'

interface ProjectFilterProps {
  projects: Project[]
  /** The chosen project, or null for all projects. */
  selectedProjectId: number | null
  /** How many cards each project has in the current view, and in total. */
  counts: Map<number, number>
  total: number
  onSelect: (projectId: number | null) => void
  onAddProject: () => void
}

/** The project filter: a button showing the current choice and a listbox to change it. */
export function ProjectFilter(props: ProjectFilterProps) {
  const { projects, selectedProjectId, counts, total, onSelect, onAddProject } = props
  const [isOpen, setIsOpen] = useState(false)
  const container = useRef<HTMLDivElement>(null)
  const list = useRef<HTMLUListElement>(null)
  const selected = projects.find((project) => project.id === selectedProjectId)

  useEffect(() => {
    if (!isOpen) return
    list.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.focus()
    const closeOutside = (event: MouseEvent): void => {
      if (!container.current?.contains(event.target as Node)) setIsOpen(false)
    }
    document.addEventListener('mousedown', closeOutside)
    return () => document.removeEventListener('mousedown', closeOutside)
  }, [isOpen])

  const choose = (action: () => void): void => {
    setIsOpen(false)
    action()
  }
  const onListKey = (event: KeyboardEvent<HTMLUListElement>): void => {
    const options = [...(list.current?.querySelectorAll<HTMLElement>('[role="option"]') ?? [])]
    const index = options.indexOf(document.activeElement as HTMLElement)
    if (event.key === 'Escape') {
      event.preventDefault()
      setIsOpen(false)
      container.current?.querySelector('button')?.focus()
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      const step = event.key === 'ArrowDown' ? 1 : -1
      options[(index + step + options.length) % options.length]?.focus()
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      options[index]?.click()
    }
  }
  const option = (key: string, isSelected: boolean, label: ReactNode, onClick: () => void) => (
    <li
      key={key}
      role="option"
      aria-selected={isSelected}
      tabIndex={-1}
      className="menu-option"
      onClick={() => choose(onClick)}
    >
      <span className="menu-check" aria-hidden="true">
        {isSelected ? '✓' : ''}
      </span>
      {label}
    </li>
  )

  return (
    <div className="filter" ref={container}>
      <button
        type="button"
        className="filter-button"
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        aria-label={`Project: ${selected?.name ?? 'All projects'}`}
        onClick={() => setIsOpen((open) => !open)}
      >
        {selected?.name ?? 'All projects'} <span aria-hidden="true">▾</span>
      </button>
      {isOpen && (
        <ul className="menu" role="listbox" aria-label="Project" ref={list} onKeyDown={onListKey}>
          {option(
            'all',
            selectedProjectId === null,
            <>
              All projects <span className="count">{total}</span>
            </>,
            () => onSelect(null),
          )}
          <li role="separator" className="menu-separator" />
          {projects.map((project) =>
            option(
              String(project.id),
              project.id === selectedProjectId,
              <>
                {project.name} <span className="count">{counts.get(project.id) ?? 0}</span>
              </>,
              () => onSelect(project.id),
            ),
          )}
          <li role="separator" className="menu-separator" />
          {option('add', false, 'Add project…', onAddProject)}
        </ul>
      )}
    </div>
  )
}

interface ViewHeaderProps extends ProjectFilterProps {
  view: 'board' | 'archive'
  summary?: string
  /** What goes on the right: the New task button on the board, the search box in the Archive. */
  children?: ReactNode
}

/** The header shared by the board and the Archive: the view switch, the project filter and a summary. */
export function ViewHeader({ view, summary, children, ...filter }: ViewHeaderProps) {
  return (
    <header className="view-header">
      <div className="segmented" role="tablist" aria-label="View">
        <a role="tab" aria-selected={view === 'board'} href="#/">
          Board
        </a>
        <a role="tab" aria-selected={view === 'archive'} href="#/archive">
          Archive
        </a>
      </div>
      <ProjectFilter {...filter} />
      {summary && <span className="view-summary">{summary}</span>}
      <div className="ml-auto flex items-center gap-2">{children}</div>
    </header>
  )
}

/** Count cards per project. */
export function countByProject(cards: { project_id: number }[]): Map<number, number> {
  const counts = new Map<number, number>()
  for (const card of cards) counts.set(card.project_id, (counts.get(card.project_id) ?? 0) + 1)
  return counts
}
