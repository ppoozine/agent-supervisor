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

## 運作良好的地方

- Codex 讀到了 `AGENTS.md`，照「先提計畫、確認後再動手」停下來提計畫；確認之後用 Chat 送一則訊息，就在同一個 session 接著做（任務 27 的 run 1、run 2）。計畫確認的流程直接搬進 Chat，不需要額外的機制。
- agent 照指示只改檔案，commit、push、開 PR 都由 supervisor 完成，CI 在 PR 上自動跑完並通過。

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
- **想法**：`agsv task new`、`agsv task send`、`agsv task wait` 這類 CLI，呼叫本機的 daemon。也和階段 6 的主 agent 派工有關，一起評估。
- **狀態**：待排
