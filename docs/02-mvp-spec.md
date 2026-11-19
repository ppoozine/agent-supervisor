# 02 · MVP 規格

最後更新：2026-11-19

## 目標與範圍

MVP 只證明一件事：一個任務從開單到 CI 綠燈，中間 CI 失敗能自動回到同一個 agent 修復，人只在例外時介入。

**包含**

- 加入一個本機 repo
- 手動開任務（描述 + 送出），支援 Claude Code 與 Codex 兩種 agent（Codex 在階段 1.5 加入）
- 每個任務自動建 worktree 與 branch
- 啟動 agent session，log 即時串流到前端
- agent 正常結束的每一次 run 之後由 supervisor push，第一次 push 後開 PR
- 輪詢 GitHub 的 PR / CI 狀態，推導看板欄位
- CI 失敗時抓 log 餵回同一個 session
- 四欄看板與任務詳情頁
- 每個任務的稽核紀錄與 token 花費（花費為估算值）
- 無人看管時的基本權限政策（sandbox 等級、是否允許連網、工具與指令的限制，屬於基本防護）

**不包含**

- Orchestrator 自動拆任務
- Review 留言與 merge conflict 的自動處理
- 從 issue 自動建任務、webhook
- 互動式的人工核准關卡（階段 5）
- 自製的 API agent（階段 6）
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
│ SQLite    AgentAdapter              │              │
└──────────────┼──────────────────────┼──────────────┘
     Claude Code / Codex              ↓
   （各任務獨立 worktree）          GitHub
                                 （每 30 秒輪詢）
```

- Poller 查到 CI 失敗後交給 Dispatcher，Dispatcher 請 Runner 續接同一個 session。
- **push 與開 PR 由 supervisor 執行，不交給 agent。** Agent 只在 worktree 內改檔。它可能會自己 commit，也可能不會（Codex 在 sandbox 下無法在 worktree 裡 commit，見 `codex-notes.md`），沒有 commit 的變更由 supervisor 在收尾時補。
- 四個元件都讀寫 SQLite。

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
| `require_review` | 是否要有 review 核准才算可合併，預設 `false` |

### tasks

| 欄位 | 說明 |
| --- | --- |
| `id`, `project_id` | 主鍵與所屬專案 |
| `title`, `prompt` | 卡片標題與給 agent 的任務描述 |
| `agent_type`, `model` | 使用哪一個 adapter 與模型 |
| `session_id` | agent session，續接對話用 |
| `branch`, `worktree_path` | 此任務專屬的分支與工作目錄 |
| `pr_number`, `pr_state` | 開 PR 後回填；`pr_state` 為 `open` / `merged` / `closed` |
| `head_sha` | Poller 最近一次查到的 PR 最新 commit |
| `ci_state` | `head_sha` 上的 CI 結果，見下方說明 |
| `review_state` | `none` / `approved` / `changes_requested` |
| `merge_state` | PR 能不能合併進基底分支：`unknown` / `clean` / `conflicting` |
| `last_handled_head_sha` | 已經餵回過 CI 失敗的 commit，避免重複餵回 |
| `ci_fix_attempts` | 自動修復次數 |
| `last_handled_feedback_event_id` | 已經餵回 agent 的最後一則留言（`review_comment` 事件的編號）；沒處理過是 `NULL` |
| `feedback_attempts` | 自動處理留言的次數 |
| `last_handled_conflict_head_sha` | 已經請 agent 解過衝突的 commit，避免重複處理 |
| `archived_at`, `created_at`, `updated_at` | 時間戳 |

`pr_state`、`head_sha`、`ci_state`、`review_state`、`merge_state` 是 Poller 對 GitHub 狀態的快取，不是看板欄位。

後來才加的欄位在 daemon 啟動時自動補到舊的資料庫上，不需要手動處理。反過來，資料庫被比較新的版本開過之後，舊版讀到不認得的欄位會略過，不會因此讀不了。

`ci_state` 的意思：

| 值 | 意思 |
| --- | --- |
| `none` | 這個 commit 上沒有任何 CI 檢查 |
| `pending` | 至少一個檢查還沒結束 |
| `passed` | 所有檢查都結束且全部通過 |
| `failed` | 所有檢查都結束且至少一個失敗 |

有檢查失敗但其他檢查還在跑時仍是 `pending`，等全部結束才一次處理。

單一檢查的結果如何歸類（任務 3 實測 `gh pr view --json statusCheckRollup` 後決定）：

| GitHub 回報的結果 | 歸類 | 說明 |
| --- | --- | --- |
| 還沒結束（`QUEUED`、`IN_PROGRESS` 等）；外部狀態 `PENDING`、`EXPECTED` | 還沒結束 | |
| `SUCCESS`、`NEUTRAL`、`SKIPPED` | 通過 | 被略過的 job 不該擋住合併 |
| `FAILURE`、`TIMED_OUT`、`STARTUP_FAILURE`；外部狀態 `FAILURE`、`ERROR` | 失敗 | |
| `CANCELLED` | 失敗 | 實測 job 超過 `timeout-minutes` 回報的也是這個值，agent 修得了；手動取消會多花一次修復，由修復次數上限擋住 |
| `ACTION_REQUIRED`、`STALE`、沒看過的新值 | 還沒結束 | 等人核准或等重跑，agent 修不了；卡片停在 In review |

- 檢查有兩種形狀：GitHub Actions 的檢查（有 `status` 與 `conclusion`）與外部服務回報的狀態（只有 `state`）。兩種都要處理。
- 同一個檢查重跑時，GitHub 只回報最新的一次，不需要自己挑。
- `ci_state` 為 `none` 時卡片停在 In review（標籤 `No checks`），不算通過。剛 push 完的幾秒內檢查清單也是空的，算通過的話卡片會短暫跳到 Ready to merge。
- 實測過的結果：成功、失敗、略過、手動取消、逾時、還在跑、外部狀態 `PENDING`、沒有任何檢查、重跑。其餘是依列舉值的意思歸類。

### runs

| 欄位 | 說明 |
| --- | --- |
| `id`, `task_id` | 主鍵與所屬任務 |
| `trigger` | `initial` / `ci_failure` / `user_message` / `review_feedback` / `merge_conflict` |
| `input_text` | 這次送給 agent 的內容。`ci_failure` run 建立時先寫一段簡短說明，準備階段抓到 CI log 後更新成完整內容 |
| `source_head_sha` | `ci_failure` 與 `merge_conflict` run 針對的 commit；其他 trigger 為 `NULL` |
| `started_at`, `ended_at` | 執行區間 |
| `exit_status` | `success` / `error` / `stopped` / `limit_reached` / `interrupted` |
| `tokens_in`, `tokens_out` | 用量；run 被中斷而拿不到時為 `NULL` |
| `cost_usd` | 花費估算值；adapter 無法回報時為 `NULL` |

每個任務同時最多只有一個未結束的 run，由資料庫約束保證。`exit_status` 在 run 結束前為 `NULL`，結束後的意思：

| 值 | 意思 |
| --- | --- |
| `success` | agent 正常結束這一輪 |
| `error` | agent 或 adapter 出錯，或收尾時 push、開 PR 失敗 |
| `stopped` | 使用者按了 Stop，或 PR 被合併、關閉時由 Poller 中止 |
| `limit_reached` | 這一輪撞到輪數、花費或執行時間上限 |
| `interrupted` | daemon 在 run 執行中被關掉或當掉 |

### events

只新增，不修改、不刪除。

| 欄位 | 說明 |
| --- | --- |
| `id`, `task_id`, `run_id`, `ts` | 主鍵、歸屬、時間；不屬於任何 run 的事件（例如 Poller 寫的 `ci_status`）`run_id` 為 `NULL` |
| `type` | `session_started` / `agent_message` / `tool_call` / `command` / `permission_denied` / `pushed` / `pr_opened` / `ci_status` / `review_comment` / `state_change` |
| `payload` | JSON，依 `type` 而異 |

Agent 的推理內容與計畫更新也記成 `agent_message`，在 `payload` 裡用 `kind` 標明是一般訊息、推理還是計畫。

幾種事件的 `payload` 慣例：

| 事件 | 內容 |
| --- | --- |
| `session_started` | `session_id`（新 session 在 agent 確認建立之前是空的，確認後會再送一筆帶 id 的），以及 `process_marker`：一個會出現在 agent 程序命令列上的字串，重啟復原時用它找遺留的程序 |
| `tool_call`、`command` | 一次工具呼叫寫成兩筆事件，`phase` 分別是 `started` 與 `finished`，用 `tool_use_id` 對應。因為事件只能新增，結果不能回填到第一筆 |
| `permission_denied` | 被拒絕的工具、完整輸入、拒絕的原因 |
| `state_change` | run 結案時寫一筆，`change` 為 `run_ended`，帶 `exit_status`、`detail` 與 `usage_raw` |
| `ci_status` | Poller 查到的五個狀態欄位與 PR 編號。只在其中任何一個有變化時才寫，不是每次輪詢都寫 |
| `review_comment` | PR 上的一則留言：GitHub 的編號（`github_id`）、種類（`comment` / `review` / `inline_comment`）、作者、內文、檔案與行號、連結。`ignored` 不是空的表示這則留言不餵回 agent，內容是原因 |

## Run 的生命週期

一個 run 從建立到結案會經過四段：**建立 → 準備 → agent 執行 → 收尾**。流程圖見 `06-diagrams.md`。

| 階段 | 做什麼 |
| --- | --- |
| 建立 | 在資料庫交易內寫入 run。開任務時，任務與它的 `initial` run 在同一個交易內建立，所以不會有「沒有任何 run 的任務」 |
| 準備 | 確保任務有自己的 worktree 與 branch（已經有了就略過，所以只有第一個 run 會真的建立）。`ci_failure` 另外要抓失敗的 CI log；`merge_conflict` 另外要開始合併 |
| agent 執行 | 透過 adapter 跑一輪，事件寫進 `events` |
| 收尾 | commit、push、開 PR，見下一節 |

**一個 run 由同一個可取消的工作從頭負責到尾。**

- 四段都算「run 正在執行」，全部做完才寫入 `ended_at`。
- 執行時間上限從 run 建立時起算，涵蓋準備與收尾，不只是 agent 執行的時間。`git`、`gh` 等外部指令各自有逾時。
- 不論在哪一段出錯、逾時或被取消，都由這個工作把 run 結案，不會留下沒人管的 run。準備階段失敗（例如 worktree 建不起來、抓不到 log）時 run 記為 `error`。
- 一個 run 只會有一個結案結果。已經被 Stop 的 run 不會被後來完成的收尾改寫成 `success`。

**每個任務一把鎖。** 建立 run、Stop、封存這三種操作都要先拿到該任務的鎖才能進行，所以封存進行到一半時不會有新的 run 被建立。鎖只存在於 daemon 的記憶體內，搭配資料庫「每個任務最多一個未結束的 run」的約束。

## Worktree 與 branch

| 項目 | 規則 |
| --- | --- |
| 位置 | 資料目錄下的 `worktrees/<專案名>/task-<任務編號>`，預設是 `~/.agent-supervisor/worktrees/…` |
| branch 名稱 | `asv/task-<任務編號>-<標題的英數字>`，最長 60 個字元；標題沒有英數字時只用編號 |
| 基底 | 先 `git fetch`，再從遠端的預設分支（`origin/<default_branch>`）切出來。不受本機預設分支是否更新影響，也不動使用者自己的工作目錄。同一時間只進行一個 `git fetch`：同一個 repo 的 worktree 共用同一份 git 資料，同時 fetch 會互搶鎖 |
| 專案登記 | 記下 repo 路徑、GitHub 的 `owner/name`、預設分支、是否要求 review。GitHub repo 與預設分支可以從 `origin` 自動讀出 |

任務的工作目錄如果不是這個工具建立的 worktree（沒有自己的 branch），準備階段會拒絕執行，封存時也不會去動那個目錄。

## Run 結束後的收尾

Agent 正常結束這一輪之後，Runner 依序做下面的事。不論是哪一種 trigger 都一樣。

收尾是 run 的一部分：全部做完才寫入 `ended_at`，所以收尾期間卡片仍在 Working。

1. worktree 有未 commit 的變更，或停在合併到一半的狀態 → 由 supervisor 補一個 commit，訊息是 `chore(agent): save uncommitted changes from run <run 編號>`。合併到一半時先檢查衝突是否真的解掉，見「Merge conflict」。
2. branch 相對於遠端有新的 commit → push，寫入 `pushed` 事件，並立刻把 `head_sha` 更新為剛 push 的 commit、`ci_state` 設為 `pending`、`review_state` 設為 `none`、`merge_state` 設為 `unknown`。這樣不用等下一次輪詢，卡片就不會被舊 commit 的 CI 結果或舊的核准誤判。
3. 還沒有 PR 且 branch 相對於基底分支有 commit → 開 PR，回填 `pr_number` 並把 `pr_state` 設為 `open`，寫入 `pr_opened` 事件。PR 的標題是任務標題，內容是任務描述加上一行註明來源的文字。
4. branch 相對於基底分支沒有任何 commit → 不開 PR，卡片進 Needs you（標籤 `No changes`）。

補 commit、push 或開 PR 任何一步失敗時，run 的 `exit_status` 記為 `error`，寫入一筆事件說明原因，卡片進 Needs you。不自動重試。

收尾的每一步都要先查現況再動手，重做一次不會出錯：已經 push 過就不再 push，GitHub 上這個 branch 已經有 PR 就回填它的編號與實際狀態、不再開一個。收尾做到一半 daemon 當掉時，run 會被標成 `interrupted`；之後使用者送訊息讓它繼續，那一次 run 結束時的收尾會把沒做完的補上。

`ci_failure` run 結束後若沒有產生新的 commit，`head_sha` 不會變，這個任務不再符合自動修復條件，卡片進 Needs you。

## 看板狀態

看板欄位不存進資料庫，每次由 `status.py` 的純函式從 run、PR、CI、review 狀態算出來。

**先判斷是否離開看板**：任務已封存，或 `pr_state` 為 `merged` / `closed` 且沒有 run 正在執行，就不出現在四個欄位裡，改列在 Archive 區。標籤優先顯示 PR 的結果（`Merged` / `Closed`）；已封存而 PR 還開著或沒有 PR 時是 `Archived`。

PR 被合併或關閉時如果還有 run 在執行，Poller 會中止那個 run（`exit_status` 為 `stopped`，不做收尾），停下之後卡片才離開看板。

其餘任務由上往下判斷，先符合者優先：

| 欄位 | 條件 |
| --- | --- |
| Working | 有 run 正在執行；或符合「留言回饋條件」「解衝突條件」「自動修復條件」其中之一、正在等 Dispatcher 接手 |
| Needs you | 最後一次 run 的 `exit_status` 不是 `success`；或最後一次 run 是 `success` 但任務沒有 PR（agent 沒有產生任何變更）；或還有沒處理的 review 意見卻不符合留言回饋條件；或 PR 有衝突卻不符合解衝突條件；或 CI 失敗但不符合自動修復條件（含已達修復上限、累計花費超過上限） |
| Ready to merge | PR 開著、CI 通過、確定沒有衝突（`merge_state` 是 `clean`），且（專案不要求 review，或 review 已核准） |
| In review | 其餘 PR 開著的情況：CI 進行中、PR 上沒有任何 CI 檢查、CI 通過但還不知道有沒有衝突（標籤 `Checking mergeability`），或 CI 通過但還在等審查 |

三種自動處理同時成立時的順序是留言、衝突、CI 失敗，Dispatcher 與看板的標籤用同一個順序。

## 自動修復條件與防迴圈

**自動修復條件**是一個純函式，`status.py` 與 Dispatcher 共用同一份，九條全部成立才算符合：

1. `head_sha` 上的所有 CI 檢查都已結束，且至少一個失敗
2. `head_sha` 不等於 `last_handled_head_sha`
3. `ci_fix_attempts` 小於 3
4. 任務的剩餘預算大於零：花費上限減去累計 `cost_usd`，以及 token 上限減去累計 token 數，兩者都要大於零
5. 沒有 run 正在執行
6. PR 開著，任務未封存
7. 最後一次 run 的 `exit_status` 是 `success`
8. 沒有還沒處理的 review 意見（定義見「Review 留言回饋」）
9. PR 沒有衝突（`merge_state` 不是 `conflicting`）

第 7 條的用意：使用者按了 Stop、run 被中斷、出錯或撞到上限之後，不會再自動開新的 run，要等人送訊息且那一次 run 正常結束，才恢復自動修復。

第 9 條的用意：有衝突的 PR 上 CI 不會跑，要先解掉衝突才知道 CI 的結果。

第 8 條的用意：有留言還沒處理時先處理留言。處理留言通常會產生新的 commit，舊 commit 的 CI 失敗就不用修了；沒有產生新的 commit 時，同一個 commit 的失敗接著照常修復。

Dispatcher 發現任務符合條件時：

1. 在同一個資料庫交易內做完三件事：重新檢查條件、建立 trigger 為 `ci_failure` 的 run（`source_head_sha` 記下目前的 `head_sha`）、把 `last_handled_head_sha` 設為同一個值並將 `ci_fix_attempts` 加一
2. 交易結束後才抓 `source_head_sha` 上所有失敗檢查的 log，只擷取失敗的片段
3. 把 run 交給 Runner 續接同一個 session

抓 log 是 run 準備階段的一部分，所以這時 run 已經存在、可以被 Stop。抓 log 失敗時，這個 run 以 `error` 結束，卡片進 Needs you；已經扣掉的修復次數不退回。

**送給 agent 的 CI log**

- 只有 GitHub Actions 的檢查抓得到 log（`gh run view --job <編號> --log-failed`）。被取消或逾時的 job 沒有「失敗的步驟」，改抓整份 log。
- 外部服務回報的失敗抓不到 log，只在指示裡列出名稱。所有失敗的檢查都抓不到 log 時，run 以 `error` 結束。
- 抓 log 之前會再確認 PR 的最新 commit 仍是 `source_head_sha`，已經不是就以 `error` 結束。
- log 會去掉每行的時間戳與顏色碼，每個失敗的檢查只保留尾段。所有檢查合計的長度上限是全域設定（暫定 20,000 個字元），平均分給各個檢查。實測一個吵雜的失敗步驟有 3,008 行、320 KB，真正的錯誤在最後兩行。

**修復次數的歸零**：使用者送訊息時，在建立 run 的同一個交易內把 `ci_fix_attempts` 與 `feedback_attempts` 歸零。人已經介入，自動修復重新有 3 次機會、留言處理重新有 5 次；花費與 token 上限仍然有效。PR 上的留言不會讓這兩個次數歸零，理由見「Review 留言回饋」。

使用者送訊息（`POST /tasks/{id}/messages`）走同一套建立 run 的程序。任務的花費或 token 已達上限時，目前的實作會拒絕新的訊息（這一點尚未定案，見 `03-decisions.md`）。任務已經有未結束的 run、已封存，或 PR 已合併或關閉時，新的 run 建不起來，API 回報錯誤。這樣 Dispatcher 與使用者不會同時讓兩個 run 用同一個 session 與 worktree。

**防無限迴圈**

1. 同一個 commit 的 CI 失敗只餵回一次：比對 `last_handled_head_sha`。同一個 commit 的衝突只處理一次：比對 `last_handled_conflict_head_sha`。同一則留言也只餵回一次：比對 `last_handled_feedback_event_id`。
2. 自動修復最多 3 次：`ci_fix_attempts` 到 3 就停止，解衝突也算在這 3 次裡。留言的自動處理最多 5 次：`feedback_attempts` 到 5 就停止。
3. 單一任務的花費上限與 token 上限：累計 `cost_usd` 或累計 token 數任何一個達到上限，就不再啟動自動修復；兩個上限值都是全域設定。累計 token 數是所有 run 的 `tokens_in` 加 `tokens_out`，其中 `tokens_in` 包含快取的輸入 token。`TurnResult` 回報的必須是**這一輪**的用量；agent 給的是整個 session 的累計值時（例如 Codex 的 CLI），由 adapter 相減換算。
4. 單一 run 的上限：Runner 把「剩餘預算」與「最多輪數」交給 adapter，在執行中就擋下，不等 run 結束。Adapter 做不到時由 `capabilities` 表明。
5. 單一 run 的執行時間上限：由 Runner 自己計時，不依賴 agent。超過就中止這個 run，`exit_status` 記為 `limit_reached`。這是所有 adapter 都有的最後一道防線。

這些規則管的是「能不能再啟動自動修復」與「正在跑的 run 要不要被擋下」，**不影響成果能不能合併**：

- 修復次數上限只擋第四次。第三次修復照常跑完；它把 CI 修好了，卡片一樣會到 Ready to merge。
- 已經超過花費上限但 CI 是綠的，卡片照常是 Ready to merge 或 In review。
- CI 仍然失敗而規則不允許再自動修復時，卡片才進 Needs you。
- 正在跑的 run 被第 4、5 條擋下時，`exit_status` 為 `limit_reached`，卡片進 Needs you。

**關於花費**

- `cost_usd` 是 agent 依價目表算出的估算值，不是帳單。使用訂閱方案時它代表相對用量。介面上一律標示為估算。
- Adapter 無法回報花費時（例如 Codex）`cost_usd` 為 `NULL`，第 3 條的花費上限不生效，改由同一條的 token 上限擋住，另外還有第 2 條與第 5 條。
- Run 被中斷而拿不到用量時，`cost_usd` 也是 `NULL`。累計時不把它當成零：這個任務的花費統計標示為「不完整」。

## Review 留言回饋

PR 上出現新的留言時，自動餵回同一個 agent session 處理。流程和 CI 自動修復相同：Poller 發現 → Dispatcher 建立 run → Runner 續接 session → 收尾時 push。

**觸發條件是「有新的留言」，不是 GitHub 的 Request changes。** PR 是用使用者自己的帳號開的，GitHub 不允許作者對自己的 PR 要求修改，所以一個人使用時 `review_state` 不會是 `changes_requested`（決策第 119 筆）。

**留言的三種形式**（任務 7 實測）

| 形式 | 例子 | 怎麼查 |
| --- | --- | --- |
| 一般留言 | PR 對話頁最下面的留言 | `gh api repos/{repo}/issues/{n}/comments` |
| Review 的總評 | 送出 review 時填的說明，帶著 `COMMENTED` / `CHANGES_REQUESTED` / `APPROVED` | `gh api repos/{repo}/pulls/{n}/reviews` |
| 逐行留言 | 在 Files changed 的某一行上留言 | `gh api repos/{repo}/pulls/{n}/comments` |

- 用 REST API 查完整內容。`gh pr view --json comments,reviews` 看不出作者是不是機器人（`cli-triage[bot]` 會顯示成 `cli-triage`，也沒有型別欄位），而且完全沒有逐行留言。
- 三種留言都有 `node_id`，和 `gh pr view` 回的 `id` 是同一個值，用它當作留言的編號。
- 每一則逐行留言都屬於某一個 review；單獨留一則逐行留言時，GitHub 會自動建立一個內文是空的 `COMMENTED` review 來裝它。
- **還沒送出的 review 查得到**（狀態是 `PENDING`），因為查詢用的就是使用者本人的帳號。這種 review 與它底下的逐行留言一律略過，等送出後才算。
- 多查三次 API 的成本只在有新留言時才付：Poller 原本那一次 `gh pr view` 順便帶回一般留言與已送出 review 的編號，全部都記錄過就不再多查。

**哪些留言要餵回**

Poller 把每一則留言寫成一筆 `review_comment` 事件，同一則只記一次（比對 `github_id`）。下面幾種照樣記錄，但標上略過的原因、不餵回 agent：

| 略過的原因 | 情況 |
| --- | --- |
| `bot` | 作者是機器人（REST 的 `user.type` 為 `Bot`） |
| `empty` | 內文是空的 `COMMENTED` review，只是用來裝逐行留言的空殼 |
| `not_a_change_request` | 核准或已撤銷的 review。它底下的逐行留言是另外的項目，照常餵回 |

- 內文是空的 `CHANGES_REQUESTED` review 仍然餵回，讓 agent 知道 reviewer 要求修改。
- 留言被編輯不會再餵回一次。
- 已封存的任務不抓留言。
- 任務第一次被查到時，PR 上原本就有的留言也算，不另外判斷新舊。

**留言回饋條件**是一個純函式，`status.py` 與 Dispatcher 共用，全部成立才算符合：

1. 有還沒餵回的留言（`review_comment` 事件的編號大於 `last_handled_feedback_event_id`，且沒有被略過）
2. `feedback_attempts` 小於 5
3. 任務的剩餘預算大於零（花費與 token）
4. 沒有 run 正在執行
5. PR 開著，任務未封存
6. 最後一次 run 的 `exit_status` 是 `success`

Dispatcher 發現任務符合條件時，在同一個資料庫交易內做完：重新檢查條件、讀出所有還沒餵回的留言、建立 trigger 為 `review_feedback` 的 run（內容是這些留言）、把 `last_handled_feedback_event_id` 設為最後一則的事件編號、`feedback_attempts` 加一。所以同一則留言不會被餵回兩次，也不會有留言在中間被跳過。

- **連續留好幾則時合成一次處理**：這一輪輪詢才看到新留言的任務，這一輪先不派工，等下一輪；下一輪又有新的就再等。這一輪查詢失敗的任務也先不派工。所以從留言到 agent 開始處理，最少隔一個輪詢週期。
- **和 CI 失敗同時發生時先處理留言**（自動修復條件第 8 條）。
- 送給 agent 的內容列出每一則留言的作者、檔案與行號、內文，並要求它不要自己 commit、push 或回覆。總長度上限 20,000 個字元，平均分給各則留言，超過就截斷。
- 這個工具不回覆留言，也不把留言標成已解決。agent 的回答（例如不同意某則留言的理由）在它最後的訊息裡，從任務詳情頁看。

**還沒處理的 review 意見**（自動修復條件第 8 條與 Needs you 都用這個定義）：有還沒餵回的留言；或 `review_state` 是 `changes_requested` 而這個任務從來沒有處理過留言。

後半的用意：GitHub 在 reviewer 重新審查之前會一直回報「要求修改」。留言處理過之後如果還把它當成沒處理，卡片會卡在 Needs you，新 commit 的 CI 失敗也不會再自動修。所以處理過留言之後，`changes_requested` 不再擋住流程，卡片照 CI 與核准的狀態走。這一段沒有實測過：一個人使用時產生不了 `changes_requested`。

**防迴圈的考量**：留言的人、PR 的作者、agent 能用的 `gh` 身分是同一個帳號，所以分不出一則留言是人留的還是 agent 自己留的。因此：

- 留言不算「人已經介入」，不會讓 `ci_fix_attempts` 或 `feedback_attempts` 歸零；只有從看板或 CLI 送訊息才會。
- `feedback_attempts` 的上限 5 次是不靠預算的硬上限。到了上限還有新留言時，卡片進 Needs you（標籤 `Changes requested`），等人送訊息。

## Merge conflict

PR 和基底分支有衝突時，自動請同一個 agent session 解掉。流程和 CI 自動修復相同，差別在準備階段做的事。

**GitHub 怎麼回報**（任務 8 實測）：`gh pr view --json mergeable` 的值是 `MERGEABLE`、`CONFLICTING` 或 `UNKNOWN`。

- 基底分支有新的 commit 之後，GitHub 要重新算：實測先繼續回報舊的 `MERGEABLE` 約 3 秒，接著 `UNKNOWN` 約 3 秒，然後才是 `CONFLICTING`。剛 push 完也一樣會經過 `UNKNOWN`。
- 有衝突時，舊 commit 上的 CI 結果仍然是通過。所以不看 `mergeable` 的話，卡片會停在 Ready to merge，按下合併才發現不行。
- 有衝突的 PR 上，新 push 的 commit 不會跑 `pull_request` 觸發的 CI（GitHub 產生不了合併後的結果）。

`merge_state` 的對應：`MERGEABLE` 是 `clean`，`CONFLICTING` 是 `conflicting`，其餘（含沒看過的新值）是 `unknown`。同一個 commit 上已經知道答案時，後來查到的 `unknown` 不會把它蓋掉，否則這個值會來回跳動、每跳一次就寫一筆事件。`unknown` 既不當成有衝突，也不當成沒有衝突：不會派工，卡片也不會進 Ready to merge。

**解衝突條件**是一個純函式，`status.py` 與 Dispatcher 共用，全部成立才算符合：

1. `merge_state` 是 `conflicting`
2. `head_sha` 不等於 `last_handled_conflict_head_sha`
3. `ci_fix_attempts` 小於 3（和 CI 自動修復共用同一個次數）
4. 任務的剩餘預算大於零
5. 沒有 run 正在執行
6. PR 開著，任務未封存
7. 最後一次 run 的 `exit_status` 是 `success`
8. 沒有還沒處理的 review 意見

Dispatcher 發現任務符合條件時，在同一個交易內重新檢查條件、建立 trigger 為 `merge_conflict` 的 run、把 `last_handled_conflict_head_sha` 設為目前的 `head_sha`、`ci_fix_attempts` 加一。「已處理」的欄位和 CI 失敗用的是分開的：同一個 commit 可能既有衝突又有 CI 失敗，兩者不能互相蓋掉。

**用 merge，不用 rebase**，而且合併由 supervisor 開始、由 supervisor commit：

1. 準備階段：`git fetch` 之後在 worktree 執行 `git merge --no-commit --no-ff origin/<基底分支>`。有衝突時 git 以非零結束，工作目錄停在合併到一半的狀態，衝突的檔案裡留著標記。
2. 把衝突的檔案清單寫成這個 run 要送給 agent 的內容，要求它編輯檔案解掉衝突、移除所有標記，不要自己執行 `git merge`、`git commit`、`git push`。
3. Agent 只需要改檔案。
4. 收尾時由 supervisor commit（這個 commit 就是合併的 commit，訊息是 `chore(agent): merge <基底分支> and resolve conflicts (run <run 編號>)`）並 push。因為是 merge，push 是一般的快轉，不需要 force push。

這樣做是因為 Codex 在 worktree 裡不能執行 git 的寫入指令（`codex-notes.md`），但改檔案沒有問題；rebase 還需要 force push，會改寫已經 push 出去的 commit。

- 以本機的 git 為準，不以 GitHub 的回報為準。本機合併沒有衝突（或本來就不需要合併）時，這個 run 以 `error` 結束、還原到合併之前，卡片進 Needs you。
- worktree 有未 commit 的變更，或已經停在合併到一半的狀態時，準備階段拒絕開始合併，run 以 `error` 結束。
- Agent 沒有產生新的 commit、衝突還在時，同一個 commit 不會再處理第二次，卡片進 Needs you（標籤 `Merge conflict`）。

**不能把沒解完的衝突發布出去**

- 收尾時只要 worktree 停在合併到一半的狀態，不論這個 run 是什麼原因開始的，都先檢查 git 認定有衝突的檔案裡還有沒有衝突標記（以 `<<<<<<< ` 或 `>>>>>>> ` 開頭的行）。還有的話 run 以 `error` 結束，不 commit、不 push。
- 不論原因都要檢查，是因為解衝突的 run 可能被 Stop 或逾時，留下合併到一半的工作目錄；下一個 run（例如使用者送訊息讓它繼續）收尾時不能把標記推出去。
- 只檢查 git 認定有衝突的檔案，內容剛好長得像標記的其他檔案不會被誤判。Agent 自己執行了 `git add` 的檔案不在檢查範圍內。
- 合併到一半時，即使工作目錄看起來沒有任何變更也要 commit。實測 agent 把衝突全部解成「保留自己這邊」時，內容和合併前一模一樣，git 看不到變更，但沒有這個 commit，合併就不算完成、衝突也還在。
- 封存時如果還停在合併到一半的狀態，先放棄這次合併再清掉 worktree。任務自己的 commit 都還在，只丟掉解到一半的合併。

## 通知

卡片進 Needs you 時，daemon 發一則系統通知。目前只支援 macOS（用系統內建的 `osascript`），其他平台不發。

- **只在「進入」的那一刻通知一次**：卡片從別的欄位（或剛建立）進到 Needs you 時才發。停在裡面期間資料再怎麼變都不重複通知；離開之後再回來會再通知一次。
- **daemon 啟動時原本就在 Needs you 的卡片不通知**：啟動時先記下每張卡片在哪一欄，之後才開始比對。
- **內容**：標題是 `Needs you · <狀態標籤>`，內文是任務的標題。兩者用參數傳給 `osascript`，不拼進腳本，所以標題裡有引號也沒有問題。
- **判斷的位置**和推送卡片給看板的是同一處（資料庫每次 commit 之後比對指紋、重新推導卡片），所以看板上看得到的移動都會被通知到，不用在每個會改狀態的地方各自處理。
- **發不出去不影響其他功能**：通知在背景發，不讓推送等它；失敗或逾時（10 秒）只記一行 log。
- **可以關掉**：設定檔的 `notify_needs_you = false`。預設是開的。
- 只通知 Needs you 這一欄。沒有通知的歷史紀錄，點通知也不會開啟看板（`osascript` 發的通知做不到）。
- 第一次發通知時 macOS 可能會問要不要允許；在「系統設定 → 通知」裡關掉的話，指令照樣成功但不會顯示，daemon 不會知道。

## 權限政策（階段 1 版本）

Agent 是在沒有人看著的情況下執行，所以階段 1 就要有最基本的限制。

**信任前提**：階段 1 到 4 只把這個工具用在自己信任的 repo 上。這裡的政策是基本防護，**不是安全邊界**。Agent 只要能執行測試或 shell，就有辦法透過它們間接做到清單外的事（例如讓測試程式去 push）。worktree 只隔離工作目錄，各 worktree 仍共用同一份 git 資料。真正的隔離要等階段 5 的 container sandbox。

- `PermissionPolicy` 是通用的資料結構，包含三部分：
  - sandbox 等級：唯讀，或只能寫工作目錄
  - 是否允許連網
  - 工具與指令的限制：內容由各 adapter 自行解讀，因為每種 agent 的機制不同（Claude Code 是工具清單與拒絕的指令前綴；Codex 的非互動模式無法逐次攔截，這一部分對它不生效）
- 各 adapter 把政策轉成該 agent 自己的設定，並盡力落實。不同 agent 能做到的程度不同：有的可以逐次攔截工具呼叫，有的只有 sandbox 等級。做不到的部分由 `capabilities` 表明。
- Adapter 攔得到的清單外動作一律拒絕，並寫入 `permission_denied` 事件。階段 1 不做「跳出來問人」。
- Agent 不需要 push、開 PR 或呼叫 `gh`，這些由 supervisor 做，允許清單不含這些動作。
- Runner 內不出現任何 agent 專屬的字串。

互動式核准、路徑限制、依專案設定的政策留到階段 5。

## Daemon 重啟後的復原

同一時間只允許一個 daemon 在執行。第二個 daemon 啟動時發現已經有一個在跑，就直接結束，避免把別人正在跑的 run 當成遺留的。

Daemon 的啟動順序：先裝好訊號處理，拿到單一實例的鎖，做完下面的復原，才開始接受連線。關閉時（Ctrl-C 或 SIGTERM）先停止接受連線、結束開著的 SSE 連線，再把還在執行的 run 中止並標成 `interrupted`。收到 `asv stop` 送來的 SIGUSR1 時中止所有 run，daemon 繼續執行。

階段 1 的前景指令（`start`、`message`、`watch`、`archive`）保留，和 daemon 共用同一把鎖，所以 daemon 在跑的時候它們會被拒絕；`list`、`show`、`project add` 只讀寫資料庫，隨時可用。

Daemon 啟動時，在開始接受任何新工作之前，先找出所有 `ended_at` 為空的 run：

- 先確認上一次啟動時留下的程序（agent 與它的子程序、收尾時的 `git` / `gh`）都已經停掉，還在跑的就終止它們

- 把 `exit_status` 設為 `interrupted`，`ended_at` 設為現在
- 寫入一筆 `state_change` 事件
- 卡片依推導規則進 Needs you（標籤 `Interrupted`），由人決定要不要送訊息讓它繼續

不自動重跑，避免重啟後一口氣重跑一堆任務。

先停掉遺留程序的原因：daemon 是突然死掉的話，上一次啟動的 agent 可能還在背景繼續改檔，這時讓新的 run 進同一個 worktree 會互相覆蓋。

無法確認某個 run 的遺留程序已經停止時，那個 run 維持未結束，該任務因此建不了新的 run；daemon 在啟動訊息中列出這些任務，由人手動處理。

辨識遺留程序的方法：adapter 在 `session_started` 事件裡提供 `process_marker`，復原時找出命令列包含這個字串的程序，先送 SIGINT，等不到再強制終止。未結束的 run 如果連一筆 `session_started` 都沒有，表示 agent 還沒啟動，直接標成 `interrupted`。

實測（Claude Code）：daemon 被強制終止後，agent 的程序與它啟動的子程序都還在執行；下一次啟動時兩者都被停掉，run 標成 `interrupted`。

## 中止與封存

**中止一個 run**（使用者按 Stop、撞到執行時間上限、PR 被合併或關閉）

1. 取消負責這個 run 的工作。不論它正在準備、執行 agent 還是收尾，都不再開始下一個外部動作。
2. 正在執行 agent 時，adapter 把 agent 以及它啟動的所有子程序（測試、shell）一起停掉；等一小段時間還沒結束就強制終止。正在執行 `git` 或 `gh` 時同樣終止。
3. 確認都結束之後，才寫入 `ended_at` 與 `exit_status`。在那之前任務仍算有 run 在執行，不能開新的 run。

Stop 只阻止後續動作，不回滾已經完成的事：已經 push 出去的 commit 不會被撤回。

Stop 只能用在已經存在的 run 上。CI 失敗後、Dispatcher 還沒建立 run 的那一小段時間（最長一個輪詢週期），卡片雖然顯示 Working，但沒有 run 可以停，Stop 不可用。

**封存一個任務**

- 封存要先拿到任務的鎖。有 run 正在執行時拒絕封存，要先 Stop。
- worktree 裡有未 commit 的變更時，supervisor 先把它們 commit 到任務的 branch，再清掉 worktree。這個 commit 只留在本機，不 push。
- 封存只清掉 worktree，branch 保留，所以成果不會因為封存而消失。
- 補 commit 失敗時（例如 commit hook 擋下）拒絕封存並回報原因，不刪除 worktree。
- 封存不會關掉 PR，也沒有取消封存。PR 還開著的任務封存之後，Poller 仍然繼續查它，所以之後在 GitHub 上合併或關閉，卡片的標籤會跟著變；但不再自動修復 CI，也不能再送訊息。

## API

| 端點 | 用途 |
| --- | --- |
| `POST /api/projects` | 加入本機 repo；路徑要是 repo 工作目錄的最上層，已經加入過就回報錯誤 |
| `GET /api/projects` | 專案列表 |
| `POST /api/tasks` | 開任務：建立任務與 `initial` run，接著建 worktree、啟動 agent |
| `GET /api/tasks?project_id=` | 看板資料，含推導出的欄位 |
| `GET /api/tasks/{id}` | 任務詳情，含這個任務所有的 run |
| `POST /api/tasks/{id}/messages` | 對同一個 session 追加指令；已有 run 在執行時回報錯誤 |
| `POST /api/tasks/{id}/stop` | 中止正在跑的 run；沒有 run 在跑時回報錯誤 |
| `POST /api/tasks/{id}/archive` | 封存並清掉 worktree；條件見「中止與封存」 |
| `GET /api/tasks/{id}/events` | 稽核紀錄，分頁 |
| `GET /api/usage?project_id=` | token 與花費彙總 |
| `GET /api/agents` | 這個 daemon 能用的 agent 種類，以及各自是否回報花費；給開任務的表單用 |
| `GET /api/stream` | SSE：狀態變更與 agent log |

API 由常駐的 daemon 提供（`asv serve`），只綁 `127.0.0.1`，連接埠由設定檔的 `api_port` 決定（預設 8765）。所有端點都在 `/api` 底下；`/` 留給前端的檔案（見「前端」）。下文提到端點時省略 `/api`。沒有登入，也沒有 CORS 設定。每個請求的 `Host` 必須是本機的名稱（`127.0.0.1`、`localhost`、`::1`），有 `Origin` 的話必須是本機上的網頁（連接埠不限），否則拒絕；這是為了擋掉使用者瀏覽器裡其他網頁發出的請求。

**回應的內容**

- 看板的一張卡片（`GET /tasks` 的每一筆、`GET /tasks/{id}` 的基本欄位）帶著推導出的 `column` 與 `label`，以及畫卡片需要的欄位：agent 種類、標題、branch、PR 編號與狀態、CI 與 review 狀態、修復次數、更新時間、用量。
- `cost_usd` 為 `null` 表示這種 agent 不回報花費（由 adapter 的 `capabilities.reports_cost` 判斷），花費是未知而不是零；這時改看 `tokens`。`usage_is_complete` 為否表示有 run 的用量拿不到（例如被中止），數字偏低。
- `POST /tasks` 在任務與 `initial` run 建立後就回應，worktree 與 agent 在背景進行。worktree 建立失敗是那個 run 以 `error` 結束，不是 HTTP 錯誤。
- `POST /tasks/{id}/stop` 只中止指定任務的 run，等它完全停下才回應。
- `GET /tasks/{id}/events` 用 `after`（事件編號）與 `limit` 分頁，可用 `type` 篩選；回應的 `next_after` 不是 `null` 時還有下一頁。

**錯誤回應**

一律是 `{"error": {"code": "...", "message": "..."}}`。`code` 給程式判斷，`message` 可以直接顯示給使用者。

| 情況 | HTTP 狀態 | `code` |
| --- | --- | --- |
| `Host` 不是本機，或 `Origin` 不是本機上的網頁 | 403 | `forbidden` |
| 任務或專案不存在，或沒有這個路徑 | 404 | `not_found` |
| 這個路徑不接受這種方法 | 405 | `method_not_allowed` |
| 專案已經加入過 | 409 | `project_exists` |
| 任務已有 run 在執行（送訊息、封存） | 409 | `run_already_active` |
| 任務已封存，或 PR 已合併或關閉 | 409 | `task_not_runnable` |
| 花費或 token 已達上限 | 409 | `budget_exhausted` |
| 沒有 run 可以中止 | 409 | `no_active_run` |
| 欄位不合法、agent 種類不存在、路徑不是 git repo 工作目錄的最上層、body 不是以 JSON 的型別送出 | 422 | `invalid_request` |
| 沒預期到的錯誤（例如封存時 git 失敗） | 500 | `internal_error` |

**SSE**

`GET /stream` 送兩種訊息：

| 訊息 | 內容 | 斷線重連時 |
| --- | --- | --- |
| `event` | `events` 表的一列。SSE 的 `id` 就是事件編號 | 重連的請求帶 `Last-Event-ID` 時，daemon 從那個編號之後補送。內建的前端不靠這個，改用事件分頁補抓（見「前端」） |
| `task` | 某個任務的卡片最新內容，和 `GET /tasks` 的一筆相同 | 不補送；重連的一方自己重新抓一次 `GET /tasks` |

- 分成兩種的原因：不是每個會讓卡片移動的寫入都有事件列（例如建立 run），所以看板不能只靠 `events` 推導。
- 卡片的變化這樣偵測：資料庫每次 commit 之後，用一次查詢算出每個任務的指紋，有變的才重新推導卡片並推送。所有寫入都經過同一種交易，所以不會漏。
- 只推送已經 commit 的資料。
- 每條連線有一個有上限的佇列；對方讀得太慢、佇列滿了就結束這條連線，讓它重連後用編號補上，不拖住其他人。
- 沒有訊息時每 15 秒送一行註解，避免連線被中途的設備關掉。
- 前端要**先連上 `/stream`，再抓 `GET /tasks` 與事件分頁**，事件用編號去掉重複的。順序反過來的話，抓完資料到連上之間發生的變化會漏掉。

## 前端

React + Vite + TypeScript，放在 `web/`。畫面的範圍見 `05-ui-reference.md`。

- **由誰提供**：`npm run build` 的產物在 `web/dist`，daemon 啟動時如果有這個目錄就把它掛在 `/`，開 `http://127.0.0.1:8765` 就是看板。沒有 build 過時 daemon 只提供 API。開發時跑 `npm run dev`（連接埠 5173），Vite 把 `/api` 轉給 daemon。
- **兩個畫面**：看板（`#/`）與任務詳情頁（`#/tasks/{id}`），用網址的 hash 切換。
- **不自己推導狀態**：卡片放哪一欄、顯示什麼標籤，一律用 API 回的 `column` 與 `label`；前端只決定標籤的顏色。Stop 能不能按只看 `has_open_run`。
- **花費**：`cost_usd` 是 `null` 時顯示 token 數；有數字時前面加 `~` 表示估算值；`usage_is_complete` 為否時後面加 `+`，表示實際的數字比這個高。
- **連線**：先連上 `/stream` 再抓資料。連線出錯時前端自己關掉，2 秒後重開（瀏覽器的 `EventSource` 在 daemon 沒開時不會自己重試）。每次連上都重新抓專案、agent、看板；開著的詳情頁從已經有的最後一筆事件之後補抓，用編號去掉重複的。
- **Log 分頁與 Events 分頁用同一份事件**：Log 把 agent 的訊息、指令與輸出、工具呼叫排成容易讀的樣子，Events 原樣列出並可依類型篩選。
- **封存前的確認**：任務的 PR 還開著時，按 Archive 先跳出確認框說明後果，確認才送出。只做在介面上，API 與 CLI 不擋。
- **錯誤**：API 回的 `message` 直接顯示在操作的旁邊。

## 背景工作與 Agent adapter

三個 asyncio 背景工作和 API 跑在同一個 process。Daemon 另外有一個工作負責把資料庫的變化推送給 SSE 連線（見「API」的 SSE 一段）。

| 工作 | 負責 |
| --- | --- |
| Runner | 啟動或續接 agent session，把事件寫進 `events`，結束時回填用量並做收尾 |
| Poller | 每 30 秒查詢 PR 開著的任務（含已封存的），把 `pr_state`、`head_sha`、`ci_state`、`review_state`、`merge_state` 當成一組一起更新；未封存的任務另外把 PR 上新的留言記成事件 |
| Dispatcher | 依序檢查三種條件，符合的就建立對應的 run 交給 Runner：留言回饋（`review_feedback`）、解衝突（`merge_conflict`）、自動修復（`ci_failure`） |

Poller 用一次 `gh pr view` 查回五個欄位。Review 狀態照 GitHub 回報的寫入：收尾時 push 之後會先設成 `none`，但 GitHub 預設不會因為新的 commit 撤銷核准，所以下一次輪詢可能又變回 `approved`。單一任務查詢失敗不影響其他任務，下一輪再查。

**所有模組共用一條資料庫連線，寫入由一把鎖排隊。** Dispatcher 的交易包含好幾步，沒有鎖的話 Runner 同時間的寫入會把它切成兩半。

**Poller 不能用舊資料蓋掉新資料。** 查詢 GitHub 需要時間，期間 supervisor 可能剛 push 了新的 commit。Poller 查詢前記下任務當時的 `head_sha`，要寫入時如果資料庫裡的 `head_sha` 已經不一樣，就丟掉這次結果，等下一輪再查。反過來，GitHub 的資料也可能落後剛 push 的 commit：回報的最新 commit 如果是這個任務更早 push 過的，同樣丟掉。這一項只在 GitHub 回報 PR 還開著時適用：合併或關閉的 PR 最新 commit 不會再變，停在舊的 commit 是正常的，照常寫入。

Runner 只透過 adapter 介面控制 agent。介面以「一輪對話」為單位：送一段輸入，拿回一串事件，最後一筆是這一輪的結果。

```python
@dataclass(frozen=True)
class AdapterCapabilities:
    can_resume_session: bool
    can_limit_turns: bool
    can_limit_budget: bool
    can_gate_tool_calls: bool
    can_restrict_network: bool
    reports_cost: bool


@dataclass(frozen=True)
class RunLimits:
    max_turns: int | None
    max_budget_usd: float | None


@dataclass(frozen=True)
class TurnResult:
    session_id: str | None
    exit_status: ExitStatus
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    usage_raw: dict[str, Any] | None
    detail: str | None = None


class AgentAdapter(Protocol):
    capabilities: AdapterCapabilities

    def run_turn(
        self,
        *,
        prompt: str,
        cwd: Path,
        session_id: str | None,
        model: str | None,
        limits: RunLimits,
        policy: PermissionPolicy,
        previous_usage: dict[str, Any] | None,
    ) -> AsyncIterator[AgentEvent | TurnResult]: ...
```

- `session_id` 為 `None` 表示開新 session；有值表示續接。
- Adapter 一拿到 session id 就要送出一筆 `session_started` 事件，Runner 收到後立刻寫進 `tasks.session_id`。這樣 run 被中止、中斷或出錯時仍然可以續接。`TurnResult` 也帶 session id，作為最後確認。
- `model` 為 `None` 表示用該 agent 的預設模型。
- `previous_usage` 是同一個任務上一輪的 `usage_raw`。有些 agent 回報的是整個 session 的累計值（Claude Code 的花費、Codex 的 token 數），adapter 用它相減換算成單輪。Runner 不解讀它的內容。
- `tokens_in` 要包含快取的輸入 token（快取寫入與快取讀取）。實測兩種 agent 的輸入都有九成左右是快取，不計入的話數字沒有意義。
- `TurnResult.detail` 是給人看的補充說明，例如撞到的是哪一種上限。
- `usage_raw` 是 agent 回報的原始用量資料（含快取、推理等各種 token 數），原樣寫進事件。`tokens_in` 與 `tokens_out` 只是其中兩個數字，之後要自己估算花費時需要完整的資料。
- `TurnResult` 的 `session_id` 與 token 欄位可以是 `None`：agent 還沒啟動成功就出錯時沒有 session，被中止時可能拿不到完整用量。未知的用量不要填 0。
- 中止一個 run：Runner 取消正在讀取串流的工作，adapter 必須在串流被關閉時把底下的 agent 與它的子程序都停掉，流程見「中止與封存」。
- 串流結束卻沒有送出 `TurnResult` 時，Runner 把這個 run 記為 `error`。被中止的 run 不等 `TurnResult`，由 Runner 自己結案。
- `AgentEvent` 是有型別的事件，對應 `events.type`，不直接把 agent 的原始輸出丟給 Runner。
- `capabilities` 讓 Runner 知道這個 adapter 做不到什麼。`can_limit_turns` 或 `can_limit_budget` 為 `False` 時，對應的上限無法在執行中生效，只剩 Runner 的執行時間上限擋著。

**各 adapter 的做法**

| Adapter | 做法 | 何時做 |
| --- | --- | --- |
| Claude Code | 官方 Python Agent SDK（`claude-agent-sdk`） | 階段 1 |
| Codex | 用 subprocess 包 `codex exec` 的非互動模式，逐行讀 JSONL 事件；調查與實測結果見 `codex-notes.md` | 階段 1.5 |
| 自製 API agent | 直接呼叫模型 API，自己寫 agent 迴圈 | 階段 6 |

Claude Code 選 SDK 而不是自己包 CLI 的理由見 `03-decisions.md` 第 16 筆；Codex 選包 CLI 而不是官方 SDK 的理由見第 48 筆。

Claude Code adapter 的做法與已知限制（實測，細節見 `claude-sdk-notes.md`）：

- 新 session 的 id 由 adapter 事先指定。啟動前先送一筆只有 `process_marker` 的 `session_started`，等 agent 確認 session 建立後才送出帶 id 的那一筆。這樣啟動失敗的 run 不會留下一個不存在的 session 讓之後的 run 去續接。
- 只載入專案層級的 Claude Code 設定，所以 agent 讀得到目標 repo 的 `CLAUDE.md`，但不受使用者個人的 hooks 與外掛影響。
- 寫檔與編輯工具的目標路徑必須在任務的 worktree 之內。只比對路徑字串、不解析符號連結，也擋不住透過指令寫到外面。
- 工具與指令的限制由「工具呼叫前的 hook」落實，它對每一次工具呼叫都會執行。只靠核准回呼不夠：實測發現目標 repo 的專案設定裡若有允許規則，或指令被判定為唯讀（例如 `ls`），呼叫就不會經過核准回呼。核准回呼仍然保留，負責放行需要詢問的呼叫。
- 目標 repo 專案設定裡的 hooks 也會在 agent 上執行，這是載入專案設定的代價。
- 拒絕背景執行的指令。一個 run 就是一輪做完；背景指令在這一輪結束時會被終止，agent 卻會回報已經完成。
- 沒有內容的訊息與思考區塊不寫入事件。**sandbox 等級與是否允許連網目前無法落實**：SDK 的 sandbox 設定實測沒有生效，原因未查明，所以 `can_restrict_network` 為否。唯讀等級以拒絕所有會寫入的工具來近似。
- 被中止或逾時的 run 沒有用量。它的花費會算進同一個 session 的下一個 run，因為花費是用累計值相減得到的。

Codex adapter 的做法與已知限制（實測，細節見 `codex-notes.md`）：

- 每一輪啟動一個 `codex exec` 程序，續接用 `codex exec resume <id>`，一律在任務的 worktree 執行。指示從 stdin 送入。
- sandbox 等級與是否允許連網每次都明確傳入，不依賴使用者自己的 Codex 設定。實測這兩項對 Codex 執行的指令有效，所以 `can_restrict_network` 為是。但使用者自己的 Codex 設定仍會被載入：實測 agent 用了使用者設定的 MCP 工具讀檔，這類工具受不受 sandbox 與連網限制**沒有驗證**。
- session id 要等 Codex 啟動後才知道，所以 `process_marker` 另外用一個啟動前就確定的字串：放在「最後一則訊息」輸出檔的檔名裡，會出現在命令列上。這個檔案放在 worktree 之外，用完就刪。
- 用量是整個 session 的累計值，adapter 用 `previous_usage` 相減成單輪。不回報花費，`cost_usd` 一律為 `NULL`。被中止的 run 拿不到用量。
- 沒有輪數與花費上限，`can_limit_turns` 與 `can_limit_budget` 為否；只靠任務的 token 上限、修復次數與執行時間上限擋住。
- 中止時送 SIGINT，Codex 會連同它啟動的子程序一起結束；等不到就強制終止它與所有子孫程序。
- **無法逐次攔截指令**，`can_gate_tool_calls` 為否，政策裡「拒絕的指令」對 Codex 不生效，也不會有 `permission_denied` 事件。允許連網時 Codex 可以自己執行 `git push`（實測 `--dry-run` 成功）；它在 worktree 裡無法 commit，所以能推出去的只有 supervisor 已經 commit 的內容。這是已知限制，見 `03-decisions.md` 第 89 筆。
- 有些被 sandbox 擋下的指令（例如 `git add`）不會出現在事件流裡，只在 agent 的文字訊息中提到，所以 Codex 任務的稽核紀錄不完整。
- 指令執行寫成 `command` 事件；檔案變更、MCP 工具等其他項目寫成 `tool_call` 事件，內容原樣保留。種類是 `error` 的項目也寫成 `tool_call`，標示為失敗。

## 開發順序

1. Adapter + Runner：用 Claude Code adapter 跑起一個任務，事件寫進 `events`，含權限政策與重啟復原
2. Worktree 管理 + run 收尾（commit、push、開 PR）
3. Poller + Dispatcher：CI 失敗自動餵回，含防迴圈規則
4. Codex adapter：用第二個實作驗證 adapter 介面
5. FastAPI 端點 + SSE
6. React 看板與任務詳情頁

第 1 到 3 步是階段 1，第 4 步是階段 1.5，第 5、6 步是階段 2，見 `01-roadmap.md`。
