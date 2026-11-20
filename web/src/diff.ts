/** Split git's unified diff into files for the Diff tab. Pure functions only. */

export type DiffLineKind = 'added' | 'removed' | 'hunk' | 'context' | 'meta'

export interface DiffLine {
  kind: DiffLineKind
  text: string
}

export interface DiffFile {
  path: string
  /** The path before a rename; null when the file was not renamed. */
  previousPath: string | null
  added: number
  removed: number
  lines: DiffLine[]
}

const FILE_HEADER = /^diff --git a\/(.+) b\/(.+)$/
// Keep only these header lines of a file; the others (index, rename, --- and +++) are hidden
// because the path is already in the title.
const NOTE_PREFIXES = ['new file mode', 'deleted file mode', 'Binary files ', '\\ No newline']

function lineKind(line: string): DiffLineKind {
  if (line.startsWith('@@')) return 'hunk'
  if (line.startsWith('+')) return 'added'
  if (line.startsWith('-')) return 'removed'
  return 'context'
}

/** Split by file. Content that is not understood (not starting with `diff --git`) is skipped. */
export function parseDiff(text: string): DiffFile[] {
  const files: DiffFile[] = []
  let current: DiffFile | null = null
  let isInHunk = false
  for (const line of text.split('\n')) {
    const header = FILE_HEADER.exec(line)
    if (header) {
      const [, from, to] = header
      current = { path: to, previousPath: from === to ? null : from, added: 0, removed: 0, lines: [] }
      files.push(current)
      isInHunk = false
      continue
    }
    if (current === null) continue
    if (line.startsWith('@@')) isInHunk = true
    if (!isInHunk) {
      if (NOTE_PREFIXES.some((prefix) => line.startsWith(prefix))) {
        current.lines.push({ kind: 'meta', text: line })
      }
      continue
    }
    if (line.startsWith('\\ No newline')) {
      current.lines.push({ kind: 'meta', text: line })
      continue
    }
    const kind = lineKind(line)
    if (kind === 'added') current.added += 1
    if (kind === 'removed') current.removed += 1
    current.lines.push({ kind, text: line })
  }
  // The empty string after the final newline is not part of the diff.
  for (const file of files) {
    const last = file.lines[file.lines.length - 1]
    if (last && last.kind === 'context' && last.text === '') file.lines.pop()
  }
  return files
}
