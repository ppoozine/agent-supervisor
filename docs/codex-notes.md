# Codex 介面調查筆記

最後更新：2026-11-19

任務 0 的產出（見 `04-phase1-tasks.md`）。只調查，不實作。目的是在階段 1 定下 `AgentAdapter` 介面之前，先知道第二種 agent 的限制。

## 調查範圍與可信度

- 本機版本：`codex-cli 0.167.0`
- 「六個調查項目」是實測之前寫的，結論來自下表的來源。之後做的實測記在「實測結果」，兩者不一致時以實測為準。

| 標記 | 來源 | 可信度 |
| --- | --- | --- |
| 【本機】 | 在本機執行 `codex --help` 與各子指令的 `--help` | 對 0.167.0 確定 |
| 【文件】 | OpenAI 官方文件與 `openai/codex` repo 內的 README、docs | 文件沒有標版本，未對 0.167.0 驗證 |
| 【原始碼】 | `openai/codex` repo `main` 分支的程式碼 | 不是文件承諾的行為，可能隨版本改變 |
| 【未確認】 | 找不到依據 | 需要實測 |

官方文件網址 `developers.openai.com/codex/...` 目前會轉址到 `learn.chatgpt.com/docs/...`（已實際發請求確認），下面的出處寫轉址後的路徑。

兩個限制：

- 官方文件是透過摘要工具讀取的，不是逐字原文；設定參考頁很長，不保證讀完整。所以標成「找不到」的項目（例如輪數上限）只代表這次沒找到，不代表確定沒有。
- 【本機】的 `app-server` 協定是用 `codex app-server generate-json-schema` 產生後搜尋的，共 322 個定義檔，只確認了方法與欄位存在，沒有實際呼叫。

## 結論摘要

1. **`AgentAdapter` 的形狀裝得下 Codex**，不用推翻。一輪一個呼叫、先送 session 事件、最後回結果，這三點 Codex 都對得上。
2. **有兩條整合路線**：包 `codex exec` 這個 CLI，或用官方 Python SDK（`openai-codex`）。實測後決定 MVP 用前者，見「兩條整合路線」。
3. **Codex 不回報美元花費，也沒有輪數、花費、時間上限的選項。** 規格裡的任務花費上限對 Codex 任務不會生效。
4. **權限模型和 Claude Code 不同。** Codex 是「sandbox 等級 + 核准政策 + 指令規則」，沒有「允許的工具清單」這個概念。`PermissionPolicy` 需要調整才能同時表達兩者。
5. **Codex 在 worktree 裡無法自己 commit**（實測確認）。這剛好符合我們「commit 與 push 由 supervisor 做」的設計：Codex 任務的 commit 全部由 supervisor 在收尾時補。
6. **兩條路線都實測過，基本流程都可行**，各有沒查明的問題，見「實測結果」。

## 六個調查項目

### 1. 非互動執行與結構化輸出

- 【本機】`codex exec [PROMPT]` 是非互動模式。prompt 可以從參數或 stdin 傳入。
- 【本機】`--json` 把事件以 JSONL 印到 stdout；`-o <FILE>` 把 agent 最後一則訊息寫到檔案。
- 【本機】`-m <MODEL>` 指定模型；`-C <DIR>` 指定工作目錄；`-s <MODE>` 指定 sandbox 等級。
- 【文件】事件型別：`thread.started`、`turn.started`、`turn.completed`、`turn.failed`、`item.*`、`error`。（`/docs/non-interactive-mode`）
- 【文件】item 的種類：agent 訊息、推理、指令執行、檔案變更、MCP 工具呼叫、網頁搜尋、計畫更新。
- 【原始碼】另有 `item.updated`；`turn.failed` 的內容是 `{error: {message}}`，`error` 是 `{message}`。（`sdk/typescript/src/events.ts`）

對應到我們的事件型別：

| Codex 事件 | 我們的 `events.type` |
| --- | --- |
| `thread.started` | `session_started` |
| item：agent 訊息 | `agent_message` |
| item：指令執行 | `command` |
| item：檔案變更、MCP 工具呼叫、網頁搜尋 | `tool_call` |
| `turn.completed` | 轉成 `TurnResult` |
| `turn.failed`、`error` | `TurnResult` 的 `exit_status` 為 `error` |

推理與計畫更新目前沒有對應的型別，見「介面調整建議」第 6 點。

### 2. 續接指定 session

- 【本機】`codex exec resume [SESSION_ID] [PROMPT]`。ID 可以是 UUID 或 session 名稱。
- 【本機】`--last` 會選最近的一個 session。**平行執行多個任務時不能用**，一定要傳明確的 ID。
- 【文件】`thread.started` 帶 `thread_id`，是事件流的第一筆。【原始碼】型別定義的註解也寫明它是第一個事件。這符合規格「一拿到 session id 就送出事件」的要求。
- 【文件】session 存在 `~/.codex/sessions`。
- 【本機】`--ephemeral` 會讓 session 不寫入磁碟。**我們不能用這個選項**，否則無法續接。
- 【本機】**`resume` 的選項比首次執行少**：沒有 `-C`（工作目錄）、沒有 `-s`（sandbox 等級）、沒有 `--add-dir`。
- 【本機】`resume` 的 `--all` 說明是「顯示所有 session（停用工作目錄過濾）」。這表示 session 的查找預設會依目前的工作目錄過濾。
- 【未確認】用明確的 ID 續接時是否也受這個過濾影響。保守的做法是一律在原本的工作目錄（任務的 worktree）執行續接。
- 【未確認】續接時 sandbox 與核准設定是沿用原 session，還是要重新指定。`resume` 仍有 `-c key=value` 可以覆寫設定，但用它指定 sandbox 是否有效沒有依據。

### 3. 用量與花費

- 【文件】`turn.completed` 的 `usage` 有：`input_tokens`、`cached_input_tokens`、`output_tokens`、`reasoning_output_tokens`。
- 【原始碼】另有 `cache_write_input_tokens`。
- 【未確認】**找不到任何美元花費的欄位**，文件與型別定義都只有 token 數。
- 結論：Codex adapter 的 `reports_cost` 為 `False`，`cost_usd` 為 `NULL`。要有花費數字，得由 supervisor 依價目表自己算。

### 4. 輪數、花費、時間上限

- 【本機】`codex exec` 沒有任何輪數、花費、時間上限的選項。
- 【文件】設定檔裡也找不到「最多輪數」「花費上限」「單次執行時間上限」。只有周邊的設定（`/docs/config-file/config-reference`）：

| 設定 | 說明 |
| --- | --- |
| `features.rollout_budget.limit_tokens` | token 預算。【本機】`codex features list` 顯示 `rollout_budget` 與 `token_budget` 都是開發中、未啟用 |
| `model_providers.<id>.stream_idle_timeout_ms` | 串流閒置逾時，預設 300000 |
| `mcp_servers.<id>.tool_timeout_sec` | MCP 工具逾時，預設 60 秒 |

- 結論：`can_limit_turns` 與 `can_limit_budget` 都是 `False`。Codex 任務只靠 Runner 自己的執行時間上限與三次修復上限擋住。

### 5. 權限、核准與 sandbox

Codex 用三個獨立的機制控制 agent 能做什麼：

| 機制 | 選項 | 說明 |
| --- | --- | --- |
| Sandbox 等級 | 【本機】`-s read-only` / `workspace-write` / `danger-full-access` | 限制檔案寫入範圍與網路 |
| 核准政策 | 【文件】設定檔的 `approval_policy` | 什麼時候要問人 |
| 指令規則 | 【文件】`~/.codex/rules/*.rules` | 依指令前綴決定允許、詢問或禁止 |

**Sandbox**

- 【文件】`codex exec` 預設是 `read-only`。要讓 agent 改檔，必須明確指定 `workspace-write`。（`/docs/non-interactive-mode`）
- 【文件】`workspace-write` 下**網路預設關閉**，由 `sandbox_workspace_write.network_access` 控制。測試需要連網或安裝套件的專案會受影響。
- 【文件】`workspace-write` 下，可寫目錄裡的 `.git`、`.codex`、`.agents` **仍然是唯讀**。（`/docs/agent-approvals-security`）
- 【未確認】git worktree 的 commit 實際寫入的是主 repo 的 `.git` 目錄，在可寫範圍之外。所以 Codex 在 worktree 裡很可能無法 `git commit`。如果屬實，Codex 任務的 commit 全部由 supervisor 在收尾時補，這和規格一致。

**核准**

- 【本機】**`codex exec` 沒有 `-a` / `--ask-for-approval` 選項。** 這個選項只存在於互動模式的 `codex`。不要把它傳給 `exec`。
- 【本機】`codex exec` 與核准有關的選項只有 `--approve-for-me`：把核准請求交給自動審查，並使用 `workspace-write` sandbox。它實際會放行什麼沒有查到依據。
- 【文件】核准政策可以用設定檔的 `approval_policy` 指定（`on-request`、`never` 等），理論上能用 `-c approval_policy=...` 覆寫，但在 exec 模式下是否有效未確認。
- 【原始碼】`codex exec` 預設的核准政策是「從不詢問」；收到核准請求一律拒絕，訊息是「approval is not supported in exec mode」。（`codex-rs/exec/src/lib.rs`）
- 【文件】文件本身沒有寫 exec 模式遇到需要核准的指令時會怎樣。上面這點只有原始碼依據。
- 結論：包 CLI 的路線下，supervisor **無法**逐次決定放行或拒絕，`can_gate_tool_calls` 為 `False`。

**指令規則**

- 【文件】標示為實驗性。語法是 `prefix_rule(pattern, decision=allow|prompt|forbidden, ...)`，多條命中時取最嚴格的。（`/docs/agent-configuration/rules`）
- 【文件】專案內的 `<repo>/.codex/rules/` 只在專案受信任時載入。
- 【本機】`codex execpolicy check` 可以測試一條指令會被規則怎麼判定；`--ignore-rules` 可以略過規則檔。
- 【本機】`codex execpolicy check --rules <PATH>` 的規則檔路徑可以指定多個。但 `codex exec` 本身沒有「這次執行用哪個規則檔」的選項。
- 這是「依前綴禁止或允許指令」，不是「只允許清單內的工具」。它能表達「禁止 `git push`」。
- 規則檔的位置是使用者層級或專案層級，不是每次執行傳入。要依任務套用不同規則，可能的做法是每次執行換一組設定（【本機】`-p/--profile`，或換 `CODEX_HOME`），這兩種做法都未確認可行。

**登入方式**

- 【文件】`codex exec` 預設沿用已儲存的 CLI 登入；也可以用 `CODEX_API_KEY` 環境變數單次指定。
- 【文件】官方建議程式化使用時走 API key，按 API 計價，不用 ChatGPT 方案的額度。（`/docs/auth`）
- 【未確認】找不到明文禁止用 ChatGPT 訂閱登入做自動化的條款。

### 6. 中止執行中的工作

- 【文件】文件完全沒有提到訊號處理、子程序清理或結束代碼。
- 【原始碼】`codex exec` 只處理 Ctrl-C（SIGINT），收到後送出中斷這一輪的請求。沒有找到 SIGTERM 的處理。
- 【未確認】被中止時，agent 啟動的子程序（測試、shell）會不會一起結束。
- 結論：規格要求「停掉 agent 與它的所有子程序並確認結束」，這一點對 Codex **一定要實測**，不能假設結束主程序就夠。

## 兩條整合路線

| | 路線 A：包 `codex exec` CLI | 路線 B：官方 Python SDK |
| --- | --- | --- |
| 套件 | 不需要，用 subprocess | 【本機查證】PyPI 上的 `openai-codex`，目前 0.169.0，需 Python 3.10 以上 |
| 底層 | 每一輪一個 process，讀 JSONL | 【文件】透過 JSON-RPC 控制本機的 `codex app-server`，內含固定版本的 CLI |
| 開始與續接 | `exec`、`exec resume <id>` | 【文件】`thread_start`、`thread_resume` |
| 續接時指定工作目錄與 sandbox | `resume` 沒有這兩個選項 | 【文件】`thread_resume` 可帶 `cwd`、`sandbox` |
| 中止 | 送訊號，子程序清理未確認 | 【文件】這一輪的控制物件有 `interrupt()`。【本機】協定裡有 `turn/interrupt` |
| 逐次核准 | 不支援，一律拒絕 | 【文件】app-server 會向 client 發核准請求，可回覆接受或拒絕。【本機】協定裡有指令執行、檔案變更、權限三種核准請求 |
| 用量 | `turn.completed.usage` | 【本機】協定裡有用量通知，欄位是輸入、快取輸入、輸出、推理、總計五種 token 數，沒有花費 |
| 穩定性 | `exec` 是正式功能 | 【文件】Python SDK 標示 stable；但 CLI 參考頁說 `app-server` 是實驗性、可能不經通知變動。兩處說法不一致 |
| 版本 | 用本機安裝的 0.167.0 | SDK 自帶 CLI，版本可能和本機安裝的不同 |

**決定：MVP 用路線 A（包 `codex exec`）。階段 5 需要逐次核准時再評估路線 B。**（決策第 48 筆）

這個決定是實測之後做的，細節見「實測結果」。

選路線 A 的理由：

- MVP 需要的能力實測都可行：開始、續接指定 session、即時串流、用 SIGINT 中止並連子程序一起結束。
- 它的事件流有指令執行的項目，稽核紀錄需要這些。路線 B 的事件流裡沒有看到。
- 不需要新套件。路線 B 的環境約 410 MB，還自帶另一版 CLI。

路線 A 的代價：

- 被中止的 run 拿不到用量，記成 `NULL`。
- 用量是整個 session 的累計值，adapter 要自己相減。
- 有些被 sandbox 擋下的指令不會出現在事件流裡，所以 Codex 任務的 `permission_denied` 紀錄不完整。

路線 B 原本的優勢實測後沒有成立：

1. **公開的 async 介面不提供逐次核准。** 只有「全部拒絕」與「交給自動審查」兩種模式。自訂核准要用 SDK 內部較底層的同步 client，或直接走 `app-server` 協定。
2. **中斷一輪不會停掉正在執行的指令。** 中斷後子程序仍在跑，關閉 client 才結束。
3. **事件流裡沒有看到指令執行與檔案變更的項目**，原因還沒查明。

路線 B 仍然較好的地方是：建立 session 時就有 id、續接時可以指定工作目錄與 sandbox、被中斷時仍拿得到用量。階段 5 要做人工核准關卡時，再評估直接走 `app-server` 協定。adapter 介面不用動，只換 `adapters/codex.py` 的實作。

## 介面調整建議

對照 `02-mvp-spec.md` 的 `AgentAdapter`。以下八項都已確認採用，並更新到規格（決策第 49 到 53 筆）。

| # | 規格現況 | 問題 | 建議 |
| --- | --- | --- | --- |
| 1 | `TurnResult` 只有 `tokens_in`、`tokens_out` | Codex 與 Claude 都另有快取 token，Codex 還有推理 token。只存兩個數字，之後無法自己估算花費 | `TurnResult` 加一個 `usage_raw`（agent 原始的用量資料），寫進事件；`runs` 表先不加欄位 |
| 2 | 任務花費上限靠 `cost_usd` | Codex 不回報花費，這條規則對 Codex 任務不生效。規格已經寫明這種情況靠修復次數與執行時間上限擋住，所以不是漏洞 | 加強項：加一個以 token 計的任務上限，和花費上限並存，兩者任一到頂就停。這是新的設計決定，請你定 |
| 3 | `PermissionPolicy` 是「允許的工具與指令清單 + sandbox 等級」 | Codex 沒有工具允許清單；它有的是 sandbox 等級、是否允許連網、指令前綴規則 | 改成三個欄位：sandbox 等級、是否允許連網、adapter 各自解讀的工具與指令限制。前兩項 Codex 能落實；Claude Code 能不能落實要等任務 1 調查 SDK 後才知道 |
| 4 | 沒有「是否允許連網」 | Codex 預設關閉網路，需要連網跑測試的專案會失敗；Claude Code 有沒有對應的限制未確認 | 併入第 3 點；`AdapterCapabilities` 加 `can_restrict_network` |
| 5 | `can_gate_tool_calls` 一個布林值 | 路線 A 是 `False`，路線 B 可能是 `True`，取決於路線選擇 | 不用改介面，但選定路線後才能填 |
| 6 | `events.type` 沒有推理與計畫更新 | Codex 會送這兩種 item | 加一個 `agent_reasoning`，或併入 `agent_message` 並在 payload 標示種類。我傾向後者，少一個型別 |
| 7 | 規格假設 agent 會在 worktree 內 commit | Codex 在 worktree 裡無法 commit（實測確認） | 不用改介面。把規格的說法改成「agent 可能不會自己 commit，收尾時由 supervisor 補」 |
| 8 | `run_turn` 的 `cwd` 在續接時也會傳入 | 路線 A 的 `resume` 沒有工作目錄選項 | 不用改介面，adapter 用 subprocess 的工作目錄處理。路線 B 沒有這個問題 |

不需要調整的部分：

- `run_turn(prompt, cwd, session_id, model, limits, policy)` 的參數形狀
- 先送 `session_started` 再送其他事件
- `session_id` 與 token 欄位可以是 `None`
- `can_resume_session`、`can_limit_turns`、`can_limit_budget`、`reports_cost` 四個能力（Codex 分別是 `True`、`False`、`False`、`False`）

## 實測結果

2026-11-15 在 `agent-supervisor-sandbox` 的一個臨時 worktree 上做的，測完已刪除 worktree 與測試用的 branch。CLI 是本機的 0.167.0；SDK 是 `openai-codex` 0.169.0，裝在暫存環境，測完已移除。兩者都用已儲存的登入，沒有另外設定 API key。

每一項只測了一到兩次，是「觀察到的行為」，不是保證。

訊號測試做了兩次。第一次的量測方法有錯（用來找子程序的字串同時比對到 Codex 自己，訊號在啟動瞬間就送出），結果作廢；下表是修正後重做的結果。

### 路線 A：`codex exec`

| 問題 | 結果 |
| --- | --- |
| 事件順序與欄位 | 和文件一致。順序是 `thread.started`、`turn.started`、數個 `item.*`、`turn.completed`。`thread.started` 是第一筆，約 0.6 秒出現 |
| 是否即時串流 | stdout 接管線時逐行即時送出，每個事件發生時就收得到 |
| 用量 | `turn.completed.usage` 有 `input_tokens`、`cached_input_tokens`、`cache_write_input_tokens`、`output_tokens`、`reasoning_output_tokens`。沒有花費 |
| 用量是單輪還是累計 | **是整個 session 的累計值，不是這一輪的。** 同一個 session 連續四輪的 `input_tokens` 是 37,877、77,038、118,109、159,403。adapter 要自己用前後兩輪相減算出單輪用量 |
| 快取的比例 | 輸入 token 中約 85% 到 95% 是快取（第一輪 37,877 中有 32,000） |
| 用指定 ID 續接 | 可行，對話內容有接上 |
| 續接與工作目錄 | 從別的目錄續接也能成功，但 agent 的工作目錄變成新的目錄，而且在那裡**沒有寫入權限**。在原本的 worktree 續接則可以正常寫入。結論：續接一律在任務的 worktree 執行 |
| 續接時的 sandbox | 在原目錄續接，不指定 sandbox 也沿用了可寫模式；另外加 `-c sandbox_mode="workspace-write"` 也可行 |
| worktree 裡能否 commit | **不能。** agent 回報 `git add` 與 `git commit` 都失敗，原因是無法建立 `index.lock`。事後檢查 repo：新建的檔案是未追蹤狀態，沒有新的 commit |
| 網路 | `workspace-write` 下 `curl` 失敗，無法解析網域 |
| 被擋下的指令看不看得出來 | 部分。`curl` 以一個失敗的指令項目出現（有結束代碼與錯誤輸出）。但失敗的 `git add` 與 `git commit` **沒有出現在事件流裡**，只在 agent 最後的文字訊息中提到 |
| SIGINT | 指令執行中送出後，程序在 0.8 秒內自己結束，結束代碼是 1。**子程序一起結束了。** 訊號之後沒有再送出任何事件 |
| SIGTERM | 程序立刻被終止（結束代碼是「被訊號 15 終止」）。**子程序沒有結束**，3 秒後還在執行。訊號之後沒有再送出任何事件 |
| 被中止時的用量 | 兩種訊號都拿不到：沒有 `turn.completed`，所以沒有用量 |
| 中止的做法 | 要用 SIGINT，不能用 SIGTERM；並且仍要確認子程序真的結束，必要時終止整個程序群組 |

### 路線 B：Python SDK

| 問題 | 結果 |
| --- | --- |
| 套件 | `openai-codex` 0.169.0，附帶 `openai-codex-cli-bin` 0.169.0（自帶的 CLI）。整個環境約 410 MB。和本機安裝的 0.167.0 並存沒有出問題 |
| session id | `thread_start` 回傳時就有，不用等第一輪開始 |
| 串流 | 可行，逐字送出 agent 訊息。有 `turn/started`、`item/started`、`item/completed`、`thread/tokenUsage/updated`、`turn/completed` 等通知 |
| 指令與檔案變更的事件 | **沒有看到。** 一個「建立檔案並執行 `git add`」的任務，37 筆通知裡只有使用者訊息與 agent 訊息的項目，檔案確實建立了。原因未查明，可能需要額外設定 |
| 用量 | `TurnResult.usage` 與用量通知都有，分成「最近一次」與「累計」，各有輸入、快取輸入、快取寫入、輸出、推理、總計六個數字，另有模型的 context 大小。沒有花費 |
| 續接 | `thread_resume(id, cwd=..., sandbox=...)` 可行，對話內容有接上 |
| worktree 裡能否 commit | 一樣不能，`git add` 失敗 |
| 中斷 | `interrupt()` 之後串流立刻結束，這一輪的狀態是 `interrupted`，用量仍然拿得到 |
| 中斷後的子程序 | **仍在執行。** 中斷後 5.7 秒 `sleep` 還在；在另一次測試中，關閉 client 之後它才消失 |
| 核准 | 公開介面只有 `deny_all` 與 `auto_review` 兩種模式，預設是 `auto_review`。**adapter 一律要明確傳 `deny_all`。**SDK 內部的同步 client 有核准處理函式的參數，沒有提供時的預設行為是**全部接受**；`AsyncCodex` 沒有把這個參數開放出來 |
| 背景程序 | 這台機器上本來就有 Codex 的常駐 `app-server`（來自 Codex 與 ChatGPT 桌面程式）。SDK 用完後沒有多留下程序 |

### 實測得出的 Codex adapter 能力

| 能力 | 值 | 依據 |
| --- | --- | --- |
| `can_resume_session` | `True` | 兩條路線都實測可行 |
| `can_limit_turns` | `False` | 沒有對應選項 |
| `can_limit_budget` | `False` | 沒有對應選項 |
| `reports_cost` | `False` | 只有 token |
| `can_gate_tool_calls` | `False` | `codex exec` 做不到 |
| `can_restrict_network` | `True` | 實測網路被擋下 |

### 還沒查明的事

階段 1.5 實作 Codex adapter 時要查的（路線 A）：

1. 被擋下的 `git` 指令為什麼沒有出現在事件流裡，有沒有辦法看到 → 再次確認不會出現，原因仍未查明
2. 檔案變更在事件流裡長什麼樣子（這次的測試只觀察到指令執行的項目）→ 已查明，見「階段 1.5 的補充實測」
3. 用 API key 登入時行為是否相同 → 沒有查（手邊沒有 API key）

階段 5 評估路線 B 時要查的：

4. 事件流為什麼沒有指令與檔案變更的項目，怎麼打開
5. 中斷後怎麼可靠地停掉子程序
6. 用底層 client 或直接走協定做逐次核准是否可行

## 階段 1.5 的補充實測

2026-11-19 做的，實作 Codex adapter（任務 4）之前。CLI 仍是本機的 0.167.0，用已儲存的 ChatGPT 登入；在 `agent-supervisor-sandbox` 的臨時 worktree 上測，測完已刪除 worktree 與 branch。每一項只測了一次。

**和先前筆記對照**：`codex exec` 與 `codex exec resume` 的選項和「六個調查項目」寫的一致（重新執行兩者的 `--help` 比對）。事件順序、用量欄位、SIGINT 的行為也和先前的實測一致。沒有發現出入。

| 問題 | 結果 |
| --- | --- |
| 開啟連網 | `-c sandbox_workspace_write.network_access=true` 有效：`curl -sI https://example.com` 回 `HTTP/2 200`。不加時網路是關的（先前實測） |
| 開啟連網後能不能 push | **能。** `git push --dry-run origin HEAD` 成功，結束代碼 0。`.git` 唯讀擋得住 commit，擋不住 push。所以允許連網時，Codex 可以把 branch 上已有的 commit 推出去 |
| 檔案變更的事件 | 這次 agent 用 shell 指令改檔（`printf ... > a.txt`、`python3 -c ...`），事件流裡是兩個 `command_execution` 項目，沒有出現檔案變更的項目。檔案變更項目的形狀仍然沒有觀察到 |
| 被擋下的 `git add` | 和先前一樣**沒有出現在事件流裡**：前後的指令項目編號是連續的，中間沒有 `git add` 的項目，只有 agent 最後的文字訊息提到它失敗（無法建立 `index.lock`）。原因沒有查明 |
| 指令項目的形狀 | `{"type": "command_execution", "command": "/bin/zsh -lc '...'", "aggregated_output": "...", "exit_code": 0, "status": "completed"}`。`item.started` 時 `exit_code` 是 `null`、`status` 是 `in_progress`。指令一律包在 `/bin/zsh -lc` 裡 |
| 續接時的選項 | `codex exec resume <id> --json -c sandbox_mode="workspace-write" -o <檔案> <prompt>` 可行，在原本的 worktree 執行 |
| 辨識程序 | `-o <檔案>` 的路徑會出現在 Codex 程序的命令列上，首次執行與續接都有這個選項。把檔名取成唯一的字串，就能用 `pgrep -f` 找到那個程序。`codex` 這個指令會直接換成平台專屬的執行檔，所以啟動它的程序編號就是 Codex 本身 |
| 程序群組 | Codex 把它啟動的指令放在**各自的程序群組**（子程序的群組編號是它自己，不是 Codex 的）。所以對 Codex 的程序群組送訊號碰不到子程序；要強制終止時得先找出子孫程序 |
| SIGINT | 指令執行中送出後 1.1 秒結束，結束代碼 1，子程序一起結束，之後沒有再送出任何事件（沒有 `turn.completed`，拿不到用量）。stderr 有一行 `failed to record rollout items: thread ... not found` |
| 指令規則 | `codex execpolicy check --rules <檔案> -- git push origin main` 對 `prefix_rule(pattern=["git", "push"], decision="forbidden")` 回報 `forbidden`。但同一條規則對 `/bin/zsh -lc 'git push origin main'` 沒有命中，而 agent 實際執行的指令都是後面這種形狀。`codex exec` 執行時會不會把包在 shell 裡的指令拆開來比對，沒有實測 |

同一天稍後補測的：

| 問題 | 結果 |
| --- | --- |
| 檔案變更的項目 | 要求用內建的檔案編輯工具時出現了：`{"type": "file_change", "changes": [{"path": "<絕對路徑>", "kind": "update"}, {"path": "...", "kind": "add"}], "status": "in_progress"}`，完成時 `status` 是 `completed`。只有路徑與種類，沒有變更內容 |
| MCP 工具呼叫的項目 | `{"type": "mcp_tool_call", "server": "...", "tool": "...", "arguments": {...}, "result": {...}, "error": null, "status": "completed"}`。這台機器的 Codex 設定了額外的 MCP 工具，agent 用它讀檔 |
| 續接時回報的 id | `thread.started` 的 `thread_id` 和傳入的 id 相同 |
| 從 stdin 送指示 | 最後一個參數給 `-`，指示從 stdin 讀入；首次執行與續接都可行，以連字號開頭的指示也沒問題 |
| 失敗時的事件 | 指定不存在的模型：先有一筆 `error`（`{"message": ...}`），再一筆 `turn.failed`（`{"error": {"message": ...}}`），結束代碼 1，沒有 `turn.completed` |
| 推理 token | 一輪的 `output_tokens` 是 509、`reasoning_output_tokens` 是 23。推理 token 有沒有包含在 `output_tokens` 裡無法從這組數字判斷；adapter 的 `tokens_out` 直接用 `output_tokens` |

**沒有查的**：用 API key 登入時行為是否相同（手邊沒有 API key）；`~/.codex/rules/` 下的規則在 `codex exec` 裡實際會不會擋下指令（要改使用者的設定目錄才能測，沒有動）。

**對 adapter 的影響**：

1. `PermissionPolicy.allow_network` 對 Codex 執行的指令可以落實，預設值是允許連網。使用者設定的 MCP 工具（實測 agent 用了一個來讀檔）受不受這個限制沒有驗證。但允許連網時 Codex 擋不住 `git push`，而 `can_gate_tool_calls` 是 `False`，`tool_rules` 裡「拒絕的指令」對 Codex 不生效。這和「push 由 supervisor 執行」的規則有落差，處理方式待決定。
2. `process_marker` 用 `-o` 的檔名。
3. 中止用 SIGINT；等不到結束時，先列出子孫程序再逐一強制終止，不能靠程序群組。

## 檢查能不能用（任務 15，2026-11-21 實測，codex-cli 0.167.0）

- 【實測】`codex login status` 不呼叫 API，輸出寫在 **stderr**：
  - ChatGPT 登入 → `Logged in using ChatGPT`，結束碼 0
  - 存了 API key（`codex login --with-api-key`）→ `API key configured - sk-…`（只遮住一部分，所以工具不回傳、不記錄原始輸出），結束碼 0
  - 沒有登入 → `Not logged in`，結束碼 1
- 【實測】`CODEX_API_KEY` 環境變數**不會**反映在 `codex login status`：設了它，沒有存登入時一樣回 `Not logged in`。但 `codex exec` 會用它（見上方「登入方式」），所以檢查先看這個變數有沒有設（不讀值），有就算 API key。
- 【實測】`OPENAI_API_KEY` 也不會反映在 `login status`。文件沒寫 `codex exec` 會不會用它，工具不把它當成登入。
- 【實測】存的 API key 只看有沒有設定，不驗證（`Authentication is checked when you send a request.`）。

## 出處

- 本機：`codex --help`、`codex exec --help`、`codex exec resume --help`、`codex app-server --help`、`codex sandbox --help`、`codex execpolicy --help`、`codex execpolicy check --help`、`codex features list`、`codex app-server generate-json-schema`（0.167.0）
- 官方文件（`https://learn.chatgpt.com/docs/` 之下）：`non-interactive-mode`、`developer-commands`、`config-file/config-reference`、`agent-approvals-security`、`agent-configuration/rules`、`codex-sdk`、`app-server`、`auth`
- `openai/codex` repo（`main` 分支）：`sdk/typescript/README.md`、`sdk/typescript/src/events.ts`、`sdk/python/docs/api-reference.md`、`codex-rs/exec/src/lib.rs`
- PyPI：`https://pypi.org/project/openai-codex/`
