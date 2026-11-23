import { useState } from 'react'

import { readPreference, savePreference } from '../preferences'

import { columnTone } from '../format'
import type { Card, Project } from '../types'

interface SidebarProps {
  projects: Project[]
  cards: Card[]
  openTaskId: number | null
  isSettingsOpen: boolean
  onAddProject: () => void
  onNewTask: () => void
  newTaskBlocker: string | null
  connection?: { status: string; text: string }
}

/** Left sidebar: the project list; expanding a project shows its tasks still on the board. */
export function Sidebar({
  projects,
  cards,
  openTaskId,
  isSettingsOpen,
  onAddProject,
  onNewTask,
  newTaskBlocker,
  connection,
}: SidebarProps) {
  const [isCompact, setCompact] = useState(() => readPreference('sidebar-collapsed') === 'true')
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set())
  const toggle = (projectId: number): void =>
    setCollapsed((current) => {
      const next = new Set(current)
      if (!next.delete(projectId)) next.add(projectId)
      return next
    })

  return (
    <nav className={`sidebar${isCompact ? ' is-collapsed' : ''}`}>
      <div className="flex items-center gap-1">
        {!isCompact && (
          <a className="brand" href="#/">
            agent-supervisor
          </a>
        )}
        <button
          className="ghost"
          aria-label={isCompact ? 'Expand sidebar' : 'Collapse sidebar'}
          onClick={() => {
            setCompact(!isCompact)
            savePreference('sidebar-collapsed', String(!isCompact))
          }}
        >
          <span aria-hidden="true">{isCompact ? '»' : '«'}</span>
        </button>
      </div>
      <button
        className="primary"
        onClick={onNewTask}
        disabled={newTaskBlocker !== null}
        title={newTaskBlocker ?? 'New task'}
        aria-label="+ New task"
      >
        {isCompact ? '+' : '+ New task'}
      </button>
      {isCompact && (
        <a className="ghost text-center" href="#/" aria-label="Board" title="Board">
          ▦
        </a>
      )}
      {!isCompact && (
        <div className="flex flex-col gap-3">
          <div className="sidebar-heading">Projects</div>
          {projects.map((project) => {
            const tasks = cards
              .filter((card) => card.project_id === project.id && card.column !== 'Archive')
              .sort((first, second) => second.id - first.id)
            const isOpen = !collapsed.has(project.id)
            return (
              <div key={project.id}>
                <button
                  className="project"
                  onClick={() => toggle(project.id)}
                  title={project.repo_path}
                >
                  {isOpen ? '▾' : '▸'} {project.name}
                  <span className="count">{tasks.length}</span>
                </button>
                {isOpen &&
                  tasks.map((card) => (
                    <a
                      key={card.id}
                      href={`#/tasks/${card.id}`}
                      className={`sidebar-task tone-${columnTone(card.column)}${card.id === openTaskId ? ' is-open' : ''}`}
                      title={`${card.title} — ${card.label}`}
                    >
                      <span className={`dot${card.has_open_run ? ' is-live' : ''}`} />
                      <span className="sidebar-task-title">{card.title}</span>
                    </a>
                  ))}
                <button className="ghost ml-4 text-left text-xs" disabled title="Coming in 0.5">
                  ✦ Plan with main agent
                </button>
              </div>
            )
          })}
          <button className="ghost add-project" onClick={onAddProject}>
            + Add project
          </button>
        </div>
      )}
      {isCompact && (
        <button
          className="ghost"
          onClick={onAddProject}
          aria-label="+ Add project"
          title="Add project"
        >
          ⊕
        </button>
      )}
      <div className="mt-auto flex flex-col gap-2">
        {connection && (
          <span
            title={connection.text}
            aria-label={connection.text}
            className={`connection connection-${connection.status} ml-2`}
          >
            {!isCompact && connection.text}
          </span>
        )}
        <a
          aria-label="Settings"
          title="Settings"
          className={`sidebar-settings${isSettingsOpen ? ' is-open' : ''}`}
          href="#/settings"
        >
          {isCompact ? '⚙' : '⚙ Settings'}
        </a>
      </div>
    </nav>
  )
}
