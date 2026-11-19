/** 把 git 的 unified diff 拆成一個個檔案，給 Diff 分頁用。全部是純函式。 */

export type DiffLineKind = 'added' | 'removed' | 'hunk' | 'context' | 'meta'

export interface DiffLine {
  kind: DiffLineKind
  text: string
}

export interface DiffFile {
  path: string
  /** 改名前的路徑；沒有改名時是 null。 */
  previousPath: string | null
  added: number
  removed: number
  lines: DiffLine[]
}

const FILE_HEADER = /^diff --git a\/(.+) b\/(.+)$/
// 檔案開頭的說明行裡只留這幾種；其餘的（index、改名、--- 與 +++）不顯示，路徑已經在標題上。
const NOTE_PREFIXES = ['new file mode', 'deleted file mode', 'Binary files ', '\\ No newline']

function lineKind(line: string): DiffLineKind {
  if (line.startsWith('@@')) return 'hunk'
  if (line.startsWith('+')) return 'added'
  if (line.startsWith('-')) return 'removed'
  return 'context'
}

/** 依檔案分段。看不懂的內容（不是以 `diff --git` 開頭）會被略過。 */
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
  // 最後一行換行之後的空字串不是 diff 的內容。
  for (const file of files) {
    const last = file.lines[file.lines.length - 1]
    if (last && last.kind === 'context' && last.text === '') file.lines.pop()
  }
  return files
}
