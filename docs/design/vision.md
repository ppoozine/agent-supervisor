# 想做的東西

最後更新：2026-11-22

## 一句話

一個讓多個 AI coding agent 同時工作的本機工具：每個任務各自隔離，CI 失敗自動回到負責的 agent 修復，人只處理例外。

## 服務目標

使用者的角色從「寫程式的人」變成「派工與驗收的人」。

| # | 目標 | 原本的痛點 | 做法 |
| --- | --- | --- | --- |
| 1 | **平行不互踩** | 多個 agent 改同一份工作目錄，檔案互相覆蓋 | 每個任務一個獨立的 git worktree 與 branch，不動使用者自己的工作目錄 |
| 2 | **不用一直盯** | CI 掛了、reviewer 留言了，都得自己複製貼上給對的 agent | 輪詢 PR / CI，把失敗的 log、留言、衝突送回同一個 agent session，修好由工具 push |
| 3 | **一眼看到全貌** | 哪個在跑、哪個卡住、哪個可以合併，散在好幾個 terminal 裡 | 一個看板，欄位由實際狀態推導，不靠手動拖 |
| 4 | **不會失控** | 無人看管的 agent 可能在迴圈裡把用量燒光 | 自動處理的次數、花費、token、執行時間都有上限，到頂就停下來等人 |
| 5 | **事後查得到** | 不知道 agent 做了什麼、花了多少 | 只增不改的事件紀錄 |

目標 1 到 3 解決原本的痛點，4、5 讓人敢放手讓它自己跑。

## 功能

**`0.3` 已經有的**：加入專案、開任務（Claude Code 或 Codex）、每個任務獨立的 worktree、由工具 commit / push / 開 PR、CI 失敗與 PR 留言與 merge conflict 自動送回 agent、各種上限、基本權限政策、事件紀錄與用量統計、Stop 與封存、當機復原、四欄看板與任務詳情（Log、Diff、Events、Runs）、即時更新、系統通知、agent 設定頁、外部 adapter。用法見 `../guide/usage.md`。

**之後要做的**：新介面與每個任務的 Chat、Shell（階段 5），同時執行數上限與主 agent 派工（6），桌面版（7），看板內合併、issue 觸發、接手模式（8），直接打 endpoint 的 agent（9），治理（10）。內容與完成標準見 `roadmap.md`。

## 明確不做

- 卡片拖曳：欄位是推導出來的。
- 雲端代管：是跑在自己電腦上的工具。
- 支援所有 agent 與所有 Git 平台：先 Claude Code、Codex + GitHub。
- 自己做完整的 coding agent：用現成的，直接打 endpoint 的最小 agent（階段 9）只當作第三個 adapter 的示範。
- 多人使用與登入：目前不做，階段 7 之後再決定。

## 一張卡片的生命週期

1. 我開一個任務：描述想要的結果，送出。
2. 工具建立 worktree 與 branch，啟動 agent。卡片在 **Working**。
3. Agent 寫完，工具 push 並開 PR。卡片到 **In review**。
4. CI 失敗 → 工具把失敗 log 餵回同一個 agent，卡片回到 **Working**；修完後工具再 push。
5. 修了幾次還是不過，或超過花費上限 → 卡片到 **Needs you**，等我介入。
6. CI 綠燈（專案要求 review 時還要審查通過）→ 卡片到 **Ready to merge**，我按合併。
7. PR 合併後，卡片離開看板，進 Archive。

對應的狀態圖與流程圖見 `diagrams.md`。

## 定位

一開始的目的是**學習與自己使用**（第 17 筆）。MVP 確認可行之後，方向改成**開源給別人用**（第 142 筆）：先做到別人裝得起來、看得懂、接得上自己的 agent（`0.3` 已完成），再補功能的廣度。

這個領域已經有不少同類工具（Agent Orchestrator、Conductor、Vibe Kanban、Claude Squad 等）。每個任務一個 worktree、CI 失敗自動送回 agent、review 留言自動處理，這些核心迴圈別人都有，功能的廣度目前也是對方領先。

我們的差別在於**讓人敢放手**，新增的功能都不能放掉這幾點：

- **硬上限**：修復次數、留言處理次數、花費、token、執行時間都有上限，到頂就停下來等人
- **只增不改的事件紀錄**：每個 agent 做了什麼、花了多少，事後查得到
- **先確認再派工**：主 agent 拆完任務要人確認才開始（階段 6）
- **可以拿來改**：adapter 介面、HTTP API、CLI 都有文件，每個設計決定都記了理由與沒選的方案

和 Agent Orchestrator 的比較見 `../history/plans/09-open-source-plan.md` 主題 2 與 `../history/plans/11-next-plan.md`（2026-11-22 讀了它的原始碼）。

## 名稱

- 專案：`agent-supervisor`
- 測試用沙盒：`agent-supervisor-sandbox`
- Python import 名稱：`agent_supervisor`
