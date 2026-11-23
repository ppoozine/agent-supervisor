# 介面

最後更新：2026-11-23

階段 5 新介面的需求（任務 20 的產出）。任務 21 到 24 都照這份做：任務 21 做 Chat 的結構，任務 22、23 由 Codex 改外觀，任務 24 加 Shell。任務詳情頁的靜態草稿是 `ui-mockup.html`，直接用瀏覽器打開，不需要連網；草稿和這份文件不一致時以這份為準。

介面上的文字用英文（決策第 143 筆），這份文件裡引號中的英文就是畫面上要出現的字。

## 原則

1. **一眼看出狀態，一眼看出是誰說的。** 任務在哪個階段、卡在哪裡、每一則訊息是誰送的，不用點開或讀完內容就知道。
2. **只顯示 API 給的。** 看板欄位、狀態標籤一律用 API 回傳的 `column` 與 `label`，前端不自己推導（設計規則 1）；前端只決定顏色與排版。
3. **花費是估算值。** 金額前面加 `~`；`cost_usd` 是 `null` 時改顯示 token 數；`usage_is_complete` 為否時後面加 `+`（沿用現在的規則）。
4. **鍵盤可以操作。** `Cmd/Ctrl+Enter` 送出（第 178 筆），對話框可以用 Escape 關閉，所有按鈕與分頁都能用 Tab 走到。
5. **不複製 Agent Orchestrator** 的程式碼、圖示與視覺設計（第 170 筆）。
6. **不裝元件庫、圖示庫。** 需要的圖示用 Unicode 字元或幾行內嵌的 SVG。

## 版面總覽

```
┌───────────────┬──────────────────────────────────────────────────────┐
│ agent-        │  主區：看板 / 任務詳情 / 設定                          │
│ supervisor    │                                                      │
│               │                                                      │
│ [+ New task]  │                                                      │
│               │                                                      │
│ PROJECTS      │                                                      │
│ ▾ demo        │                                                      │
│   ● add cube  │                                                      │
│   ● fix login │                                                      │
│   ✦ Plan with │  ← 主 agent 的入口（階段 6，現在只顯示、不能按）        │
│     main agent│                                                      │
│ + Add project │                                                      │
│               │                                                      │
│ ⚙ Settings    │                                                      │
└───────────────┴──────────────────────────────────────────────────────┘
```

- 側欄寬 240px，可以收合成只剩圖示（56px）。收合狀態記在 `localStorage`，讀不到時預設展開。
- 側欄的任務清單：每個專案底下列出未封存的任務，前面的圓點顏色代表欄位（見「顏色」），正在跑的圓點會呼吸閃爍。點了打開任務詳情。
- 網址仍用 hash：`#/`、`#/tasks/{id}`、`#/settings`（第 109 筆，不裝路由）。

## 看板（`#/`）

```
Board                                             [+ New task]
┌ Working (2) ──┐┌ Needs you (1) ┐┌ In review (3) ┐┌ Ready to merge (1) ┐
│ ┌───────────┐ ││ ┌───────────┐ ││               ││                    │
│ │add cube(x)│ ││ │fix login  │ ││     ...       ││       ...          │
│ │demo·Claude│ ││ │demo·Codex │ ││               ││                    │
│ │● Working  │ ││ │● CI failed│ ││               ││                    │
│ │#12 ✓CI    │ ││ │#9 ✗CI 3/3 │ ││               ││                    │
│ │~$0.42     │ ││ │48k tok    │ ││               ││                    │
│ └───────────┘ ││ └───────────┘ ││               ││                    │
└───────────────┘└───────────────┘└───────────────┘└────────────────────┘
▸ Archive (12)
```

| 卡片上的東西 | 來源 |
| --- | --- |
| 標題 | `title` |
| 專案與 agent | `project_id` 對到專案名稱；`agent_type` 對到 `GET /api/agents` 的 `display_name` |
| 狀態標籤 | `label`，顏色依 `column` |
| 正在跑 | `has_open_run` 為真時標籤前面的圓點閃爍 |
| PR 與 CI | `pr_number`；`ci_state`（✓ passed、✗ failed、◌ pending、無 CI 時不顯示）；`review_state` 是 approved 時加 ✓ review；`merge_state` 是 conflicting 時加 ⚠ conflict |
| 自動修復次數 | `ci_fix_attempts` > 0 時顯示 `fixes 1/3`；到上限時改用 Needs you 的顏色 |
| 花費 | `cost_usd`、`tokens`、`usage_is_complete`，規則見原則 3 |

- 每欄標題後面是張數；空的欄位顯示淡色的「Nothing here」。
- Archive 區預設收合，展開後是一個簡單的清單（標題、PR 結果 `Merged` / `Closed` / `Archived`、日期）。
- 沒有任何專案時，看板位置顯示說明與「Add project」按鈕；沒有能用的 agent 時，上方顯示提示並連到設定頁（沿用現在的行為）。
- 卡片不能拖曳。

## 任務詳情頁（`#/tasks/{id}`）

這個頁面是這次改版的重點。草稿見 `ui-mockup.html`。

### 頁首

```
← Board   add cube(x)                                   [Stop] [Archive]
          ● Working · Claude Code · demo
          asv/task-12-add-cube ⧉   PR #12 ↗  ✗ CI  · review —  · no conflict
          ~$0.42 · 1.2M tokens        fixes 1/3 · comments 0/5
```

| 東西 | 來源 |
| --- | --- |
| 標題、狀態 | `title`、`label`（顏色依 `column`）；`has_open_run` 時圓點閃爍 |
| agent、專案 | 同看板 |
| branch | `branch`，旁邊一個複製按鈕；還沒有時不顯示 |
| PR | `pr_number` 連到 GitHub；`pr_state` 是 merged / closed 時顯示在旁邊 |
| CI、review、衝突 | `ci_state`、`review_state`、`merge_state`，用和看板相同的符號 |
| 花費 | `cost_usd`、`tokens`、`usage_is_complete`；滑過去顯示 `tokens_in` / `tokens_out` 的總和 |
| 次數 | `fixes {ci_fix_attempts}/{max_ci_fix_attempts}`、`comments {feedback_attempts}/{max_feedback_attempts}`（後兩個欄位要新增，見「需要新增的 API」） |
| Stop | 只有 `has_open_run` 時可以按 |
| Archive | PR 還開著時先跳確認框（第 117 筆，沿用現在的文字與行為） |

### 主區的排法

**用分割版**（決策第 182 筆）：左邊固定是 Chat，右邊是可以調整寬度的面板，用分頁放 Diff、Shell、Events、Runs。看對話時常常要同時看 diff 或在 shell 裡確認，分割版不用一直切換。視窗寬度小於 1100px 時自動退回分頁版：Chat、Diff、Shell、Events、Runs 五個分頁，一次只看一個。

草稿裡兩種都畫了（上方的切換列），分頁版就是窄視窗時的樣子。

不論哪一種，預設打開的都是 Chat；右側面板（或分頁）記住上次選的，記在 `localStorage`。

### Chat：對話的時間軸

把現在的 Log 分頁換成對話的時間軸（任務 21 做結構）。資料只用現有的 API：任務詳情的 `runs` 與事件（每個事件有 `run_id`），不另存一份。

**以 run 分段。** 每一次 run 前面一條分隔線，寫出這次 run 的編號、為什麼開始、怎麼結束、花了多少：

```
──── Run #3 · Automatic: CI failed · ✓ success · 2m 14s · ~$0.12 ────
```

| `trigger` | 分隔線上的文字 |
| --- | --- |
| `initial` | `Task opened` |
| `user_message` | `Your message` |
| `review_feedback` | `Automatic: PR comments` |
| `ci_failure` | `Automatic: CI failed` |
| `merge_conflict` | `Automatic: merge conflict` |

`exit_status`：`success` ✓、`error` ✗、`stopped` ■、`limit_reached` ⏱、`interrupted` ⚡；還在跑時顯示「running」。用量是 `null` 時不顯示金額。

**每一則的來源**

| 來源 | 資料 | 位置與外觀 | 標籤 |
| --- | --- | --- | --- |
| **你** | `trigger` 是 `initial`、`user_message` 的 run 的 `input_text` | 靠右的對話泡泡，底色是「你」的顏色 | `You` |
| **Reviewer** | `review_comment` 事件 | 靠左的卡片，左邊一條「Reviewer」顏色的線；頭部是作者、留言的種類、檔案與行號（有的話），連到 GitHub | `Reviewer · @alice` |
| **自動** | `trigger` 是 `ci_failure`、`merge_conflict`、`review_feedback` 的 run 的 `input_text` | 置中、整行寬的卡片，「自動」的顏色，前面是 ⚙；第一行寫原因（例如「CI failed on 3f2a9c1」），內容（含 CI log）預設收合 | `Automatic · sent by agent-supervisor` |
| **Agent** | `agent_message`（`kind` 是 `message`） | 靠左，沒有底色的段落，前面是 agent 的縮寫圖章（例如 `CC`、`CX`） | `Agent · Claude Code` |
| Agent 的動作 | `command`、`tool_call`、`agent_message`（`kind` 是 `reasoning`、`plan`） | 接在 Agent 底下的精簡列：`$ npm test`、`Edit src/app.py`、`Thinking…`；點開才看輸出或內容。失敗的指令標紅、被擋下的動作（`permission_denied`）標 Needs you 的顏色並寫原因 | — |
| **系統** | `pushed`、`pr_opened`、`ci_status`、`state_change`（只在 run 不是成功結束時；成功已經寫在分隔線上）| 置中的一行小字，前面一個符號：`↑ Pushed 3f2a9c1`、`⎇ Opened PR #12`、`✗ CI failed`、`■ Run stopped` | — |

- **Reviewer 與自動的關係**：PR 上的留言先以 Reviewer 的卡片出現在它被看到的時間點（事件沒有 `run_id`，放在依時間的位置）；下一輪派工時開始的 `review_feedback` run，以「自動」卡片呈現 supervisor 實際送給 agent 的內容，第一行寫「Sent new pull request comments to the agent」。
- **沒送給 agent 的留言**（`ignored` 有值）：Reviewer 卡片變淡，加上一行說明，對應 `bot` → 「Not sent: posted by a bot」、`untrusted_author` → 「Not sent: the author is not a collaborator」、`empty` → 「Not sent: no text」、`not_a_change_request` → 「Not sent: approval」。
- **預設收合**：自動卡片的內容（CI log 很長）、指令的輸出、工具呼叫的內容、agent 的推理。收合時顯示第一行與總行數。
- **跟隨最新**：捲到底部時，新事件進來自動捲動；往上捲之後不自動捲，右下角出現「↓ New activity」按鈕（沿用現在 Log 的行為，加上按鈕）。
- **還在跑**：最後一個 run 還沒結束時，底部顯示 agent 的圖章與「Working…」。

### 輸入框

```
┌────────────────────────────────────────────────────────────┐
│ Send a follow-up to the same session…                      │
│                                                            │
└────────────────────────────────────────────── ⌘↵  [Send] ──┘
```

- `Cmd/Ctrl+Enter` 送出，Enter 換行，輸入法選字中不送出（第 178 筆）。
- run 執行中送出的處理（現在是拒絕，回 409）維持拒絕（決策第 183 筆）：執行中時輸入框上方顯示「The agent is working. Stop it to send a message now.」，Send 停用。
- 任務已封存或 PR 已合併時，輸入框換成一行說明，不能輸入；PR 已關閉時仍可送（可能已在 GitHub 重新開啟，由 daemon 檢查，決策第 183 筆）。

### 右側面板（或其他分頁）

- **Diff**：沿用現在的行為（依檔案分段、新增與刪除的行數、新增綠底刪除紅底）；檔案標題可以收合，最上面一列是檔案清單與總行數。
- **Shell**：任務 24 做。這個階段先放一個說明「A terminal in this task's worktree. Coming in 0.4.」。worktree 不在（封存後）時顯示「The worktree was removed when the task was archived.」。
- **Events**：沿用現在的表格與類型篩選；`type` 依來源上色，和 Chat 一致。
- **Runs**：每一次 run 一列：編號、原因、開始時間、經過時間、結果、token、花費；點一列會在 Chat 捲到那一次 run。

## 設定頁（`#/settings`）

| 東西 | 來源 |
| --- | --- |
| 每種 agent 與 gh 一張卡片 | `GET /api/setup` 的 `agents`、`github` |
| 狀態 | `usable`：綠色「Ready」或橘色「Not ready」 |
| 版本、登入方式 | `version`、`auth_label` |
| 為什麼不能用、怎麼修 | `detail`；`fix` 的每一條指令放在可以複製的程式碼區塊 |
| 提醒 | `notice`，黃色左邊框 |
| 上次檢查 | `checked_at`，加上「Check again」按鈕（`POST /api/setup/check`） |

## 對話框

- **New task**：專案、agent（只列 `usable` 的）、標題（可留空）、描述。描述欄 `Cmd/Ctrl+Enter` 送出。沒有能用的 agent 時送不出去，並連到設定頁（沿用現在的行為）。
- **Add project**：repo 的路徑、「Require an approving review」勾選框。
- **Archive 確認**：PR 還開著時出現，說明封存不會關掉 PR、不能取消封存。
- 共通：API 的錯誤訊息（`message`）顯示在按鈕上方；送出中按鈕顯示「Working…」、不能關閉。

## 空白、載入中、錯誤

- 載入中用骨架（灰色的方塊），不用轉圈。
- daemon 連不上時，最上方一條橘色的橫幅「Can't reach the daemon. Retrying…」，連上後自動消失（SSE 會自己重連，第 113 筆）。
- API 的錯誤訊息顯示在操作的旁邊（沿用）。

## 顏色

### 欄位（看板、側欄的圓點、狀態標籤）

| 欄位 | token | 深色 | 淺色 |
| --- | --- | --- | --- |
| Working | `--color-working` | `#4c8dff` | `#2563eb` |
| Needs you | `--color-needs-you` | `#f08c3a` | `#c2410c` |
| In review | `--color-in-review` | `#e2b93b` | `#a16207` |
| Ready to merge | `--color-ready` | `#3fb96b` | `#15803d` |
| Archive | `--color-archived` | `#74808f` | `#6b7280` |

### 訊息來源（Chat、Events）

| 來源 | token | 深色 | 淺色 |
| --- | --- | --- | --- |
| 你 | `--color-source-you` | `#4c8dff` | `#2563eb` |
| Reviewer | `--color-source-reviewer` | `#b48cff` | `#7c3aed` |
| 自動 | `--color-source-auto` | `#e2b93b` | `#a16207` |
| Agent | `--color-source-agent` | `#3cc8c8` | `#0f766e` |
| 系統 | `--color-source-system` | `#74808f` | `#6b7280` |

「你」和 Working 用同一個藍色：兩者都代表「人推動的事」。自動和 In review 同色系，但自動的卡片用 ⚙ 與置中的位置區分。

### 介面

| token | 用途 | 深色 | 淺色 |
| --- | --- | --- | --- |
| `--color-bg` | 頁面底色 | `#0e1116` | `#f7f8fa` |
| `--color-surface` | 側欄、欄位、卡片 | `#151a21` | `#ffffff` |
| `--color-surface-raised` | 卡片滑過、泡泡、輸入框 | `#1b222b` | `#f1f3f6` |
| `--color-border` | 一般邊框 | `#262e39` | `#e3e6eb` |
| `--color-border-strong` | 聚焦、分隔線 | `#36404d` | `#c9ced6` |
| `--color-text` | 主要文字 | `#e3e8ee` | `#1a1f27` |
| `--color-text-muted` | 次要文字 | `#8592a3` | `#5d6878` |
| `--color-danger` | 失敗、刪除 | `#ef5b5b` | `#dc2626` |
| `--color-success` | 通過、新增的行 | `#3fb96b` | `#15803d` |
| `--color-accent` | 主要按鈕、連結、聚焦框 | `#4c8dff` | `#2563eb` |

深色的數值沿用現在的 `styles.css`，畫面的感覺不會差太多。半透明的底色（標籤、diff 的行）用 `color-mix(in srgb, <token> 15%, transparent)`。

### 淺色主題

**這次就做**（決策第 182 筆），跟著系統的 `prefers-color-scheme` 切換，不另外做手動切換：token 一開始就有兩套，成本只是多一組數值；之後再補要逐一修改寫死的顏色。所有顏色都要經過 token，元件裡不寫死色碼。

## 字與間距

- 字型：`--font-sans` 用系統字型（`ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif`），`--font-mono` 沿用現在的 `ui-monospace, "SF Mono", Menlo, Consolas, monospace`。
- 字級：用 Tailwind 預設的 `text-xs`（12px）到 `text-lg`（18px）；內文 14px（`text-sm`），標題 18px，卡片內的次要資訊 12px。
- 間距：Tailwind 預設的 4px 為一單位；卡片內距 12px，區塊之間 16px 到 24px。
- 圓角：`--radius-sm` 4px（標籤）、`--radius-md` 8px（卡片、輸入框）、`--radius-lg` 12px（對話泡泡、對話框）。
- 這些 token 用 Tailwind v4 的 `@theme` 寫在 `web/src/styles.css`（任務 22），名稱照上表；Tailwind 的 class 會是 `bg-surface`、`text-text-muted`、`border-border`、`text-source-reviewer` 這類。

## 預留位置

| 東西 | 位置 | 這個階段的樣子 |
| --- | --- | --- |
| Shell | 右側面板（或分頁）的「Shell」 | 說明文字，任務 24 才做功能 |
| 主 agent | 側欄每個專案底下的「✦ Plan with main agent」 | 淡色、不能按，滑過去顯示「Coming in 0.5」 |

## 需要新增的 API

任務 21 補上，只在回應裡多兩個欄位，不改既有的欄位：

| 欄位 | 在哪裡 | 內容 |
| --- | --- | --- |
| `feedback_attempts` | 卡片 | 留言的自動處理用掉的次數（資料庫裡已經有） |
| `max_feedback_attempts` | 卡片 | 上限（`status.py` 的 `MAX_FEEDBACK_ATTEMPTS`，目前是 5） |

其他畫面需要的資料 API 都已經有了。

## 不做

- 卡片拖曳、在看板內合併（階段 8）
- 元件庫、圖示庫、路由與資料抓取的套件
- 手動切換主題（跟著系統即可）
- 行動裝置的版面（窄視窗能用即可：側欄收合、分割版退回分頁版）
