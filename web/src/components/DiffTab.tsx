import { useEffect, useMemo, useState } from 'react'

import { errorMessage, getDiff } from '../api'
import { parseDiff } from '../diff'
import type { TaskDiff } from '../types'

interface DiffTabProps {
  taskId: number
  /** The task's latest commit; refetch when it changes so an open tab keeps up with new pushes. */
  headSha: string | null
  connectionCount: number
}

/** Changes on the task's branch relative to the base branch, by file. Only committed content. */
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
    return <p className="empty">No changes: this task has no commits against {loaded.base_branch} yet.</p>
  }
  return (
    <div className="diff">
      <p className="diff-summary">
        {files.length} {files.length === 1 ? 'file' : 'files'} changed against {loaded.base_branch}
        {loaded.is_truncated && ' · diff too long, the rest is not shown'}
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
