# 03 · 決策紀錄

最後更新：2026-11-15

每個決定記下選了什麼、沒選什麼、為什麼。之後要推翻某個決定時，在下面新增一筆，不要直接改舊的。

| # | 決定 | 沒選的方案 | 理由 |
| --- | --- | --- | --- |
| 1 | 專案名稱 `agent-supervisor` | `agent-warden`、`agent-dispatch`、`agent-foreman` | 最白話，一看就懂在做什麼。Warden 這個字不直覺，而且會被誤認為資安工具 |
| 2 | Repo 名稱不加 `-mvp` | `agent-supervisor-mvp` | MVP 是階段不是產品，同一個 repo 會一路長成正式版；階段用版本號 `0.x` 表達 |
| 3 | 另建 `agent-supervisor-sandbox` | 直接拿主專案或其他專案測試 | 核心迴圈需要一個可以故意把 CI 弄壞的對象 |
| 4 | 後端用 Python | – | 我最熟的語言 |
| 5 | 前端用 React，MVP 做成本機 web，不包桌面殼 | 一開始就用 Tauri 或 Electron | Python 打包成桌面 app 很費工；先用瀏覽器開 `localhost`，之後套 Tauri 時前端幾乎不用改 |
| 6 | 資料庫用 SQLite | PostgreSQL | 單機工具，零安裝 |
| 7 | GitHub 狀態用輪詢（30 秒） | Webhook | Webhook 需要公開網址，本機工具很麻煩 |
| 8 | 看板欄位由狀態推導，不存資料庫 | 存一個 `status` 欄位 | 存下來的狀態遲早會跟實際的 PR / CI 狀態不同步 |
| 9 | Agent 透過 adapter 介面控制，MVP 只做 Claude Code | 直接在 Runner 裡寫死 | 之後加第二種 agent 不必改 Runner |
| 10 | Claude Code adapter 用 subprocess 包 CLI 的 headless 模式 | 官方 SDK | 最快看到結果；之後要換只動 adapter 一個檔案 |
| 11 | 單一任務設花費上限 | 不設限 | 成本很低，而且能防止 agent 在迴圈裡把 token 燒光 |
| 12 | 自動修復 CI 最多 3 次 | 不設限 | 同上；超過就交給人 |
| 13 | MVP 就放入稽核紀錄（`events` 表只新增不修改）與花費統計 | 留到治理階段 | 成本低，而且是和同類工具的差異點 |
| 14 | 任務入口先只做手動開單 | Orchestrator 自動拆任務、issue 觸發 | 最簡單；自動拆任務是最難的部分，留到階段 4 |
| 15 | 開發工具用 Claude Code CLI | 桌面 app、雲端對話 | 產品本身就是在驅動這個 CLI，直接用它最能掌握它的行為 |
| 16 | Claude Code adapter 改用官方 Python Agent SDK（推翻第 10 筆） | 用 subprocess 包 CLI 的 headless 模式 | SDK 底下跑的是同一個 Claude Code，已經提供串流、續接、中止、單次 run 的輪數與花費上限，以及逐次工具呼叫的核准回呼。階段 5 的核准關卡需要這個回呼。代價是少了自己解析 CLI 輸出的練習 |
| 17 | 專案的主要目的是學習與自己使用 | 做成產品發佈、直接使用現成工具 | Agent Orchestrator 已經做了 CI 自動修復與 review 留言處理，這些不是差異點。既然目的是學習，不需要靠定位來決定路線 |
| 18 | Adapter 介面以「一輪對話」為單位，並由 adapter 宣告 `capabilities` | 原本的 `start` / `resume` / `stop` / `events` 四個方法 | 原介面要求先回傳 session id、續接時沒有工作目錄、用量沒有回傳路徑。一輪一個呼叫可以同時容納包 CLI、用 SDK、自製 agent 三種做法 |
| 19 | Codex adapter 提前到階段 1.5（延續第 9 筆） | 留在階段 4 | 只有一個實作的介面會偏向那個實作；第二個 adapter 是驗證介面最便宜的方法，也是明確想要的功能 |
| 20 | 自製的 API agent 排成階段 6，當作第三個 adapter | 現在就改成直接呼叫模型 API | 直接呼叫 API 等於自己做 coding agent，是另一個專案的份量。等介面被兩個 adapter 驗證過再做。想要的管控先用 SDK 的核准回呼達成 |
| 21 | 多人使用、登入、角色權限移出規劃 | 留在產品化階段 | 以自己使用為主，用不到 |
| 22 | push 與開 PR 由 supervisor 執行，agent 正常結束的每一次 run 都做收尾 | 讓 agent 自己 push 與開 PR | 原規格只寫了第一次 push，CI 修復後沒有人 push，迴圈會斷。由 supervisor 做也讓 agent 不需要 push 與 `gh` 的權限 |
| 23 | 防重複餵回改為比對 `last_handled_head_sha` | `last_handled_ci_run_id` | 一個 commit 可能有多個 CI run，失敗也可能屬於舊的 commit。以 commit 為單位，等該 commit 的檢查全部結束後一次餵回 |
| 24 | 專案加上 `require_review`，預設 `false` | 一律要求 review 核准 | GitHub 不允許 PR 作者核准自己的 PR，單人使用時 Ready to merge 永遠到不了 |
| 25 | PR 已合併或關閉的任務離開看板，列在 Archive 區 | 留在看板上、新增第五個欄位 | 四個欄位都不適用，而且這些任務不再需要注意 |
| 26 | 單一 run 也設上限（剩餘預算與最多輪數），由 adapter 在執行中擋下 | 只在 run 與 run 之間檢查 | 用量在 run 結束時才回填，單一失控的 run 原本不受限 |
| 27 | `cost_usd` 視為估算值，可為 `NULL`，介面標示為估算 | 當成實際帳單金額 | 它是 agent 依價目表算的；訂閱方案下不代表實際花費，有些 agent 可能不回報 |
| 28 | 階段 1 就放入最基本的權限政策：固定允許清單，清單外一律拒絕並記錄 | 權限全部留到階段 5 | Agent 是無人看管地執行，worktree 只隔離檔案不是 sandbox。互動式核准仍留到階段 5 |
| 29 | Daemon 重啟時把未結束的 run 標成 `interrupted`，不自動重跑 | 自動重跑、不處理 | 不處理的話卡片會永遠停在 Working；自動重跑可能在重啟後一口氣燒掉很多用量 |
| 30 | 多人使用、登入、角色權限要不要做，延到階段 7 再決定（推翻第 21 筆） | 現在就移出規劃 | 現在沒有足夠的資訊判斷，先不排除 |
| 31 | 自動修復條件加上：PR 開著、任務未封存、最後一次 run 是 `success` | 只看 CI 與次數 | 否則使用者按 Stop 或 run 被中斷後，Dispatcher 仍會自動開新的 run |
| 32 | 建立 run 與更新防迴圈欄位在同一個資料庫交易內完成；每個任務同時最多一個未結束的 run | 先記帳、抓 log、再建立 run | 原順序在中間當掉會白白扣掉修復次數，抓 log 期間使用者送訊息會產生兩個同時執行的 run |
| 33 | Runner 自己有執行時間上限；`capabilities` 增加 `can_limit_turns` | 假設每個 adapter 都能限制輪數與花費 | Codex CLI 沒有公開的輪數或花費上限選項，需要一道不依賴 agent 的防線 |
| 34 | 階段 1 的權限政策明寫信任前提：只用在信任的 repo，政策是基本防護不是安全邊界（修正第 28 筆的說法） | 承諾清單外一律擋下 | Agent 能執行測試或 shell 就能間接繞過；各 agent 能落實的程度也不同。承諾做不到的保證比沒有保證更危險 |
| 35 | 中止要停掉 agent 與它的所有子程序並確認結束；有 run 在執行或有未 commit 的變更時拒絕封存，封存保留 branch | 只取消串流；封存時直接刪除 | 取消串流不代表底下的程序停了；封存不該默默丟掉成果 |
| 36 | Codex 介面的調查（只調查不實作）提前到階段 1 開始前（任務 0），實作仍在階段 1.5 | 到階段 1.5 才調查 | 介面在階段 1 就會定下來，先知道第二種 agent 的限制比較便宜 |
| 37 | 收尾的每一步先查現況再做，可以安全地重做 | 另外記錄收尾進度的狀態欄位 | 重啟後不會重複開 PR，又不必多一組狀態 |
| 38 | 封存時若有未 commit 的變更，由 supervisor 先 commit 到任務的 branch（不 push）再清掉 worktree（修正第 35 筆中「拒絕封存」的部分） | 拒絕封存、加一個丟棄變更的選項 | 被 Stop、出錯或中斷的 run 不做收尾，通常會留下未 commit 的變更，而這些正是最常要封存的任務。拒絕會讓它們封存不了，丟棄會默默失去成果 |
| 39 | 一個 run 從建立、準備、agent 執行到收尾，由同一個可取消的工作負責；執行時間上限從建立時起算；Stop 不回滾已完成的動作 | 只有 agent 執行的部分可以取消 | 否則抓 log 或 push 的時候按 Stop 沒有東西可取消，抓 log 卡住時也沒有人把 run 結案 |
| 40 | 建立 run、Stop、封存共用每個任務一把鎖（daemon 記憶體內） | 只靠資料庫的唯一約束 | 唯一約束擋不住「封存檢查完、正在補 commit 時剛好有 run 被建立」 |
| 41 | Poller 寫入前發現 `head_sha` 已變就丟棄這次結果；push 時連 `review_state` 一起重設 | 查到什麼就寫什麼 | 查詢期間可能剛 push 了新 commit，舊結果會讓卡片錯顯 Ready，舊的核准也會沿用到新 commit |
| 42 | 重啟復原必須先停掉遺留程序才能放行任務；同時只允許一個 daemon | 只把 run 標成 `interrupted` | 遺留的 agent 可能還在改檔，新的 run 進同一個 worktree 會互相覆蓋 |
| 43 | 防迴圈規則只管「能不能再自動修復」，不影響成果能不能合併；剩餘預算小於等於零就不啟動新的自動 run | 任一規則觸發就進 Needs you | 第三次修復修好了、或超過預算但 CI 已綠，成果是可以合併的 |
| 44 | PR 被合併或關閉時還有 run 在跑，由 Poller 中止它，停下後卡片才離開看板 | 卡片直接進 Archive | 否則 run 會在看不到的地方繼續跑，結束後還會 push |
| 45 | Reviewer 要求修改時不自動修 CI，卡片直接進 Needs you | CI 失敗就照常自動修 | 這時任務需要人來看；review 留言的自動處理是階段 3 的範圍 |
| 46 | Stop 只能用在已存在的 run 上；CI 失敗後、Dispatcher 接手前的空窗 Stop 不可用 | 加一個「暫停自動修復」的持久化旗標 | 空窗最長一個輪詢週期，為它多維護一個狀態不划算 |
| 47 | 任務與它的 `initial` run 在同一個交易內建立；worktree 建立失敗記為該 run 的 `error` | 先建任務、再建 run | 不會出現「沒有任何 run 的任務」，看板推導不必處理這種情況 |
| 48 | Codex adapter 在 MVP 用 subprocess 包 `codex exec`；階段 5 需要逐次核准時再評估官方 Python SDK 或 `app-server` 協定 | 官方 Python SDK（`openai-codex`） | 兩條路線都實測過。包 CLI 在 MVP 需要的能力上都可行，事件流有指令項目，不需要新套件。SDK 原本的優勢沒有成立：公開介面不提供逐次核准、中斷後子程序仍在執行、事件流沒有看到指令與檔案變更。和第 16 筆（Claude Code 用 SDK）方向不同，是因為兩家 SDK 的實際能力不同。細節見 `codex-notes.md` |
| 49 | `TurnResult` 加上 `usage_raw`，保留 agent 回報的原始用量 | 只存輸入與輸出兩個 token 數 | 兩種 agent 都另有快取 token，Codex 還有推理 token。只存兩個數字的話之後無法自己估算花費 |
| 50 | 任務加上以 token 計的上限，和花費上限並存，任一到頂就不再自動修復 | 只有花費上限 | Codex 不回報花費，花費上限對它不生效。原本只剩次數與時間上限擋著，加上 token 上限多一道 |
| 51 | `PermissionPolicy` 改成三部分：sandbox 等級、是否允許連網、由各 adapter 自行解讀的工具與指令限制；`capabilities` 加上 `can_restrict_network`（修正第 34 筆的結構） | 允許的工具與指令清單 + sandbox 等級 | Codex 沒有工具允許清單，它有的是 sandbox 等級、網路開關、指令前綴規則。前兩項可以跨 agent 統一，第三項只能各自表述 |
| 52 | Agent 的推理與計畫更新記成 `agent_message`，在 payload 用 `kind` 區分 | 新增事件型別 | 少一個型別，查詢稽核紀錄時也比較單純 |
| 53 | 規格不假設 agent 會自己 commit；沒有 commit 的變更一律由 supervisor 在收尾時補 | 假設 agent 會在 worktree 內 commit | 實測 Codex 在 sandbox 下無法在 worktree 裡 commit |

## 尚未決定

- 是否公開 repo、用哪個授權（Apache-2.0 或 MIT）
- CLI 指令名稱（暫定 `asv`）
- 花費上限、token 上限與單一 run 最多輪數的預設值
- token 上限要不要把快取的輸入 token 算進去：實測 Codex 的輸入有 85% 到 95% 是快取，全部計入的話上限要設得很高（任務 1 看過 Claude Code 的用量資料後決定）
- 階段 1 允許清單的預設內容（任務 1 調查 SDK 的權限控制後決定）
- Supervisor 補 commit 時的 commit message 格式
- PR 上完全沒有 CI 檢查（`ci_state` 為 `none`）時要算通過還是停在 In review
- GitHub 上取消、逾時、略過等檢查結果如何對應到 `ci_state`（任務 3 調查後決定）
- 單一 run 執行時間上限的預設值
- Daemon 突然死掉後，如何找到並停掉遺留的 agent（任務 1 調查後決定）
- 多人使用與登入要不要做（階段 7 決定）
- Claude Code 能不能落實「是否允許連網」這項政策（任務 1 調查後決定）
- 重啟後確認不了遺留程序已停止的任務：run 會維持未結束、卡片一直顯示 Working，目前只在啟動訊息中列出，沒有在工具內解除的方法。要不要加一個手動結案的操作（任務 1 實作時決定）
- Agent 的登入方式：訂閱方案或 API key。SDK 文件提到不允許第三方產品提供 claude.ai 登入；自己在本機用訂閱登入是否可行還沒查證，任務 1 要確認，結果可能影響第 16 筆
- 任務花費已超過上限時，使用者手動送訊息要不要放行
- 階段 5（治理）與階段 6（自製 agent）的先後（MVP 完成後評估）
