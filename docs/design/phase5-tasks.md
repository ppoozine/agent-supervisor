# 階段 5 任務單

最後更新：2026-11-23

階段 5「新介面」的八個任務，對應 `roadmap.md` 的階段 5。規劃的過程見 `../history/plans/11-next-plan.md` 主題 1、4、5，現行的決定見 `decisions.md`。階段做完後這份任務單會刪掉（決策第 179 筆）。

這個階段的目標是：**介面從「只求可用」變成每天願意用的樣子；在一個任務裡就看得懂 agent 做了什麼、是誰叫它做的，也能自己在 worktree 裡動手。** 做完發 `0.4.0`。

做法：**一次做一個任務，合併之後才開始下一個**（第 150 筆）。每個任務一個 branch、一個 PR，開始前先提計畫、確認後再動手。任務 22 由 Codex、任務 23 由 Claude Code，都透過 agsv 開任務執行（第 176、190 筆），其他由 Claude Code 在對話裡直接做。用 agsv 開發時碰到的問題記在 `../history/dogfooding.md`（第 191 筆）。

順序的理由：

- Tailwind 最先裝好，Codex 在 agsv 的 sandbox 裡可能不能連網（第 176 筆）。
- UI 需求文件定下每個畫面的資訊架構，後面的任務都照它做。
- **Chat 排在 Codex 的外觀改版之前**（第 180 筆）：Chat 會把 Log 分頁換成對話的時間軸。如果先改外觀，Codex 會花力氣美化一個馬上要被換掉的 Log；先把結構做出來，外觀改版就直接套在最終的結構上。Chat 這一步只求結構正確、樣式用需求文件的 tokens 即可。
- Shell 放在外觀改版之後，直接用新的版面，不必改兩次。

---

## 任務 19 · 裝 Tailwind

```
請先讀 AGENTS.md 與 docs/README.md，再讀 docs/design/ 下的文件。

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
5. 更新 AGENTS.md 不歸你，在 PR 描述提醒我前端規則要改

不要做：改任何畫面、加元件庫、建 design tokens（那是任務 20、22）。
```

**我要驗證的事**
- [ ] `npm run dev` 打開看板、任務詳情、設定頁，和改之前看起來一樣
- [ ] PR 描述寫了 Tailwind 的版本、接法，以及 preflight 的處理

---

## 任務 20 · UI 需求文件

```
請先讀 AGENTS.md 與 docs/README.md，再讀 docs/design/ 下的文件
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
請先讀 AGENTS.md 與 docs/README.md，再讀 docs/design/ 下的文件，
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

## 任務 22 · 外觀改版的底層（Codex，已完成）

PR #43：Tailwind 的 `@theme` tokens、開啟 Preflight、深淺主題跟著系統、分割面板與窄視窗的分頁、側欄收合、舊樣式隔離在 `web/src/legacy.css`。看過之後決定視覺改走方向 A（第 186 筆），這次的成果當作任務 23 的底層。

---

## 任務 23 · 外觀改版：所有畫面（Claude Code）

由 Claude 在 agsv 開一張 Claude Code 的任務，指示如下（第 190 筆）。PR 由 agsv 開出，我看畫面後在 PR 上留言，agsv 會把留言餵回 agent；Claude 做 code review、截圖和草稿比對。

```
Read AGENTS.md and docs/README.md, then docs/design/ui.md and every mockup
in docs/design/mockups/ (the HTML files hold the exact values; the PNG
files show how each screen should look).

Bring every screen to the design in docs/design/ui.md (direction A,
decisions 186 to 189). Task 22 already set up the tokens, Preflight, the
light and dark themes and the split panel; build on that.

Scope:
1. Tokens: change the @theme names and values in web/src/styles.css to
   the ones in ui.md, light and dark
2. Sidebar: the Board and Archive items, projects with their active
   tasks, the selected-project state, Live and Settings at the bottom
3. Board: the header with the Board | Archive switch, the project filter
   menu, the summary line and the New task button; columns and cards
4. Archive as its own view at #/archive: outcome filter, search and the
   table. It replaces the Archive area below the board
5. Task page: the header; Chat with your messages on the right, everyone
   else on the left, system and automatic lines centred; Diff; Events
   with the source and run filters and expandable rows; Runs with the
   totals and cards that jump to the run in Chat; the Shell placeholder;
   the message box
6. Dialogs: New task (agent cards, Title before Description, the note
   about what happens next), Add project (the field error, the
   requirements note), the archive confirmation
7. Settings, read-only: the Agents, GitHub, Projects and Usage sections.
   Leave out Limits and permissions; a separate task adds its API
8. Remove web/src/legacy.css and any CSS that no screen uses any more
9. Keep every behavior, data flow, API call, keyboard shortcut and
   accessible name that exists today. Nothing under src/ (the Python
   backend) changes. Filters and totals are computed in the frontend
   from data the API already returns
10. Tests for the new interactions: the project filter, the archive
   filter and search, the events source filter, jumping from Runs to
   Chat. npm test and npm run build pass
11. Update docs/guide/usage.md where it describes the screens
12. Code comments in English

Do not: load fonts from the network; add component, icon, routing or
data-fetching libraries; make settings editable; build the Shell
terminal (task 24).
```

**我要驗證的事**
- [ ] 每個畫面的截圖和 `mockups/` 的 PNG 對得起來，深色主題也看過
- [ ] Chat 裡你的訊息在右、agent 與 reviewer 在左、系統與自動置中
- [ ] 專案篩選、Archive、Events 的來源篩選、Runs 跳到 Chat 都能用
- [ ] PR 上至少一則留言經 agsv 餵回 agent 並被改好
- [ ] Code review 沒有發現後端、資料流或 API 的改動；`legacy.css` 已刪除

---

## 任務 23b · 設定頁的 Limits and permissions

由 Claude Code 在對話裡直接做（第 189 筆）。

```
請先讀 AGENTS.md 與 docs/README.md，再讀 docs/design/ui.md 的設定頁。

加上唯讀的 GET /api/config，並在設定頁加上「Limits and permissions」。

範圍：
1. 後端：GET /api/config 回傳 ui.md 列出的值（每個任務的費用與
   token 上限、每次 run 的輪數與時間上限、自動修復與留言的次數
   上限、輪詢間隔、通知、sandbox、網路、禁止的指令）。只回這些
   欄位，不回本機路徑以外的資訊；沿用 api/guard.py 的檢查
2. 前端：設定頁照 mockups/settings.html 加上這一區，含
   「To change these, edit … and restart agsv serve.」的說明
3. 測試：API 的回應形狀、guard 擋得住別的來源；前端顯示正確
4. 更新 docs/guide/http-api.md

不要做：修改設定的 API 或介面。
```

**我要驗證的事**
- [ ] 改 `config.toml` 的一個上限並重啟後，設定頁顯示新的值

---

## 任務 24 · Shell

```
請先讀 AGENTS.md 與 docs/README.md，再讀 docs/design/ 下的文件，
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

## 任務 24b · 介面打磨

任務 24 之後，實際用了一陣子再 review 一次介面，由 Claude Code 在對話裡直接做（決策第 198 筆）：Diff 改成 GitHub 的排法與色票、Chat 收合連續的動作並照 markdown 顯示 agent 的訊息、頁首只留有意義的狀態、側欄分區、卡片圓角。細節見 `ui.md`。

**我要驗證的事**
- [ ] Diff 有舊／新行號，顏色和 GitHub 一樣（深淺兩種主題）
- [ ] 長的 run 在 Chat 裡看得到 agent 說的話，動作收成一行
- [ ] 側欄的區塊、專案與任務分得清楚

---

## 任務 25 · 驗收與發 0.4

```
請先讀 AGENTS.md 與 docs/README.md，再讀 docs/design/ 下的文件。

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
