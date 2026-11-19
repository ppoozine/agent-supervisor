import { useEffect, useMemo, useState } from 'react'

import { errorMessage, getDiff } from '../api'
import { parseDiff } from '../diff'
import type { TaskDiff } from '../types'

interface DiffTabProps {
  taskId: number
  /** 任務最新的 commit；變了就重新抓，開著的分頁才會跟上新的 push。 */
  headSha: string | null
  connectionCount: number
}

/** 任務的 branch 相對於基底分支的變更，依檔案分段。只包含已經 commit 的內容。 */
export function DiffTab({ taskId, headSha, connectionCount }: DiffTabProps) {
  const [loaded, setLoaded] = useState<TaskDiff | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let isCurrent = true
    getDiff(taskId)
      .then((result) => {
        if (!isCurrent) return
        setLoaded(result)
        setError(null)
      })
      .catch((failure: unknown) => isCurrent && setError(errorMessage(failure)))
    return () => {
      isCurrent = false
    }
  }, [taskId, headSha, connectionCount])

  const files = useMemo(() => (loaded ? parseDiff(loaded.diff) : []), [loaded])

  if (error) {
    return (
      <div className="alert" role="alert">
        {error}
      </div>
    )
  }
  if (loaded === null) return <p className="empty">Loading…</p>
  if (files.length === 0) {
    return <p className="empty">沒有變更：這個任務還沒有相對於 {loaded.base_branch} 的 commit。</p>
  }
  return (
    <div className="diff">
      <p className="diff-summary">
        {files.length} {files.length === 1 ? 'file' : 'files'} changed against {loaded.base_branch}
        {loaded.is_truncated && ' · diff 太長，後面的部分沒有顯示'}
      </p>
      {files.map((file) => (
        <section className="diff-file" key={file.path} aria-label={file.path}>
          <header>
            <span className="diff-path">
              {file.previousPath && `${file.previousPath} → `}
              {file.path}
            </span>
            <span className="diff-added">+{file.added}</span>
            <span className="diff-removed">−{file.removed}</span>
          </header>
          <pre>
            {file.lines.map((line, index) => (
              <span className={`diff-line diff-${line.kind}`} key={index}>
                {line.text || ' '}
                {'\n'}
              </span>
            ))}
          </pre>
        </section>
      ))}
    </div>
  )
}
