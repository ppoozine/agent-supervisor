// 與 daemon 的 SSE 連線。斷線後自己重開，每次連上都通知呼叫端重新抓資料。
import type { Card, EventRow } from './types'

export type StreamStatus = 'connecting' | 'live' | 'offline'

export interface StreamHandlers {
  /** 每次連上（含第一次與重連）都會呼叫：這時要重新抓一次資料，補上斷線期間漏掉的。 */
  onOpen(): void
  onEvent(event: EventRow): void
  onTask(card: Card): void
  onStatus(status: StreamStatus): void
}

/** 測試時換成假的連線。 */
export type SourceFactory = (url: string) => EventSource

const RETRY_MILLISECONDS = 2000

/** 開始接收推送，回傳用來結束的函式。 */
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
      // 瀏覽器只在連線中途斷掉時自己重連；daemon 沒開、拿到的不是 200 時會直接放棄。
      // 所以一律自己關掉重開，兩種情況走同一條路。
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
