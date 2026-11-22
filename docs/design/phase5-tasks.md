# 階段 5 任務單

最後更新：2026-11-22

階段 5「新介面」的七個任務，對應 `roadmap.md` 的階段 5。規劃的過程見 `../history/plans/11-next-plan.md` 主題 1、4、5，現行的決定見 `decisions.md`。階段做完後這份任務單會刪掉（決策第 179 筆）。

這個階段的目標是：**介面從「只求可用」變成每天願意用的樣子；在一個任務裡就看得懂 agent 做了什麼、是誰叫它做的，也能自己在 worktree 裡動手。** 做完發 `0.4.0`。

做法：**一次做一個任務，合併之後才開始下一個**（第 150 筆）。每個任務一個 branch、一個 PR，開始前先提計畫、確認後再動手。任務 22、23 交給 Codex，透過 agsv 開任務執行（第 176 筆），其他由 Claude Code 做。

順序的理由：

- Tailwind 最先裝好，Codex 在 agsv 的 sandbox 裡可能不能連網（第 176 筆）。
- UI 需求文件定下每個畫面的資訊架構，後面的任務都照它做。
- **Chat 排在 Codex 的外觀改版之前**（第 180 筆）：Chat 會把 Log 分頁換成對話的時間軸。如果先改外觀，Codex 會花力氣美化一個馬上要被換掉的 Log；先把結構做出來，外觀改版就直接套在最終的結構上。Chat 這一步只求結構正確、樣式用需求文件的 tokens 即可。
- Shell 放在外觀改版之後，直接用新的版面，不必改兩次。

---

## 任務 19 · 裝 Tailwind

```
請先讀 CLAUDE.md 與 docs/README.md，再讀 docs/design/ 下的文件。

在 web/ 裝好 Tailwind，讓之後的任務可以直接用（決策第 175 筆）。

範圍：
1. 依 Tailwind 官方文件裝目前的穩定版，接到 Vite。不要憑印象寫
   設定，裝之前先查官方文件確認做法，版本與做法寫在 PR 描述
2. 現有的 styles.css 保留，畫面要和現在完全一樣。特別注意
   Tailwind 的基礎樣式（preflight）會重設瀏覽器的預設樣式，
   要確認它不會改變現有的畫面；會的話提出處理的方式
3. 用一個很小的地方證明 Tailwind 的 class 真的生效（例如
   一個不影響外觀的工具 class），之後的任務會移除或沿用
4. npm test、npm run build 都要過；build 的產物大小前後寫在
   PR 描述
5. 更新 CLAUDE.md 不歸你，在 PR 描述提醒我前端規則要改

不要做：改任何畫面、加元件庫、建 design tokens（那是任務 20、22）。
```

**我要驗證的事**
- [ ] `npm run dev` 打開看板、任務詳情、設定頁，和改之前看起來一樣
- [ ] PR 描述寫了 Tailwind 的版本、接法，以及 preflight 的處理

---

## 任務 20 · UI 需求文件

```
請先讀 CLAUDE.md 與 docs/README.md，再讀 docs/design/ 下的文件
與 docs/history/plans/11-next-plan.md。

寫 docs/design/ui.md：新介面的需求文件。任務 21 到 24 都照它做，
所以它要具體到 Codex 拿到就能做、我看了就知道長什麼樣子。

範圍：
1. 整體版面：側欄、看板、任務詳情頁、設定頁、開任務與加入專案
   的對話框。每個畫面列出要顯示的資料（對應 API 的欄位）與
   能做的操作
2. 任務詳情頁重新設計，這是這份文件的重點：
   - 上方：標題、狀態、PR、branch、花費與 token、各種次數
     （fixes n/3、comments n/5）、Stop 與 Archive
   - 主區：Chat（對話的時間軸）、Diff、Events、Runs、Shell
     （任務 24 才做，先留位置）怎麼擺：分頁或分割畫面，提出
     建議並說明理由
   - 下方：輸入框，Cmd/Ctrl+Enter 送出（第 178 筆，要保留）
3. 訊息來源要一眼分得出來。送給 agent 的內容有四種來源，
   加上 agent 自己與系統事件：
   | 來源     | 資料                                         |
   | 你       | runs.trigger 是 initial、user_message         |
   | Reviewer | review_comment 事件（作者、檔案與行號）與      |
   |          | trigger 是 review_feedback 的 run             |
   | 自動     | trigger 是 ci_failure、merge_conflict 的 run   |
   |          | （supervisor 送出的指示，含 CI log）           |
   | Agent    | agent_message、tool_call、command 事件        |
   | 系統     | pushed、pr_opened、ci_status、state_change 等  |
   每一種的外觀（位置、顏色、標籤、圖示）、哪些預設收合
   （例如很長的 CI log、工具呼叫的輸出、agent 的推理），
   以及「沒送給 agent 的留言」（ignored）怎麼標示。
   每一則都要看得出是哪一次 run
4. Design tokens：顏色、字級、間距、圓角，用 Tailwind 的設定
   表達。淺色主題要不要做，提出建議讓我決定
5. 預留位置：Shell 分頁、主 agent 的入口（階段 6）放在哪裡，
   這個階段只留位置不做功能
6. 每個主要畫面畫一張線框圖（ASCII 即可）。任務詳情頁另外
   做一份靜態的 HTML 草稿 docs/design/ui-mockup.html（單一檔案、
   不接 API、用假資料，涵蓋每一種訊息來源），讓我在瀏覽器看，
   也讓任務 22 的 Codex 照著做
7. 不複製 Agent Orchestrator 的程式碼、圖示與視覺設計
8. 文件寫完先給我看，我確認之後才合併

不要做：寫任何 web/ 底下的程式。
```

**我要驗證的事**
- [ ] 看 HTML 草稿就能分出每一則訊息是誰說的、屬於哪一次 run
- [ ] 文件裡每個畫面都對得到 API 的欄位，沒有需要新 API 才做得到的東西（需要的話明確列出來）
- [ ] 淺色主題、詳情頁的分頁或分割畫面已經決定

---

## 任務 21 · Chat：對話的時間軸

```
請先讀 CLAUDE.md 與 docs/README.md，再讀 docs/design/ 下的文件，
特別是 ui.md 的任務詳情頁與訊息來源。

把任務詳情頁的 Log 分頁換成 ui.md 定義的 Chat 時間軸。

範圍：
1. 依 ui.md 把每一則內容標出來源（你、Reviewer、自動、Agent、
   系統），並看得出屬於哪一次 run
2. 資料只用現有的 API（任務詳情的 runs 與事件），不另存一份；
   需要改 API 的話先提出來
3. 送出訊息的流程不變：一則訊息仍然是一次 run，走現有的追加
   指令與防迴圈規則。Cmd/Ctrl+Enter 送出要保留
4. run 執行中送出的訊息：現在是拒絕（409）。比較「維持拒絕」與
   「排隊，run 結束後依序各開一次 run」兩種做法（排隊要考慮
   Stop、出錯、預算、daemon 重啟時佇列怎麼辦），提出建議讓我
   決定；決定後記進決策紀錄
5. 樣式用 ui.md 的 tokens，結構對就好，細緻的外觀是任務 22 的事
6. 前端的測試：每一種來源都標得正確、新推送的事件會出現在
   對的位置、長內容預設收合
7. 更新 docs/guide/usage.md 對應的段落

不要做：Shell、其他畫面的改版、改 events 表。
```

**我要驗證的事**
- [ ] 開一個任務、送一則追加訊息、在 PR 留一則言、讓 CI 失敗一次，Chat 裡分得出這四種來源，以及各自屬於哪一次 run
- [ ] 訊息排隊的做法已經決定，並記進決策紀錄

---

## 任務 22 · 外觀改版：看板與任務詳情頁（Codex）

由 Claude 在 agsv 開一張 Codex 的任務，指示如下。Codex 的 PR 由 agsv 開出，我看畫面後在 PR 上留言，agsv 會把留言餵回 Codex；Claude 做 code review。

```
Read CLAUDE.md and docs/README.md, then docs/design/ui.md and the static
mockup docs/design/ui-mockup.html.

Restyle the board and the task detail page to match docs/design/ui.md
and the mockup, using Tailwind and the design tokens defined there.

Scope:
1. Design tokens in the Tailwind configuration, as listed in ui.md
2. Board: sidebar, columns, cards, the Archive area
3. Task detail page: header, tabs, the Chat timeline, Diff, Events,
   Runs, the message box
4. Keep every existing behavior, data flow, API call and keyboard
   shortcut. Do not change anything under src/ (the Python backend)
5. Keep the existing accessible names (labels, roles) so the current
   tests keep passing; npm test and npm run build must pass
6. Code comments in English

Do not: change the settings page or the dialogs (next task), add
component libraries, add routing or data-fetching libraries, copy
Agent Orchestrator's visual design.
```

**我要驗證的事**
- [ ] 看板與任務詳情頁和 ui.md 與 `ui-mockup.html` 一致，訊息來源一眼分得出來
- [ ] PR 上至少一則留言經 agsv 餵回 Codex 並被改好（順便驗證留言回饋的迴圈）
- [ ] Code review 沒有發現資料流、API 或後端的改動

---

## 任務 23 · 外觀改版：其他畫面（Codex）

做法同任務 22。

```
Read CLAUDE.md and docs/README.md, then docs/design/ui.md.

Restyle the remaining screens to match docs/design/ui.md, using the
design tokens already in the Tailwind configuration.

Scope:
1. Settings page
2. New task and Add project dialogs, the archive confirmation
3. Empty states, error messages, loading states on every screen
4. Remove whatever is left of the old hand-written CSS that no screen
   uses any more
5. Same rules as before: keep behavior, data flow, API calls, keyboard
   shortcuts and accessible names; npm test and npm run build pass;
   nothing under src/ changes

Do not: add component libraries, routing or data-fetching libraries.
```

**我要驗證的事**
- [ ] 每個畫面都已經是新的樣子，舊的 CSS 沒有殘留沒用到的部分
- [ ] Code review 沒有發現行為的改動

---

## 任務 24 · Shell

```
請先讀 CLAUDE.md 與 docs/README.md，再讀 docs/design/ 下的文件，
以及 docs/history/plans/11-next-plan.md 主題 1。

在任務詳情頁加上 Shell：在任務的 worktree 開一個 terminal，
給人自己跑指令、看檔案（決策第 173 筆）。

範圍：
1. 後端：在 worktree 開 PTY，經 WebSocket 接到前端。預計用
   標準庫的 pty，不另裝套件；要裝的話先問我。做法先查 FastAPI
   與 uvicorn 的 WebSocket 文件，不要憑印象寫
2. 前端：用 xterm.js（第 175 筆）顯示，放在 ui.md 留好的位置；
   視窗大小改變時同步 PTY 的大小
3. 安全：WebSocket 的連線要和 API 一樣檢查 Host 與 Origin
   （沿用 api/guard.py 的規則），並加測試證明別的來源連不進來。
   這是這個任務最重要的一項
4. Shell 永遠不控制 agent：不寫進 agent 的 session，不經過
   adapter
5. 生命週期：worktree 不在（封存後）就不能開；封存時關掉這個
   任務所有的 shell；daemon 關閉時全部關掉並確認子程序結束
6. 兩件事提出建議讓我決定：shell 裡的指令要不要記進事件紀錄
   （預計不記，它是人的操作）、一個任務能不能開多個 shell
7. 測試不得真的啟動 agent；PTY 的測試在 macOS 與 Linux 的 CI
   上都要過
8. 更新 docs/guide/usage.md、docs/guide/trust-model.md（內嵌
   shell 等於透過 HTTP 執行指令，只靠本機與 Host/Origin 的
   檢查保護）、docs/guide/http-api.md

不要做：接手模式（在 agent 本身的 TUI 裡操作，階段 8）、
Windows 支援。
```

**我要驗證的事**
- [ ] 在詳情頁開 shell，跑 `git status` 與測試，輸出正確、可以互動（例如 Ctrl-C 中斷）
- [ ] 從另一個來源（例如別的連接埠上的網頁）連 WebSocket 會被拒絕
- [ ] 封存任務後 shell 被關掉，`ps` 看不到殘留的程序

---

## 任務 25 · 驗收與發 0.4

```
請先讀 CLAUDE.md 與 docs/README.md，再讀 docs/design/ 下的文件。

驗收階段 5，並準備發 0.4.0。

範圍：
1. 對照 roadmap.md 階段 5 的完成標準，實際在 sandbox repo 跑一次，
   結果列給我看
2. README 的截圖換成新介面；README 與 docs/guide/ 裡描述畫面的
   地方和新介面一致
3. 版本改 0.4.0，寫 .github/release-notes/v0.4.0.md 的摘要
   （第 172 筆）
4. roadmap.md 標記階段 5 完成；刪掉這份任務單（第 179 筆）
5. 推 v0.4.0 的 tag 要我明確說可以才做；推之前確認 HEAD 等於
   origin/main、版本號正確

不要做：階段 6 的任何東西。
```

**我要驗證的事**（也就是階段 5 的完成標準）
- [ ] 在新介面開一個任務，從對話看懂 agent 做了什麼、每一則是誰說的、追加一則訊息，在同一頁的 shell 跑一次測試，全程不需要離開這一頁
- [ ] 介面經過我的視覺驗收
- [ ] `v0.4.0` 的 Release 有附套件檔，下載安裝後看板是新介面
