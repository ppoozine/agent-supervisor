# Claude Agent SDK 調查筆記

最後更新：2026-11-15

任務 1 的事前調查（當時的任務單已刪除，原文在 tag `v0.3.0`）。實作 `ClaudeCodeAdapter` 之前，先確認官方 Python Agent SDK 的實際用法。

## 調查範圍與可信度

- 套件：`claude-agent-sdk` 0.2.182，內含 Claude Code CLI 2.1.325（和本機安裝的版本相同）
- 每一項標明來源：

| 標記 | 來源 | 可信度 |
| --- | --- | --- |
| 【原始碼】 | 已安裝套件的型別定義與程式碼 | 對 0.2.182 確定 |
| 【文件】 | 官方文件 `code.claude.com/docs/en/agent-sdk/` | 透過摘要工具讀取，不是逐字原文 |
| 【實測】 | 在 `agent-supervisor-sandbox` 的臨時 worktree 上實際執行 | 每項只測一到兩次，是觀察到的行為 |
| 【未確認】 | 找不到依據或測不出來 | 需要再查 |

實測的限制：是在另一個 Claude Code session 裡面執行的，用的是已登入的訂閱帳號，沒有設定 API key。測完已刪除 worktree、測試用的 branch 與暫存環境。

## 結論摘要

1. **SDK 能做到規格要求的每一項**：串流、續接、中止、單次 run 的輪數與花費上限、逐次核准工具呼叫。
2. **session id 在第一筆訊息就拿得到**，也可以由我們事先指定。
3. **逐次核准實測可行，但要用 hook 而不是核准回呼**：核准回呼會被專案設定的允許規則與唯讀指令跳過。`can_gate_tool_calls` 為 `True`。
4. **花費是整個 session 的累計值，用量是單次呼叫的值**。兩者單位不同，adapter 要把花費相減換算成單輪。
5. **中止實測可靠**：中斷後子程序 3 秒內結束，而且仍拿得到用量與花費。
6. **sandbox 設定實測沒有生效**，原因未查明。在查明之前，Claude Code adapter 不能宣稱能限制連網。
7. **預設會載入使用者自己的 Claude Code 設定**（hooks、skills、`CLAUDE.md`）。adapter 必須明確指定要載入哪些。

## 調查項目

### 1. 基本用法與訊息格式

- 【原始碼】`ClaudeSDKClient(options)` 是可以互動的 client：`connect()`、`query(prompt)`、`receive_response()`、`interrupt()`、`disconnect()`，也支援 `async with`。
- 【原始碼】訊息型別：`SystemMessage`、`AssistantMessage`、`UserMessage`、`ResultMessage`、`StreamEvent`、`RateLimitEvent`。
- 【原始碼】`AssistantMessage.content` 是內容區塊的清單：`TextBlock`、`ThinkingBlock`、`ToolUseBlock`（`id`、`name`、`input`）等。工具的執行結果以 `UserMessage` 裡的 `ToolResultBlock`（`tool_use_id`、`content`、`is_error`）回來。
- 【實測】一次執行的訊息順序：

| 順序 | 訊息 | 說明 |
| --- | --- | --- |
| 1 | `SystemMessage`，`subtype` 為 `init` | `data` 裡有 `session_id`、`cwd`、`model`、`claude_code_version` 等 |
| 2 | `AssistantMessage` | 帶 `ToolUseBlock` 或 `TextBlock`，每一則都有 `session_id` 與 `usage` |
| 3 | `UserMessage` | 帶 `ToolResultBlock` |
| … | 2、3 重複 | 中間還會穿插 `RateLimitEvent` 與其他 `SystemMessage`（例如 `vcs_state_changed`） |
| 最後 | `ResultMessage` | 結果、用量、花費 |

- 【實測】第一次啟動約 6 秒才收到 `init`；之後的啟動約 0.3 秒。

對應到我們的事件型別：

| SDK 訊息 | 我們的 `events.type` |
| --- | --- |
| `SystemMessage`（`init`） | `session_started` |
| `TextBlock` | `agent_message`（`kind` 為一般訊息） |
| `ThinkingBlock` | `agent_message`（`kind` 為推理） |
| `ToolUseBlock`，工具是 `Bash` | `command` |
| `ToolUseBlock`，其他工具 | `tool_call` |
| `ToolResultBlock` | 併入對應的 `command` 或 `tool_call` 事件的結果；做法在實作計畫中決定 |
| 核准回呼拒絕 | `permission_denied` |
| `ResultMessage` | 轉成 `TurnResult` |

### 2. Session 與續接

- 【實測】`init` 訊息的 `data["session_id"]` 是最早拿到 session id 的地方，早於任何 agent 訊息。
- 【實測】事先指定的 session id 在 CLI 真的啟動之前並不存在。第一次啟動失敗後拿這個 id 去續接，會得到「找不到這個 session」的錯誤。所以要等收到 `init` 訊息才把 id 當成可以續接的。
- 【原始碼】`ClaudeAgentOptions.session_id` 可以**事先指定** session 的 UUID，SDK 會把它以 `--session-id=<uuid>` 傳給 CLI。這樣在啟動之前就知道 id，而且這個字串會出現在 CLI 程序的命令列上，可以用來辨識遺留的程序（見第 8 項）。
- 【實測】`ClaudeAgentOptions(resume=session_id)` 開一個新的 client 續接，對話內容有接上，`session_id` 不變。
- 【文件】對話紀錄存在 `~/.claude/projects/<編碼後的工作目錄>/<id>.jsonl`。2.1.223 之後續接時會跨專案目錄尋找，但仍建議工作目錄保持不變。

### 3. 單次 run 的上限

- 【原始碼】`max_turns: int | None`、`max_budget_usd: float | None`。
- 【實測】`max_turns=1`：agent 做完一次工具呼叫後就停，`ResultMessage.subtype` 是 `error_max_turns`，`terminal_reason` 是 `max_turns`，`is_error` 為真。回報的 `num_turns` 是 2。
- 【文件】「一輪」是指一次帶工具呼叫的回應，最後那則純文字回應也算一輪。
- 【文件】`max_budget_usd` 是用戶端的估算，在每次模型回應結束後比對，不會在回應中途打斷，所以可能略為超過。只計算這次呼叫的花費，不含續接前的累計。超過時 `subtype` 是 `error_max_budget_usd`。**這一項沒有實測。**
- 【原始碼】另有 `task_budget`（以 token 計），沒有調查。

### 4. 用量與花費

- 【原始碼】`ResultMessage` 的欄位：`subtype`、`is_error`、`num_turns`、`session_id`、`duration_ms`、`total_cost_usd`、`usage`、`model_usage`、`stop_reason`、`terminal_reason`、`permission_denials`、`result`、`errors`。
- 【實測】`usage` 是**這次呼叫**的用量。續接後的那次呼叫只回報很小的數字。
- 【實測】`total_cost_usd` 是**整個 session 的累計**。第一次呼叫是 0.3517，續接後回報 0.3641。文件也說續接的呼叫回報的是整個 session 的花費。
- 【實測】`usage` 的主要欄位：`input_tokens`、`cache_creation_input_tokens`、`cache_read_input_tokens`、`output_tokens`，另有 `iterations`（每次模型呼叫的明細）。
- 【實測】輸入幾乎都是快取：一次小任務的 `input_tokens` 是 6，`cache_creation_input_tokens` 是 20,397，`cache_read_input_tokens` 是 37,890。**只看 `input_tokens` 會嚴重低估。**
- 【實測】用訂閱登入時 `total_cost_usd` 仍然有值。【文件】它是依內建價目表算的估算，不是帳單資料。
- 【文件】`usage` 只含主迴圈；`model_usage` 與 `total_cost_usd` 含子代理。

對 adapter 的要求：

- `tokens_in` 取 `input_tokens`、`cache_creation_input_tokens`、`cache_read_input_tokens` 三者相加；`tokens_out` 取 `output_tokens`。原始資料放進 `usage_raw`。
- `cost_usd` 要用這次的 `total_cost_usd` 減去同一個 session 上一次的值。

### 5. 權限

- 【原始碼】`permission_mode` 的值：`default`、`acceptEdits`、`plan`、`bypassPermissions`、`dontAsk`、`auto`。
- 【文件】判斷順序：hooks、拒絕規則、詢問規則、權限模式、允許規則，最後才是核准回呼。回呼只在前面都沒有決定時才被呼叫。
- 【文件】`allowed_tools` 是「預先核准」，**不是**「只能用這些工具」。`disallowed_tools` 寫工具名稱會把該工具整個拿掉；寫成帶範圍的規則（例如限定某種指令）則在任何模式下都拒絕符合的呼叫。
- 【文件】`default` 模式下沒有提供回呼時，需要核准的呼叫一律被拒絕。
- 【原始碼】核准回呼的型別是 `(tool_name, tool_input, context) -> PermissionResultAllow | PermissionResultDeny`。拒絕時可以帶訊息，也可以要求中斷整個執行。
- 【實測】`permission_mode="default"` 加上核准回呼：`Write` 與每一次 `Bash` 呼叫都進了回呼，回呼拿得到完整的指令內容。
- 【實測】被回呼拒絕的呼叫有三個地方看得到：
  - 回呼本身（最可靠，`permission_denied` 事件從這裡寫）
  - 事件流裡一筆 `is_error` 為真的 `ToolResultBlock`，內容是拒絕訊息
  - `ResultMessage.permission_denials`，列出工具名稱與完整輸入
- 【實測】沒有開 sandbox 時，Claude Code 可以在 worktree 裡 `git commit`。這和 Codex 不同。
- 【實測】**核准回呼不是每次都會被呼叫**，有兩種情況會跳過它：
  - 目標 repo 的 `.claude/settings.json` 有允許規則（例如允許 `gh`）時，符合的呼叫直接執行。
  - 被 Claude Code 判定為唯讀的指令（例如 `ls`）直接執行，即使政策根本不允許執行指令。
- 【實測】透過 `hooks` 選項註冊的 `PreToolUse` hook 對每一次工具呼叫都會執行，上面兩種情況都擋得住。被 hook 拒絕的呼叫在事件流裡是一筆失敗的工具結果，內容是拒絕的原因。
- 結論：權限政策要用 `PreToolUse` hook 落實，核准回呼只用來放行。
- 【實測】agent 可以用指令工具的 `run_in_background` 參數把指令丟到背景，然後直接結束這一輪，結果是成功。client 一斷線，背景指令就被終止，沒有留下程序。另外，Claude Code 不允許在前景執行 `sleep`，agent 會自動改成背景執行。
- 【實測】SDK 有時會送出沒有文字的思考區塊。

### 6. Sandbox 與網路

- 【原始碼】`ClaudeAgentOptions.sandbox` 接受 `SandboxSettings`：`enabled`、`autoAllowBashIfSandboxed`、`allowUnsandboxedCommands`、`excludedCommands`、`network`（含 `allowedDomains`、`deniedDomains` 等）。
- 【文件】sandbox 預設關閉。開啟後寫入範圍限於工作目錄、額外指定的目錄與暫存目錄；網路透過代理，允許清單預設是空的。
- 【實測】**設定 `enabled=True` 後沒有看到任何限制生效**：`curl` 成功、寫入 worktree 之外的檔案成功、`git commit` 成功。
- 【未確認】沒有生效的原因。可能的因素：實測是在另一個 Claude Code session 內執行；同時指定了不載入任何設定來源；寫入的目標剛好在暫存目錄之下。這三個都沒有個別驗證。
- 結論：在查明之前，`can_restrict_network` 填 `False`，權限政策裡的「是否允許連網」與「sandbox 等級」對 Claude Code 任務不保證落實。工具與指令的限制則由核准回呼落實，實測可行。

### 7. 設定來源的隔離

- 【文件】預設會載入使用者層級、專案層級、本機層級的設定，包含 `CLAUDE.md`、hooks、skills、子代理。
- 【原始碼】`setting_sources` 可以指定 `user`、`project`、`local` 的任意組合；空清單表示都不載入。
- 【文件】即使是空清單，下列仍會載入：受管理的政策、`~/.claude.json`、自動記憶；claude.ai 的連接器也會載入，除非另外關閉。
- 【實測】用空清單可以正常執行。
- 這是要你決定的事：不載入任何設定，agent 就讀不到目標 repo 的 `CLAUDE.md`；載入使用者層級的設定，你自己的 hooks 與外掛會影響無人看管的 agent。見實作計畫。

### 8. 中止與程序清理

- 【文件】`interrupt()` 只在串流模式下有效；中斷後要繼續讀完 `receive_response()`。
- 【實測】agent 正在執行一個長時間指令時呼叫 `interrupt()`：
  - `ResultMessage` 立刻回來，`subtype` 是 `error_during_execution`，`terminal_reason` 是 `aborted_tools`
  - **用量與花費仍然有值**
  - 子程序在 3 秒內結束
  - 關閉 client 後沒有留下 CLI 程序
- 【實測】daemon 突然死掉後的情況測了兩次，結果不同：
  - 讓 Python 程序自己直接結束：子程序 4 秒內就不見了，CLI 程序在 12 秒時還查得到。
  - 用 `kill -9` 從外部終止（實作完成後的驗證）：**CLI 程序與它啟動的子程序都還在執行。**
  - 所以不能假設 daemon 死掉後 agent 會自己停，重啟時一定要主動清理。
- 【文件】文件沒有說明中斷、斷線或程序死掉時子程序會怎樣。
- 【原始碼】SDK 沒有公開的方法取得 CLI 的程序編號。辨識方式是 session id：新 session 時命令列上有 `--session-id=<uuid>`，續接時有 `--resume=<uuid>`，兩種情況 session id 都在命令列上。實作完成後實測：用這個方法找到並停掉了遺留的 CLI 與子程序。

### 9. 登入方式

- 【文件】原文大意：「除非事先取得許可，Anthropic 不允許第三方開發者在自己的產品中提供 claude.ai 登入或其用量額度，包含用 Agent SDK 做的 agent。請改用 API key。」
- 【未確認】文件沒有提到「開發者在自己的電腦上、用自己的訂閱、給自己用」這種情況算不算。限制的寫法是針對「提供給別人的產品」。
- 【實測】已登入訂閱的情況下，SDK 不需要任何額外設定就能執行。
- 【文件】驗證來源的優先順序中，`ANTHROPIC_API_KEY` 排在訂閱登入之前。**daemon 的環境裡如果有這個變數，就會改用 API 計費。**
- 【文件】`claude setup-token` 產生的 `CLAUDE_CODE_OAUTH_TOKEN` 是給無法用瀏覽器登入的腳本使用的，用的是訂閱額度。
- 結論：自己用的階段沿用現有登入。這個工具如果之後要給別人用，要改成 API key。這和決策第 16 筆記下的顧慮一致，不影響那個決定。

### 10. 其他

- 【文件】【實測】SDK 內含 Claude Code 的執行檔，版本跟著套件走；整個環境約 366 MB。`cli_path` 可以改用別的執行檔。
- 【實測】在另一個 Claude Code session 裡面執行 SDK 可以正常運作。文件沒有提到這種用法的注意事項。

## 對 Claude Code adapter 的能力結論

| 能力 | 值 | 依據 |
| --- | --- | --- |
| `can_resume_session` | `True` | 實測 |
| `can_limit_turns` | `True` | 實測 |
| `can_limit_budget` | `True` | 文件，未實測 |
| `can_gate_tool_calls` | `True` | 實測 |
| `reports_cost` | `True` | 實測；是估算值 |
| `can_restrict_network` | `False` | sandbox 實測沒有生效，查明前先填否 |

## 還沒查明的事

1. sandbox 設定為什麼沒有生效。需要在不是巢狀的環境下、分別改變設定來源與寫入位置再測
2. `max_budget_usd` 的實際行為
3. 被中止的 run 能不能在中斷後多讀一筆結果，把用量與花費記下來（SDK 中斷後仍會回報，目前的實作沒有讀）

## 檢查能不能用（任務 15，2026-11-21 實測）

- 【實測】SDK 先用它自己附帶的 CLI（`claude_agent_sdk/_bundled/claude`，本機是 2.1.325），沒有附帶時才找 PATH 上的 `claude`（`_internal/transport/subprocess_cli.py` 的 `_find_cli`）。所以 PATH 上有沒有 `claude`、是哪個版本，都不影響 adapter；檢查也要用同一個 CLI，不然結果和實際不一致。
- 【實測】`claude auth status --json`（預設就是 JSON）約 0.3 秒，不呼叫 API。欄位：`loggedIn`、`authMethod`、`apiProvider`、`apiKeySource`、`subscriptionType`，以及 `email`、`orgId`、`orgName`（工具不讀這三個）。沒有登入時結束碼是 1，但照樣印出 JSON。
- 【實測】`authMethod` 的值：
  - 訂閱登入（`claude auth login`）→ `claude.ai`，附 `subscriptionType`（例如 `max`）
  - `ANTHROPIC_API_KEY` → `api_key`，`apiKeySource` 為 `ANTHROPIC_API_KEY`
  - `CLAUDE_CODE_OAUTH_TOKEN`（`claude setup-token` 產生的，用訂閱額度）→ `oauth_token`
  - `ANTHROPIC_AUTH_TOKEN`（gateway 用的 bearer token）→ 也是 `oauth_token`，要看環境變數才分得出來；依文件的優先順序，兩個都有時用 `ANTHROPIC_AUTH_TOKEN`
  - `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX` → `third_party`，`apiProvider` 為 `bedrock` / `vertex`
  - 什麼都沒有 → `none`，`loggedIn` 為否
- 【實測】只看有沒有設定憑證，不驗證：設一個假的 `ANTHROPIC_API_KEY` 也回 `loggedIn: true`。要驗證就得打 API，所以檢查不做；key 錯了要到第一次 run 才知道。
- 【文件】Agent SDK 說明頁（`agent-sdk/overview`）「Get started」底下的原文："Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK. Use the API key authentication methods described in the Quickstart instead." 頁面沒有寫怎麼取得許可。設定頁的說明引用這段（決策第 140 筆）。
- 【文件】驗證來源的優先順序（`authentication` 的「Authentication precedence」）：雲端供應商 → `ANTHROPIC_AUTH_TOKEN` → `ANTHROPIC_API_KEY` → `apiKeyHelper` → `CLAUDE_CODE_OAUTH_TOKEN` → `ant` CLI 的 profile → 訂閱登入。`ANTHROPIC_API_KEY` 在非互動模式下有設就一定會用，所以設了它就會優先於訂閱登入。

## 出處

- 已安裝套件：`claude_agent_sdk/types.py`、`client.py`、`_internal/transport/subprocess_cli.py`（0.2.182）
- 官方文件（`https://code.claude.com/docs/en/` 之下）：`agent-sdk/overview`、`agent-sdk/quickstart`、`agent-sdk/python`、`agent-sdk/sessions`、`agent-sdk/agent-loop`、`agent-sdk/permissions`、`agent-sdk/cost-tracking`、`agent-sdk/claude-code-features`、`agent-sdk/hosting`、`agent-sdk/streaming-vs-single-mode`、`authentication`、`sandboxing`
