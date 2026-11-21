// Every call to the daemon goes through here. Errors are always turned into ApiError, whose
// message can be shown as is.
import type {
  Agent,
  Card,
  EventPage,
  EventRow,
  Project,
  Setup,
  TaskDetail,
  TaskDiff,
} from './types'

export class ApiError extends Error {
  readonly status: number
  readonly code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}

/** Turn any error into one sentence that can be shown to the user. */
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
    throw new ApiError(0, 'unreachable', 'Cannot reach the daemon. Make sure asv serve is running.')
  }
  const data: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const problem = (data as { error?: { code?: string; message?: string } } | null)?.error
    throw new ApiError(
      response.status,
      problem?.code ?? 'http_error',
      problem?.message ?? `Request failed (HTTP ${response.status})`,
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
export const getSetup = () => request<Setup>('GET', '/setup')
export const checkSetup = () => request<Setup>('POST', '/setup/check')
export const listTasks = () => request<Card[]>('GET', '/tasks')
export const createTask = (task: NewTask) => request<TaskDetail>('POST', '/tasks', task)
export const getTask = (taskId: number) => request<TaskDetail>('GET', `/tasks/${taskId}`)
export const sendMessage = (taskId: number, text: string) =>
  request<TaskDetail>('POST', `/tasks/${taskId}/messages`, { text })
export const stopTask = (taskId: number) => request<TaskDetail>('POST', `/tasks/${taskId}/stop`)
export const archiveTask = (taskId: number) =>
  request<TaskDetail>('POST', `/tasks/${taskId}/archive`)
export const getDiff = (taskId: number) => request<TaskDiff>('GET', `/tasks/${taskId}/diff`)

const EVENT_PAGE_SIZE = 500

/** Fetch all events of a task with an id greater than `after`, following pages until done. */
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
