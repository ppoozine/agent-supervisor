// 所有對 daemon 的呼叫都經過這裡。錯誤一律轉成 ApiError，訊息可以直接顯示。
import type { Agent, Card, EventPage, EventRow, Project, TaskDetail } from './types'

export class ApiError extends Error {
  readonly status: number
  readonly code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

/** 把任何錯誤變成可以顯示給使用者看的一句話。 */
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let response: Response
  try {
    response = await fetch(`/api${path}`, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new ApiError(0, 'unreachable', '連不上 daemon，請確認 asv serve 正在執行')
  }
  const data: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const problem = (data as { error?: { code?: string; message?: string } } | null)?.error
    throw new ApiError(
      response.status,
      problem?.code ?? 'http_error',
      problem?.message ?? `請求失敗（HTTP ${response.status}）`,
    )
  }
  return data as T
}

export interface NewProject {
  repo_path: string
  require_review: boolean
}

export interface NewTask {
  project_id: number
  agent_type: string
  title: string | null
  prompt: string
}

export const listProjects = () => request<Project[]>('GET', '/projects')
export const addProject = (project: NewProject) => request<Project>('POST', '/projects', project)
export const listAgents = () => request<Agent[]>('GET', '/agents')
export const listTasks = () => request<Card[]>('GET', '/tasks')
export const createTask = (task: NewTask) => request<TaskDetail>('POST', '/tasks', task)
export const getTask = (taskId: number) => request<TaskDetail>('GET', `/tasks/${taskId}`)
export const sendMessage = (taskId: number, text: string) =>
  request<TaskDetail>('POST', `/tasks/${taskId}/messages`, { text })
export const stopTask = (taskId: number) => request<TaskDetail>('POST', `/tasks/${taskId}/stop`)
export const archiveTask = (taskId: number) =>
  request<TaskDetail>('POST', `/tasks/${taskId}/archive`)

const EVENT_PAGE_SIZE = 500

/** 抓某個任務編號大於 `after` 的所有事件；一頁裝不完就接著抓。 */
export async function listEventsAfter(taskId: number, after: number): Promise<EventRow[]> {
  const events: EventRow[] = []
  let cursor: number | null = after
  while (cursor !== null) {
    const page: EventPage = await request<EventPage>(
      'GET',
      `/tasks/${taskId}/events?after=${cursor}&limit=${EVENT_PAGE_SIZE}`,
    )
    events.push(...page.events)
    cursor = page.next_after
  }
  return events
}
