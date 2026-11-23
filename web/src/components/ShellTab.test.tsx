import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'

import * as api from '../api'
import type { SocketLike } from '../shell'
import { ShellTab, themeFromTokens, type TerminalLike } from './ShellTab'

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof api>()),
  restartShell: vi.fn(),
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

class FakeSocket implements SocketLike {
  binaryType: BinaryType = 'blob'
  readyState = 1
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  sent: unknown[] = []
  constructor(readonly url: string) {}
  send(data: unknown): void {
    this.sent.push(data)
  }
  close(): void {
    this.readyState = 3
    this.onclose?.({} as CloseEvent)
  }
  receive(data: string | ArrayBuffer): void {
    act(() => this.onmessage?.({ data } as MessageEvent))
  }
}

function fakeTerminal() {
  let typed: (data: string) => void = () => undefined
  const terminal = {
    rows: 24,
    cols: 80,
    written: [] as string[],
    open: vi.fn(),
    write: vi.fn((data: string | Uint8Array) => {
      terminal.written.push(typeof data === 'string' ? data : new TextDecoder().decode(data))
    }),
    onData: vi.fn((listener: (data: string) => void) => {
      typed = listener
      return { dispose: vi.fn() }
    }),
    onResize: vi.fn(() => ({ dispose: vi.fn() })),
    fit: vi.fn(),
    setTheme: vi.fn(),
    clear: vi.fn(),
    reset: vi.fn(),
    focus: vi.fn(),
    dispose: vi.fn(),
    type: (data: string) => typed(data),
  }
  return terminal
}

async function renderShell(props: { isAgentWorking?: boolean } = {}) {
  const terminal = fakeTerminal()
  const sockets: FakeSocket[] = []
  const createSocket = (url: string) => {
    const socket = new FakeSocket(url)
    sockets.push(socket)
    return socket
  }
  const loadTerminal = () => Promise.resolve(terminal as unknown as TerminalLike)
  render(
    <ShellTab
      taskId={7}
      isAgentWorking={props.isAgentWorking ?? false}
      visible
      createSocket={createSocket}
      loadTerminal={loadTerminal}
    />,
  )
  await vi.waitFor(() => expect(sockets.length).toBe(1))
  return { terminal, sockets }
}

test('connects to the task’s shell at the terminal’s size and shows where it runs', async () => {
  const { terminal, sockets } = await renderShell()

  expect(sockets[0].url).toMatch(/\/api\/tasks\/7\/shell\?rows=24&cols=80$/)
  sockets[0].receive(JSON.stringify({ type: 'opened', shell: 'zsh', cwd: '/work/task-7' }))
  sockets[0].receive(new TextEncoder().encode('hello\r\n').buffer)

  expect(screen.getByRole('status').textContent).toBe('zsh')
  expect(screen.getByText('/work/task-7')).toBeTruthy()
  expect(terminal.written).toContain('hello\r\n')
})

test('what is typed in the terminal goes to the shell', async () => {
  const { terminal, sockets } = await renderShell()

  terminal.type('ls\r')

  expect(Array.from(sockets[0].sent[0] as Uint8Array)).toEqual([108, 115, 13])
})

test('when the shell exits the terminal says so, and Restart starts a new one', async () => {
  vi.mocked(api.restartShell).mockResolvedValue({ restarted: false })
  const { terminal, sockets } = await renderShell()
  sockets[0].receive(JSON.stringify({ type: 'closed', reason: 'exited', exit_code: 0 }))

  expect(screen.getByRole('status').textContent).toBe('Ended')
  expect(terminal.written.join('')).toContain('Shell exited with code 0. Press Restart')

  fireEvent.click(screen.getByRole('button', { name: 'Restart' }))

  await vi.waitFor(() => expect(sockets.length).toBe(2))
  expect(api.restartShell).toHaveBeenCalledWith(7)
  expect(terminal.reset).toHaveBeenCalled()
})

test('a restart from another page reconnects by itself', async () => {
  const { sockets } = await renderShell()

  sockets[0].receive(JSON.stringify({ type: 'closed', reason: 'restarted', exit_code: null }))

  await vi.waitFor(() => expect(sockets.length).toBe(2))
})

test('Clear empties the terminal without touching the shell', async () => {
  const { terminal, sockets } = await renderShell()

  fireEvent.click(screen.getByRole('button', { name: 'Clear' }))

  expect(terminal.clear).toHaveBeenCalled()
  expect(sockets[0].sent).toEqual([])
})

test('while the agent works in the same worktree, the tab warns about the shared commit', async () => {
  await renderShell({ isAgentWorking: true })

  expect(screen.getByRole('note').textContent).toMatch(/end up in the same commit/)
})

test('the terminal takes its colors from the page’s tokens', () => {
  const tokens: Record<string, string> = {
    '--color-surface': ' #16191f',
    '--color-text': '#e6e8ec',
    '--color-accent': '#8b8ef0',
  }
  const theme = themeFromTokens({ getPropertyValue: (name: string) => tokens[name] ?? '' })

  // A dark screen keeps xterm's own palette.
  expect(theme).toEqual({
    background: '#16191f',
    foreground: '#e6e8ec',
    cursor: '#8b8ef0',
    selectionBackground: '#8b8ef055',
  })
})

test('on a light screen, white and yellow text are darkened so they stay readable', () => {
  const theme = themeFromTokens({
    getPropertyValue: (name: string) => (name === '--color-surface' ? '#ffffff' : ''),
  })

  expect(theme.white).toBe('#5f6672')
  expect(theme.brightYellow).toBe('#8f4700')
})

test('reconnecting starts from a clean screen before the recent output is replayed', async () => {
  const { terminal, sockets } = await renderShell()

  sockets[0].receive(JSON.stringify({ type: 'opened', shell: 'zsh', cwd: '/w' }))

  expect(terminal.reset).toHaveBeenCalled()
})
