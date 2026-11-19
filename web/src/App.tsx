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

/** 目前網址指到哪個任務；`#/tasks/12` 是任務 12 的詳情頁，其他都是看板。 */
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
  // 每次連上 daemon 就加一；詳情頁靠它知道要補抓斷線期間的資料。
  const [connectionCount, setConnectionCount] = useState(0)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [isCreatingTask, setIsCreatingTask] = useState(false)
  const [isAddingProject, setIsAddingProject] = useState(false)
  const eventListeners = useRef(new Set<EventListener>())
  // 重新抓看板的期間收到的卡片：它們比抓回來的那一份新，合併時要優先。
  const arrivedWhileLoading = useRef<Map<number, Card> | null>(null)
  // 只有最後一次發出的載入可以寫入畫面；較早的載入晚回來時直接丟掉。
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
      // 先連上推送再抓資料，兩者之間發生的變化才不會漏掉。
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
            // 重新抓一次：正在進行的舊載入會因此作廢，不會把新專案蓋掉。
            void reload()
          }}
          onClose={() => setIsAddingProject(false)}
        />
      )}
    </div>
  )
}
