import { describe, expect, test, vi } from 'vitest'

import { closedNotice, ShellConnection, shellUrl, type SocketLike } from './shell'

class FakeSocket implements SocketLike {
  binaryType: BinaryType = 'blob'
  readyState = 1
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  sent: unknown[] = []
  send(data: unknown): void {
    this.sent.push(data)
  }
  close(): void {
    this.readyState = 3
    this.onclose?.({} as CloseEvent)
  }
  receive(data: string | ArrayBuffer): void {
    this.onmessage?.({ data } as MessageEvent)
  }
  drop(): void {
    this.readyState = 3
    this.onclose?.({} as CloseEvent)
  }
}

function connect() {
  const socket = new FakeSocket()
  const handlers = { onOpened: vi.fn(), onData: vi.fn(), onClosed: vi.fn(), onLost: vi.fn() }
  const connection = new ShellConnection(socket, handlers)
  return { socket, handlers, connection }
}

test('the shell is on the page’s own host, over wss when the page is https', () => {
  expect(shellUrl({ protocol: 'http:', host: '127.0.0.1:8765' }, 7, { rows: 24, cols: 80 })).toBe(
    'ws://127.0.0.1:8765/api/tasks/7/shell?rows=24&cols=80',
  )
  expect(shellUrl({ protocol: 'https:', host: 'localhost:8765' }, 7, { rows: 1, cols: 2 })).toMatch(
    /^wss:\/\/localhost:8765\//,
  )
})

describe('what the page says when the shell ends', () => {
  test('a restart reconnects without a notice; the rest explain what happened', () => {
    expect(closedNotice({ type: 'closed', reason: 'restarted', exit_code: null })).toBeNull()
    expect(closedNotice({ type: 'closed', reason: 'exited', exit_code: 3 })).toBe(
      'Shell exited with code 3. Press Restart to start a new one.',
    )
    expect(closedNotice({ type: 'closed', reason: 'archived', exit_code: null })).toMatch(/archived/)
    expect(closedNotice({ type: 'closed', reason: 'shutdown', exit_code: null })).toMatch(/stopped/)
    expect(closedNotice({ type: 'closed', reason: 'unavailable', exit_code: null })).toMatch(/no worktree/)
  })
})

describe('the connection', () => {
  test('terminal bytes and control messages go to their handlers', () => {
    const { socket, handlers } = connect()
    expect(socket.binaryType).toBe('arraybuffer')

    socket.receive(JSON.stringify({ type: 'opened', shell: 'zsh', cwd: '/w' }))
    socket.receive(new Uint8Array([104, 105]).buffer)
    socket.receive(JSON.stringify({ type: 'closed', reason: 'exited', exit_code: 0 }))
    socket.drop()

    expect(handlers.onOpened).toHaveBeenCalledWith({ type: 'opened', shell: 'zsh', cwd: '/w' })
    expect(Array.from(handlers.onData.mock.calls[0][0] as Uint8Array)).toEqual([104, 105])
    expect(handlers.onClosed).toHaveBeenCalledWith({ type: 'closed', reason: 'exited', exit_code: 0 })
    // The daemon said why it closed, so this is not a lost connection.
    expect(handlers.onLost).not.toHaveBeenCalled()
  })

  test('typing is sent as bytes and a resize as JSON', () => {
    const { socket, connection } = connect()

    connection.send('ls\r')
    connection.resize(30, 100)

    expect(Array.from(socket.sent[0] as Uint8Array)).toEqual([108, 115, 13])
    expect(JSON.parse(socket.sent[1] as string)).toEqual({ type: 'resize', rows: 30, cols: 100 })
  })

  test('a connection that drops without a word is lost; one the page closes is not', () => {
    const lost = connect()
    lost.socket.drop()
    expect(lost.handlers.onLost).toHaveBeenCalled()

    const left = connect()
    left.connection.close()
    expect(left.handlers.onLost).not.toHaveBeenCalled()
  })

  test('nothing is sent once the socket is closed', () => {
    const { socket, connection } = connect()
    socket.readyState = 3

    connection.send('x')
    connection.resize(1, 1)

    expect(socket.sent).toEqual([])
  })
})
