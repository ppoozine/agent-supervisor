# 07 · 階段 2 任務單

最後更新：2026-11-19

階段 2 的兩個任務，對應 `02-mvp-spec.md` 開發順序的第 5、6 步。做法和 `04-phase1-tasks.md` 一樣：一次只給 Claude Code 一個，做完、測試通過、合併後再給下一個；每個任務都讓它先提計畫，確認後再動手。

這是 MVP 的後半，介面只求可用，不做美化（見 `01-roadmap.md` 階段 2 的主要風險）。

---

## 任務 5 · API + SSE + 常駐 daemon

```
請先讀 CLAUDE.md 與 docs/ 下的所有文件。

實作 docs/02-mvp-spec.md 開發順序的第 5 步：FastAPI 端點與 SSE。

範圍：
1. 常駐的 daemon：一個指令啟動 uvicorn，API、Runner、Poller、
   Dispatcher 跑在同一個 process。啟動時沿用現有的單一實例鎖與
   重啟復原，復原做完才開始接受請求；收到 Ctrl-C 時中止所有 run
   再結束
2. Poller 與 Dispatcher 改成 daemon 裡的背景工作，依設定的間隔
   輪詢；取代階段 1 的前景監看模式
3. 規格「API」一節的十個 REST 端點。錯誤的回應方式先向我提出
   （任務不存在、已有 run 在執行、任務已封存或 PR 已結束、
   預算用完、沒有 run 可以中止），確認後補進規格
4. GET /tasks 與 GET /tasks/{id} 回傳的欄位與狀態標籤一律由
   status.py 推導，不存資料庫；回傳內容要足夠畫出
   05-ui-reference.md 的卡片（agent 種類、標題、branch、狀態標籤、
   更新時間、PR 編號與狀態、花費或 token 數）
5. GET /stream：一條 SSE，推送任務狀態的變化與 agent 的事件。
   事件的格式、斷線重連後怎麼補上漏掉的事件，先向我提出
6. POST /tasks/{id}/stop 只中止指定任務的 run（階段 1 的
   asv stop 只能中止全部）
7. 階段 1 的 CLI 指令在有了 daemon 之後怎麼處理（保留哪些、
   哪些改成呼叫 API、哪些移除），先向我提出再動手
8. 不回報花費的任務（Codex）在回傳內容裡要分得出「花費未知」
   與「花費是零」
9. 測試：用 FastAPI 的測試 client 與 fake adapter，GitHub 用替身；
   每個端點的正常與錯誤情況各至少一個；SSE 至少驗證「開任務後
   收得到狀態變化與 agent 事件」。不得真的啟動 agent

不要做：前端、登入與多人、webhook、規格「不包含」清單裡的功能。

開始前先給我你的實作計畫，我確認後再動手。
```

**我要驗證的事**
- [x] `uv run pytest` 與 `uv run ruff check .` 都通過
- [x] 啟動 daemon 後用 `curl` 加入 sandbox 專案、開一個任務，GitHub 上出現 PR
- [x] `curl -N` 接上 `/stream`，開任務後即時看到 agent 的輸出與狀態變化
- [x] 不介入，CI 失敗後 daemon 自動修到綠燈，`GET /tasks` 的欄位跟著變成 Ready to merge
- [x] 同時開兩個任務，兩個 run 真的同時在跑、互不影響（階段 1 做不到的事）
- [x] `POST /tasks/{id}/stop` 只停掉那一個任務，另一個繼續跑
- [x] 任務有 run 在執行時送訊息，API 回報錯誤，沒有兩個 run 同時執行
- [x] 封存任務後 worktree 被清掉；對已封存的任務送訊息被拒絕
- [x] daemon 已經在跑的時候再啟動一個，第二個直接結束
- [x] run 執行中把 daemon 強制關掉再啟動，遺留的 agent 程序已經不在、該 run 變成 `interrupted`
- [x] `GET /tasks/{id}/events` 可以分頁；`GET /usage` 的數字和各 run 加總一致
- [x] Codex 任務的花費顯示為未知，不是 0

---

## 任務 6 · React 看板與任務詳情頁

```
請先讀 CLAUDE.md 與 docs/ 下的所有文件。

實作 docs/02-mvp-spec.md 開發順序的第 6 步：React 看板與任務詳情頁。

範圍：
1. 在 web/ 建立 React + Vite 專案。要用哪些套件（路由、資料抓取、
   樣式）先向我提出，確認後再安裝；能不用就不用
2. 版面與內容照 docs/05-ui-reference.md 的「MVP 介面範圍」：
   左側欄（專案與任務）、四欄看板、可展開的 Archive 區、
   New task 表單（選專案、選 agent、標題、描述）
3. 卡片內容照 05-ui-reference.md 的表格；花費標示為估算值，
   agent 無法回報花費時改顯示 token 數
4. 任務詳情頁：標題列的資訊、Log / Events / Runs 三個分頁、
   追加指令的輸入框、Stop 與 Archive 按鈕；沒有 run 在執行時
   Stop 不可按
5. 透過 SSE 即時更新看板與 log，不用重新整理；斷線後自動重連
6. 欄位與狀態標籤一律用 API 回傳的值，前端不自己推導
7. API 的錯誤要顯示給使用者看（例如已有 run 在執行）
8. 開發時前端怎麼連到 daemon、正式使用時由誰提供前端的檔案，
   先向我提出
9. 加入專案的介面：最簡單的表單即可（repo 路徑、是否要求 review）
10. 測試：狀態顯示與表單的元件測試；測試工具先向我提出

不要做：搜尋、通知、Settings 頁、內嵌 terminal、diff 檢視、
淺色主題、卡片拖曳、登入。介面只求可用，不做美化。

開始前先給我你的實作計畫，我確認後再動手。
```

**我要驗證的事**（也就是階段 2 的完成標準，做到這裡發 `0.1`）
- [x] 從瀏覽器加入 sandbox 專案、開一個任務，卡片出現在 Working
- [x] 不重新整理，卡片隨著狀態變化移動到對應的欄位，最後到 Ready to merge
- [x] 任務詳情頁的 Log 分頁即時看到 agent 的輸出
- [x] 開一個會讓 CI 失敗的任務，看到卡片回到 Working（Fixing CI）再到 Ready to merge
- [x] 查得到這個任務花了多少錢；Codex 任務顯示的是 token 數
- [x] 開任務時可以選 agent，Claude Code 與 Codex 各開一個都能跑完
- [x] 在詳情頁送一則訊息，新的 run 出現在 Runs 分頁
- [x] 按 Stop 後 run 停下，卡片進 Needs you；沒有 run 時 Stop 不可按
- [x] 按 Archive 後卡片移到 Archive 區；在 GitHub 上合併 PR 後卡片也移過去
- [x] Events 分頁可以依類型篩選
- [x] 把 daemon 關掉再開，頁面自動重新連上並顯示最新狀態
- [x] 操作被拒絕時（例如 run 執行中送訊息）畫面上看得到原因

---

## 階段 2 做完後

回頭更新文件：
- `02-mvp-spec.md`：實作中發現規格要調整的地方
- `03-decisions.md`：新增的決定
- `01-roadmap.md`：階段 3 的範圍是否要調整
- 發 `0.1`
