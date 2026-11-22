# 現行的設計決定

最後更新：2026-11-22

目前仍然有效的決定，依主題整理，每條一兩句。括號裡是 `../history/decision-log.md` 的編號，想知道當時考慮過什麼、為什麼沒選其他方案，去那裡查。被推翻的決定不列在這裡。

**新增決定的規則**：每一筆仍然加在 `../history/decision-log.md`（只增不改）；如果它改變了現行的做法，同時更新這份文件。

## 架構與技術選型

- 後端 Python 3.13+，用 `uv` 管理；下限暫不降低（63、164）。
- FastAPI daemon，背景工作與 API 在同一個 process；同時只允許一個 daemon（42）。
- SQLite，不用 ORM；全程一條連線，所有寫入由 `db.py` 的一把鎖排隊，交給外部的讀取也先拿鎖（6、78、98）。
- 加欄位時在啟動時用 `PRAGMA table_info` 檢查並補上；讀任務時略過不認得的欄位（127、133）。
- GitHub 的狀態用輪詢（30 秒），不用 webhook（7）。
- 前端 React + Vite + TypeScript，樣式用 Tailwind；不裝路由與資料抓取的套件，元件庫要用時再提出；Shell 會加 xterm.js（109、175）。
- daemon 在 `/` 提供 build 好的前端（`src/agent_supervisor/web_dist`），API 都在 `/api` 底下；開發時 Vite 把 `/api` 轉給 daemon（110、111、163）。
- 支援 macOS 與 Linux，不支援 Windows（144）。
- 模組的分工：列舉值在 `enums.py`、重啟復原在 `recovery.py`、準備與收尾在 `stages.py`、外部指令的底層在 `process.py`、檢查結果的歸類在 `github.py`、log 的截取在 `ci_log.py`（60、70、80）。

## Agent 與 adapter

- Runner 只透過 adapter 控制 agent；介面以「一輪對話」為單位，adapter 如實宣告 `capabilities`（9、18）。
- Adapter 用 entry point（`agent_supervisor.adapters`）註冊，內建的兩個也一樣；載入失敗寫 log 後跳過，名稱重複時內建的優先（165）。
- 範例 adapter 在 `examples/example-adapter/`，是獨立的套件、預設不載入（166）。
- 用量：`TurnResult` 保留原始的 `usage_raw`；Runner 傳入上一輪的用量，由 adapter 相減出單輪（49、56）。
- 遺留程序用 `session_started` 事件裡的 `process_marker` 辨識（57）。
- 新 session 的 id 等 agent 確認建立後才保存（62）。
- **Claude Code**：用官方 Python Agent SDK（16）。只載入專案層級的設定（54）；權限政策用「工具呼叫前的 hook」落實，核准回呼只負責放行（61）。
- **Codex**：用 subprocess 包 `codex exec`（48）。指示從 stdin 送入（91）；在自己的 session 裡執行，中止送 SIGINT，等不到再強制終止整棵程序樹（92）；`process_marker` 用輸出檔的檔名（90）；沒看過的事件種類原樣記成 `tool_call`（93）。
- 能不能用的檢查：daemon 啟動時檢查一次並記下，設定頁可以重新檢查；每項限時 30 秒，出錯當成不能用（158、168）。沒有 agent 能用時照常啟動（159）；開任務的 API 不檢查，由表單過濾（160）；「已登入」只表示有設定憑證，不驗證（161）。

## 任務、run 與 worktree

- 任務和它的第一個 run 在同一個交易內建立（47）。
- 每個任務同時最多一個未結束的 run；建立 run 與更新防迴圈欄位在同一個交易內（32）。
- 一個 run 由同一個可取消的工作負責到結案；執行時間從建立時起算；Stop 不回滾已完成的動作（39）。
- 建立 run、Stop、封存共用每個任務一把鎖（40）。取消前先等工作真的開始；已經要求取消的不再取消第二次（82、88）；API 的這幾個請求用 `asyncio.shield` 包住（101）。
- Worktree 放在資料目錄的 `worktrees/<專案名>/task-<編號>`，從 fetch 之後的遠端預設分支切出；branch 叫 `asv/task-<編號>-<標題>`（65、66、67）。
- 同一時間只進行一個 `git fetch`（132）。
- 建 worktree 或封存做到一半中斷時，下一次接著完成（154）。

## 發布：commit、push、開 PR

- push 與開 PR 由 supervisor 做；agent 正常結束的每一次 run 都做收尾，其他結束方式不發布（22）。
- 不假設 agent 會 commit，沒 commit 的變更在收尾時補 commit，訊息固定；PR 標題用任務標題（53、68）。
- 收尾的每一步先查現況再做，可以安全地重做（37）。
- Agent 改寫了已 push 的 commit：遠端仍是我們上次 push 的就用 `--force-with-lease` 覆蓋；有別人的 commit 就不覆蓋，run 以錯誤結束（156）。

## 看板與狀態

- 欄位由 run / PR / CI / review 狀態推導，不存資料庫（8）。PR 合併或關閉的任務進 Archive 區（25）。
- 專案可設 `require_review`，預設否（24）。
- CI 檢查的歸類：略過與中性算通過；取消、逾時、啟動失敗算失敗；其他沒看過的值算還沒結束；沒有任何檢查時不算通過（72、73）。
- `merge_state` 是 `unknown` 時不進 Ready to merge，也不蓋掉同一個 commit 上已知的答案（130）。
- Poller 丟棄過期的資料：查詢期間 `head_sha` 變了、或 GitHub 回報的是我們更早 push 過的 commit（PR 還開著時）（41、83、84）。
- `ci_status` 事件只在狀態有變化時寫（76）。
- 已封存但 PR 還開著的任務繼續查；PR 關閉後重新開啟，每 10 輪查一次最近 14 天內的，送訊息時即時查（118、155）。

## 自動處理與上限

- **觸發條件**：PR 開著、任務未封存、最後一次 run 是 `success`，才會自動開新的 run；Stop、中斷、出錯之後不自動開（29、31）。
- **不重複處理**：CI 失敗以 commit 為單位（`last_handled_head_sha`）；衝突另外記（`last_handled_conflict_head_sha`）；留言記到事件編號（`last_handled_feedback_event_id`）（23、122、129）。
- **次數**：CI 修復與解衝突合計 3 次（`ci_fix_attempts`）；留言的自動處理 5 次（`feedback_attempts`）；PR 上的留言不會讓它們歸零，只有人送訊息才會（12、77、124、129）。
- **預算**：任務有花費與 token 兩種上限，token 包含快取；從最近一次人介入起累計，送訊息不會因預算被拒（11、50、55、157）。
- **單一 run**：輪數與花費由 adapter 在執行中擋下；執行時間由 Runner 計時（26、33）。
- 這些規則只決定能不能再自動處理，不影響成果能不能合併（43）。
- 花費是估算值，可以是未知（27）。
- CI log：抓 log 在 `ci_failure` run 的準備階段；每個失敗的檢查保留尾段，合計 20,000 字元（79、81）。

## PR 留言

- 觸發條件是「PR 上有新的留言」，不是 GitHub 的 Request changes（119）。
- 用 `gh pr view` 判斷有沒有新的，有才用 REST 查完整內容；每則留言寫成一筆 `review_comment` 事件（121、122）。
- 同一輪看到的留言下一輪才派工，合併成一次（123）。
- 不餵回：核准的 review、機器人的留言、作者不是 `OWNER` / `MEMBER` / `COLLABORATOR` 的留言；都照樣記成事件並標上原因（126、171）。
- 處理過留言之後，`changes_requested` 不再擋住流程；沒處理過時，reviewer 要求修改會讓卡片進 Needs you、不自動修 CI（45、125）。

## Merge conflict

- 用 merge 不用 rebase；supervisor 開始合併並 commit，agent 只改檔案（128）。
- 收尾時 worktree 停在合併到一半，不論 run 的原因都檢查衝突標記，有標記就不 commit、不 push；封存時放棄解到一半的合併（131）。

## 權限、信任與復原

- 權限政策是基本防護，不是安全邊界，只用在信任的 repo（28、34）。
- 政策分三部分：sandbox 等級、是否允許連網、各 adapter 自行解讀的工具與指令限制（51）。
- 寫檔與編輯的目標路徑必須在 worktree 內；拒絕背景執行的指令（64、69）。
- Codex 允許連網時擋不住 `git push`，記成已知限制（89）。
- Stop 要停掉 agent 與所有子程序並確認結束（35）。
- 封存：未 commit 的變更先 commit 到 branch（不 push）再清 worktree；PR 還開著時要先確認；封存不關 PR、不能取消封存（38、117）。
- 重啟復原：未結束的 run 標成 `interrupted`、不自動重跑；先停掉遺留程序才放行；確認不了的用 Stop 再停一次（29、42、153）。
- API 每個請求檢查 `Host` 與 `Origin` 是本機（104）。

## API、即時更新與通知

- 錯誤一律是 `{"error": {"code", "message"}}`，衝突 409、不存在 404、不合法 422（95）。
- SSE 分 `event`（可補送）與 `task`（卡片最新內容，不補送）兩種；變化由 commit 後比對每個任務的指紋偵測；全部讀成功才推進游標（96、97、108）。
- 前端的 SSE 出錯時自己重開，連上後重抓看板、詳情頁從最後一筆事件補抓（113）。
- Daemon 自己處理 SIGINT、SIGTERM、SIGUSR1；關閉時最多等連線 3 秒；背景輪詢出錯記下來、下一輪照常（100、102、107）。
- 加入專案的路徑必須是 repo 的最上層；已加入過的回 409，不改設定（103、105、106）。
- Diff 從本機的 git 用三個點的寫法取得，前端自己拆成檔案與行，不裝套件（136、137）。
- 卡片剛進 Needs you 時發系統通知（macOS `osascript`、Linux `notify-send`），在推送卡片的地方判斷（134、135、152）。

## 介面

- 卡片不能拖曳（欄位是推導出來的）。
- 版面最初參考 Agent Orchestrator 的看板，只參考資訊的配置，不複製它的程式碼、圖示與視覺設計（170）。
- 合併在 GitHub 上進行，看板內合併排在階段 8（114、174）。
- 訊息框與開任務的描述欄用 `Cmd+Enter` / `Ctrl+Enter` 送出，輸入法選字中不送出（178）。
- 階段 5：UI 改版由 Codex 透過 agsv 實作，只改外觀，排在 Chat 之後；任務詳情頁加入 Chat 與 worktree 裡的 Shell，Shell 永遠不控制 agent（173、176、180）。
- 任務詳情頁標出每一則內容的來源（你、reviewer、自動、agent、系統）與所屬的 run（180）。

## 開源、安裝與發布

- 定位：先做「讀得懂、可以自己接 agent」，再補功能的廣度；差別在硬上限、只增不改的紀錄、先確認再派工（142）。
- 授權 Apache-2.0。對外的東西用英文，`docs/` 的設計文件用中文（143）。
- 第一版支援 Claude Code 與 Codex（141）。Claude 預設引導使用者用 `ANTHROPIC_API_KEY`，沿用本機登入不擋；工具不處理任何憑證（140）。
- 指令叫 `agsv`，不保留 `asv`；內部識別字（branch 前綴、程序標記）不改（147、162）。
- 不發布到 PyPI；主要的安裝方式是 GitHub Release 附的套件檔，前端由 CI build 好打包進去（148、163）。
- `0.x` 期間 adapter 介面、HTTP API、設定檔與資料庫 schema 都不承諾穩定（146、167）。
- 對外的英文文件在 `docs/guide/`；HTTP API 的說明手寫（167）。
- 資安問題只收 GitHub 的 private vulnerability reporting（169）。Release 說明是手寫摘要加自動產生的 PR 清單（172）。

## 開發方式

- 一次做一個任務，合併之後才開始下一個；每個任務先提計畫，確認後再動手（150）。
- 階段的順序見 `roadmap.md`（174）。主 agent 拆完要人確認才派工（177）。
- 文件分成 `design/`（現在的樣子）、`history/`（決策紀錄與規劃的過程）、`guide/`（對外的英文文件）；任務單在階段做完後刪掉，舊版在 git 的 tag 裡（179）。

## 尚未決定

**階段 5 之後**
- 桌面殼用 Tauri 還是 Electron；要不要申請 Apple Developer 帳號（階段 7）
- run 執行中送出的訊息要排隊還是維持拒絕；Shell 的指令要不要記進事件紀錄、一個任務能不能開多個 shell（階段 5）
- 淺色主題要不要做（UI 需求文件）
- 主 agent 開任務的方式、同時執行數上限的預設值（階段 6）
- 什麼才算 `1.0`；多人使用與登入要不要做（階段 7 之後）
- 看板內合併時，合併方式（merge / squash / rebase）要不要可選（階段 8）

**預設值**（程式裡是暫定值）
- 花費 5 美元、token 2,000 萬、單一 run 50 輪與 30 分鐘、CI log 20,000 字元、輪詢 30 秒
- 允許清單：允許讀檔、搜尋、寫檔、編輯、執行指令、待辦清單；拒絕以 `git push`、`gh`、`sudo` 開頭的指令

**已知的問題與限制**
- Claude Code 的 sandbox 設定沒有生效，原因未明；查明之前 Claude Code 任務無法限制連網與寫入範圍（見 `claude-sdk-notes.md`）
- Codex 會載入使用者自己的設定與 MCP 工具，是否受 sandbox 限制沒有驗證
- Codex 任務裡 `gh` 能不能用、用 API key 登入時行為是否相同、推理 token 是否已含在輸出 token 裡，都沒有查
- 資料目錄在符號連結之下（例如 `/tmp`）時，寫檔路徑檢查可能誤擋
- 重啟復原強制終止 agent 後，沒有確認它的子程序也消失
- 有人把 branch 強制退回到我們更早 push 過的 commit 時，Poller 會一直當成過期資料丟棄（83 的代價）
- 手動取消 CI 會被當成失敗，花掉一次自動修復；準備階段發現 CI 已不再失敗時，run 以 `error` 結束、次數不退回
- `agsv stop` 會中止所有 run、不能指定任務，也不暫停派工；和同時啟動的 `agsv start` 可能搶鎖
- `agsv list` 對 Codex 任務顯示 `cost~$0.00`，實際是未知
- Agent 自己 `git add` 衝突標記時收尾攔不到；本機合併沒衝突但 GitHub 說有時，run 以錯誤結束
- 留言被編輯後不會再餵回；任務第一次被查到時 PR 上原本的留言也會餵回；工具不回覆、不解決 PR 上的留言
- 「處理過留言後 `changes_requested` 不再擋住流程」（125）沒有實測
- 點通知不會開啟看板，也不知道使用者是否關掉了通知
- Diff 只有已 commit 的內容
- PR 結束後 worktree 不會自動清掉（排在階段 8）；專案設定只能用 CLI 的 `project add` 改
- 規模：每次 commit 重算所有任務的指紋、斷線重連的補送沒有上限、`GET /tasks` 與 `GET /usage` 逐任務查詢
- 本機上的其他程式仍可直接呼叫 API（沒有登入，只檢查 `Host` 與 `Origin`）
- daemon 與前景指令各有一份取得鎖、復原、關閉的流程，改一邊要記得改另一邊；連接埠被占用時結束碼是 uvicorn 的 3
- 實測 Claude Code 通常不自己 commit，PR 裡幾乎都是 supervisor 補的固定訊息
- 要不要向 Anthropic 詢問沿用本機訂閱登入需不需要許可；OpenAI 講開源應用程式使用 ChatGPT 方案的文件沒讀到
