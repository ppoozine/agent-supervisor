# agent-supervisor

平行管理多個 AI coding agent 的本機工具：每個任務一個 git worktree，CI 失敗自動回到同一個 agent session 修復，人只處理例外。

這份是給所有 coding agent 的專案指示（Codex 直接讀 `AGENTS.md`；`CLAUDE.md` 匯入這份）。改指示時只改這份。

## 文件

動手前先讀 `docs/README.md`，再讀 `docs/design/`：

| 檔案 | 內容 |
| --- | --- |
| `docs/design/vision.md` | 想做的東西、不做的事、定位 |
| `docs/design/roadmap.md` | 各個階段與各自的完成標準 |
| `docs/design/spec.md` | 規格：資料表、run 的生命週期、看板、自動處理與上限、留言、衝突、復原、API、adapter 介面 |
| `docs/design/decisions.md` | 目前仍然有效的設計決定（依主題）、尚未決定的事、已知的問題 |
| `docs/design/ui.md` | 介面的需求：每個畫面、訊息來源的標示、顏色與 token；`mockups/` 是每個畫面的靜態 HTML 草稿與截圖 |
| `docs/design/diagrams.md` | 架構圖與流程圖 |
| `docs/design/claude-sdk-notes.md`、`docs/design/codex-notes.md` | Claude Agent SDK 與 Codex CLI 的調查與實測結果 |
| `docs/guide/` | 對外的英文文件：使用說明（`usage.md`）、信任模型與已知限制（`trust-model.md`）、adapter 開發指南（`adapters.md`）、HTTP API（`http-api.md`）。改了看板、設定檔、上限、權限、adapter 介面或 API 要一起更新 |
| `docs/history/` | 完整的決策紀錄（`decision-log.md`，只增不改）、規劃的過程（`plans/`）、用 agsv 開發這個 repo 時碰到的問題（`dogfooding.md`） |

文件與本檔衝突時以 `docs/` 為準，並提醒我更新本檔。實作中做了新的設計決定：在 `docs/history/decision-log.md` 新增一筆；如果它改變了現行的做法，同時更新 `docs/design/decisions.md`。用 agsv 開發這個 repo 時碰到的操作問題或想改進的地方，記在 `docs/history/dogfooding.md`。

## 目前階段

階段 0 到 5 已完成（`v0.4.0`；階段 5 的任務 22 由 Codex、任務 23 由 Claude Code 透過 agsv 執行）。階段 6 還沒開始，任務單在開始時寫。**一次做一個，合併之後才開始下一個**（決策第 150 筆）；每個任務先提計畫，我確認後再動手（由 agent-supervisor 開的任務例外，見「開發慣例」）。階段 6 開始之前不要做它的功能；之後的任務也不要做任務單「不要做」清單裡的功能。進入新階段時我會更新這一段。

## 技術選型

- Python 3.13+，套件管理用 `uv`
- FastAPI + uvicorn，SSE 推送狀態與 log
- Claude Code 透過官方 Python Agent SDK（`claude-agent-sdk`）控制
- SQLite（標準庫 `sqlite3` 或 `aiosqlite`），不使用 ORM
- 全程 asyncio；背景工作與 API 跑在同一個 process
- 測試用 `pytest` + `pytest-asyncio`，lint / format 用 `ruff`
- 前端：React + Vite + TypeScript，放在 `web/`，測試用 vitest
  - 樣式用 Tailwind 4（`@tailwindcss/vite`），Preflight 已開啟（任務 22）。手寫的 CSS 一律放進 layer：不在 layer 裡的規則會蓋過 Tailwind 的 class（決策第 181、185 筆）
  - 視覺方向是 `docs/design/ui.md` 的方向 A（第 186 筆）；顏色一律經過那裡的 token，深色與淺色都要有，元件裡不寫死色碼（第 182 筆）。字型不從網路載入
  - Shell 用 xterm.js（`@xterm/xterm`、`@xterm/addon-fit`），只在打開 Shell 分頁時載入（決策第 195 筆）
  - 不裝路由、資料抓取的套件；元件庫、圖示庫要用之前先問我（第 175 筆）

## 專案結構

```
src/agent_supervisor/
  adapters/
    base.py          # AgentAdapter Protocol
    loading.py       # 從 entry point（agent_supervisor.adapters）載入所有 adapter，含內建的兩個
    claude_code.py   # 用官方 Python Agent SDK 控制 Claude Code
    codex.py         # 用 subprocess 包 `codex exec` 控制 Codex
  enums.py           # 共用的列舉值：run 的觸發原因、結案結果、事件型別
  runner.py          # run 的生命週期：建立、準備、agent 執行、收尾、結案
  recovery.py        # 單一 daemon 的鎖、啟動時處理未結束的 run
  cli.py             # 指令 agsv：serve 啟動 daemon，其餘是階段 1 留下的前景模式
  supervisor.py      # 組裝 Runner / Poller / Dispatcher，以及「輪詢一輪再派工」
  daemon.py          # 常駐的 daemon：啟動順序、訊號、背景輪詢、關閉
  stages.py          # run 的準備（建 worktree、抓 CI log、開始合併）與收尾（commit、push、開 PR）、封存
  process.py         # 執行外部指令的共用底層，逾時或取消時終止子程序
  tool_status.py     # 檢查 agent 與 gh 能不能用的共用結果型別（不碰憑證、不花用量）
  readiness.py       # 檢查每種 agent 與 gh，記下最近一次的結果
  worktree.py        # git worktree 與 branch 管理（所有 git 指令）
  github.py          # 所有 gh 呼叫：開 PR、查 PR / CI / review、抓 CI log；檢查結果換算成 ci_state
  ci_log.py          # 清理與截取 CI log、組出修復指示（純函式）
  feedback.py        # PR 上的留言：哪些要餵回 agent、餵回的內容（純函式）
  poller.py          # 輪詢 PR / CI / review 狀態，把 PR 上新的留言記成事件
  dispatcher.py      # 找出有新留言、有衝突或 CI 失敗的任務，交給 Runner 建立對應的 run
  status.py          # 留言回饋、解衝突、自動修復三種條件，看板欄位與狀態標籤（純函式）
  db.py              # schema 與查詢
  config.py
  notifier.py        # 卡片進 Needs you 時發系統通知（macOS 用 osascript，Linux 用 notify-send）
  shell.py           # 任務 worktree 裡的 terminal：pty、每個任務一個、結束時清掉程序（決策第 195 到 197 筆）
  api/
    app.py           # `/api` 底下的 REST 端點與 GET /api/stream；有 web_dist 時掛在 /，沒有時 / 說明怎麼取得
    feed.py          # 偵測資料庫的變化並推送給 SSE 連線
    views.py         # 把資料轉成回應內容（純函式）
    errors.py        # 錯誤回應的形狀與對應
    guard.py         # 只接受對本機、從本機網頁發出的請求；shell 的 WebSocket 只接受看板自己的頁面
tests/
docs/
  design/            # 現在的樣子（見上面的表）
  guide/             # 對外的英文文件
  history/           # 決策紀錄與規劃的過程
examples/
  example-adapter/   # 範例 adapter：獨立的套件、預設不載入，包一支假的 agent CLI，不需要帳號
web/                 # 前端：React + Vite + Tailwind
  src/
    api.ts           # 所有對 daemon 的呼叫與錯誤形狀
    stream.ts        # SSE 連線與自動重連
    format.ts        # 把資料排成畫面上的文字（純函式）
    timeline.ts      # 把 run 與事件排成 Chat 的時間軸、標出來源（純函式）
    diff.ts          # 把 git 的 diff 拆成檔案與行（純函式）
    keys.ts          # Cmd/Ctrl+Enter 送出
    shell.ts         # Shell 分頁的 WebSocket 連線與訊息
    App.tsx          # 版面、資料的載入與推送
    components/      # 看板、卡片、表單、任務詳情頁（ChatTab、DiffTab、ShellTab、TaskTabs）
```

import 名稱是 `agent_supervisor`。不要用 `supervisor`，PyPI 上有同名的知名套件。

## 必須遵守的設計規則

1. **看板欄位不存資料庫**。一律由 `status.py` 的純函式從 run / PR / CI / review 狀態推導。前端也不自己推導，只用 API 回傳的 `column` 與 `label`。
2. **Runner 只透過 `AgentAdapter` 介面控制 agent**。Runner 內不得出現任何 Claude Code 專屬的字串或參數。
3. **`events` 表只新增，不修改、不刪除**。
4. **防無限迴圈**：
   - 同一個 commit 的 CI 失敗只餵回一次（比對 `last_handled_head_sha`）；同一個 commit 的衝突只處理一次（比對 `last_handled_conflict_head_sha`）；同一則留言只餵回一次（比對 `last_handled_feedback_event_id`）
   - 自動修復與解衝突合計最多 3 次（`ci_fix_attempts`）；留言的自動處理最多 5 次（`feedback_attempts`）；PR 上的留言不會讓這兩個次數歸零
   - 單一任務累計 `cost_usd` 或累計 token 數達到全域上限就停止；從最近一次人介入（開任務或送訊息）起累計，送訊息不因預算被拒
   - 單一 run 的輪數與花費上限由 adapter 在執行中擋下；執行時間上限由 Runner 自己計時
   - Stop、中斷、出錯之後不自動開新的 run，等人介入；run 執行中送出的訊息會被拒絕，不排隊（決策第 183 筆）
   - 這些規則只決定能不能再自動修復，不影響成果能不能合併；細節以規格為準
5. **GitHub 一律用輪詢**（預設 30 秒），不做 webhook。
6. **對外部指令的呼叫**（`git`、`gh`、agent）集中在各自的模組，方便測試時替換。
7. **push 與開 PR 由 supervisor 執行**，不交給 agent；agent 正常結束的每一次 run 都做收尾，其他結束方式不自動發布、保留 worktree 內的變更。解衝突時合併也由 supervisor 開始與 commit，agent 只改檔案；衝突標記還在就不 commit、不 push。
8. **Agent 的動作受權限政策限制**（sandbox 等級、是否允許連網、工具與指令的限制）。Adapter 攔得到的違規動作一律拒絕並寫入 `permission_denied` 事件。這是基本防護不是安全邊界，治理（階段 10）做完之前只用在信任的 repo。
9. **每個任務同時最多一個未結束的 run**，由資料庫約束保證；建立 run 與更新防迴圈欄位在同一個交易內完成。
10. **一個 run 由同一個可取消的工作從建立負責到結案**（準備、agent 執行、收尾）；建立 run、Stop、封存共用每個任務一把鎖。
11. **同時只允許一個 daemon**；啟動時先停掉遺留程序再接受新工作。

## Adapter 注意事項

- Adapter 用 entry point 註冊（`pyproject.toml` 的 `agent_supervisor.adapters`），新增內建的 adapter 也一樣，不要寫死清單。
- Claude Code adapter 使用官方 Python Agent SDK（`claude-agent-sdk`），不自己包 CLI。
- **不要憑印象寫參數**。實作前先查官方文件，確認串流訊息、續接 session、中止、輪數與花費上限、權限控制的實際用法，把查到的結果記在 `docs/design/claude-sdk-notes.md`。Codex adapter 同樣先調查，記在 `docs/design/codex-notes.md`。
- Adapter 要如實填寫 `capabilities`；做不到的事不要在 adapter 裡假裝做到。
- 測試不得真的啟動 agent：用 fake adapter 吐出預錄的事件。

## 開發慣例

- 每一步一個 branch、一個 PR，PR 描述寫明對應任務單的哪一個任務
- 新增行為必須附測試；`uv run pytest` 與 `uv run ruff check` 都要過，改了前端還要 `npm test` 與 `npm run build` 都過，才算完成
- 型別註記寫完整；公開函式寫一行 docstring
- 程式碼的註解與 docstring、介面文字、對外的文件（README 等）用英文；`docs/` 下的設計文件用繁體中文（決策第 143 筆）。程式碼識別字與 commit message 用英文
- 遇到規格沒寫清楚的地方，先停下來問我，不要自行擴充範圍
- **由 agent-supervisor 開的任務**（例如任務 22 的 Codex、任務 23 的 Claude Code）：任務的指示就是已確認的範圍，**不用先提計畫等確認**，直接做（決策第 192 筆）；指示沒寫清楚、或要超出範圍時，才停下來問。只在 worktree 裡改檔案，不要 commit、push 或開 PR，那些由 supervisor 做（設計規則 7）。做完在最後的訊息裡說明改了什麼、跑了哪些檢查，以及和規格不同、需要我決定的地方

## 常用指令

```
uv sync
uv run pytest
uv run agsv --help
uv run agsv serve          # 啟動 daemon；看板與 API 在 http://127.0.0.1:8765
uv run ruff check .
uv run ruff format .

cd web
npm install
npm run build             # 產生 src/agent_supervisor/web_dist（git 不追蹤），daemon 啟動時會提供它
npm run dev               # 開發用，http://localhost:5173，/api 轉給 daemon
npm test
```
