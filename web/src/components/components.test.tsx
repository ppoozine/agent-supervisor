import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'

import * as api from '../api'
import { makeCard, makeDetail, makeEvent } from '../testing'
import type { Project, Setup, ToolStatus } from '../types'
import { AddProjectForm } from './AddProjectForm'
import { Board } from './Board'
import { DiffTab } from './DiffTab'
import { NewTaskDialog } from './NewTaskDialog'
import { SettingsPage } from './SettingsPage'
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
  getDiff: vi.fn(),
  getSetup: vi.fn(),
  checkSetup: vi.fn(),
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
  { agent_type: 'claude_code', display_name: 'Claude Code', usable: true, reports_cost: true },
  { agent_type: 'codex', display_name: 'Codex', usable: true, reports_cost: false },
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

  // The catch-up after reconnecting fails, while event 121 arrives by push.
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

test('archiving a task with an open pull request asks first', async () => {
  const archived = makeDetail({ archived_at: 't', column: 'Archive', label: 'Archived' })
  vi.mocked(api.archiveTask).mockResolvedValue(archived)
  renderTaskPage(makeCard({ pr_number: 12, pr_state: 'open' }))
  await screen.findByText('working on it')

  fireEvent.click(button('Archive'))
  expect(screen.getByText(/PR #12 is still open/)).toBeTruthy()
  fireEvent.click(button('Cancel'))

  expect(screen.queryByText(/PR #12 is still open/)).toBeNull()
  expect(api.archiveTask).not.toHaveBeenCalled()

  fireEvent.click(button('Archive'))
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Archive' }))

  await waitFor(() => expect(onTaskChanged).toHaveBeenCalledWith(archived))
  expect(api.archiveTask).toHaveBeenCalledWith(7)
  expect(screen.queryByRole('dialog')).toBeNull()
})

test('archiving a task without an open pull request does not ask', async () => {
  const archived = makeDetail({ archived_at: 't', column: 'Archive', label: 'Closed' })
  vi.mocked(api.archiveTask).mockResolvedValue(archived)
  renderTaskPage(makeCard({ pr_number: 12, pr_state: 'closed' }))
  await screen.findByText('working on it')

  fireEvent.click(button('Archive'))

  await waitFor(() => expect(api.archiveTask).toHaveBeenCalledWith(7))
  expect(screen.queryByRole('dialog')).toBeNull()
})

const ONE_FILE = `diff --git a/calc.py b/calc.py
index 1111111..2222222 100644
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
`

function diffOf(diff: string, isTruncated = false) {
  return { diff, is_truncated: isTruncated, base_branch: 'main' }
}

test('the diff tab shows each changed file with its added and removed lines', async () => {
  vi.mocked(api.getDiff).mockResolvedValue(diffOf(ONE_FILE))
  render(<DiffTab taskId={7} headSha="sha-1" connectionCount={1} />)

  const file = await screen.findByRole('region', { name: 'calc.py' })

  expect(within(file).getByText('+1')).toBeTruthy()
  expect(within(file).getByText('−1')).toBeTruthy()
  expect(within(file).getByText(/return a \+ b/).className).toContain('diff-added')
  expect(within(file).getByText(/return a - b/).className).toContain('diff-removed')
  expect(screen.getByText(/1 file changed against main/)).toBeTruthy()
})

test('the diff tab reloads when the task gets a new commit', async () => {
  vi.mocked(api.getDiff).mockResolvedValue(diffOf(ONE_FILE))
  const { rerender } = render(<DiffTab taskId={7} headSha="sha-1" connectionCount={1} />)
  await screen.findByRole('region', { name: 'calc.py' })

  vi.mocked(api.getDiff).mockResolvedValue(diffOf(ONE_FILE.replaceAll('calc.py', 'other.py')))
  rerender(<DiffTab taskId={7} headSha="sha-2" connectionCount={1} />)

  expect(await screen.findByRole('region', { name: 'other.py' })).toBeTruthy()
  expect(api.getDiff).toHaveBeenCalledTimes(2)
})

test('a task without commits says there are no changes instead of failing', async () => {
  vi.mocked(api.getDiff).mockResolvedValue(diffOf(''))
  render(<DiffTab taskId={7} headSha={null} connectionCount={1} />)

  expect(await screen.findByText(/No changes/)).toBeTruthy()
})

test('a truncated diff says so', async () => {
  vi.mocked(api.getDiff).mockResolvedValue(diffOf(ONE_FILE, true))
  render(<DiffTab taskId={7} headSha="sha-1" connectionCount={1} />)

  expect(await screen.findByText(/the rest is not shown/)).toBeTruthy()
})


test('the new task form offers only the agents that can be used', () => {
  const someReady = [agents[0], { ...agents[1], usable: false }]
  render(
    <NewTaskDialog projects={[project]} agents={someReady} onCreated={vi.fn()} onClose={vi.fn()} />,
  )

  const options = within(screen.getByLabelText('Agent')).getAllByRole('option')
  expect(options.map((option) => option.textContent)).toEqual(['Claude Code'])
  expect(screen.getByRole('link', { name: 'See Settings' }).getAttribute('href')).toBe('#/settings')
  expect(button('Start task').disabled).toBe(false)
})

test('the new task form cannot be sent when no agent can be used', () => {
  const noneReady = agents.map((agent) => ({ ...agent, usable: false }))
  render(
    <NewTaskDialog projects={[project]} agents={noneReady} onCreated={vi.fn()} onClose={vi.fn()} />,
  )

  expect(screen.getByText(/No agent can be used yet/)).toBeTruthy()
  expect(button('Start task').disabled).toBe(true)
})

const ready: ToolStatus = {
  usable: true,
  installed: true,
  version: '2.1.325',
  auth: 'subscription',
  auth_label: 'claude.ai (max)',
  detail: null,
  fix: [],
  notice: 'Claude Code is signed in with a Claude subscription.',
}

function makeSetup(codex: Partial<ToolStatus> = {}): Setup {
  return {
    checked_at: new Date().toISOString(),
    agents: [
      { agent_type: 'claude_code', display_name: 'Claude Code', ...ready },
      {
        agent_type: 'codex',
        display_name: 'Codex',
        ...ready,
        usable: false,
        installed: false,
        version: null,
        auth: 'none',
        auth_label: null,
        detail: 'not installed',
        fix: ['npm install -g @openai/codex'],
        notice: null,
        ...codex,
      },
    ],
    github: { ...ready, auth: 'account', auth_label: 'keyring', notice: null },
  }
}

test('the settings page shows each tool, what to run for the ones not ready, and notices', async () => {
  vi.mocked(api.getSetup).mockResolvedValue(makeSetup())
  render(<SettingsPage onChecked={vi.fn()} />)

  const claude = await screen.findByRole('region', { name: 'Claude Code' })
  expect(claude.textContent).toContain('Ready')
  expect(claude.textContent).toContain('Signed in with claude.ai (max)')
  expect(claude.textContent).toContain('signed in with a Claude subscription')
  const codex = screen.getByRole('region', { name: 'Codex' })
  expect(codex.textContent).toContain('Not installed')
  expect(within(codex).getByText('npm install -g @openai/codex')).toBeTruthy()
  expect(screen.getByRole('region', { name: 'gh' }).textContent).toContain('Signed in with keyring')
})

test('checking again shows the new result and tells the page', async () => {
  const checked = vi.fn()
  vi.mocked(api.getSetup).mockResolvedValue(makeSetup())
  vi.mocked(api.checkSetup).mockResolvedValue(
    makeSetup({ usable: true, installed: true, auth: 'subscription', auth_label: 'ChatGPT', fix: [] }),
  )
  render(<SettingsPage onChecked={checked} />)
  await screen.findByRole('region', { name: 'Codex' })

  fireEvent.click(button('Check again'))

  await waitFor(() => expect(checked).toHaveBeenCalled())
  expect(screen.getByRole('region', { name: 'Codex' }).textContent).toContain('Signed in with ChatGPT')
})
