import { useEffect, useRef, useState } from 'react'

import { errorMessage, restartShell } from '../api'
import { closedNotice, ShellConnection, shellUrl, type SocketLike } from '../shell'

/** The parts of xterm.js the tab uses; tests pass a fake, since jsdom cannot render a terminal. */
export interface TerminalLike {
  readonly rows: number
  readonly cols: number
  open(element: HTMLElement): void
  write(data: string | Uint8Array): void
  onData(listener: (data: string) => void): { dispose(): void }
  onResize(listener: (size: { rows: number; cols: number }) => void): { dispose(): void }
  /** Resize to fill the element it was opened in. */
  fit(): void
  setTheme(theme: TerminalTheme): void
  clear(): void
  reset(): void
  focus(): void
  dispose(): void
}

export interface TerminalTheme {
  background: string
  foreground: string
  cursor: string
  selectionBackground: string
  white?: string
  brightWhite?: string
  yellow?: string
  brightYellow?: string
}

// xterm's default white and yellow are made for a dark screen and vanish on a light one.
const ON_LIGHT = {
  white: '#5f6672',
  brightWhite: '#16181d',
  yellow: '#7a5800',
  brightYellow: '#8f4700',
}

/** Whether a `#rrggbb` color is light, by its perceived brightness. */
function isLight(color: string): boolean {
  const match = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(color)
  if (!match) return false
  const [red, green, blue] = match.slice(1).map((part) => parseInt(part, 16))
  return 0.299 * red + 0.587 * green + 0.114 * blue > 150
}

/** Load xterm.js only when a shell is first opened; it is not needed anywhere else. */
async function loadXterm(fontFamily: string, theme: TerminalTheme): Promise<TerminalLike> {
  const [{ Terminal }, { FitAddon }] = await Promise.all([
    import('@xterm/xterm'),
    import('@xterm/addon-fit'),
    import('@xterm/xterm/css/xterm.css'),
  ])
  const terminal = new Terminal({ fontFamily, fontSize: 12, theme, cursorBlink: true })
  const fitAddon = new FitAddon()
  terminal.loadAddon(fitAddon)
  return {
    get rows() {
      return terminal.rows
    },
    get cols() {
      return terminal.cols
    },
    open: (element) => terminal.open(element),
    write: (data) => terminal.write(data),
    onData: (listener) => terminal.onData(listener),
    onResize: (listener) => terminal.onResize(listener),
    fit: () => fitAddon.fit(),
    setTheme: (next) => {
      terminal.options.theme = next
    },
    clear: () => terminal.clear(),
    reset: () => terminal.reset(),
    focus: () => terminal.focus(),
    dispose: () => terminal.dispose(),
  }
}

/** Terminal colors from the page's tokens, so the terminal follows the light or dark theme. */
export function themeFromTokens(style: Pick<CSSStyleDeclaration, 'getPropertyValue'>): TerminalTheme {
  const token = (name: string, fallback: string): string =>
    style.getPropertyValue(name).trim() || fallback
  const accent = token('--color-accent', '#4b4fc9')
  const background = token('--color-surface', '#ffffff')
  return {
    background,
    foreground: token('--color-text', '#16181d'),
    cursor: accent,
    // A see-through accent; xterm accepts #rrggbbaa.
    selectionBackground: /^#[0-9a-f]{6}$/i.test(accent) ? `${accent}55` : accent,
    ...(isLight(background) ? ON_LIGHT : {}),
  }
}

type Status = 'connecting' | 'connected' | 'ended' | 'lost'

const RECONNECT_DELAY_MS = 1500

// Module-level, so they are the same on every render and do not reconnect the effect below.
const openWebSocket = (url: string): SocketLike => new WebSocket(url)

interface ShellTabProps {
  taskId: number
  /** The agent is running in the same worktree: warn that changes end up in its commit. */
  isAgentWorking: boolean
  /** Whether the tab is showing; the terminal stays connected while hidden. */
  visible: boolean
  createSocket?: (url: string) => SocketLike
  loadTerminal?: (fontFamily: string, theme: TerminalTheme) => Promise<TerminalLike>
}

/** A terminal in the task's worktree. The shell keeps running when the page goes away. */
export function ShellTab({
  taskId,
  isAgentWorking,
  visible,
  createSocket = openWebSocket,
  loadTerminal = loadXterm,
}: ShellTabProps) {
  const screenRef = useRef<HTMLDivElement>(null)
  const [terminal, setTerminal] = useState<TerminalLike | null>(null)
  const [status, setStatus] = useState<Status>('connecting')
  const [where, setWhere] = useState<{ shell: string; cwd: string } | null>(null)
  const [attempt, setAttempt] = useState(0)
  const [error, setError] = useState<string | null>(null)

  // Create the terminal once.
  useEffect(() => {
    let isCurrent = true
    let created: TerminalLike | null = null
    const root = getComputedStyle(document.documentElement)
    const fontFamily = root.getPropertyValue('--font-mono').trim() || 'monospace'
    loadTerminal(fontFamily, themeFromTokens(root))
      .then((loaded) => {
        if (!isCurrent || !screenRef.current) {
          loaded.dispose()
          return
        }
        created = loaded
        loaded.open(screenRef.current)
        setTerminal(loaded)
      })
      .catch((failure: unknown) => isCurrent && setError(errorMessage(failure)))
    return () => {
      isCurrent = false
      created?.dispose()
    }
  }, [loadTerminal])

  // Follow the system's light or dark theme.
  useEffect(() => {
    if (!terminal || typeof window.matchMedia !== 'function') return
    const query = window.matchMedia('(prefers-color-scheme: dark)')
    const retheme = () => terminal.setTheme(themeFromTokens(getComputedStyle(document.documentElement)))
    query.addEventListener('change', retheme)
    return () => query.removeEventListener('change', retheme)
  }, [terminal])

  // Fit the terminal to its box whenever the box changes size and is showing.
  useEffect(() => {
    const element = screenRef.current
    if (!terminal || !element || !visible) return
    terminal.fit()
    terminal.focus()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => terminal.fit())
    observer.observe(element)
    return () => observer.disconnect()
  }, [terminal, visible])

  // Connect, and connect again after Restart or a dropped connection.
  useEffect(() => {
    if (!terminal) return
    setStatus('connecting')
    let retry: ReturnType<typeof setTimeout> | undefined
    const connection = new ShellConnection(
      createSocket(shellUrl(window.location, taskId, { rows: terminal.rows, cols: terminal.cols })),
      {
        onOpened: (message) => {
          // The scrollback follows; start from a clean screen so a reconnect does not repeat it.
          terminal.reset()
          setStatus('connected')
          setWhere({ shell: message.shell, cwd: message.cwd })
        },
        onData: (data) => terminal.write(data),
        onClosed: (message) => {
          const notice = closedNotice(message)
          if (notice === null) {
            terminal.reset()
            setAttempt((count) => count + 1)
            return
          }
          setStatus('ended')
          terminal.write(`\r\n\x1b[2m${notice}\x1b[0m\r\n`)
        },
        onLost: () => {
          setStatus('lost')
          retry = setTimeout(() => setAttempt((count) => count + 1), RECONNECT_DELAY_MS)
        },
      },
    )
    const typing = terminal.onData((data) => connection.send(data))
    const resizing = terminal.onResize(({ rows, cols }) => connection.resize(rows, cols))
    return () => {
      clearTimeout(retry)
      typing.dispose()
      resizing.dispose()
      connection.close()
    }
  }, [terminal, taskId, attempt, createSocket])

  const restart = async (): Promise<void> => {
    setError(null)
    try {
      const { restarted } = await restartShell(taskId)
      // With no shell running there is nothing to say "restarted"; connect again here.
      if (!restarted) {
        terminal?.reset()
        setAttempt((count) => count + 1)
      }
    } catch (failure) {
      setError(errorMessage(failure))
    }
  }

  const statusText = {
    connecting: 'Connecting…',
    connected: where?.shell ?? 'Connected',
    ended: 'Ended',
    lost: 'Reconnecting…',
  }[status]

  return (
    <div className="shell">
      <div className="shell-toolbar">
        <span className={`shell-status is-${status}`} aria-hidden="true" />
        <span className="shell-name" role="status">
          {statusText}
        </span>
        <span className="shell-cwd" title={where?.cwd}>
          {where?.cwd}
        </span>
        <button className="btn h-7 px-2 text-xs" onClick={() => terminal?.clear()}>
          Clear
        </button>
        <button className="btn h-7 px-2 text-xs" onClick={() => void restart()}>
          Restart
        </button>
      </div>
      {isAgentWorking && (
        <p className="shell-warning" role="note">
          The agent is working in this worktree. Files you change here end up in the same commit.
        </p>
      )}
      {error && (
        <p className="alert m-3" role="alert">
          {error}
        </p>
      )}
      <div className="shell-screen" ref={screenRef} aria-label="Terminal" />
    </div>
  )
}
