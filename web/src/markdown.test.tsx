import { render } from '@testing-library/react'
import { expect, test } from 'vitest'

import { Markdown, parseBlocks } from './markdown'

test('blocks: paragraphs, headings, lists, tables and code', () => {
  const blocks = parseBlocks(
    [
      '## Done',
      'First line',
      'second line',
      '',
      '- one',
      '- two',
      '  continued',
      '1. first',
      '2. second',
      '',
      '| Name | Result |',
      '| --- | :---: |',
      '| a | ok |',
      '',
      '```python',
      'print(1)',
      '```',
    ].join('\n'),
  )

  expect(blocks).toEqual([
    { kind: 'heading', text: 'Done' },
    { kind: 'paragraph', lines: ['First line', 'second line'] },
    { kind: 'list', ordered: false, start: 1, items: ['one', 'two continued'] },
    { kind: 'list', ordered: true, start: 1, items: ['first', 'second'] },
    { kind: 'table', header: ['Name', 'Result'], rows: [['a', 'ok']] },
    { kind: 'code', text: 'print(1)' },
  ])
})

test('bold, inline code and links render as elements', () => {
  const { container } = render(
    <Markdown text={'Ran **59 tests**, see `pytest -q` and [the PR](https://github.com/o/r/pull/1).'} />,
  )

  expect(container.querySelector('strong')?.textContent).toBe('59 tests')
  expect(container.querySelector('code')?.textContent).toBe('pytest -q')
  const link = container.querySelector('a')!
  expect(link.getAttribute('href')).toBe('https://github.com/o/r/pull/1')
  expect(link.getAttribute('rel')).toContain('noopener')
})

test('HTML in the text stays text, and only http(s) links become links', () => {
  const { container } = render(
    <Markdown text={'<img src=x onerror=alert(1)> [click](javascript:alert(1)) **<b>x</b>**'} />,
  )

  expect(container.querySelector('img')).toBeNull()
  expect(container.querySelector('a')).toBeNull()
  expect(container.querySelector('b')).toBeNull()
  expect(container.textContent).toContain('<img src=x onerror=alert(1)>')
})

test('a row that looks like a table without a rule under it stays text', () => {
  expect(parseBlocks('| just a pipe |')).toEqual([{ kind: 'paragraph', lines: ['| just a pipe |'] }])
})

test('a numbered list broken up by sub-items keeps counting', () => {
  const { container } = render(<Markdown text={'1. **One**\n   - detail\n2. **Two**\n   - more'} />)

  expect([...container.querySelectorAll('ol')].map((list) => list.getAttribute('start'))).toEqual(['1', '2'])
})

