# agent-supervisor

平行管理多個 AI coding agent 的本機工具：每個任務一個 git worktree，CI 失敗自動回到同一個 agent session 修復，人只處理例外。

完整規格在 `docs/spec.md`。動手前先讀它；規格與本檔衝突時以規格為準，並提醒我更新本檔。

## 目前階段

MVP，版本 `0.x`。依 `docs/spec.md` 的「開發順序」一步一步做，不要提前做後面的步驟，也不要做「不包含」清單裡的功能。

## 技術選型

- Python 3.12+，套件管理用 `uv`
- FastAPI + uvicorn，SSE 推送狀態與 log
- SQLite（標準庫 `sqlite3` 或 `aiosqlite`），不使用 ORM
- 全程 asyncio；背景工作與 API 跑在同一個 process
- 測試用 `pytest` + `pytest-asyncio`，lint / format 用 `ruff`
- 前端（第 5 步才開始）：React + Vite，放在 `web/`

## 專案結構

```
src/agent_supervisor/
  adapters/
    base.py          # AgentAdapter Protocol
    claude_code.py   # 用 subprocess 包 Claude Code CLI
  runner.py          # 啟動、續接 session，寫 events，回填用量
  worktree.py        # git worktree 與 branch 管理
  poller.py          # 輪詢 PR / CI / review 狀態
  dispatcher.py      # CI 失敗 → 建立 ci_failure run
  status.py          # 由狀態推導看板欄位（純函式）
  db.py              # schema 與查詢
  config.py
  api/               # FastAPI 路由與 SSE
tests/
docs/spec.md
```

import 名稱是 `agent_supervisor`。不要用 `supervisor`，PyPI 上有同名的知名套件。

## 必須遵守的設計規則

1. **看板欄位不存資料庫**。一律由 `status.py` 的純函式從 run / PR / CI / review 狀態推導。
2. **Runner 只透過 `AgentAdapter` 介面控制 agent**。Runner 內不得出現任何 Claude Code 專屬的字串或參數。
3. **`events` 表只新增，不修改、不刪除**。
4. **防無限迴圈**：
   - 同一個 CI run 只餵回一次（比對 `last_handled_ci_run_id`）
   - 自動修復最多 3 次（`ci_fix_attempts`）
   - 單一任務累計 `cost_usd` 超過全域上限就停止
   - 以上任一觸發，卡片進 Needs you
5. **GitHub 一律用輪詢**（預設 30 秒），MVP 不做 webhook。
6. **對外部指令的呼叫**（`git`、`gh`、agent CLI）集中在各自的模組，方便測試時替換。

## Claude Code adapter 注意事項

- 用 `asyncio.create_subprocess_exec` 啟動 CLI 的非互動（headless）模式，逐行解析結構化輸出。
- **不要憑印象寫旗標**。實作前先執行 `claude --help` 並查官方文件，確認非互動模式、串流 JSON 輸出、續接 session 的實際參數與輸出格式，把查到的結果記在 `docs/claude-cli-notes.md`。
- 測試不得真的呼叫 CLI：用一個假的可執行檔或 fake adapter 吐出預錄的輸出。

## 開發慣例

- 每一步一個 branch、一個 PR，PR 描述寫明對應規格的哪一步
- 新增行為必須附測試；`uv run pytest` 與 `uv run ruff check` 都要過才算完成
- 型別註記寫完整；公開函式寫一行 docstring
- 註解與文件用繁體中文，程式碼識別字與 commit message 用英文
- 遇到規格沒寫清楚的地方，先停下來問我，不要自行擴充範圍

## 常用指令

```
uv sync
uv run pytest
uv run ruff check .
uv run ruff format .
```
