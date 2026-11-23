import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { errorMessage, listAgents, listProjects, listTasks } from './api'
import { AddProjectForm } from './components/AddProjectForm'
import { Archive } from './components/Archive'
import { Board } from './components/Board'
import { NewTaskDialog } from './components/NewTaskDialog'
import { SettingsPage } from './components/SettingsPage'
import { Sidebar } from './components/Sidebar'
import { AgentNames } from './components/TaskCard'
import { TaskPage } from './components/TaskPage'
import { readPreference, savePreference } from './preferences'
import { connectStream, type StreamStatus } from './stream'
import type { Agent, Card, EventRow, Project } from './types'

type EventListener = (event: EventRow) => void

const STATUS_TEXT: Record<StreamStatus, string> = {
  connecting: 'Connecting…',
  live: 'Live',
  offline: 'Reconnecting…',
}

type Page =
  | { kind: 'board' }
  | { kind: 'archive' }
  | { kind: 'task'; taskId: number }
  | { kind: 'settings' }

/** Which page the URL points to: `#/tasks/12` is task 12, `#/archive` the Archive, `#/settings` the settings, anything else the board. */
export function pageFromHash(hash: string): Page {
  if (hash === '#/settings') return { kind: 'settings' }
  if (hash === '#/archive') return { kind: 'archive' }
  const match = /^#\/tasks\/(\d+)$/.exec(hash)
  return match ? { kind: 'task', taskId: Number(match[1]) } : { kind: 'board' }
}

function usePage(): Page {
  const [page, setPage] = useState(() => pageFromHash(window.location.hash))
  useEffect(() => {
    const update = (): void => setPage(pageFromHash(window.location.hash))
    window.addEventListener('hashchange', update)
    return () => window.removeEventListener('hashchange', update)
  }, [])
  return page
}

/** Why a new task cannot be opened right now, or null if it can. */
function newTaskBlocker(projects: Project[], agents: Agent[]): string | null {
  if (projects.length === 0) return 'Add a project first'
  if (!agents.some((agent) => agent.usable)) return 'No agent can be used; see Settings'
  return null
}

export function App() {
  const [projects, setProjects] = useState<Project[]>([])
  const [agents, setAgents] = useState<Agent[]>([])
  const [cards, setCards] = useState<Record<number, Card>>({})
  const [status, setStatus] = useState<StreamStatus>('connecting')
  // Incremented on every connection to the daemon; the detail page uses it to know it must
  // catch up on what it missed while disconnected.
  const [connectionCount, setConnectionCount] = useState(0)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [isCreatingTask, setIsCreatingTask] = useState(false)
  const [isAddingProject, setIsAddingProject] = useState(false)
  const eventListeners = useRef(new Set<EventListener>())
  // Cards pushed while the board is being refetched: they are newer than the fetched copy,
  // so they win when the two are merged.
  const arrivedWhileLoading = useRef<Map<number, Card> | null>(null)
  // Only the most recently started load may update the screen; an earlier load that comes
  // back late is dropped.
  const latestLoad = useRef(0)
  // The project the board and the Archive are filtered to; null is all projects.
  const [selectedProjectId, setSelectedProjectId] = useState<number | null>(() => {
    const saved = Number(readPreference('project-filter'))
    return Number.isInteger(saved) && saved > 0 ? saved : null
  })
  const page = usePage()
  const openTaskId = page.kind === 'task' ? page.taskId : null

  const reload = useCallback(async (): Promise<void> => {
    const arrived = new Map<number, Card>()
    arrivedWhileLoading.current = arrived
    const load = ++latestLoad.current
    try {
      const [loadedProjects, loadedAgents, loadedCards] = await Promise.all([
        listProjects(),
        listAgents(),
        listTasks(),
      ])
      if (load !== latestLoad.current) return
      setProjects(loadedProjects)
      setAgents(loadedAgents)
      setCards(
        Object.fromEntries([...loadedCards, ...arrived.values()].map((card) => [card.id, card])),
      )
      setLoadError(null)
    } catch (error) {
      if (load === latestLoad.current) setLoadError(errorMessage(error))
    } finally {
      if (arrivedWhileLoading.current === arrived) arrivedWhileLoading.current = null
    }
  }, [])

  const showCard = useCallback((card: Card): void => {
    arrivedWhileLoading.current?.set(card.id, card)
    setCards((current) => ({ ...current, [card.id]: card }))
  }, [])

  useEffect(
    () =>
      // Connect to the stream before fetching, so changes in between are not missed.
      connectStream({
        onOpen: () => {
          setConnectionCount((count) => count + 1)
          void reload()
        },
        onEvent: (event) => eventListeners.current.forEach((listener) => listener(event)),
        onTask: showCard,
        onStatus: setStatus,
      }),
    [reload, showCard],
  )

  const subscribeToEvents = useCallback((listener: EventListener): (() => void) => {
    eventListeners.current.add(listener)
    return () => {
      eventListeners.current.delete(listener)
    }
  }, [])

  const allCards = Object.values(cards)
  const agentNames = useMemo(
    () => Object.fromEntries(agents.map((agent) => [agent.agent_type, agent.display_name])),
    [agents],
  )
  const blocker = newTaskBlocker(projects, agents)
  // Only once agents have loaded: an empty list while loading is not "none usable".
  const hasNoUsableAgent = agents.length > 0 && !agents.some((agent) => agent.usable)
  const openCard = openTaskId === null ? undefined : cards[openTaskId]
  // A saved project that no longer exists means all projects.
  const filterProjectId = projects.some((project) => project.id === selectedProjectId)
    ? selectedProjectId
    : null
  const selectProject = (projectId: number | null): void => {
    setSelectedProjectId(projectId)
    savePreference('project-filter', projectId === null ? '' : String(projectId))
    if (page.kind === 'task' || page.kind === 'settings') window.location.hash = '#/'
  }
  const filterProps = {
    cards: allCards,
    projects,
    selectedProjectId: filterProjectId,
    onSelectProject: selectProject,
    onAddProject: () => setIsAddingProject(true),
  }

  return (
    <AgentNames.Provider value={agentNames}>
      <div className="layout">
        <Sidebar
          projects={projects}
          cards={allCards}
          view={page.kind}
          openTaskId={openTaskId}
          selectedProjectId={filterProjectId}
          onSelectProject={selectProject}
          onAddProject={() => setIsAddingProject(true)}
          onNewTask={() => setIsCreatingTask(true)}
          newTaskBlocker={blocker}
          connection={{ status, text: STATUS_TEXT[status] }}
        />
        <main className="main">
          {status === 'offline' && (
            <div className="banner" role="status">
              Can't reach the daemon. Retrying…
            </div>
          )}
          {loadError && (
            <div className="alert" role="alert">
              {loadError}
            </div>
          )}
          {page.kind === 'board' && hasNoUsableAgent && (
            <div className="alert" role="status">
              No agent can be used yet. <a href="#/settings">See Settings</a> for what is missing.
            </div>
          )}
          {page.kind === 'settings' && (
            <SettingsPage
              onChecked={() => void reload()}
              projects={projects}
              cards={allCards}
              agents={agents}
              onAddProject={() => setIsAddingProject(true)}
            />
          )}
          {page.kind === 'board' && (
            <Board
              {...filterProps}
              onNewTask={() => setIsCreatingTask(true)}
              newTaskBlocker={blocker}
            />
          )}
          {page.kind === 'archive' && <Archive {...filterProps} />}
          {openTaskId !== null && (
            <TaskPage
              key={openTaskId}
              taskId={openTaskId}
              card={openCard}
              project={projects.find((project) => project.id === openCard?.project_id)}
              connectionCount={connectionCount}
              subscribeToEvents={subscribeToEvents}
              onTaskChanged={showCard}
            />
          )}
        </main>
        {isCreatingTask && (
          <NewTaskDialog
            projects={projects}
            agents={agents}
            initialProjectId={filterProjectId}
            onCreated={(task) => {
              showCard(task)
              setIsCreatingTask(false)
            }}
            onClose={() => setIsCreatingTask(false)}
          />
        )}
        {isAddingProject && (
          <AddProjectForm
            onAdded={(project) => {
              setProjects((current) => [...current, project])
              setIsAddingProject(false)
              // Fetch again: this invalidates any older load still in flight, so it cannot overwrite
              // the new project.
              void reload()
            }}
            onClose={() => setIsAddingProject(false)}
          />
        )}
      </div>
    </AgentNames.Provider>
  )
}
