# 11 · 階段 4 之後的規劃

最後更新：2026-11-22

`0.3` 發布後的檢查點（`../../design/roadmap.md`「做完階段 4 後停下來重新評估」）。`0.3` 當天才公開，還沒有外部使用者的回饋，這次的排序依據是作者自己使用時最常碰到的缺口：介面太陽春、看不到 agent 的對話、不能直接在 worktree 裡動手、一次只能手動開一張單。

這份文件記錄 2026-11-22 的討論，格式照 `09-open-source-plan.md`：一次談一個主題，結論寫回這裡，並在 `../decision-log.md` 記一筆。結果是重排後的 `../../design/roadmap.md`；階段 5 的任務單等這份合併後再寫。

## 討論的主題

| # | 主題 | 狀態 |
| --- | --- | --- |
| 1 | 每個任務的 Terminal 與 Chat | 已決定（決策第 173 筆） |
| 2 | 原本的階段 5（直接打 endpoint 的 agent）怎麼處理 | 已決定（決策第 174 筆） |
| 3 | 這次幾件事的順序與版本號 | 已決定（決策第 174 筆） |
| 4 | 前端套件放寬到哪裡 | 已決定（決策第 175 筆） |
| 5 | UI 改版怎麼交給 Codex | 已決定（決策第 176 筆） |
| 6 | 主 agent 派工、同時執行數上限、桌面版 | 只定方向（決策第 177 筆），細節到各自的階段再談 |

## Agent Orchestrator 的做法（2026-11-22 調查）

`09-open-source-plan.md` 的比較表寫了「內嵌 terminal、聊天：對方有，我們沒有」，但當時只讀了官網，沒有查它的 terminal 是什麼。這次讀了它的原始碼與文件（`OrchestratorInc/agent-orchestrator`，commit `69f6f705`）。只當作設計的參考，不複製程式碼與視覺設計（同 `05-ui-reference.md`）。

**架構**：後端是 Go 的 daemon，桌面版是 Electron + React，支援 40 多種 agent。

**Terminal 有兩種**

- Agent 本身的 TUI（預設）：互動模式的 `claude` / `codex` 跑在 PTY 裡，前端用 xterm.js 經 WebSocket 顯示。
- 普通 shell 分頁：在 worktree 開的 shell，文件稱為 escape hatch，明文規定它不會成為控制 agent 的一方。

**Chat 是另一條結構化的通道**：Claude 走 ACP（建在 Agent SDK 上），Codex 走 `codex app-server`，不是打字進 PTY。

**同一個 session 同時只有一方控制 agent**：每個 session 有 `session_mode`（`tui` 或 `chat`）。切換時先停下原本那一方，用同一個對話 id 續接，以 SQLite 的 compare-and-swap 鎖住；切換期間進來的訊息先放進佇列。

**TUI 模式的代價**

- 送訊息要模擬按鍵（貼上、等待、按 Enter，重送到 hook 確認收到）。
- 要解析畫面，判斷使用者是不是打了一半、是不是停在權限確認的提示，否則自動送進去的 Enter 會替使用者按下「允許」。
- 工具呼叫的事件靠在 worktree 寫入 Claude Code 的 hooks 回報。
- 花費要事後解析 transcript 才知道，執行中擋不了上限。

**主 agent**：一個普通的 agent session 加上很長的 system prompt，只負責協調、不改程式；透過呼叫 `ao spawn` / `ao send` 開任務。派工前不需要人確認；沒找到同時執行數的上限（沒有完全確認）。

**上限**：沒找到花費或 token 的上限；CI 修復的次數沒有上限，只靠失敗的 check 去重複；留言的處理最多 3 次。

**桌面版**：Electron Forge 打包，後端的 Go 執行檔當 sidecar 放在 app 的 Resources 裡；macOS 簽章與公證、Windows 簽章、自動更新都有。

**對我們的意義**：功能的廣度對方仍然領先，但硬上限、只增不改的事件紀錄、派工前先確認，正好是對方沒有做的。新增的功能都不能放掉這三件事。

## 主題 1 · 每個任務的 Terminal 與 Chat（決策第 173 筆）

考慮過的三種：

| | 內容 | 問題 |
| --- | --- | --- |
| A. 給人用的 shell + Chat | Shell 開在任務的 worktree，給人跑指令、看檔案；Chat 是現有的 SDK / `codex exec` session | 要新增 PTY、WebSocket、xterm.js |
| B. Agent 本身的 TUI | 在 PTY 裡跑互動模式的 agent | 不經過 adapter：事件紀錄、上限、權限攔截、由 supervisor 發布（設計規則 2、4、7、8）都失效或要重新設計 |
| C. 兩種都要 | A 為主，B 當成「接手模式」，接手期間自動處理暫停 | B 的問題仍在，只是限縮在接手期間 |

**結論：C，分兩次做。** 先做 A（階段 5），B 等 A 實際用過、確定 Chat 不夠用再決定（列在階段 8）。

理由：B 唯一補上的是「在 agent 的原生介面裡操作它」，代價是那段期間放掉上限與稽核，正好是我們和同類工具的差別。A 先把 PTY、WebSocket、xterm.js 的底層做好，B 之後直接沿用，分兩次做不會浪費。

### A 的範圍（細節在階段 5 的任務單）

**Chat**

- 把詳情頁的 Log 改成對話的樣子：使用者的訊息、agent 的訊息、工具呼叫分開呈現。資料仍然來自 `events` 表，不另存一份。
- 一則訊息仍然對應一次 run，走現有的「追加指令」流程與防迴圈規則。
- run 執行中送出的訊息要不要排隊（現在是拒絕，回 409），在任務單裡決定。排隊的話，人送的訊息每則各開一次 run，參考 AO 的做法。
- `Cmd+Enter` / `Ctrl+Enter` 送出，只按 Enter 換行，輸入法選字中不送出（#35 已在現有介面做到，改版時要保留）。

**Shell**

- 在任務的 worktree 開 PTY，經 WebSocket 接到前端的 xterm.js。一個任務能不能開多個分頁，在任務單裡決定。
- 永遠不是控制 agent 的一方：不寫進 agent 的 session，也不經過 adapter。
- 在 shell 裡執行的指令預計不進 `events` 表：它是人的操作，不是 agent 的動作。在任務單裡確認，確認後寫進 `docs/guide/trust-model.md`。
- 任務封存、worktree 被移除時，關掉這個任務的所有 shell。
- **安全**：內嵌 shell 等於透過 HTTP 執行任意指令。WebSocket 一定要檢查 `Origin` 與 `Host`，沿用 `api/guard.py` 的規則；不然任何網頁都能連進使用者的 shell。

## 主題 2、3 · 順序與版本號（決策第 174 筆）

原本排在下一個的階段 5（直接打 endpoint 的 agent）往後挪。它的價值是「示範 adapter 怎麼寫」，但 `0.3` 才剛發布，還不知道有沒有人需要這個示範；這次的幾件事是自己使用時就會碰到的缺口。

| 階段 | 名稱 | 內容 | 版本 |
| --- | --- | --- | --- |
| 5 | 新介面 | 裝 Tailwind、UI 改版（Codex）、Chat + Shell | `0.4` |
| 6 | 主 agent 派工 | 同時執行數上限與排隊、主 agent（拆完要人確認才派工） | `0.5` |
| 7 | 桌面版 | 桌面殼、Python sidecar、簽章 | `0.6` |
| 8 | 補功能的廣度 | 合併按鈕、專案設定頁、PR 合併後清掉 worktree、從 issue 開任務、接手模式（B） | 未定 |
| 9 | 直接打 endpoint 的 agent | 原本的階段 5，內容不變 | 未定 |
| 10 | 治理 | 原本的階段 8，內容不變 | 未定 |

排序的理由：

- **UI 改版在 Chat + Shell 之前**：資訊架構先定好，Chat、Shell、主 agent 的位置都先留好，再交給 Codex 實作；否則改完版加新功能時又要重排一次。
- **同時執行數上限在主 agent 之前**：主 agent 一次拆出很多任務，沒有上限會同時燒掉大量用量（第 149 筆的理由不變）。
- **桌面版排在功能之後**：它只是外殼，功能定了再包，不用在功能變動時一直重包。

和原本 roadmap 的差別：

- 原本的階段 6 拆開了：同時執行數上限併進新的階段 6，其餘（合併按鈕、專案設定頁、清 worktree）留在階段 8，不塞進 UI 改版。
- 原本階段 7 的「從 issue 開任務」與 webhook 移到階段 8；新的階段 6 只做主 agent，範圍收小。
- `1.0` 不再綁在桌面版上，等之後再決定什麼才算 `1.0`。

仍然一次做一個任務、合併後才做下一個（第 150 筆）。

## 主題 4 · 前端套件（決策第 175 筆）

第 109 筆「不裝路由、資料抓取、樣式的套件」是 MVP 時為了「介面只求可用」定的。現在要認真做介面：

- **加 Tailwind**：只在建置時用，不增加執行時的依賴；Codex 與 Claude 寫 Tailwind 都很順，改版的速度快很多。
- **加 xterm.js**：Shell 需要，階段 5 加入。
- **仍然不裝**路由與資料抓取的套件：畫面仍是單頁，資料仍是「先抓一次，之後靠 SSE 更新」。
- **元件庫**（Radix、shadcn/ui 等）現在不裝，改版時真的需要再提出來討論。

## 主題 5 · UI 改版怎麼交給 Codex（決策第 176 筆）

用 agsv 自己開 Codex 的任務來做，順便在真的 repo 上驗證留言回饋的迴圈：

1. **先開一個小 PR 裝好 Tailwind**（由 Claude 做）。Codex 在 agsv 裡的 sandbox 可能不允許連網，不能自己 `npm install`。
2. **寫 UI 需求文件**（由 Claude 寫，作者確認後才動工）：版面、每一頁要放什麼、Chat / Shell / 主 agent 的預留位置、design tokens。淺色主題要不要做，在需求文件裡決定。
3. **第一張 Codex 任務只做到「靜態設計稿等級」**：design tokens，加上看板與任務詳情頁的外觀，不改資料流與 API 的形狀。作者確認視覺後，再開第二張任務補其他畫面。
4. **驗收**：作者看截圖或跑 `npm run dev`，有意見直接在 PR 留言，agsv 會自動餵回 Codex。Claude 做 code review，確認沒有違反設計規則（例如看板欄位不存資料庫、API 形狀不變）。

不複製 Agent Orchestrator 的視覺設計（`05-ui-reference.md`）。

## 主題 6 · 只定方向的部分（決策第 177 筆）

**主 agent（階段 6）**

- 每個專案一個主 agent，給它一個目標，它拆成多個任務。
- 第一版拆完要人確認才派工（第 14 筆與原本階段 7 的看法不變）。和 AO 的差別也在這裡。
- 拆出來的任務走現有的流程：每個任務仍然有自己的 worktree、上限與事件紀錄。
- 主 agent 怎麼開任務（自訂工具、呼叫 `agsv` CLI、或吐出結構化的計畫交給人確認）在階段 6 的任務單裡比較。

**同時執行數上限（階段 6）**

- 全域上限加排隊。預設值多少、要不要依 agent 分開算，到時再定。

**桌面版（階段 7）**

- 第 5 筆選了 Tauri；AO 用的是 Electron。Python 的 daemon 怎麼包成 sidecar 是主要的工作量。
- macOS 的簽章與公證要 Apple Developer 帳號（一年 99 美元）。不簽的話，第一次打開會被 Gatekeeper 擋下，達不到「不會用 terminal 的人也裝得起來」。

## 尚未決定

- 桌面殼用 Tauri 還是 Electron；要不要申請 Apple Developer 帳號
- run 執行中送出的訊息要排隊還是維持拒絕（階段 5 的任務單）
- Shell 的指令要不要記進事件紀錄、一個任務能不能開多個 shell（階段 5 的任務單）
- 淺色主題要不要做（UI 需求文件）
- 主 agent 開任務的方式（階段 6 的任務單）
- 同時執行數上限的預設值（階段 6 的任務單）
- 什麼才算 `1.0`
