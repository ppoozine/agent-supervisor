# 02 · MVP 規格

最後更新：2026-11-15

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
| `last_handled_head_sha` | 已經餵回過 CI 失敗的 commit，避免重複餵回 |
| `ci_fix_attempts` | 自動修復次數 |
| `archived_at`, `created_at`, `updated_at` | 時間戳 |

`pr_state`、`head_sha`、`ci_state`、`review_state` 是 Poller 對 GitHub 狀態的快取，不是看板欄位。

`ci_state` 的意思：

| 值 | 意思 |
| --- | --- |
| `none` | 這個 commit 上沒有任何 CI 檢查 |
| `pending` | 至少一個檢查還沒結束 |
| `passed` | 所有檢查都結束且全部通過 |
| `failed` | 所有檢查都結束且至少一個失敗 |

有檢查失敗但其他檢查還在跑時仍是 `pending`，等全部結束才一次處理。

GitHub 上其他的檢查結果（取消、逾時、略過、需要人工核准等）要對應到哪一個值，以及同一個檢查重跑時取哪一次，在任務 3 查過 `gh` 實際回傳的資料後補進這裡。

### runs

| 欄位 | 說明 |
| --- | --- |
| `id`, `task_id` | 主鍵與所屬任務 |
| `trigger` | `initial` / `ci_failure` / `user_message` |
| `input_text` | 這次送給 agent 的內容 |
| `source_head_sha` | `ci_failure` run 針對的 commit；其他 trigger 為 `NULL` |
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
| `type` | `session_started` / `agent_message` / `tool_call` / `command` / `permission_denied` / `pushed` / `pr_opened` / `ci_status` / `state_change` |
| `payload` | JSON，依 `type` 而異 |

Agent 的推理內容與計畫更新也記成 `agent_message`，在 `payload` 裡用 `kind` 標明是一般訊息、推理還是計畫。

## Run 的生命週期

一個 run 從建立到結案會經過四段：**建立 → 準備 → agent 執行 → 收尾**。流程圖見 `06-diagrams.md`。

| 階段 | 做什麼 |
| --- | --- |
| 建立 | 在資料庫交易內寫入 run。開任務時，任務與它的 `initial` run 在同一個交易內建立，所以不會有「沒有任何 run 的任務」 |
| 準備 | `initial`：建立 worktree 與 branch。`ci_failure`：抓失敗的 CI log。`user_message`：無 |
| agent 執行 | 透過 adapter 跑一輪，事件寫進 `events` |
| 收尾 | commit、push、開 PR，見下一節 |

**一個 run 由同一個可取消的工作從頭負責到尾。**

- 四段都算「run 正在執行」，全部做完才寫入 `ended_at`。
- 執行時間上限從 run 建立時起算，涵蓋準備與收尾，不只是 agent 執行的時間。`git`、`gh` 等外部指令各自有逾時。
- 不論在哪一段出錯、逾時或被取消，都由這個工作把 run 結案，不會留下沒人管的 run。準備階段失敗（例如 worktree 建不起來、抓不到 log）時 run 記為 `error`。
- 一個 run 只會有一個結案結果。已經被 Stop 的 run 不會被後來完成的收尾改寫成 `success`。

**每個任務一把鎖。** 建立 run、Stop、封存這三種操作都要先拿到該任務的鎖才能進行，所以封存進行到一半時不會有新的 run 被建立。鎖只存在於 daemon 的記憶體內，搭配資料庫「每個任務最多一個未結束的 run」的約束。

## Run 結束後的收尾

Agent 正常結束這一輪之後，Runner 依序做下面的事。`initial`、`ci_failure`、`user_message` 三種 trigger 都一樣。

收尾是 run 的一部分：全部做完才寫入 `ended_at`，所以收尾期間卡片仍在 Working。

1. worktree 有未 commit 的變更 → 由 supervisor 補一個 commit。
2. branch 相對於遠端有新的 commit → push，寫入 `pushed` 事件，並立刻把 `head_sha` 更新為剛 push 的 commit、`ci_state` 設為 `pending`、`review_state` 設為 `none`。這樣不用等下一次輪詢，卡片就不會被舊 commit 的 CI 結果或舊的核准誤判。
3. 還沒有 PR 且 branch 相對於基底分支有 commit → 開 PR，回填 `pr_number` 並把 `pr_state` 設為 `open`，寫入 `pr_opened` 事件。
4. branch 相對於基底分支沒有任何 commit → 不開 PR，卡片進 Needs you（標籤 `No changes`）。

補 commit、push 或開 PR 任何一步失敗時，run 的 `exit_status` 記為 `error`，寫入一筆事件說明原因，卡片進 Needs you。不自動重試。

收尾的每一步都要先查現況再動手，重做一次不會出錯：已經 push 過就不再 push，GitHub 上這個 branch 已經有 PR 就回填它的編號與實際狀態、不再開一個。收尾做到一半 daemon 當掉時，run 會被標成 `interrupted`；之後使用者送訊息讓它繼續，那一次 run 結束時的收尾會把沒做完的補上。

`ci_failure` run 結束後若沒有產生新的 commit，`head_sha` 不會變，這個任務不再符合自動修復條件，卡片進 Needs you。

## 看板狀態

看板欄位不存進資料庫，每次由 `status.py` 的純函式從 run、PR、CI、review 狀態算出來。

**先判斷是否離開看板**：任務已封存，或 `pr_state` 為 `merged` / `closed` 且沒有 run 正在執行，就不出現在四個欄位裡，改列在 Archive 區（標籤 `Archived` / `Merged` / `Closed`）。

PR 被合併或關閉時如果還有 run 在執行，Poller 會中止那個 run（`exit_status` 為 `stopped`，不做收尾），停下之後卡片才離開看板。

其餘任務由上往下判斷，先符合者優先：

| 欄位 | 條件 |
| --- | --- |
| Working | 有 run 正在執行；或符合「自動修復條件」、正在等 Dispatcher 接手 |
| Needs you | 最後一次 run 的 `exit_status` 不是 `success`；或最後一次 run 是 `success` 但任務沒有 PR（agent 沒有產生任何變更）；或 CI 失敗但不符合自動修復條件（含已達修復上限、累計花費超過上限）；或 reviewer 要求修改 |
| Ready to merge | PR 開著、CI 通過，且（專案不要求 review，或 review 已核准） |
| In review | 其餘 PR 開著的情況：CI 進行中，或 CI 通過但還在等審查 |

## 自動修復條件與防迴圈

**自動修復條件**是一個純函式，`status.py` 與 Dispatcher 共用同一份，全部成立才算符合：

1. `head_sha` 上的所有 CI 檢查都已結束，且至少一個失敗
2. `head_sha` 不等於 `last_handled_head_sha`
3. `ci_fix_attempts` 小於 3
4. 任務的剩餘預算大於零：花費上限減去累計 `cost_usd`，以及 token 上限減去累計 token 數，兩者都要大於零
5. 沒有 run 正在執行
6. PR 開著，任務未封存
7. 最後一次 run 的 `exit_status` 是 `success`
8. `review_state` 不是 `changes_requested`

第 7 條的用意：使用者按了 Stop、run 被中斷、出錯或撞到上限之後，不會再自動開新的 run，要等人送訊息且那一次 run 正常結束，才恢復自動修復。

第 8 條的用意：reviewer 已經要求修改時，這個任務需要人來看，不再自動修 CI，卡片直接進 Needs you。Review 留言的自動處理是階段 3 的事。

Dispatcher 發現任務符合條件時：

1. 在同一個資料庫交易內做完三件事：重新檢查條件、建立 trigger 為 `ci_failure` 的 run（`source_head_sha` 記下目前的 `head_sha`）、把 `last_handled_head_sha` 設為同一個值並將 `ci_fix_attempts` 加一
2. 交易結束後才抓 `source_head_sha` 上所有失敗檢查的 log，只擷取失敗的片段
3. 把 run 交給 Runner 續接同一個 session

抓 log 失敗時，這個 run 以 `error` 結束，卡片進 Needs you；已經扣掉的修復次數不退回。

使用者送訊息（`POST /tasks/{id}/messages`）走同一套建立 run 的程序。任務已經有未結束的 run、已封存，或 PR 已合併或關閉時，新的 run 建不起來，API 回報錯誤。這樣 Dispatcher 與使用者不會同時讓兩個 run 用同一個 session 與 worktree。

**防無限迴圈**

1. 同一個 commit 的 CI 失敗只餵回一次：比對 `last_handled_head_sha`。
2. 自動修復最多 3 次：`ci_fix_attempts` 到 3 就停止。
3. 單一任務的花費上限與 token 上限：累計 `cost_usd` 或累計 token 數任何一個達到上限，就不再啟動自動修復；兩個上限值都是全域設定。累計 token 數是所有 run 的 `tokens_in` 加 `tokens_out`。`TurnResult` 回報的必須是**這一輪**的用量；agent 給的是整個 session 的累計值時（例如 Codex 的 CLI），由 adapter 相減換算。
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

## 權限政策（階段 1 版本）

Agent 是在沒有人看著的情況下執行，所以階段 1 就要有最基本的限制。

**信任前提**：階段 1 到 4 只把這個工具用在自己信任的 repo 上。這裡的政策是基本防護，**不是安全邊界**。Agent 只要能執行測試或 shell，就有辦法透過它們間接做到清單外的事（例如讓測試程式去 push）。worktree 只隔離工作目錄，各 worktree 仍共用同一份 git 資料。真正的隔離要等階段 5 的 container sandbox。

- `PermissionPolicy` 是通用的資料結構，包含三部分：
  - sandbox 等級：唯讀，或只能寫工作目錄
  - 是否允許連網
  - 工具與指令的限制：內容由各 adapter 自行解讀，因為每種 agent 的機制不同（Claude Code 是工具清單，Codex 是指令前綴規則）
- 各 adapter 把政策轉成該 agent 自己的設定，並盡力落實。不同 agent 能做到的程度不同：有的可以逐次攔截工具呼叫，有的只有 sandbox 等級。做不到的部分由 `capabilities` 表明。
- Adapter 攔得到的清單外動作一律拒絕，並寫入 `permission_denied` 事件。階段 1 不做「跳出來問人」。
- Agent 不需要 push、開 PR 或呼叫 `gh`，這些由 supervisor 做，允許清單不含這些動作。
- Runner 內不出現任何 agent 專屬的字串。

互動式核准、路徑限制、依專案設定的政策留到階段 5。

## Daemon 重啟後的復原

同一時間只允許一個 daemon 在執行。第二個 daemon 啟動時發現已經有一個在跑，就直接結束，避免把別人正在跑的 run 當成遺留的。

Daemon 啟動時，在開始接受任何新工作之前，先找出所有 `ended_at` 為空的 run：

- 先確認上一次啟動時留下的程序（agent 與它的子程序、收尾時的 `git` / `gh`）都已經停掉，還在跑的就終止它們

- 把 `exit_status` 設為 `interrupted`，`ended_at` 設為現在
- 寫入一筆 `state_change` 事件
- 卡片依推導規則進 Needs you（標籤 `Interrupted`），由人決定要不要送訊息讓它繼續

不自動重跑，避免重啟後一口氣重跑一堆任務。

先停掉遺留程序的原因：daemon 是突然死掉的話，上一次啟動的 agent 可能還在背景繼續改檔，這時讓新的 run 進同一個 worktree 會互相覆蓋。

無法確認某個 run 的遺留程序已經停止時，那個 run 維持未結束，該任務因此建不了新的 run；daemon 在啟動訊息中列出這些任務，由人手動處理。

怎麼辨識並停掉遺留程序（程序群組、adapter 自己記錄的識別資訊等）在任務 1 調查後決定並補進這裡，但上面的行為是必須做到的。

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

## API

| 端點 | 用途 |
| --- | --- |
| `POST /projects` | 加入本機 repo |
| `GET /projects` | 專案列表 |
| `POST /tasks` | 開任務：建立任務與 `initial` run，接著建 worktree、啟動 agent |
| `GET /tasks?project_id=` | 看板資料，含推導出的欄位 |
| `GET /tasks/{id}` | 任務詳情，含這個任務所有的 run |
| `POST /tasks/{id}/messages` | 對同一個 session 追加指令；已有 run 在執行時回報錯誤 |
| `POST /tasks/{id}/stop` | 中止正在跑的 run；沒有 run 在跑時回報錯誤 |
| `POST /tasks/{id}/archive` | 封存並清掉 worktree；條件見「中止與封存」 |
| `GET /tasks/{id}/events` | 稽核紀錄，分頁 |
| `GET /usage?project_id=` | token 與花費彙總 |
| `GET /stream` | SSE：狀態變更與 agent log |

## 背景工作與 Agent adapter

三個 asyncio 背景工作跑在同一個 process。

| 工作 | 負責 |
| --- | --- |
| Runner | 啟動或續接 agent session，把事件寫進 `events`，結束時回填用量並做收尾 |
| Poller | 每 30 秒查詢有開 PR 的任務，把 `pr_state`、`head_sha`、`ci_state`、`review_state` 當成一組一起更新 |
| Dispatcher | 發現符合自動修復條件的任務就建立 `ci_failure` run，抓 log，交給 Runner |

**Poller 不能用舊資料蓋掉新資料。** 查詢 GitHub 需要時間，期間 supervisor 可能剛 push 了新的 commit。Poller 查詢前記下任務當時的 `head_sha`，要寫入時如果資料庫裡的 `head_sha` 已經不一樣，就丟掉這次結果，等下一輪再查。

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
    exit_status: str
    tokens_in: int | None
    tokens_out: int | None
    usage_raw: dict[str, Any] | None
    cost_usd: float | None


class AgentAdapter(Protocol):
    capabilities: AdapterCapabilities

    def run_turn(
        self,
        prompt: str,
        cwd: Path,
        session_id: str | None,
        model: str | None,
        limits: RunLimits,
        policy: PermissionPolicy,
    ) -> AsyncIterator[AgentEvent | TurnResult]: ...
```

- `session_id` 為 `None` 表示開新 session；有值表示續接。
- Adapter 一拿到 session id 就要送出一筆 `session_started` 事件，Runner 收到後立刻寫進 `tasks.session_id`。這樣 run 被中止、中斷或出錯時仍然可以續接。`TurnResult` 也帶 session id，作為最後確認。
- `model` 為 `None` 表示用該 agent 的預設模型。
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

Codex adapter 已知的限制（實測）：被中止的 run 拿不到用量；有些被 sandbox 擋下的指令不會出現在事件流裡，所以 Codex 任務的 `permission_denied` 事件不完整。

## 開發順序

1. Adapter + Runner：用 Claude Code adapter 跑起一個任務，事件寫進 `events`，含權限政策與重啟復原
2. Worktree 管理 + run 收尾（commit、push、開 PR）
3. Poller + Dispatcher：CI 失敗自動餵回，含防迴圈規則
4. Codex adapter：用第二個實作驗證 adapter 介面
5. FastAPI 端點 + SSE
6. React 看板與任務詳情頁

第 1 到 3 步是階段 1，第 4 步是階段 1.5，第 5、6 步是階段 2，見 `01-roadmap.md`。
