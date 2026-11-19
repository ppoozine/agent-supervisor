import { expect, test } from 'vitest'

import { parseDiff } from './diff'

const TWO_FILES = `diff --git a/calc.py b/calc.py
index 1111111..2222222 100644
--- a/calc.py
+++ b/calc.py
@@ -1,3 +1,4 @@
 def add(a, b):
-    return a - b
+    # fixed
+    return a + b
diff --git a/notes.txt b/notes.txt
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/notes.txt
@@ -0,0 +1 @@
+hello
\\ No newline at end of file
`

test('a diff is split into files with their line counts', () => {
  const files = parseDiff(TWO_FILES)

  expect(files.map((file) => [file.path, file.added, file.removed])).toEqual([
    ['calc.py', 2, 1],
    ['notes.txt', 1, 0],
  ])
})

test('header lines are dropped and the --- and +++ lines are not counted as changes', () => {
  const [calc, notes] = parseDiff(TWO_FILES)

  expect(calc.lines.map((line) => line.kind)).toEqual([
    'hunk',
    'context',
    'removed',
    'added',
    'added',
  ])
  expect(notes.lines.map((line) => [line.kind, line.text])).toEqual([
    ['meta', 'new file mode 100644'],
    ['hunk', '@@ -0,0 +1 @@'],
    ['added', '+hello'],
    ['meta', '\\ No newline at end of file'],
  ])
})

test('a rename keeps both paths and a binary file is noted', () => {
  const files = parseDiff(`diff --git a/old.txt b/new.txt
similarity index 100%
rename from old.txt
rename to new.txt
diff --git a/logo.png b/logo.png
index 1111111..2222222 100644
Binary files a/logo.png and b/logo.png differ
`)

  expect(files.map((file) => [file.path, file.previousPath])).toEqual([
    ['new.txt', 'old.txt'],
    ['logo.png', null],
  ])
  expect(files[1].lines).toEqual([
    { kind: 'meta', text: 'Binary files a/logo.png and b/logo.png differ' },
  ])
})

test('an empty diff has no files', () => {
  expect(parseDiff('')).toEqual([])
})
