import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'

import * as api from '../api'
import { makeCard, makeDetail, makeEvent } from '../testing'
import type { Project } from '../types'
import { AddProjectForm } from './AddProjectForm'
import { Board } from './Board'
import { NewTaskDialog } from './NewTaskDialog'
import { TaskCard } from './TaskCard'
import { TaskPage } from './TaskPage'

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  addProject: vi.fn(),
  createTask: vi.fn(),
  getTask: vi.fn(),
  listEventsAfter: vi.fn(),
  sendMessage: vi.fn(),
  stopTask: vi.fn(),
  archiveTask: vi.fn(),
}))

const project: Project = {
  id: 1,
  name: 'demo',
  repo_path: '/repos/demo',
  github_repo: 'owner/demo',
  default_branch: 'main',
  require_review: false,
}
const agents = [
  { agent_type: 'claude_code', reports_cost: true },
  { agent_type: 'codex', reports_cost: false },
]

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function button(name: string): HTMLButtonElement {
  return screen.getByRole('button', { name }) as HTMLButtonElement
}

const onTaskChanged = vi.fn()

function renderTaskPage(card = makeCard()) {
  vi.mocked(api.getTask).mockResolvedValue(makeDetail(card))
  vi.mocked(api.listEventsAfter).mockResolvedValue([
    makeEvent(1, 'agent_message', { kind: 'message', text: 'working on it' }),
    makeEvent(2, 'pushed', { branch: 'asv/task-7', head_sha: 'abcdef123456' }),
  ])
  return render(
    <TaskPage
      taskId={card.id}
      card={card}
      project={project}
      connectionCount={1}
      subscribeToEvents={() => () => undefined}
      onTaskChanged={onTaskChanged}
    />,
  )
}

test('a card shows the title, branch, label from the api and estimated cost', () => {
  render(<TaskCard card={makeCard({ label: 'Fixing CI' })} />)

  expect(screen.getByText('Add a power function')).toBeTruthy()
  expect(screen.getByText('asv/task-7-add-power')).toBeTruthy()
  expect(screen.getByText('Fixing CI')).toBeTruthy()
  expect(screen.getByText(/~\$0\.42/)).toBeTruthy()
  expect(screen.queryByText(/PR #/)).toBeNull()
})

test('a card for an agent without cost shows tokens, and the pr once there is one', () => {
  const card = makeCard({ agent_type: 'codex', cost_usd: null, pr_number: 12, pr_state: 'open' })

  render(<TaskCard card={card} />)

  expect(screen.getByText(/12\.3k tok/)).toBeTruthy()
  expect(screen.queryByText(/\$/)).toBeNull()
  expect(screen.getByText(/PR #12/)).toBeTruthy()
})

test('the board puts each card in the column the api names', () => {
  const cards = [
    makeCard({ id: 1, title: 'one', column: 'Needs you', label: 'Stopped' }),
    makeCard({ id: 2, title: 'two', column: 'Ready to merge', label: 'Ready' }),
    makeCard({ id: 3, title: 'three', column: 'Archive', label: 'Merged' }),
  ]

  render(<Board cards={cards} projects={[project]} />)

  expect(screen.getByRole('region', { name: 'Needs you' }).textContent).toContain('one')
  expect(screen.getByRole('region', { name: 'Ready to merge' }).textContent).toContain('two')
  expect(screen.queryByText('three')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: /Archive/ }))
  expect(screen.getByRole('region', { name: 'Archive' }).textContent).toContain('three')
})

test('a new task is sent with the chosen project and agent', async () => {
  const created = vi.fn()
  vi.mocked(api.createTask).mockResolvedValue(makeDetail())
  render(
    <NewTaskDialog projects={[project]} agents={agents} onCreated={created} onClose={vi.fn()} />,
  )

  fireEvent.change(screen.getByLabelText('Agent'), { target: { value: 'codex' } })
  fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'add cube(x)' } })
  fireEvent.click(button('Start task'))

  await waitFor(() => expect(created).toHaveBeenCalled())
  expect(api.createTask).toHaveBeenCalledWith({
    project_id: 1,
    agent_type: 'codex',
    title: null,
    prompt: 'add cube(x)',
  })
})

test('a refused task shows the reason and keeps the form open', async () => {
  const created = vi.fn()
  vi.mocked(api.createTask).mockRejectedValue(
    new api.ApiError(422, 'invalid_request', 'unknown agent type: nope'),
  )
  render(
    <NewTaskDialog projects={[project]} agents={agents} onCreated={created} onClose={vi.fn()} />,
  )

  fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'do it' } })
  fireEvent.click(button('Start task'))

  expect((await screen.findByRole('alert')).textContent).toBe('unknown agent type: nope')
  expect(created).not.toHaveBeenCalled()
  expect(button('Start task').disabled).toBe(false)
})

test('a project is added with its path and review setting', async () => {
  const added = vi.fn()
  vi.mocked(api.addProject).mockResolvedValue(project)
  render(<AddProjectForm onAdded={added} onClose={vi.fn()} />)

  fireEvent.change(screen.getByLabelText('Repo path'), { target: { value: ' /repos/demo ' } })
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(button('Add project'))

  await waitFor(() => expect(added).toHaveBeenCalledWith(project))
  expect(api.addProject).toHaveBeenCalledWith({ repo_path: '/repos/demo', require_review: true })
})

test('a project that cannot be added shows the reason', async () => {
  vi.mocked(api.addProject).mockRejectedValue(
    new api.ApiError(409, 'project_exists', '/repos/demo is already project 1'),
  )
  render(<AddProjectForm onAdded={vi.fn()} onClose={vi.fn()} />)

  fireEvent.change(screen.getByLabelText('Repo path'), { target: { value: '/repos/demo' } })
  fireEvent.click(button('Add project'))

  expect((await screen.findByRole('alert')).textContent).toBe('/repos/demo is already project 1')
})

test('stop can only be pressed while a run is active', async () => {
  renderTaskPage(makeCard({ has_open_run: true }))
  await screen.findByText('working on it')
  expect(button('Stop').disabled).toBe(false)
  cleanup()

  renderTaskPage(makeCard({ has_open_run: false, column: 'Needs you', label: 'Stopped' }))
  await screen.findByText('working on it')
  expect(button('Stop').disabled).toBe(true)
})

test('the task page shows the log and the pull request link', async () => {
  renderTaskPage(makeCard({ pr_number: 12, pr_state: 'open' }))

  expect(await screen.findByText('working on it')).toBeTruthy()
  expect(screen.getByText(/pushed asv\/task-7 @ abcdef12/)).toBeTruthy()
  const link = screen.getByRole('link', { name: /PR #12/ }) as HTMLAnchorElement
  expect(link.href).toBe('https://github.com/owner/demo/pull/12')
})

test('a refused message shows the reason and keeps the text', async () => {
  vi.mocked(api.sendMessage).mockRejectedValue(
    new api.ApiError(409, 'run_already_active', 'task 7 already has an active run'),
  )
  renderTaskPage()
  await screen.findByText('working on it')
  const input = screen.getByLabelText('Message') as HTMLTextAreaElement

  fireEvent.change(input, { target: { value: 'also add tests' } })
  fireEvent.click(button('Send'))

  expect((await screen.findByRole('alert')).textContent).toBe('task 7 already has an active run')
  expect(api.sendMessage).toHaveBeenCalledWith(7, 'also add tests')
  expect(input.value).toBe('also add tests')
})

test('events can be filtered by type', async () => {
  renderTaskPage()
  await screen.findByText('working on it')

  fireEvent.click(screen.getByRole('tab', { name: 'Events' }))
  expect(screen.getAllByRole('row')).toHaveLength(3)
  fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'pushed' } })

  expect(screen.getAllByRole('row')).toHaveLength(2)
})

test('a successful stop reports the new state so the board card follows', async () => {
  const stopped = makeDetail({ has_open_run: false, column: 'Needs you', label: 'Stopped' })
  vi.mocked(api.stopTask).mockResolvedValue(stopped)
  renderTaskPage()
  await screen.findByText('working on it')

  fireEvent.click(button('Stop'))

  await waitFor(() => expect(onTaskChanged).toHaveBeenCalledWith(stopped))
})

test('events pushed while a catch-up fails do not hide the gap from the next catch-up', async () => {
  let push: (event: ReturnType<typeof makeEvent>) => void = () => undefined
  const subscribe = (listener: typeof push) => {
    push = listener
    return () => undefined
  }
  const page = (connectionCount: number) => (
    <TaskPage
      taskId={7}
      card={makeCard()}
      project={project}
      connectionCount={connectionCount}
      subscribeToEvents={subscribe}
      onTaskChanged={vi.fn()}
    />
  )
  vi.mocked(api.getTask).mockResolvedValue(makeDetail())
  vi.mocked(api.listEventsAfter).mockResolvedValueOnce([makeEvent(100, 'pushed', {})])
  const view = render(page(1))
  await waitFor(() => expect(api.listEventsAfter).toHaveBeenCalledTimes(1))

  // 重連後的補抓失敗，同時推送來了第 121 筆。
  vi.mocked(api.listEventsAfter).mockRejectedValueOnce(new Error('offline'))
  view.rerender(page(2))
  await waitFor(() => expect(api.listEventsAfter).toHaveBeenCalledTimes(2))
  push(makeEvent(121, 'pushed', {}))
  vi.mocked(api.listEventsAfter).mockResolvedValueOnce([])
  view.rerender(page(3))

  await waitFor(() => expect(api.listEventsAfter).toHaveBeenCalledTimes(3))
  expect(vi.mocked(api.listEventsAfter).mock.calls.map(([, after]) => after)).toEqual([0, 100, 100])
})

test('a form cannot be closed while its request is in flight', async () => {
  const closed = vi.fn()
  vi.mocked(api.addProject).mockReturnValue(new Promise(() => undefined))
  render(<AddProjectForm onAdded={vi.fn()} onClose={closed} />)

  fireEvent.change(screen.getByLabelText('Repo path'), { target: { value: '/repos/demo' } })
  fireEvent.click(button('Add project'))

  await waitFor(() => expect(button('Cancel').disabled).toBe(true))
  fireEvent(screen.getByRole('dialog', { hidden: true }), new Event('cancel', { cancelable: true }))
  expect(closed).not.toHaveBeenCalled()
})
