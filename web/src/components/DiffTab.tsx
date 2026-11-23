import { useEffect, useMemo, useState } from 'react'

import { errorMessage, getDiff } from '../api'
import { parseDiff, type DiffLine } from '../diff'
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
  if (loaded === null) {
    return (
      <div className="skeleton" aria-busy="true">
        <span className="sr-only">Loading…</span>
        <span className="w-1/2" />
        <span className="w-full" />
        <span className="w-3/4" />
      </div>
    )
  }
  if (files.length === 0) {
    return (
      <p className="empty">
        No changes: this task has no commits against {loaded.base_branch} yet.
      </p>
    )
  }
  return (
    <div className="diff">
      <p className="diff-summary">
        {files.length} {files.length === 1 ? 'file' : 'files'} changed against {loaded.base_branch}{' '}
        ·{' '}
        <span className="diff-added">+{files.reduce((total, file) => total + file.added, 0)}</span>{' '}
        <span className="diff-removed">
          −{files.reduce((total, file) => total + file.removed, 0)}
        </span>
        {loaded.is_truncated && ' · diff too long, the rest is not shown'}
      </p>
      <nav className="diff-files" aria-label="Changed files">
        {files.map((file, index) => (
          <a
            key={file.path}
            href={`#diff-file-${taskId}-${index}`}
            onClick={(event) => {
              event.preventDefault()
              document
                .getElementById(`diff-file-${taskId}-${index}`)
                ?.scrollIntoView({ block: 'start' })
            }}
          >
            <span className="diff-path">{file.path}</span>
            <span className="diff-added">+{file.added}</span>
            <span className="diff-removed">−{file.removed}</span>
          </a>
        ))}
      </nav>
      {files.map((file, index) => (
        <section
          id={`diff-file-${taskId}-${index}`}
          className="diff-file"
          key={file.path}
          aria-label={file.path}
        >
          <details open>
            <summary>
              <span aria-hidden="true">▾</span>
              <span className="diff-path">
                {file.previousPath && `${file.previousPath} → `}
                {file.path}
              </span>
              <span className="diff-added">+{file.added}</span>
              <span className="diff-removed">−{file.removed}</span>
            </summary>
            <div className="diff-scroll">
              <table className="diff-table">
                <tbody>
                  {file.lines.map((line, index) => (
                    <DiffRow line={line} key={index} />
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </section>
      ))}
    </div>
  )
}

const SIGN: Partial<Record<DiffLine['kind'], string>> = { added: '+', removed: '-' }

/** One diff line as GitHub shows it: old and new line numbers, the sign, then the code. */
function DiffRow({ line }: { line: DiffLine }) {
  if (line.kind === 'hunk' || line.kind === 'meta') {
    return (
      <tr className={`diff-row is-${line.kind}`}>
        <td className="diff-num" colSpan={2} />
        <td className="diff-code" colSpan={2}>
          {line.content}
        </td>
      </tr>
    )
  }
  return (
    <tr className={`diff-row is-${line.kind}`}>
      <td className="diff-num">{line.oldNumber}</td>
      <td className="diff-num">{line.newNumber}</td>
      <td className="diff-sign">{SIGN[line.kind] ?? ' '}</td>
      <td className="diff-code">{line.content || ' '}</td>
    </tr>
  )
}

