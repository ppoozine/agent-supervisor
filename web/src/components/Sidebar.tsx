import { useState } from 'react'

import { columnTone } from '../format'
import { readPreference, savePreference } from '../preferences'
import type { Card, Project } from '../types'
import { BOARD_COLUMNS } from './Board'

export type SidebarView = 'board' | 'archive' | 'task' | 'settings'

/** The settings page's own sections, listed in the sidebar while it is open. */
export const SETTINGS_SECTIONS = [
  ['settings-agents', 'Agents'],
  ['settings-github', 'GitHub'],
  ['settings-projects', 'Projects'],
  ['settings-usage', 'Usage'],
] as const

interface SidebarProps {
  projects: Project[]
  cards: Card[]
  view?: SidebarView
  openTaskId: number | null
  selectedProjectId?: number | null
  /** Choose a project; choosing the selected one again goes back to all projects. */
  onSelectProject?: (projectId: number | null) => void
  onAddProject: () => void
  onNewTask: () => void
  newTaskBlocker: string | null
  connection?: { status: string; text: string }
}

/**
 * Left sidebar: New task, Board and Archive, the projects with their active tasks, and the
 * connection and Settings at the bottom. On the settings page it lists the page's sections.
 */
export function Sidebar({
  projects,
  cards,
  view = 'board',
  openTaskId,
  selectedProjectId = null,
  onSelectProject = () => undefined,
  onAddProject,
  onNewTask,
  newTaskBlocker,
  connection,
}: SidebarProps) {
  const [isCompact, setCompact] = useState(() => readPreference('sidebar-collapsed') === 'true')
  const active = cards.filter((card) => BOARD_COLUMNS.includes(card.column))
  const archivedCount = cards.length - active.length

  return (
    <nav className={`sidebar${isCompact ? ' is-collapsed' : ''}`} aria-label="Main">
      <div className="flex w-full items-center gap-1">
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
      {isCompact ? (
        <>
          <a className="ghost" href="#/" aria-label="Board" title="Board">
            ▦
          </a>
          <a className="ghost" href="#/archive" aria-label="Archive" title="Archive">
            ▤
          </a>
          <button
            className="ghost"
            onClick={onAddProject}
            aria-label="+ Add project"
            title="Add project"
          >
            ⊕
          </button>
        </>
      ) : view === 'settings' ? (
        <>
          <a className="nav-item" href="#/">
            ← Board
          </a>
          <div className="sidebar-heading">Settings</div>
          {SETTINGS_SECTIONS.map(([id, name]) => (
            <button
              key={id}
              className="nav-item"
              onClick={() => document.getElementById(id)?.scrollIntoView?.({ block: 'start' })}
            >
              {name}
            </button>
          ))}
        </>
      ) : (
        <>
          <a className="nav-item" href="#/" aria-current={view === 'board' ? 'page' : undefined}>
            Board <span className="count">{active.length}</span>
          </a>
          <a
            className="nav-item"
            href="#/archive"
            aria-current={view === 'archive' ? 'page' : undefined}
          >
            Archive <span className="count">{archivedCount}</span>
          </a>
          <div className="sidebar-heading">Projects</div>
          {projects.map((project) => {
            const tasks = active
              .filter((card) => card.project_id === project.id)
              .sort((first, second) => second.id - first.id)
            const isSelected = project.id === selectedProjectId
            return (
              <div key={project.id}>
                <button
                  className={`nav-item${selectedProjectId !== null && !isSelected ? ' is-dimmed' : ''}`}
                  aria-current={isSelected ? 'true' : undefined}
                  onClick={() => onSelectProject(isSelected ? null : project.id)}
                  title={project.repo_path}
                >
                  <span className="truncate">{project.name}</span>
                  <span className="count">{tasks.length}</span>
                </button>
                {/* The Archive lists only project names; archived tasks live in its table. */}
                {view !== 'archive' && (
                  <>
                    {tasks.map((card) => (
                      <a
                        key={card.id}
                        href={`#/tasks/${card.id}`}
                        className={`sidebar-task tone-${columnTone(card.column)}${card.id === openTaskId ? ' is-open' : ''}`}
                        aria-current={card.id === openTaskId ? 'page' : undefined}
                        title={`${card.title} — ${card.label}`}
                      >
                        <span className={`dot${card.has_open_run ? ' is-live' : ''}`} />
                        <span className="truncate">{card.title}</span>
                      </a>
                    ))}
                    <button className="sidebar-plan" disabled title="Coming in 0.5">
                      ✦ Plan with main agent
                    </button>
                  </>
                )}
              </div>
            )
          })}
          <button className="nav-item text-text-muted" onClick={onAddProject}>
            + Add project
          </button>
        </>
      )}
      <div className="sidebar-foot">
        {connection && (
          <span
            title={connection.text}
            aria-label={connection.text}
            className={`connection connection-${connection.status}`}
          >
            {!isCompact && connection.text}
          </span>
        )}
        <a
          aria-label="Settings"
          title="Settings"
          className="nav-item"
          aria-current={view === 'settings' ? 'page' : undefined}
          href="#/settings"
        >
          {isCompact ? '⚙' : '⚙ Settings'}
        </a>
      </div>
    </nav>
  )
}
