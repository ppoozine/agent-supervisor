import { afterEach, beforeEach, expect, test, vi } from 'vitest'

import { connectStream, type StreamHandlers } from './stream'
import { makeCard, makeEvent } from './testing'

/** 假的 EventSource：測試自己決定什麼時候連上、收到訊息、出錯。 */
class FakeSource {
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  isClosed = false
  private listeners = new Map<string, (message: { data: string }) => void>()

  addEventListener(kind: string, listener: (message: { data: string }) => void): void {
    this.listeners.set(kind, listener)
  }

  emit(kind: string, data: unknown): void {
    this.listeners.get(kind)?.({ data: JSON.stringify(data) })
  }

  close(): void {
    this.isClosed = true
  }
}

function setUp() {
  const sources: FakeSource[] = []
  const calls: string[] = []
  const handlers: StreamHandlers = {
    onOpen: () => calls.push('open'),
    onEvent: (event) => calls.push(`event ${event.id}`),
    onTask: (card) => calls.push(`task ${card.id}`),
    onStatus: (status) => calls.push(status),
  }
  const stop = connectStream(
    handlers,
    () => {
      const source = new FakeSource()
      sources.push(source)
      return source as unknown as EventSource
    },
    2000,
  )
  return { sources, calls, stop }
}

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

test('messages are passed on by kind', () => {
  const { sources, calls } = setUp()

  sources[0].onopen?.()
  sources[0].emit('event', makeEvent(5, 'pushed', {}))
  sources[0].emit('task', makeCard({ id: 9 }))

  expect(calls).toEqual(['connecting', 'live', 'open', 'event 5', 'task 9'])
})

test('a failed connection is replaced, and the data is loaded again once it is back', () => {
  const { sources, calls } = setUp()
  sources[0].onopen?.()

  // daemon 關掉：瀏覽器自己不會再試，要換一條新的連線。
  sources[0].onerror?.()
  expect(sources[0].isClosed).toBe(true)
  expect(sources).toHaveLength(1)
  vi.advanceTimersByTime(2000)
  expect(sources).toHaveLength(2)
  sources[1].onerror?.()
  vi.advanceTimersByTime(2000)
  sources[2].onopen?.()

  expect(calls).toEqual(['connecting', 'live', 'open', 'offline', 'offline', 'live', 'open'])
})

test('stopping ends the connection and the retries', () => {
  const { sources, stop } = setUp()
  sources[0].onerror?.()

  stop()
  vi.advanceTimersByTime(10_000)

  expect(sources).toHaveLength(1)
})
