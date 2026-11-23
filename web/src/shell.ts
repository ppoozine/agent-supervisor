// The task shell's connection: a WebSocket to the daemon carrying terminal data both ways.
// Binary frames are terminal bytes; text frames are JSON control messages.

/** Why the daemon ended the shell, or why there is none. */
export type ClosedReason = 'exited' | 'restarted' | 'archived' | 'shutdown' | 'unavailable'

export type ShellMessage =
  | { type: 'opened'; shell: string; cwd: string }
  | { type: 'closed'; reason: ClosedReason; exit_code: number | null }

/** Address of a task's shell, on the same host as the page (the daemon only accepts that). */
export function shellUrl(
  location: Pick<Location, 'protocol' | 'host'>,
  taskId: number,
  size: { rows: number; cols: number },
): string {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${location.host}/api/tasks/${taskId}/shell?rows=${size.rows}&cols=${size.cols}`
}

/** What to print in the terminal when the shell ends, or null when the page reconnects by itself. */
export function closedNotice(message: Extract<ShellMessage, { type: 'closed' }>): string | null {
  switch (message.reason) {
    case 'restarted':
      return null
    case 'exited':
      return `Shell exited${message.exit_code === null ? '' : ` with code ${message.exit_code}`}. Press Restart to start a new one.`
    case 'archived':
      return 'The task was archived, so its shell was closed.'
    case 'shutdown':
      return 'agent-supervisor stopped, so the shell was closed.'
    case 'unavailable':
      return "This task has no worktree, so there is no shell."
  }
}

/** The parts of a WebSocket the connection uses, so tests can pass a fake one. */
export interface SocketLike {
  binaryType: BinaryType
  readyState: number
  onopen: ((event: Event) => void) | null
  onmessage: ((event: MessageEvent) => void) | null
  onclose: ((event: CloseEvent) => void) | null
  send(data: string | ArrayBufferLike | Blob | ArrayBufferView): void
  close(): void
}

export interface ShellHandlers {
  onOpened: (message: Extract<ShellMessage, { type: 'opened' }>) => void
  onData: (data: Uint8Array) => void
  onClosed: (message: Extract<ShellMessage, { type: 'closed' }>) => void
  /** The connection dropped without the daemon saying why (e.g. the daemon is restarting). */
  onLost: () => void
}

const OPEN = 1
const encoder = new TextEncoder()

/** One connection to a task's shell. */
export class ShellConnection {
  private closedByDaemon = false
  private closedByPage = false

  constructor(
    private readonly socket: SocketLike,
    handlers: ShellHandlers,
  ) {
    socket.binaryType = 'arraybuffer'
    socket.onmessage = (event) => {
      if (typeof event.data === 'string') {
        const message = JSON.parse(event.data) as ShellMessage
        if (message.type === 'opened') handlers.onOpened(message)
        else {
          this.closedByDaemon = true
          handlers.onClosed(message)
        }
        return
      }
      handlers.onData(new Uint8Array(event.data as ArrayBuffer))
    }
    socket.onclose = () => {
      if (!this.closedByDaemon && !this.closedByPage) handlers.onLost()
    }
  }

  /** Keystrokes or pasted text, as typed into the terminal. */
  send(text: string): void {
    if (this.socket.readyState === OPEN) this.socket.send(encoder.encode(text))
  }

  /** Tell the shell the terminal's new size. */
  resize(rows: number, cols: number): void {
    if (this.socket.readyState === OPEN) this.socket.send(JSON.stringify({ type: 'resize', rows, cols }))
  }

  /** Leave the shell running and disconnect, e.g. when the page goes away. */
  close(): void {
    this.closedByPage = true
    this.socket.close()
  }
}
