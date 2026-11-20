import { useCallback, useEffect, useRef, useState } from 'react'

import { errorMessage, listAgents, listProjects, listTasks } from './api'
import { AddProjectForm } from './components/AddProjectForm'
import { Board } from './components/Board'
import { NewTaskDialog } from './components/NewTaskDialog'
import { Sidebar } from './components/Sidebar'
import { TaskPage } from './components/TaskPage'
import { connectStream, type StreamStatus } from './stream'
import type { Agent, Card, EventRow, Project } from './types'

type EventListener = (event: EventRow) => void

const STATUS_TEXT: Record<StreamStatus, string> = {
  connecting: 'Connecting…',
  live: 'Live',
  offline: 'Offline — reconnecting…',
}

/** Which task the URL points to; `#/tasks/12` is the detail page of task 12, anything else is the board. */
function taskIdFromHash(hash: string): number | null {
  const match = /^#\/tasks\/(\d+)$/.exec(hash)
  return match ? Number(match[1]) : null
}

function useOpenTaskId(): number | null {
  const [taskId, setTaskId] = useState(() => taskIdFromHash(window.location.hash))
  useEffect(() => {
    const update = (): void => setTaskId(taskIdFromHash(window.location.hash))
    window.addEventListener('hashchange', update)
    return () => window.removeEventListener('hashchange', update)
  }, [])
  return taskId
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
  const openTaskId = useOpenTaskId()

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
      setCards(Object.fromEntries([...loadedCards, ...arrived.values()].map((card) => [card.id, card])))
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
  const openCard = openTaskId === null ? undefined : cards[openTaskId]

  return (
    <div className="layout">
      <Sidebar
        projects={projects}
        cards={allCards}
        openTaskId={openTaskId}
        onAddProject={() => setIsAddingProject(true)}
      />
      <main className="main">
        <header className="topbar">
          <h1>
            {openTaskId === null ? (
              'Board'
            ) : (
              <a className="back" href="#/">
                ← Board
              </a>
            )}
          </h1>
          <span className={`connection connection-${status}`}>{STATUS_TEXT[status]}</span>
          <button
            className="primary"
            onClick={() => setIsCreatingTask(true)}
            disabled={projects.length === 0}
            title={projects.length === 0 ? '先加入一個專案' : undefined}
          >
            + New task
          </button>
        </header>
        {loadError && (
          <div className="alert" role="alert">
            {loadError}
          </div>
        )}
        {openTaskId === null ? (
          <Board cards={allCards} projects={projects} />
        ) : (
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
  )
}
