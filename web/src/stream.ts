// The SSE connection to the daemon. Reopens itself after a disconnect and tells the caller to
// refetch on every connect.
import type { Card, EventRow } from './types'

export type StreamStatus = 'connecting' | 'live' | 'offline'

export interface StreamHandlers {
  /** Called on every connect (first and reconnects): refetch to fill in what was missed while disconnected. */
  onOpen(): void
  onEvent(event: EventRow): void
  onTask(card: Card): void
  onStatus(status: StreamStatus): void
}

/** Replaced with a fake connection in tests. */
export type SourceFactory = (url: string) => EventSource

const RETRY_MILLISECONDS = 2000

/** Start receiving pushes; returns a function that ends it. */
export function connectStream(
  handlers: StreamHandlers,
  createSource: SourceFactory = (url) => new EventSource(url),
  retryMilliseconds: number = RETRY_MILLISECONDS,
): () => void {
  let source: EventSource | null = null
  let retryTimer: ReturnType<typeof setTimeout> | undefined
  let isStopped = false

  const open = (): void => {
    const current = createSource('/api/stream')
    source = current
    current.onopen = () => {
      handlers.onStatus('live')
      handlers.onOpen()
    }
    current.addEventListener('event', (message) => {
      handlers.onEvent(JSON.parse((message as MessageEvent<string>).data) as EventRow)
    })
    current.addEventListener('task', (message) => {
      handlers.onTask(JSON.parse((message as MessageEvent<string>).data) as Card)
    })
    current.onerror = () => {
      // The browser only reconnects by itself when an open connection drops; it gives up when
      // the daemon is not running or the response is not 200. So always close and reopen
      // ourselves, and both cases take the same path.
      current.close()
      if (isStopped) return
      handlers.onStatus('offline')
      retryTimer = setTimeout(open, retryMilliseconds)
    }
  }

  handlers.onStatus('connecting')
  open()
  return () => {
    isStopped = true
    clearTimeout(retryTimer)
    source?.close()
  }
}
