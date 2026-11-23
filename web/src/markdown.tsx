// A small, safe markdown subset for agent messages. It builds React elements and never sets HTML,
// so text from the agent cannot inject markup. Supported: paragraphs, headings, bullet and
// numbered lists, tables, fenced code blocks, **bold**, `code` and [links](https://…).
import { Fragment, type ReactNode } from 'react'

type Block =
  | { kind: 'paragraph'; lines: string[] }
  | { kind: 'heading'; text: string }
  | { kind: 'list'; ordered: boolean; start: number; items: string[] }
  | { kind: 'code'; text: string }
  | { kind: 'table'; header: string[]; rows: string[][] }

const BULLET = /^\s*[-*]\s+(.*)$/
const NUMBERED = /^\s*(\d+)[.)]\s+(.*)$/
const HEADING = /^#{1,6}\s+(.*)$/
const FENCE = /^\s*```/
const TABLE_ROW = /^\s*\|.*\|\s*$/
const TABLE_RULE = /^\s*\|?[\s:|-]+\|?\s*$/

function cells(row: string): string[] {
  return row
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim())
}

/** Split text into blocks. Pure; exported for tests. */
export function parseBlocks(text: string): Block[] {
  const blocks: Block[] = []
  const lines = text.replace(/\r\n/g, '\n').split('\n')
  let index = 0
  while (index < lines.length) {
    const line = lines[index]
    if (line.trim() === '') {
      index++
      continue
    }
    if (FENCE.test(line)) {
      const code: string[] = []
      index++
      while (index < lines.length && !FENCE.test(lines[index])) code.push(lines[index++])
      index++ // the closing fence, if any
      blocks.push({ kind: 'code', text: code.join('\n') })
      continue
    }
    const heading = HEADING.exec(line)
    if (heading) {
      blocks.push({ kind: 'heading', text: heading[1] })
      index++
      continue
    }
    if (TABLE_ROW.test(line) && index + 1 < lines.length && TABLE_RULE.test(lines[index + 1])) {
      const header = cells(line)
      const rows: string[][] = []
      index += 2
      while (index < lines.length && TABLE_ROW.test(lines[index])) rows.push(cells(lines[index++]))
      blocks.push({ kind: 'table', header, rows })
      continue
    }
    const ordered = NUMBERED.test(line)
    if (ordered || BULLET.test(line)) {
      const pattern = ordered ? NUMBERED : BULLET
      // A numbered list interrupted by sub-items carries on from its own number, not from 1.
      const start = ordered ? Number(NUMBERED.exec(line)![1]) : 1
      const items: string[] = []
      while (index < lines.length && pattern.test(lines[index])) {
        const match = pattern.exec(lines[index])!
        items.push(ordered ? match[2] : match[1])
        index++
        // An indented line right after an item continues it.
        while (index < lines.length && /^\s{2,}\S/.test(lines[index]) && !BULLET.test(lines[index]) && !NUMBERED.test(lines[index])) {
          items[items.length - 1] += ` ${lines[index].trim()}`
          index++
        }
      }
      blocks.push({ kind: 'list', ordered, start, items })
      continue
    }
    const paragraph: string[] = []
    while (
      index < lines.length &&
      lines[index].trim() !== '' &&
      !FENCE.test(lines[index]) &&
      !HEADING.test(lines[index]) &&
      !BULLET.test(lines[index]) &&
      !NUMBERED.test(lines[index]) &&
      !TABLE_ROW.test(lines[index])
    ) {
      paragraph.push(lines[index++])
    }
    if (paragraph.length === 0) {
      // A table-looking row without a rule under it: keep it as text.
      paragraph.push(lines[index++])
    }
    blocks.push({ kind: 'paragraph', lines: paragraph })
  }
  return blocks
}

const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[[^\]]+\]\((https?:\/\/[^\s)]+)\))/g

/** Inline code, bold and links; everything else stays plain text. */
export function renderInline(text: string): ReactNode[] {
  const nodes: ReactNode[] = []
  let last = 0
  for (const match of text.matchAll(INLINE)) {
    const start = match.index ?? 0
    if (start > last) nodes.push(text.slice(last, start))
    const [whole, code, bold, link, url] = match
    if (code) nodes.push(<code key={start}>{code.slice(1, -1)}</code>)
    // Bold may wrap code or a link, as in **`calc.py`:**; its own text holds no **, so this ends.
    else if (bold) nodes.push(<strong key={start}>{renderInline(bold.slice(2, -2))}</strong>)
    else if (link && url) {
      const label = link.slice(1, link.indexOf(']('))
      nodes.push(
        <a key={start} href={url} target="_blank" rel="noreferrer noopener">
          {label}
        </a>,
      )
    } else nodes.push(whole)
    last = start + whole.length
  }
  if (last < text.length) nodes.push(text.slice(last))
  return nodes
}

/** An agent message rendered from its markdown. */
export function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      {parseBlocks(text).map((block, index) => {
        switch (block.kind) {
          case 'heading':
            return (
              <p className="md-heading" key={index}>
                {renderInline(block.text)}
              </p>
            )
          case 'code':
            return (
              <pre className="md-code" key={index}>
                <code>{block.text}</code>
              </pre>
            )
          case 'list': {
            const items = block.items.map((item, itemIndex) => (
              <li key={itemIndex}>{renderInline(item)}</li>
            ))
            return block.ordered ? (
              <ol key={index} start={block.start}>
                {items}
              </ol>
            ) : (
              <ul key={index}>{items}</ul>
            )
          }
          case 'table':
            return (
              <div className="md-table" key={index}>
                <table>
                  <thead>
                    <tr>
                      {block.header.map((cell, cellIndex) => (
                        <th key={cellIndex}>{renderInline(cell)}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, rowIndex) => (
                      <tr key={rowIndex}>
                        {row.map((cell, cellIndex) => (
                          <td key={cellIndex}>{renderInline(cell)}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          case 'paragraph':
            return (
              <p key={index}>
                {block.lines.map((line, lineIndex) => (
                  <Fragment key={lineIndex}>
                    {lineIndex > 0 && <br />}
                    {renderInline(line)}
                  </Fragment>
                ))}
              </p>
            )
        }
      })}
    </div>
  )
}
