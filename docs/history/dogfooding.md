# 用 agsv 開發 agsv 的紀錄

最後更新：2026-11-23

從階段 5 任務 22 開始，這個 repo 的部分任務透過 agent-supervisor 交給 agent 做（決策第 176、190 筆）。這份記下實際操作時碰到的問題與想改進的地方（決策第 191 筆），定期挑出來排進任務；排進去之後在「狀態」寫上去處。只記實際發生過的事，不記猜測。

狀態：**待排**（還沒決定）、**已排**（寫上任務或階段）、**不改**（寫上理由）、**已改**（寫上 PR）。

## 一覽

| # | 問題 | 影響 | 狀態 |
| --- | --- | --- | --- |
| D1 | agent 只回覆、沒改檔案時，卡片只寫「No changes」 | 看不出 agent 在等你回答 | 待排 |
| D2 | supervisor 的 commit 訊息與 PR 描述太籠統 | 合併後 `main` 的歷史不好讀 | 待排 |
| D3 | 要看任務做出來的畫面，得自己在 worktree 開 dev server | review 前端任務很麻煩 | 待排 |
| D4 | agent 的 sandbox 裡沒有 `ps`，測試失敗並留下子程序 | agent 回報假的測試失敗，本機留下孤兒程序 | 待排 |
| D5 | agent 在 sandbox 裡不能開瀏覽器截圖 | 視覺驗收全落在人身上 | 待排 |
| D6 | 卡片的 token 數把快取的輸入也算進去 | 用量看起來比實際大很多 | 待排 |
| D7 | 開任務、送訊息、等 run 結束都只能打 API | 從終端機或其他 agent 操作 agsv 不方便 | 待排 |
| D8 | 上限沒辦法關掉、不能依任務調整，改了要重啟 daemon | 大任務前要先改設定檔、等 run 結束才能重啟 | 待排 |
| D9 | 同一份 `AGENTS.md`，Codex 停下來提計畫、Claude Code 直接做 | 行為不一致，Codex 白跑一次 run | 已改（決策第 192 筆） |
| D10 | Chat 把 agent 回覆的 markdown 原樣顯示 | 長的回覆（`**粗體**`、清單）很難讀 | 待排 |
| D11 | `agsv stop` 只停掉進行中的 run，不會關掉 daemon | 以為關掉了，daemon 其實還在跑 | 待排 |

## 運作良好的地方

- Codex 讀到了 `AGENTS.md`，照「先提計畫、確認後再動手」停下來提計畫；確認之後用 Chat 送一則訊息，就在同一個 session 接著做（任務 27 的 run 1、run 2）。計畫確認的流程直接搬進 Chat，不需要額外的機制。
- agent 照指示只改檔案，commit、push、開 PR 都由 supervisor 完成，CI 在 PR 上自動跑完並通過。
- **PR 留言的回饋迴圈第一次在真的任務上跑通**（任務 23，PR #45）：Owner 在 PR 上留一則四點的修改意見，agsv 在下一輪輪詢讀到，開了 `review_feedback` 的 run，Claude Code 在同一個 session 裡 1 分鐘改完四點並補上測試（$0.47），supervisor push 後 CI 通過、卡片回到 Ready to merge。

## 細節

### D1 · agent 只回覆、沒改檔案時，看不出它在等你

- **任務**：任務 22（agsv 任務 27，Codex），run 1，2026-11-22
- **發生什麼**：Codex 讀完文件後只回了一份計畫與「請確認是否依此計畫開始」，沒改任何檔案。run 以 `success` 結束，卡片進 Needs you，標籤是「No changes」。
- **影響**：從看板只看得出「沒有變更」，看不出 agent 其實在問問題、等你回答；要點進去讀 Chat 才知道。
- **想法**：run 正常結束、沒有變更、最後一則 `agent_message` 不是空的時，標籤改成「Agent replied」這類，卡片上顯示最後一則訊息的開頭。標籤由 `status.py` 推導（設計規則 1），要改規格。
- **狀態**：待排

### D2 · supervisor 的 commit 訊息與 PR 描述太籠統

- **任務**：任務 22，PR #43
- **發生什麼**：commit 訊息是 `chore(agent): save uncommitted changes from run 51`；PR 標題是任務標題（「Restyle the board and the task page (phase 5, task 22)」），描述只有原本的指示與「Opened by agent-supervisor for task 27.」。PR 用 merge commit 合併，這筆 commit 訊息進了 `main` 的歷史。
- **影響**：`main` 的歷史看不出這筆變更做了什麼；PR 描述沒有 agent 自己回報的變更內容與做過的檢查，reviewer 要去 Chat 找。合併時要記得手動改標題，這次就忘了。
- **想法**：commit 訊息用任務標題（可以選擇加 Conventional Commits 的前綴）；PR 描述附上 agent 最後一則訊息（它照 `AGENTS.md` 會寫改了什麼、跑了哪些檢查）。同一個任務後續 run 的 commit 用「address review comments」「fix CI」這類依 `trigger` 的訊息。
- **再次發生**：任務 23（PR #45）的兩個 commit 是 `chore(agent): save uncommitted changes from run 52`、`… run 53`，PR 又用 merge commit 合併，兩筆都進了 `main`。留言觸發的 run 用的訊息和第一次一樣，看不出是「處理 review」。
- **狀態**：待排

### D3 · 要看任務做出來的畫面，得自己開 dev server

- **任務**：任務 22 的 review
- **發生什麼**：daemon 提供的是 `main` 上 build 好的前端（`web_dist`），看不到任務 branch 的畫面。review 時要在任務的 worktree 手動跑 `npx vite`，用 5173 埠打開（`/api` 轉給 daemon），看完再關掉。
- **影響**：前端任務的 review 多了好幾步，也容易忘記關掉 dev server。
- **想法**：任務 24 的 Shell 做好後，可以在任務詳情頁直接跑；更進一步是詳情頁的「Preview」：由 daemon 在 worktree 啟動專案設定好的預覽指令，給一個連結。需要專案層級的設定，排在 Shell 之後評估。
- **狀態**：待排

### D4 · sandbox 裡沒有 `ps`，測試失敗並留下子程序

- **任務**：任務 22，run 2
- **發生什麼**：Codex 在 sandbox 裡跑 `uv run pytest`，3 個跟子程序有關的測試失敗，原因是 sandbox 不能用 `ps`、`pgrep`。其中一個測試的假 codex 程序（`tests/fake_codex.py`）沒被終止，run 結束後還在本機執行，由 Claude 手動結束。同樣的測試在本機與 CI 上都通過。
- **影響**：agent 回報了實際上不存在的測試失敗；本機留下孤兒程序，agsv 沒有察覺。
- **想法**：兩件事。(1) run 結束時，supervisor 確認 agent 啟動的整個程序群組都結束了（`process.py` 已經會終止子程序，要查為什麼這支沒被收掉）。(2) 這幾個測試在沒有 `ps` 的環境改成 skip 並說明原因，或在 `AGENTS.md` 告訴 agent 這幾個測試在 sandbox 裡會失敗。
- **狀態**：待排

### D5 · agent 在 sandbox 裡不能截圖

- **任務**：任務 22，run 2
- **發生什麼**：Codex 想用 headless Chrome 檢查深淺主題與窄視窗，Chrome 啟動失敗，瀏覽器工具也連不上，最後只做了程式碼層面的檢查。
- **影響**：前端任務的視覺驗收全落在人（或 Claude 的 review）身上。
- **任務 23 的觀察**：Claude Code 沒有嘗試截圖；`mockups/` 只讀了 `board.html` 的樣式，沒有打開 PNG，畫面的還原度主要靠 `ui.md` 的文字。視覺驗收由 Claude 在瀏覽器做，深色主題是把深色的 media rule 強制套用後截圖；headless Chrome 截有 SSE 連線的頁面會卡住。
- **想法**：任務 23 先觀察 Claude Code 在 agsv 的權限政策下能不能截圖。不行的話，考慮讓 supervisor 在 run 結束後自己截圖附在 PR 上（和 D3 的預覽一起想）。
- **狀態**：待排

### D6 · token 數把快取的輸入也算進去

- **任務**：任務 22，run 2
- **發生什麼**：run 2 回報的輸入 token 是 3,918,876，其中 3,748,736 是快取命中，輸出只有 26,139。卡片顯示「3.9M tok」。
- **影響**：看起來非常貴，實際上大部分是便宜的快取讀取；拿它和上限（`task_token_limit`）比較時也會提早停下。
- **想法**：分開記錄與顯示快取的輸入（`cached_input_tokens` 已經在 `usage_raw` 裡）；上限要不要算快取另外決定。
- **狀態**：待排

### D7 · 只能用 API 操作任務

- **任務**：任務 22 的整個流程
- **發生什麼**：加入專案、開任務、送確認訊息、等 run 結束，都是用 `curl` 打 API（還要帶 `Origin` 標頭才過得了 guard），等待則是自己寫迴圈輪詢 `GET /api/tasks/{id}`。
- **影響**：從終端機或從其他 agent（例如在對話裡協作的 Claude Code）操作 agsv 不方便，每次都要重寫一樣的指令。
- **再次發生**：任務 23 等留言觸發的 run 時，自己寫的輪詢迴圈有錯（zsh 不會把變數拆成多個字），沒偵測到 run 已經結束，一直跑到 PR 合併後才發現。
- **想法**：`agsv task new`、`agsv task send`、`agsv task wait` 這類 CLI，呼叫本機的 daemon。也和階段 6 的主 agent 派工有關，一起評估。
- **狀態**：待排

### D8 · 上限沒辦法關掉、不能依任務調整

- **任務**：任務 23（agsv 任務 28，Claude Code），開始前
- **發生什麼**：任務 23 的範圍比任務 22 大很多，預設上限（每個任務 $5、每次 run 50 輪、30 分鐘）很可能中途擋下。討論過先拿掉費用上限，但程式沒有「關掉」的設定，只能在 `config.toml` 填很大的數字；設定只在啟動時讀，改了要重啟 daemon，而重啟會中斷正在跑的 run。最後沿用預設值，這次 run 花了 $3.38、8 分 52 秒，沒有碰到上限，但離費用上限不遠。
- **影響**：大任務開始前要先改設定檔、等 run 結束才能重啟；不同大小的任務只能共用一組上限。
- **想法**：開任務時可以指定這個任務的上限（覆蓋全域值）；或讓上限能填 `0` 表示不限。設定改成不用重啟就生效的話，要注意進行中的 run 用哪一組值。
- **狀態**：待排

### D9 · 同一份指示，兩個 agent 對「先提計畫」的理解不同

- **任務**：任務 22（Codex）與任務 23（Claude Code）
- **發生什麼**：`AGENTS.md` 寫「每個任務先提計畫，我確認後再動手」。Codex 照做，第一次 run 只回了計畫就結束；Claude Code 判斷「agsv 開的任務範圍已經列清楚」，直接做完，並在最後說明沒有先提計畫。
- **影響**：行為不可預期；Codex 多跑一次 run、多花一輪來回。
- **想法**：在 `AGENTS.md` 寫清楚 agsv 開的任務不用另外確認，不清楚時才停下來問。
- **狀態**：已改（決策第 192 筆，`AGENTS.md` 的「開發慣例」）

### D10 · Chat 把 agent 回覆的 markdown 原樣顯示

- **任務**：任務 23 的 review
- **發生什麼**：agent 的最後一則訊息是 markdown（標題、粗體、清單、程式碼），Chat 直接顯示成文字，`**Chat**`、`- ` 都原樣出現。
- **影響**：agent 的總結通常最長、最重要，卻最難讀。
- **想法**：Chat 的 agent 訊息支援基本的 markdown（段落、粗體、清單、行內程式碼、程式碼區塊）。不能直接塞 HTML（內容來自 agent，要防 XSS）；自己寫一個小的轉換或用套件都要先討論（套件要先問）。
- **狀態**：待排

### D11 · `agsv stop` 不會關掉 daemon

- **任務**：任務 23b 的驗證
- **發生什麼**：為了在瀏覽器確認設定頁，另外用一份暫時的設定檔（不同的資料目錄與連接埠）啟動第二個 daemon。驗證完執行 `agsv --config … stop`，輸出「stop requested」，但 daemon 仍在回應；`stop` 送的是 SIGUSR1，意思是「停掉進行中的 run」，關掉 daemon 要送 SIGTERM（或在前景按 Ctrl-C）。
- **影響**：名稱容易讓人以為會關掉 daemon；要關掉背景執行的 daemon 只能自己找 pid 送訊號。
- **想法**：把現在的行為改名為 `agsv stop-runs`（或 `agsv stop --runs`），另外加 `agsv shutdown` 正常關掉 daemon；`--help` 寫清楚兩者的差別。和 D7 的 CLI 一起評估。
- **狀態**：待排

