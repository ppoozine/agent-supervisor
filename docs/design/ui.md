# 介面

最後更新：2026-11-23

階段 5 新介面的需求。視覺方向是 **A · Calm**（決策第 186 筆）：安靜、密度適中、只有一個強調色。任務 22 已經把底層做好（tokens、Preflight、深淺主題、分割面板，PR #43）；任務 23 由 Claude Code 透過 agsv 把所有畫面調成這份文件的樣子，任務 24 做 Shell。

靜態草稿在 `mockups/`：每個畫面一個 HTML（直接用瀏覽器打開）與一張截圖（PNG）。HTML 是數值的依據（顏色、間距、字級照抄即可），PNG 方便快速瀏覽與實作後比對。草稿用假資料，字型從 Google Fonts 載入；草稿和這份文件不一致時以這份為準。

| 草稿 | 畫面 |
| --- | --- |
| `mockups/board.html` | 看板 |
| `mockups/board-project-filter.html` | 看板，專案篩選打開、選了一個專案 |
| `mockups/archive.html` | Archive |
| `mockups/task-chat-diff.html` | 任務詳情頁：Chat 與 Diff |
| `mockups/panel-shell.html`、`panel-events.html`、`panel-runs.html` | 任務詳情頁右側面板的另外三個分頁 |
| `mockups/dialog-new-task.html`、`dialog-add-project.html` | 兩個對話框 |
| `mockups/settings.html` | 設定頁 |

介面上的文字用英文（決策第 143 筆），這份文件裡引號中的英文就是畫面上要出現的字。

## 原則

1. **一眼看出狀態，一眼看出是誰說的。** 任務在哪個階段、卡在哪裡、每一則訊息是誰送的，不用點開或讀完內容就知道。
2. **只顯示 API 給的。** 看板欄位、狀態標籤一律用 API 回傳的 `column` 與 `label`，前端不自己推導（設計規則 1）；前端只決定顏色與排版。篩選、加總這類只用 API 資料就能算出來的，可以在前端做。
3. **花費是估算值。** 金額前面加 `~`；`cost_usd` 是 `null` 時改顯示 token 數；`usage_is_complete` 為否時後面加 `+`。
4. **鍵盤可以操作。** `Cmd/Ctrl+Enter` 送出（第 178 筆），對話框可以用 Escape 關閉，所有按鈕、分頁、選單都能用 Tab 走到。
5. **安靜。** 大面積用中性色，顏色只用來表示狀態與訊息來源；不用漸層、大陰影、左邊框的卡片、emoji。
6. **不複製 Agent Orchestrator** 的程式碼、圖示與視覺設計（第 170 筆）。不裝元件庫、圖示庫，圖示用幾行內嵌的 SVG 或 Unicode 字元。

## 版面總覽

```
┌──────────────────┬────────────────────────────────────────────────────┐
│ agent-supervisor ▣│ [Board|Archive] [All projects ▾]  6 active · 2 need you  [New task] │
│ [ New task      ]│                                                    │
│ Board          6 │  主區：看板 / Archive / 任務詳情 / 設定              │
│ Archive       25 │                                                    │
│ PROJECTS         │                                                    │
│ web-app        3 │                                                    │
│   ● Restyle …    │                                                    │
│   ● Migrate …    │                                                    │
│   ✦ Plan with main agent                                              │
│ api-service    2 │                                                    │
│   ● Add rate …   │                                                    │
│ + Add project    │                                                    │
│ ● Live           │                                                    │
│ Settings         │                                                    │
└──────────────────┴────────────────────────────────────────────────────┘
```

- 側欄寬 240px，底色比主區深一階（`--color-sidebar`），可以收合成只剩圖示（56px）。收合狀態記在 `localStorage`，讀不到時預設展開。
- **側欄的內容依畫面而不同**（決策第 188 筆）：
  - **看板**：每個專案底下列出它**進行中**的任務（看板四欄裡的卡片），前面的圓點顏色代表欄位，正在跑的圓點會呼吸閃爍。點任務打開詳情。
  - **看板且選了專案**：同上，選中的專案標成目前選取（`aria-current`），其他專案變淡（不隱藏）。
  - **Archive**：只列專案名稱，**不列已封存的任務**；已封存的任務只在 Archive 的表格裡。
  - **任務詳情、設定**：同看板。
- 側欄上方是「New task」，接著是「Board」「Archive」兩個項目（右邊是數量），再來是專案清單與「+ Add project」；最下面是連線狀態（「Live」與綠點；斷線時見「空白、載入中、錯誤」）與「Settings」。
- 網址仍用 hash（第 109 筆，不裝路由）：`#/`、`#/archive`、`#/tasks/{id}`、`#/settings`。

## 看板（`#/`）

草稿：`mockups/board.html`、`mockups/board-project-filter.html`。

**頁首**：左邊是「Board | Archive」的分段切換（`role="tablist"`）、專案篩選按鈕、一行摘要（「6 active · 2 need you」，用目前篩選後的卡片算）；右邊是主要按鈕「New task」。

**四欄**：Working、Needs you、In review、Ready to merge。欄位標題是圓點、名稱、張數；空的欄位顯示虛線框與淡色文字（沒篩選時「Nothing here」，篩選時「Nothing running in web-app」這類，依欄位換動詞）。欄寬平均分配，窄視窗時自動換行（`repeat(auto-fit, minmax(240px, 1fr))`）。

**卡片**

| 位置 | 內容 | 來源 |
| --- | --- | --- |
| 第一行（小字） | 專案 · `#id`，右邊是 agent | `project_id` 對到專案名稱；`agent_type` 對到 `display_name` |
| 標題 | 任務標題 | `title` |
| 狀態列 | 彩色圓點加標籤文字，後面是用量與經過時間 | `label`（顏色依 `column`）；`has_open_run` 時圓點閃爍；用量規則見原則 3 |
| 底部（有 PR 時） | 細分隔線下面：`PR #43`、CI 與 review 的結果、`comments 1/5` 或 `fixes 1/3`（大於 0 時） | `pr_number`、`ci_state`、`review_state`、`merge_state`、`ci_fix_attempts`、`feedback_attempts` |

- 卡片白底、1px 邊框、8px 圓角，滑過時邊框加深；不用陰影。
- 卡片不能拖曳。
- 沒有任何專案時，看板位置顯示說明與「Add project」按鈕；沒有能用的 agent 時，上方顯示提示並連到設定頁（沿用現在的行為）。

### 專案篩選（新功能）

- 頁首的按鈕顯示目前的選擇（「All projects ▾」或專案名稱），點了打開選單（`role="listbox"`）：「All projects」與總數、分隔線、每個專案與它進行中的任務數（選中的打勾）、分隔線、「Add project…」。
- 選了專案之後：看板只顯示那個專案的卡片；側欄如上；頁首的主要按鈕變成「New task in web-app」，開出的對話框預先選好這個專案。
- 點側欄的專案名稱等於在選單裡選它；再點一次回到「All projects」。
- 選擇記在 `localStorage`，讀不到時用「All projects」。專案篩選只在前端做（API 的 `GET /api/tasks` 已經有 `project_id` 參數，但卡片本來就一次全部載入，前端篩即可）。

## Archive（`#/archive`，新畫面）

草稿：`mockups/archive.html`。取代原本看板底下的 Archive 區（決策第 188 筆）。

- 頁首同看板：分段切換停在「Archive」、專案篩選（和看板共用同一個選擇）、右邊是搜尋框（「Search title or branch」，比對標題與 branch，不分大小寫）。
- 頁首下面一列結果的篩選：「All 25」「Merged 19」「PR closed 2」「Archived by you 4」，數字是目前專案篩選之下的張數。
- 表格：Task、Project、Agent、Outcome、PR、Finished、Usage。結果用圓點加文字：Merged（強調色）、PR closed（灰）、Archived by you（灰色空心）。窄視窗時表格可以左右捲動。
- 排序：完成時間新的在上。結果與完成時間的來源：`pr_state` 是 `merged` / `closed` 時是 PR 的結果，否則是你封存的（`archived_at`）；完成時間用 `archived_at`，沒有時用 `updated_at`。
- 點一列打開任務詳情；已封存的任務只能看（輸入框換成說明，沿用）。
- 表格下面一行說明：「Opening an archived task shows its Chat, Diff and history read-only.」

## 任務詳情頁（`#/tasks/{id}`）

草稿：`mockups/task-chat-diff.html`、`panel-*.html`。

### 頁首

```
Board /  Restyle the board and the task page  ● In review · CI running        [Archive] [Stop]
web-app · Codex   asv/task-27-restyle-the-board ⧉   PR #43   fixes 0/3   comments 1/5   3.9M tok
```

| 東西 | 來源 |
| --- | --- |
| 麵包屑「Board /」 | 回到看板（保留專案篩選） |
| 標題、狀態 | `title`、`label`（顏色依 `column`）；`has_open_run` 時圓點閃爍 |
| 專案、agent | 同看板 |
| branch | `branch`，等寬字，旁邊一個複製按鈕；還沒有時不顯示 |
| PR | `pr_number` 連到 GitHub；`pr_state` 是 merged / closed 時顯示在旁邊 |
| CI、review、衝突 | `ci_state`、`review_state`、`merge_state` |
| 次數 | `fixes {ci_fix_attempts}/{max_ci_fix_attempts}`、`comments {feedback_attempts}/{max_feedback_attempts}` |
| 用量 | `cost_usd`、`tokens`、`usage_is_complete`；滑過去顯示 `tokens_in` / `tokens_out` |
| Stop | 紅字白底的次要按鈕，只有 `has_open_run` 時可以按 |
| Archive | PR 還開著時先跳確認框（第 117 筆，沿用現在的文字與行為） |

### 主區的排法

**分割版**（決策第 182 筆）：左邊是 Chat，右邊是可以拖拉調整寬度的面板，分頁是 Diff、Shell、Events、Runs。視窗寬度小於 1100px 時退回分頁版：Chat、Diff、Shell、Events、Runs 五個分頁。預設打開 Chat；右側面板記住上次選的分頁（`localStorage`）。分頁的樣式是文字加底線，選中的是主要文字色的 2px 底線。

### Chat：對話的時間軸

**左右分邊**（決策第 187 筆）：**你說的話靠右，別人說的話靠左，系統與自動的事件置中**。一眼就分得出哪些是你下的指令。

**以 run 分段。** 每一次 run 前面一條分隔線：粗體「Run 2」，接著原因、開始時間、經過時間、結果符號，右邊是延伸到底的細線。

| `trigger` | 分隔線上的原因 |
| --- | --- |
| `initial` | `Task opened` |
| `user_message` | `Your message` |
| `review_feedback` | `Automatic: PR comments` |
| `ci_failure` | `Automatic: CI failed` |
| `merge_conflict` | `Automatic: merge conflict` |

`exit_status`：`success` ✓、`error` ✗、`stopped` ■、`limit_reached` ⏱、`interrupted` ⚡；還在跑時寫「running」。

**每一則的來源**

| 來源 | 資料 | 位置與外觀 |
| --- | --- | --- |
| **你** | `trigger` 是 `initial`、`user_message` 的 run 的 `input_text` | **靠右**。淡強調色底（`--color-you-bubble`）的泡泡，右下角小圓角；頭像在泡泡右邊（強調色方塊、白字「YO」）；泡泡內第一行是靠右的「You」 |
| **Agent** | `agent_message`（`kind` 是 `message`） | **靠左**。白底、細邊框的泡泡，左下角小圓角；頭像在左邊（agent 色方塊、白字縮寫 `CC`、`CX`）；泡泡內第一行是 agent 名稱（agent 色） |
| Agent 的動作 | `command`、`tool_call`、`agent_message`（`kind` 是 `reasoning`、`plan`） | 在 Agent 泡泡裡、訊息文字之前，每個動作一列淺灰底的精簡列：`▸ $ npm test   74 passed`、`▸ Edited 14 files`、`▸ Thinking`；點開才看輸出或內容。失敗的指令標紅，被擋下的動作（`permission_denied`）標紅並寫原因 |
| **Reviewer** | `review_comment` 事件 | **靠左**。淡紫底的泡泡、紫色頭像（作者縮寫）；第一行是作者、「reviewer」、檔案與行號（有的話），連到 GitHub |
| **自動** | `trigger` 是 `ci_failure`、`merge_conflict`、`review_feedback` 的 run 的 `input_text` | **置中**的一行小字，自動色：「Automatic · sent 1 new pull request comment to the agent」「Automatic · CI failed on 3f2a9c1e: sent the failing logs」；點開看實際送出的內容（CI log 很長，預設收合） |
| **系統** | `pushed`、`pr_opened`、`ci_status`、`state_change`（只在 run 不是成功結束時） | **置中**的一行小字，次要文字色：「Pushed f2e2268 · Opened PR #43 · CI passed」，同一個時間點的系統事件合成一行 |

- Reviewer 雖然是人，但不是「你」，所以靠左；左右分的是「你」與「其他」。
- **沒送給 agent 的留言**（`ignored` 有值）：Reviewer 泡泡變淡，加一行說明：`bot` → 「Not sent: posted by a bot」、`untrusted_author` → 「Not sent: the author is not a collaborator」、`empty` → 「Not sent: no text」、`not_a_change_request` → 「Not sent: approval」。
- 泡泡最大寬度：你的 72%、其他 78%，長文字換行。
- **預設收合**：自動訊息的內容、指令的輸出、工具呼叫的內容、agent 的推理。
- **跟隨最新**：捲到底部時新事件進來自動捲動；往上捲之後不自動捲，右下角出現「↓ New activity」按鈕。
- **還在跑**：最後一個 run 還沒結束時，底部是 agent 的頭像與「Working…」。

### 輸入框

- 白底、細邊框、8px 圓角的框，裡面是輸入區（placeholder「Send a follow-up to the same session…」），下面一列左邊是說明、右邊是「Send ⌘↵」（Windows / Linux 顯示 Ctrl+↵）。
- `Cmd/Ctrl+Enter` 送出，Enter 換行，輸入法選字中不送出（第 178 筆）。
- run 執行中維持拒絕（第 183 筆）：可以打草稿，說明「The agent is working. Stop it to send a message now.」，Send 停用。
- 任務已封存或 PR 已合併時，輸入框換成一行說明；PR 已關閉時仍可送（第 183 筆）。

### 右側面板：Diff

- 第一行：「17 files · +1680 −1230」。
- 檔案清單：白底卡片，每列是等寬字的路徑與 `+n −n`；點一列捲到該檔案。
- 每個檔案一張卡片：標題列是路徑（可以收合），內容是 diff，新增的行淡綠底、刪除的行淡紅底，等寬字 12px、行高 20px，太寬時左右捲動。

### 右側面板：Shell（任務 24）

草稿：`mockups/panel-shell.html`。

- 工具列：連線狀態的圓點、shell 名稱（`zsh`）、worktree 的路徑（等寬字，太長時截斷）、「Clear」「Restart」兩個按鈕。
- **agent 執行中**（`has_open_run`）時，工具列下面一條淡黃色的提醒：「The agent is working in this worktree. Files you change here end up in the same commit.」
- 終端機的配色**跟著主題**：淺色主題是白底深字，深色主題是深底淺字；顏色從 tokens 轉成 xterm.js 的 theme。字型用 `--font-mono`。
- 任務 24 之前只放說明「A terminal in this task's worktree. Coming in 0.4.」。worktree 不在（封存後）時顯示「The worktree was removed when the task was archived.」。

### 右側面板：Events

草稿：`mockups/panel-events.html`。

- 分頁標題後面是事件總數。
- 篩選的第一列是**依來源**的膠囊按鈕（`aria-pressed`）：All、Agent、You、Reviewer、Automatic、System，對應的規則和 Chat 一樣（在前端由事件類型與所屬 run 的 `trigger` 算出來）。第二列是兩個下拉選單：事件類型（沿用現在的類型篩選）、run（All runs、Run 1、Run 2…）。
- 列表：每列是時間（等寬字）、類型（等寬字，依來源上色；`permission_denied` 用危險色）、一行摘要。新的在上。
- 點一列展開：標題列是類型、run、時間與「Copy JSON」，內容是格式化的 `payload`。

### 右側面板：Runs

草稿：`mockups/panel-runs.html`。

- 分頁標題後面是 run 的數量。
- 上方四個數字：Tokens（任務累計）、Agent time（各 run 經過時間的加總，前端算）、Automatic fixes（`ci_fix_attempts`/`max_ci_fix_attempts`）、Comment rounds（`feedback_attempts`/`max_feedback_attempts`）。
- 說明「Select a run to jump to it in Chat.」
- 每個 run 一張卡片（新的在上）：第一列是「Run n」、來源色的圓點與原因、右邊是結果（✓ Success、✗ Error、● Running…，顏色同狀態）；第二列是開始時間、經過時間、用量、對應的 commit（`source_head_sha` 或這次 push 的 commit；沒有變更時寫「no changes」）。
- 點卡片：在 Chat 捲到那一次 run 的分隔線（窄視窗時先切到 Chat 分頁）。

## 對話框

草稿：`mockups/dialog-new-task.html`、`dialog-add-project.html`。

共通：白底、12px 圓角、半透明的深色遮罩；標題列是標題與關閉按鈕（`aria-label="Close"`），底部一列左邊是快捷鍵說明（「⌘↵ to start」），右邊是「Cancel」與主要按鈕。API 的錯誤訊息（`message`）顯示在對應欄位下方或按鈕上方；送出中主要按鈕顯示「Working…」、不能關閉。Escape 關閉。

### New task

欄位依序：

1. **Project**：下拉選單；從篩選了專案的看板打開時預先選好。
2. **Agent**：每個 agent 一張可以點的卡片（radio），內容是名稱與狀態：「Ready · reports cost」「Ready · reports tokens only」。不能用的 agent 也列出，變灰、不能選，狀態寫原因（「Not installed」「Not signed in」）並連到設定頁。
3. **Title**：選填，標籤後面淡色的「· optional」，placeholder 是描述的開頭（留空時用描述的開頭，沿用）。
4. **Description**：多行，placeholder「What should the agent do?」，`Cmd/Ctrl+Enter` 送出。
5. 欄位下面一個淺灰底的說明：「Starts right away in a new worktree on asv/task-…. The supervisor commits, pushes and opens the PR when the agent finishes.」branch 名稱用描述或標題即時預覽，只是示意，實際名稱由 daemon 決定。

主要按鈕「Start task」。沒有能用的 agent 時送不出去（沿用）。

### Add project

1. **Repo path**：等寬字輸入框，placeholder `/Users/you/code/my-repo`。送出後 API 回錯時，輸入框變紅框，錯誤訊息在下方（例如「Not the top of a git repository」，文字用 API 回傳的 `message`）。
2. 淺灰底的說明，列出資料夾的條件：「to be the top of a local git repository」「a GitHub remote that gh can reach; tasks open their pull requests there」「no other project already using it」。
3. **Require an approving review**：勾選框加一行說明「Tasks reach Ready to merge only after someone approves the PR.」

主要按鈕「Add project」，`Cmd/Ctrl+Enter` 也能送出。

### Archive 確認

PR 還開著時出現，說明封存不會關掉 PR、不能取消封存（沿用現在的文字），樣式同上。

## 設定頁（`#/settings`）

草稿：`mockups/settings.html`。**只能看，不能改**（決策第 189 筆）。

- 左側是設定頁自己的導覽（取代主側欄的內容）：「← Board」，接著 Agents、GitHub、Projects、Usage、Limits and permissions，點了捲到該區。
- 頁首：「Settings」、上次檢查的時間（`checked_at`）、「Check again」（`POST /api/setup/check`）。下面一段說明（沿用現在的文字：agsv 不幫你登入、不存金鑰）。
- 每一區是白底、細邊框的卡片。

| 區 | 內容 | 來源 |
| --- | --- | --- |
| **Agents** | 列出所有註冊的 agent，每個一列：名稱、狀態（綠「Ready」、橘「Not signed in」「Not installed」等）、版本、登入方式與是否回報費用。**沒準備好的**下面展開「Run in a terminal, then Check again:」與指令，每段指令一個「Copy」按鈕；`notice` 用淡黃底的一行 | `GET /api/setup` 的 `agents`：`usable`、`installed`、`detail`、`version`、`auth_label`、`fix`、`notice`；是否回報費用來自 `GET /api/agents` |
| **GitHub** | `gh` 一列，規則同 Agents（例如沒登入時顯示 `gh auth login`） | `GET /api/setup` 的 `github` |
| **Projects** | 表格：Name、Local path、GitHub、Requires review、Tasks（進行中與已封存的數量）；右上角「Add project」 | `GET /api/projects`（`name`、`repo_path`、`github_repo`、`require_review`）與卡片 |
| **Usage** | 三個數字：Cost（只算會回報費用的 agent）、Tokens in、Tokens out，涵蓋所有任務（含已封存）；有不回報費用的 agent 時加一行說明 | `GET /api/usage`（已經有，之前畫面沒用到） |
| **Limits and permissions** | 兩欄的清單：每個任務的費用與 token 上限、每次 run 的輪數與時間上限、自動修復與留言的次數上限、輪詢間隔、通知、sandbox、網路、禁止的指令。下面說明「To change these, edit ~/.agent-supervisor/config.toml and restart agsv serve.」 | **需要新的 API**（見「需要新增的 API」），由獨立的小任務做（決策第 189 筆）；任務 23 先不放這一區 |

## 空白、載入中、錯誤

- 載入中用骨架（灰色的方塊），不用轉圈。
- daemon 連不上時，側欄的「Live」換成橘點「Reconnecting…」，主區最上方一條淡橘色的橫幅「Can't reach the daemon. Retrying…」，連上後自動消失（SSE 會自己重連，第 113 筆）。
- API 的錯誤訊息顯示在操作的旁邊。

## 顏色

所有顏色都經過 token，元件裡不寫死色碼（第 182 筆）。深淺兩套，跟著系統的 `prefers-color-scheme` 切換。淺色是草稿上的數值；深色是同一套色相調亮、底色調暗，文字對比度都至少 4.5:1。

### 介面

| token | 用途 | 淺色 | 深色 |
| --- | --- | --- | --- |
| `--color-bg` | 主區底色 | `#fbfbfc` | `#0f1115` |
| `--color-sidebar` | 側欄底色 | `#f7f7f9` | `#13161b` |
| `--color-surface` | 卡片、頁首、面板、對話框 | `#ffffff` | `#16191f` |
| `--color-surface-raised` | 動作列、滑過、說明框 | `#f4f5f7` | `#1c2027` |
| `--color-border` | 一般邊框 | `#e6e8ec` | `#262a33` |
| `--color-border-strong` | 滑過的邊框、分隔 | `#c9cdd5` | `#3a404b` |
| `--color-text` | 主要文字 | `#16181d` | `#e6e8ec` |
| `--color-text-muted` | 次要文字 | `#5f6672` | `#9399a5` |
| `--color-accent` | 主要按鈕、連結、選取、聚焦框 | `#4b4fc9` | `#8b8ef0` |
| `--color-accent-contrast` | 主要按鈕上的文字 | `#ffffff` | `#0f1115` |
| `--color-danger` | 失敗、Stop、刪除的行 | `#b4232f` | `#f0717a` |
| `--color-success` | 通過、新增的行 | `#1e7a45` | `#4cc38a` |
| `--color-warning-bg` | Shell 的提醒、notice | `#fff6e5` | `#2a2112` |
| `--color-you-bubble` | 你的泡泡底色 | `#eef0fd` | `#23254a` |

### 欄位（看板、側欄的圓點、狀態標籤）

圓點用 `--color-<欄位>`；標籤文字在淺色主題用較深的 `--color-<欄位>-text`，才有足夠的對比。

| 欄位 | token | 淺色圓點 | 淺色文字 | 深色（圓點與文字） |
| --- | --- | --- | --- | --- |
| Working | `--color-working` | `#3e63dd` | `#2f54c9` | `#6e8cf0` |
| Needs you | `--color-needs-you` | `#e8590c` | `#b4410a` | `#f28c4b` |
| In review | `--color-in-review` | `#c99a06` | `#7a5800` | `#e0b93e` |
| Ready to merge | `--color-ready` | `#2f9e5b` | `#1e7a45` | `#4cc38a` |
| Archive | `--color-archived` | `#8b919c` | `#5f6672` | `#8b919c` |

### 訊息來源（Chat、Events、Runs）

頭像的底色用 `--color-source-<來源>`（白字）；文字用 `-text` 的版本。

| 來源 | token | 淺色頭像 | 淺色文字 | 深色（頭像與文字） |
| --- | --- | --- | --- | --- |
| 你 | `--color-source-you` | `#4b4fc9` | `#4b4fc9` | `#8b8ef0` |
| Agent | `--color-source-agent` | `#0e7c74` | `#0e7c74` | `#3cc0b4` |
| Reviewer | `--color-source-reviewer` | `#8e4ec6` | `#7e3fb8` | `#b78bea` |
| 自動 | `--color-source-auto` | `#a35200` | `#8f4700` | `#e0a04a` |
| 系統 | `--color-source-system` | `#6b7280` | `#5f6672` | `#8b919c` |

Reviewer 泡泡的底色與邊框、diff 的行、各種淡底色用 `color-mix(in srgb, <token> 8%–15%, var(--color-surface))`，不另外開 token。

## 字與間距

- 字型：`--font-sans` 是 `Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif`，`--font-mono` 是 `"JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace`。**不從網路載入字型**（本機工具不連外），裝了 Inter 與 JetBrains Mono 的電腦會用到，沒裝時用系統字型。要不要把字型檔打包進來，另外決定。
- 字級：內文與卡片標題 13px，次要資訊 12px，小標籤 11px，頁面標題 16px，設定頁的頁面標題 18px。
- 字重：一般 400、標題與名稱 500 到 600；不用 700 以上。
- 間距：4px 為一單位；卡片內距 12px，欄位之間 16px，頁面左右 24px。
- 圓角：`--radius-sm` 6px（按鈕、輸入框、動作列）、`--radius-md` 8px（卡片、面板）、`--radius-lg` 12px（對話框）；泡泡 10px，靠說話者那一角 2px。
- 陰影：只有對話框與下拉選單有陰影，其他用邊框。
- 這些 token 用 Tailwind v4 的 `@theme` 寫在 `web/src/styles.css`（任務 22 已建立，任務 23 改成這裡的數值與名稱）。

## 預留位置

| 東西 | 位置 | 這個階段的樣子 |
| --- | --- | --- |
| Shell | 右側面板的「Shell」 | 說明文字，任務 24 才做功能 |
| 主 agent | 側欄每個專案底下的「✦ Plan with main agent」（草稿裡沒畫，沿用任務 22 的位置） | 淡色、不能按，滑過去顯示「Coming in 0.5」 |

## 需要新增的 API

| 端點 | 內容 | 誰做 |
| --- | --- | --- |
| `GET /api/config`（唯讀） | 設定頁「Limits and permissions」要顯示的值：上限、輪詢、通知、權限政策。只回這些欄位，不回路徑以外的本機資訊 | 獨立的小任務（決策第 189 筆） |

其他畫面需要的資料 API 都已經有了；篩選、加總在前端算。

## 不做

- 在設定頁修改設定、修改專案（之後的階段）
- 卡片拖曳、在看板內合併（階段 8）
- 元件庫、圖示庫、路由與資料抓取的套件
- 手動切換主題（跟著系統即可）
- 從網路載入字型
- 行動裝置的版面（窄視窗能用即可：側欄收合、分割版退回分頁版、表格左右捲動）
