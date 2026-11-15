# 04 · 階段 1 與 1.5 任務單

最後更新：2026-11-15

一個事前調查、階段 1 的三個任務與階段 1.5 的一個任務。一次只給 Claude Code 一個，做完、測試通過、合併後再給下一個。每個任務都讓它先提計畫，確認後再動手。

---

## 任務 0 · Codex 介面調查（只調查，不實作）

```
請先讀 CLAUDE.md 與 docs/ 下的所有文件。

這個任務只做調查，不寫任何程式碼。

執行 codex --help 與相關子指令的 --help，並查官方文件，確認：
1. 非互動執行與結構化輸出的方式
2. 續接指定 session 的方式，session id 何時可以取得
3. 用量與花費如何回報
4. 有沒有輪數、花費、時間的上限選項
5. 權限、核准與 sandbox 的控制方式
6. 中止執行中的工作的方式

把結果寫進 docs/codex-notes.md，並對照 docs/02-mvp-spec.md 的
AgentAdapter 介面，列出介面或 capabilities 需要調整的地方。

不要修改規格，把建議列給我。
```

**我要驗證的事**
- [ ] `docs/codex-notes.md` 寫了實際查到的用法與出處，不是猜的
- [ ] 它列出的介面調整建議，我確認後已經更新到 `02-mvp-spec.md`

---

## 任務 1 · Adapter + Runner

```
請先讀 CLAUDE.md 與 docs/ 下的所有文件。

實作 docs/02-mvp-spec.md 開發順序的第 1 步：Adapter + Runner。

範圍：
1. 用 uv 初始化專案（src layout、pytest、ruff）
2. 建立 SQLite schema（四張表）與 db.py
3. 定義 AgentAdapter Protocol 與相關型別
   （AdapterCapabilities、RunLimits、TurnResult、AgentEvent、PermissionPolicy）
4. 實作 ClaudeCodeAdapter，使用官方 Python Agent SDK：
   先查官方文件，確認串流訊息、續接 session、中止、
   單次 run 的輪數與花費上限、權限控制的實際用法，
   以及用訂閱方案登入時能不能這樣使用，
   把結果寫進 docs/claude-sdk-notes.md 後再實作
5. 權限政策（見規格「權限政策」）：設定檔裡的允許清單與
   sandbox 等級，adapter 攔得到的清單外動作拒絕並寫入
   permission_denied 事件
6. 實作 Runner：啟動 run、把 adapter 事件寫進 events、
   收到 session 建立事件時立刻保存 session id、
   結束時回填 tokens、cost_usd 與 exit_status；
   每個任務同時最多一個未結束的 run；
   run 的生命週期與每個任務一把鎖（見規格「Run 的生命週期」）；
   執行時間上限從 run 建立時起算；
   中止流程（見規格「中止與封存」）
7. Daemon 啟動時的復原（見規格「Daemon 重啟後的復原」）：
   只允許一個 daemon；先停掉上次遺留的程序，再把沒有 ended_at
   的 run 標成 interrupted。辨識與停掉遺留程序的做法先調查並
   向我提出，確認後實作
8. 提供最小的 CLI 指令：開任務、對任務送訊息、Stop，方便我驗證
9. 測試用 fake adapter，不得真的啟動 agent

不要做：worktree、PR、Poller、Dispatcher、API、前端。

開始前先給我你的實作計畫，我確認後再動手。
```

**我要驗證的事**
- [ ] `uv run pytest` 與 `uv run ruff check .` 都通過
- [ ] 用 CLI 指令真的跑一個簡單任務（例如「建立 hello.txt」），`events` 表有紀錄
- [ ] `runs` 表有 token 數與花費
- [ ] `docs/claude-sdk-notes.md` 寫了實際查到的用法，不是猜的
- [ ] Runner 裡沒有任何 Claude Code 專屬的字串
- [ ] 讓 agent 做一件清單外的事，確認被拒絕且有 `permission_denied` 事件
- [ ] 把輪數上限調得很低，確認 run 在執行中被擋下，`exit_status` 是 `limit_reached`
- [ ] run 執行中把程式強制關掉再啟動，確認遺留的 agent 程序已經不在、該 run 變成 `interrupted`
- [ ] daemon 已經在跑的時候再啟動一個，確認第二個直接結束
- [ ] 對一個被中止的任務再送訊息，確認續接的是同一個 session
- [ ] 按 Stop 後確認 agent 與它啟動的子程序都真的結束了
- [ ] 把執行時間上限調得很低，確認 run 被中止，`exit_status` 是 `limit_reached`
- [ ] 任務有 run 在執行時再觸發一次，確認被拒絕

---

## 任務 2 · Worktree + run 收尾

```
請先讀 CLAUDE.md 與 docs/ 下的所有文件。

實作 docs/02-mvp-spec.md 開發順序的第 2 步：Worktree 管理與 run 收尾。

範圍：
1. worktree.py：依任務建立 branch 與 worktree、封存時清除
   （封存的條件見規格「中止與封存」）
2. 新增 projects 的建立流程（登記本機 repo 路徑、GitHub repo、
   require_review）
3. 開任務時自動建 worktree，agent 在該目錄下執行
4. run 收尾（見規格「Run 結束後的收尾」）：每次 run 以 success
   結束後補 commit、push、必要時開 PR，寫入 pushed 與 pr_opened
   事件；沒有任何變更時不開 PR；push 或開 PR 失敗時 run 記為 error；
   每一步先查現況再做，重做一次不會重複 push 或重複開 PR
5. push 與 gh 由 supervisor 執行，agent 的允許清單不含這些動作
6. 開任務時，任務與 initial run 在同一個交易內建立；worktree
   建立失敗時該 run 記為 error
7. 測試：用暫存的 git repo 驗證 worktree 建立與清除、收尾的
   四種情況與各步驟失敗的情況；gh 的呼叫用替身，不得真的打 GitHub

不要做：Poller、Dispatcher、API、前端。

開始前先給我你的實作計畫，我確認後再動手。
```

**我要驗證的事**
- [ ] 對 sandbox repo 開一個任務，GitHub 上出現一個新的 PR
- [ ] 對同一個任務再送一則訊息，新的 commit 有被 push 到同一個 PR
- [ ] 開一個不會產生任何變更的任務，確認沒有開 PR
- [ ] 同時開兩個任務，兩個 worktree 互不影響
- [ ] 封存任務後 worktree 目錄被清掉，branch 還在
- [ ] 收尾進行中按 Stop，確認沒有繼續 push 或開 PR，run 是 `stopped`
- [ ] 對已封存的任務送訊息，確認被拒絕
- [ ] worktree 有未 commit 的變更時封存，確認變更被 commit 到 branch 上、沒有被 push
- [ ] 主 repo 的工作目錄沒有被動到

**做這步之前**：先在 sandbox repo 放一個最小的 Python 專案（一個函式、一個測試）和一條跑 pytest 的 GitHub Actions。

---

## 任務 3 · Poller + Dispatcher

```
請先讀 CLAUDE.md 與 docs/ 下的所有文件。

實作 docs/02-mvp-spec.md 開發順序的第 3 步：Poller 與 Dispatcher。

範圍：
1. poller.py：每 30 秒查詢有開 PR 的任務，更新 pr_state、head_sha、
   ci_state 與 review_state，寫入 ci_status 事件。
   先查 gh 實際回傳的檢查結果種類（取消、逾時、略過等），
   向我提出它們如何對應到 ci_state，確認後補進規格
2. 「自動修復條件」純函式，status.py 與 Dispatcher 共用
3. dispatcher.py：任務符合條件時，在同一個資料庫交易內建立
   trigger 為 ci_failure 的 run 並更新 last_handled_head_sha 與
   ci_fix_attempts；交易結束後才擷取失敗 log 的相關片段，
   交給 Runner 續接同一個 session
4. 五條防迴圈規則（見規格「自動修復條件與防迴圈」）
5. status.py：由狀態推導看板欄位與狀態標籤的純函式，
   包含離開看板的情況（封存、PR 已合併或關閉）
6. 一個 CLI 指令列出所有任務與目前推導出的欄位
7. Poller 寫入前比對 head_sha，已變就丟棄結果；PR 被合併或
   關閉時中止該任務正在跑的 run
8. 測試：每條防迴圈規則各一個測試；status.py 用參數化測試
   涵蓋規格表格的每一列、優先順序與交叉組合

不要做：API、前端。

開始前先給我你的實作計畫，我確認後再動手。
```

**我要驗證的事**（也就是階段 1 的完成標準）
- [ ] 在 sandbox 開一個會讓測試失敗的任務，不介入，最後 CI 變綠燈
- [ ] 開一個修不好的任務，確認 3 次後停下來，欄位顯示 Needs you
- [ ] 把花費上限調得很低，確認超過後停下來
- [ ] 同一個 commit 的 CI 失敗沒有被重複餵回
- [ ] 過長的 CI log 有被截取，沒有整份塞給 agent
- [ ] sandbox 專案不要求 review 時，CI 綠燈的卡片顯示 Ready to merge
- [ ] 在 GitHub 上合併 PR 後，卡片離開看板
- [ ] CI 失敗時按 Stop，確認之後沒有自動開新的 run
- [ ] 自動修復進行中送訊息，確認被拒絕，沒有兩個 run 同時執行
- [ ] 第三次自動修復把 CI 修好，確認卡片到 Ready to merge 而不是 Needs you
- [ ] Reviewer 要求修改且 CI 失敗時，確認沒有自動修復，卡片在 Needs you
- [ ] run 執行中在 GitHub 上關閉 PR，確認 run 被中止、之後沒有 push
- [ ] 自動修復沒有產生新 commit 時，確認停下來進 Needs you

---

## 任務 4 · Codex adapter（階段 1.5）

```
請先讀 CLAUDE.md 與 docs/ 下的所有文件。

實作 docs/02-mvp-spec.md 開發順序的第 4 步：Codex adapter。

範圍：
1. 先讀 docs/codex-notes.md（任務 0 的調查結果），確認內容
   對目前安裝的 Codex 版本仍然正確，有出入就更新
2. 依調查結果向我提出做法（包 CLI 或用 SDK），確認後再實作
3. 實作 CodexAdapter，如實填寫 capabilities
4. 介面不合用的地方提出修改建議，不要在 Runner 裡加入
   針對特定 agent 的分支
5. 開任務時可以指定 agent_type
6. 測試不得真的啟動 agent

不要做：API、前端。

開始前先給我你的實作計畫，我確認後再動手。
```

**我要驗證的事**（也就是階段 1.5 的完成標準）
- [ ] 任務 1 到 3 的驗證清單中不依賴特定能力的項目，用 Codex 重跑一次
- [ ] Codex 支援的能力各自通過對應的驗證
- [ ] Codex 做不到的能力，確認降級行為符合規格（例如只靠執行時間上限擋下）
- [ ] Runner、Dispatcher、Poller 沒有因為 agent 種類而分支
- [ ] Codex 做不到的事（如果有）在 `capabilities` 與規格裡都有寫明

---

## 每個階段做完後

回頭更新文件：
- `02-mvp-spec.md`：實作中發現規格要調整的地方
- `03-decisions.md`：新增的決定
- `01-roadmap.md`：下一個階段的範圍是否要調整
