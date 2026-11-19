import { useState } from 'react'

import { labelTone } from '../format'
import type { Card, Project } from '../types'

interface SidebarProps {
  projects: Project[]
  cards: Card[]
  openTaskId: number | null
  onAddProject: () => void
}

/** 左側欄：專案列表，展開後是那個專案還在看板上的任務。 */
export function Sidebar({ projects, cards, openTaskId, onAddProject }: SidebarProps) {
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set())
  const toggle = (projectId: number): void =>
    setCollapsed((current) => {
      const next = new Set(current)
      if (!next.delete(projectId)) next.add(projectId)
      return next
    })

  return (
    <nav className="sidebar">
      <a className="brand" href="#/">
        agent-supervisor
      </a>
      <div className="sidebar-heading">Projects</div>
      {projects.map((project) => {
        const tasks = cards
          .filter((card) => card.project_id === project.id && card.column !== 'Archive')
          .sort((first, second) => second.id - first.id)
        const isOpen = !collapsed.has(project.id)
        return (
          <div key={project.id}>
            <button className="project" onClick={() => toggle(project.id)} title={project.repo_path}>
              {isOpen ? '▾' : '▸'} {project.name}
              <span className="count">{tasks.length}</span>
            </button>
            {isOpen &&
              tasks.map((card) => (
                <a
                  key={card.id}
                  href={`#/tasks/${card.id}`}
                  className={`sidebar-task tone-${labelTone(card.label, card.column)}${card.id === openTaskId ? ' is-open' : ''}`}
                  title={`${card.title} — ${card.label}`}
                >
                  <span className="dot" />
                  <span className="sidebar-task-title">{card.title}</span>
                </a>
              ))}
          </div>
        )
      })}
      <button className="ghost add-project" onClick={onAddProject}>
        + Add project
      </button>
    </nav>
  )
}
