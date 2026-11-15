# agent-supervisor MVP 規格

最後更新：2026-11-15

## 目標與範圍

MVP 只證明一件事：一個任務從開單到 CI 綠燈，中間 CI 失敗能自動回到同一個 agent 修復，人只在例外時介入。

**包含**

- 加入一個本機 repo
- 手動開任務（描述 + 送出），只支援 Claude Code
- 每個任務自動建 worktree 與 branch
- 啟動 agent session，log 即時串流到前端
- Agent 完成後開 PR
- 輪詢 GitHub 的 PR / CI 狀態，推導看板欄位
- CI 失敗時抓 log 餵回同一個 session
- 四欄看板與任務詳情頁
- 每個任務的稽核紀錄與 token 花費

**不包含**

- 多種 agent、orchestrator 自動拆任務
- Review 留言與 merge conflict 的自動處理
- 從 issue 自動建任務、webhook
- 桌面安裝檔、多人與登入
- Container sandbox

## 架構

一個 Python daemon 跑在本機，React 前端由它 serve 在 `localhost`，MVP 不包桌面殼。

```
React 看板（瀏覽器）
      ⇅ REST + SSE
┌─ Python daemon（本機單一 process）──────────────────┐
│ FastAPI → Runner ← Dispatcher ← Poller             │
│    ↓         ⇅                      ↓              │
│ SQLite       │                      │              │
└──────────────┼──────────────────────┼──────────────┘
        Claude Code CLI ── push、開 PR ──→ GitHub
        （各任務獨立 worktree）        （每 30 秒輪詢）
```

Poller 查到 CI 失敗後交給 Dispatcher，Dispatcher 請 Runner 續接同一個 session。四個元件都讀寫 SQLite。

## 資料表

SQLite，四張表。

### projects

| 欄位 | 說明 |
| --- | --- |
| `id` | 主鍵 |
| `name` | 顯示名稱 |
| `repo_path` | 本機 repo 路徑 |
| `github_repo` | `owner/name` |
| `default_branch` | worktree 的基底分支 |

### tasks

| 欄位 | 說明 |
| --- | --- |
| `id`, `project_id` | 主鍵與所屬專案 |
| `title`, `prompt` | 卡片標題與給 agent 的任務描述 |
| `agent_type`, `model` | MVP 固定為 Claude Code，欄位先留 |
| `session_id` | agent session，續接對話用 |
| `branch`, `worktree_path` | 此任務專屬的分支與工作目錄 |
| `pr_number` | 開 PR 後回填 |
| `ci_state`, `review_state` | Poller 最近一次查到的狀態 |
| `last_handled_ci_run_id` | 已處理過的 CI run，避免重複餵回 |
| `ci_fix_attempts` | 自動修復次數 |
| `archived_at`, `created_at`, `updated_at` | 時間戳 |

### runs

| 欄位 | 說明 |
| --- | --- |
| `id`, `task_id` | 主鍵與所屬任務 |
| `trigger` | `initial` / `ci_failure` / `user_message` |
| `input_text` | 這次送給 agent 的內容 |
| `started_at`, `ended_at`, `exit_status` | 執行區間與結果 |
| `tokens_in`, `tokens_out`, `cost_usd` | 用量與花費 |

### events

只新增，不修改。

| 欄位 | 說明 |
| --- | --- |
| `id`, `task_id`, `run_id`, `ts` | 主鍵、歸屬、時間 |
| `type` | `agent_message` / `tool_call` / `command` / `pr_opened` / `ci_status` / `state_change` |
| `payload` | JSON，依 `type` 而異 |

## 看板狀態與防迴圈

看板欄位不存進資料庫，每次由一個函式從 run、PR、CI、review 狀態算出來。

| 欄位 | 條件（由上往下判斷，先符合者優先） |
| --- | --- |
| Working | 有 run 正在執行 |
| Needs you | 最後一次 run 出錯；或 `ci_fix_attempts` 達上限；或花費超過上限；或 reviewer 要求修改 |
| Ready to merge | CI 通過且 review 已核准 |
| In review | PR 開著，CI 進行中或已通過、等待審查 |

**防無限迴圈**

1. 同一個 CI run 只餵回一次：Dispatcher 處理前比對 `last_handled_ci_run_id`，處理後立刻更新。
2. 自動修復最多 3 次：`ci_fix_attempts` 到 3 就停止，卡片進 Needs you。
3. 單一任務花費上限：累計 `cost_usd` 超過上限就停止，卡片進 Needs you；上限值為全域設定。

## API

| 端點 | 用途 |
| --- | --- |
| `POST /projects` | 加入本機 repo |
| `GET /projects` | 專案列表 |
| `POST /tasks` | 開任務：建 worktree、啟動 agent |
| `GET /tasks?project_id=` | 看板資料，含推導出的欄位 |
| `GET /tasks/{id}` | 任務詳情 |
| `POST /tasks/{id}/messages` | 對同一個 session 追加指令 |
| `POST /tasks/{id}/stop` | 中止正在跑的 run |
| `POST /tasks/{id}/archive` | 封存並清掉 worktree |
| `GET /tasks/{id}/events` | 稽核紀錄，分頁 |
| `GET /usage?project_id=` | token 與花費彙總 |
| `GET /stream` | SSE：狀態變更與 agent log |

## 背景工作與 Agent adapter

三個 asyncio 背景工作跑在同一個 process，Runner 只透過 adapter 介面控制 agent。

| 工作 | 負責 |
| --- | --- |
| Runner | 啟動或續接 agent session，把輸出寫進 `events`，結束時回填用量 |
| Poller | 每 30 秒查詢有開 PR 的任務，更新 `ci_state` 與 `review_state` |
| Dispatcher | 發現新的 CI 失敗就抓 log，建立 `ci_failure` run 交給 Runner |

```python
class AgentAdapter(Protocol):
    async def start(self, prompt: str, cwd: str) -> str: ...
    async def resume(self, session_id: str, msg: str) -> None: ...
    async def stop(self, session_id: str) -> None: ...
    def events(self, session_id: str) -> AsyncIterator[dict]: ...
```

MVP 只實作 Claude Code 一個 adapter，用 subprocess 包 CLI 的 headless 模式。之後要換官方 SDK 只動 adapter 一個檔案。

## 開發順序

前三步完成就能在 terminal demo 核心迴圈，UI 最後做。

1. Adapter + Runner：用 CLI 跑起一個任務，輸出寫進 `events`
2. Worktree 管理 + 開 PR
3. Poller + Dispatcher：CI 失敗自動餵回，含三條防迴圈規則
4. FastAPI 端點 + SSE
5. React 看板與任務詳情頁

## 正式版待辦（MVP 之後）

| 類別 | 項目 |
| --- | --- |
| 回饋迴圈 | Review 留言、merge conflict 自動處理 |
| 多 agent | Codex、Gemini 等，各一個 adapter |
| Orchestrator | 大目標自動拆任務、派工 |
| 觸發來源 | GitHub / Linear issue 自動建任務，輪詢改 webhook |
| 治理 | 權限政策、人工核准關卡、預算上限、稽核查詢 |
| 隔離 | 從 worktree 升級到 container sandbox |
| 體驗 | 內嵌 terminal、diff 檢視、通知、Tauri 桌面安裝檔 |
| 團隊 | 多人、登入、角色權限 |
