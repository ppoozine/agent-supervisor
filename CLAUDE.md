# agent-supervisor

平行管理多個 AI coding agent 的本機工具：每個任務一個 git worktree，CI 失敗自動回到同一個 agent session 修復，人只處理例外。

## 文件

動手前先讀 `docs/`：

| 檔案 | 內容 |
| --- | --- |
| `docs/00-vision.md` | 想做的東西與定位 |
| `docs/01-roadmap.md` | 九個階段與各自的完成標準 |
| `docs/02-mvp-spec.md` | MVP 規格：資料表、API、背景工作、adapter 介面 |
| `docs/03-decisions.md` | 決策紀錄 |
| `docs/04-phase1-tasks.md` | 階段 1 與 1.5 的任務單與驗證清單 |
| `docs/05-ui-reference.md` | 介面參考截圖、卡片內容、MVP 介面範圍 |
| `docs/06-diagrams.md` | 架構圖與流程圖 |
| `docs/07-phase2-tasks.md` | 階段 2 的任務單與驗證清單 |
| `docs/codex-notes.md` | Codex 介面的調查與實測結果（任務 0 的產出） |
| `docs/claude-sdk-notes.md` | Claude Agent SDK 的調查與實測結果 |

文件與本檔衝突時以 `docs/` 為準，並提醒我更新本檔。實作中做了新的設計決定，在 `docs/03-decisions.md` 新增一筆。

## 目前階段

階段 2（看板介面）已完成，版本 `0.1.0`，MVP 到這裡做完。現在停在 `docs/01-roadmap.md` 的檢查點：先實際使用一陣子，再決定階段 3 到 7 的順序（決策第 116 筆），還沒有開始階段 3。階段 1.5 的完成標準有 4 項沒有實測（決策第 94 筆）。沒有我明確指示，不要開始下一個階段的功能，也不要做規格「不包含」清單裡的功能。進入新階段時我會更新這一段。

## 技術選型

- Python 3.13+，套件管理用 `uv`
- FastAPI + uvicorn，SSE 推送狀態與 log
- Claude Code 透過官方 Python Agent SDK（`claude-agent-sdk`）控制
- SQLite（標準庫 `sqlite3` 或 `aiosqlite`），不使用 ORM
- 全程 asyncio；背景工作與 API 跑在同一個 process
- 測試用 `pytest` + `pytest-asyncio`，lint / format 用 `ruff`
- 前端：React + Vite + TypeScript，放在 `web/`；不裝路由、資料抓取、樣式的套件，測試用 vitest

## 專案結構

```
src/agent_supervisor/
  adapters/
    base.py          # AgentAdapter Protocol
    claude_code.py   # 用官方 Python Agent SDK 控制 Claude Code
    codex.py         # 用 subprocess 包 `codex exec` 控制 Codex
  enums.py           # 共用的列舉值：run 的觸發原因、結案結果、事件型別
  runner.py          # run 的生命週期：建立、準備、agent 執行、收尾、結案
  recovery.py        # 單一 daemon 的鎖、啟動時處理未結束的 run
  cli.py             # 指令 asv：serve 啟動 daemon，其餘是階段 1 留下的前景模式
  supervisor.py      # 組裝 Runner / Poller / Dispatcher，以及「輪詢一輪再派工」
  daemon.py          # 常駐的 daemon：啟動順序、訊號、背景輪詢、關閉
  stages.py          # run 的準備（建 worktree）與收尾（commit、push、開 PR）、封存
  process.py         # 執行外部指令的共用底層，逾時或取消時終止子程序
  worktree.py        # git worktree 與 branch 管理（所有 git 指令）
  github.py          # 所有 gh 呼叫：開 PR、查 PR / CI / review、抓 CI log；檢查結果換算成 ci_state
  ci_log.py          # 清理與截取 CI log、組出修復指示（純函式）
  poller.py          # 輪詢 PR / CI / review 狀態
  dispatcher.py      # 找出符合自動修復條件的任務，交給 Runner 建立 ci_failure run
  status.py          # 自動修復條件、看板欄位與狀態標籤（純函式）
  db.py              # schema 與查詢
  config.py
  api/
    app.py           # `/api` 底下的 REST 端點與 GET /api/stream；有 web/dist 時掛在 /
    feed.py          # 偵測資料庫的變化並推送給 SSE 連線
    views.py         # 把資料轉成回應內容（純函式）
    errors.py        # 錯誤回應的形狀與對應
    guard.py         # 只接受對本機、從本機網頁發出的請求
tests/
docs/
web/                 # 前端：React + Vite
  src/
    api.ts           # 所有對 daemon 的呼叫與錯誤形狀
    stream.ts        # SSE 連線與自動重連
    format.ts        # 把資料排成畫面上的文字（純函式）
    App.tsx          # 版面、資料的載入與推送
    components/      # 看板、卡片、表單、任務詳情頁
```

import 名稱是 `agent_supervisor`。不要用 `supervisor`，PyPI 上有同名的知名套件。

## 必須遵守的設計規則

1. **看板欄位不存資料庫**。一律由 `status.py` 的純函式從 run / PR / CI / review 狀態推導。
2. **Runner 只透過 `AgentAdapter` 介面控制 agent**。Runner 內不得出現任何 Claude Code 專屬的字串或參數。
3. **`events` 表只新增，不修改、不刪除**。
4. **防無限迴圈**：
   - 同一個 commit 的 CI 失敗只餵回一次（比對 `last_handled_head_sha`）
   - 自動修復最多 3 次（`ci_fix_attempts`）
   - 單一任務累計 `cost_usd` 或累計 token 數達到全域上限就停止
   - 單一 run 的輪數與花費上限由 adapter 在執行中擋下；執行時間上限由 Runner 自己計時
   - Stop、中斷、出錯之後不自動開新的 run，等人介入
   - 這些規則只決定能不能再自動修復，不影響成果能不能合併；細節以規格為準
5. **GitHub 一律用輪詢**（預設 30 秒），MVP 不做 webhook。
6. **對外部指令的呼叫**（`git`、`gh`、agent）集中在各自的模組，方便測試時替換。
7. **push 與開 PR 由 supervisor 執行**，不交給 agent；agent 正常結束的每一次 run 都做收尾，其他結束方式不自動發布、保留 worktree 內的變更。
8. **Agent 的動作受權限政策限制**（sandbox 等級、是否允許連網、工具與指令的限制）。Adapter 攔得到的違規動作一律拒絕並寫入 `permission_denied` 事件。這是基本防護不是安全邊界，階段 1 到 4 只用在信任的 repo。
9. **每個任務同時最多一個未結束的 run**，由資料庫約束保證；建立 run 與更新防迴圈欄位在同一個交易內完成。
10. **一個 run 由同一個可取消的工作從建立負責到結案**（準備、agent 執行、收尾）；建立 run、Stop、封存共用每個任務一把鎖。
11. **同時只允許一個 daemon**；啟動時先停掉遺留程序再接受新工作。

## Adapter 注意事項

- Claude Code adapter 使用官方 Python Agent SDK（`claude-agent-sdk`），不自己包 CLI。
- **不要憑印象寫參數**。實作前先查官方文件，確認串流訊息、續接 session、中止、輪數與花費上限、權限控制的實際用法，把查到的結果記在 `docs/claude-sdk-notes.md`。Codex adapter 同樣先調查，記在 `docs/codex-notes.md`。
- Adapter 要如實填寫 `capabilities`；做不到的事不要在 adapter 裡假裝做到。
- 測試不得真的啟動 agent：用 fake adapter 吐出預錄的事件。

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
uv run asv --help
uv run asv serve          # 啟動 daemon；看板與 API 在 http://127.0.0.1:8765
uv run ruff check .
uv run ruff format .

cd web
npm install
npm run build             # 產生 web/dist，daemon 啟動時會提供它
npm run dev               # 開發用，http://localhost:5173，/api 轉給 daemon
npm test
```
