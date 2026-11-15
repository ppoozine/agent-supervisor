# 06 · 架構圖與流程圖

最後更新：2026-11-15

這份文件把 `02-mvp-spec.md` 的內容畫成圖，方便一眼看懂。**圖與規格不一致時以規格為準**，並回頭修正這裡的圖。

圖用 Mermaid 寫成，GitHub 上可以直接顯示；在編輯器裡看需要支援 Mermaid 的預覽功能。

| # | 圖 | 回答的問題 |
| --- | --- | --- |
| 1 | 系統架構 | 有哪些元件，誰和誰溝通 |
| 2 | 模組與外部指令 | 哪個模組可以呼叫哪個外部工具 |
| 3 | Adapter 架構 | 怎麼接上不同的 agent |
| 4 | 資料表關係 | 資料怎麼存 |
| 5 | 一張卡片的生命週期 | 卡片怎麼在欄位間移動 |
| 6 | 看板欄位怎麼推導 | 一個任務該放在哪一欄 |
| 7 | 自動修復條件 | 什麼時候會自動修 CI |
| 8 | 開任務到開 PR | 從送出任務到 PR 出現的過程 |
| 9 | CI 自動修復迴圈 | CI 失敗後發生什麼事 |
| 10 | Run 的生命週期 | 一次執行會怎麼結束 |
| 11 | Run 結束後的收尾 | commit、push、開 PR 的判斷 |
| 12 | 中止與封存 | 按 Stop 與 Archive 會發生什麼 |
| 13 | Daemon 啟動與復原 | 當機重啟後怎麼處理 |
| 14 | 路線圖 | 各階段的先後 |

---

## 1. 系統架構

一個 Python daemon 跑在本機，裡面有 API 與三個背景工作，全部共用一個 SQLite。

```mermaid
flowchart LR
    user["使用者"]
    browser["React 看板<br/>瀏覽器"]

    subgraph daemon["Python daemon（本機單一 process）"]
        api["FastAPI<br/>REST + SSE"]
        runner["Runner<br/>執行 run 與收尾"]
        dispatcher["Dispatcher<br/>CI 失敗時建立修復 run"]
        poller["Poller<br/>每 30 秒查詢"]
        status["status.py<br/>推導看板欄位"]
        db[("SQLite<br/>projects / tasks / runs / events")]
        adapter["AgentAdapter 介面"]
    end

    subgraph local["本機"]
        agents["Agent<br/>Claude Code / Codex"]
        worktrees["各任務的 worktree"]
    end

    github["GitHub<br/>PR / CI / review"]

    user --> browser
    browser <-->|"REST + SSE"| api
    api --> runner
    api --> status
    dispatcher --> runner
    poller --> dispatcher
    runner --> adapter
    adapter --> agents
    agents -->|"改檔與 commit"| worktrees
    runner -->|"push 與開 PR"| github
    poller -->|"查詢狀態"| github
    dispatcher -->|"抓失敗的 CI log"| github
    api --- db
    runner --- db
    poller --- db
    dispatcher --- db
    status --- db
```

重點：

- Agent 只在 worktree 內改檔與 commit。**push 與開 PR 是 supervisor 做的**，不是 agent。
- 對 GitHub 一律用輪詢，不用 webhook。
- 看板欄位不存在資料庫裡，每次由 `status.py` 從資料算出來。

---

## 2. 模組與外部指令

對外部工具的呼叫集中在固定的模組，測試時可以整個換掉。

```mermaid
flowchart TD
    subgraph core["協調邏輯（不直接呼叫外部工具）"]
        runner["runner.py"]
        dispatcher["dispatcher.py"]
        poller["poller.py"]
        status["status.py<br/>純函式"]
        apimod["api/"]
    end

    subgraph outward["對外模組"]
        worktree["worktree.py"]
        githubmod["github.py"]
        adapters["adapters/"]
    end

    dbmod["db.py"]

    git["git"]
    gh["gh"]
    agent["agent<br/>Claude Code / Codex"]

    apimod --> runner
    apimod --> status
    dispatcher --> runner
    dispatcher --> githubmod
    dispatcher --> status
    poller --> githubmod
    runner --> worktree
    runner --> githubmod
    runner --> adapters
    worktree --> git
    githubmod --> gh
    adapters --> agent
    core --> dbmod
```

---

## 3. Adapter 架構

Runner 只認得 `AgentAdapter` 介面，不知道背後是哪一種 agent。每個 adapter 用 `capabilities` 說明自己做得到什麼。

```mermaid
flowchart TD
    runner["Runner"]
    iface["AgentAdapter 介面<br/>run_turn(prompt, cwd, session_id, model, limits, policy)<br/>capabilities"]

    runner --> iface

    iface --> claude["ClaudeCodeAdapter<br/>階段 1"]
    iface --> codex["CodexAdapter<br/>階段 1.5"]
    iface --> custom["自製 API agent<br/>階段 6"]
    iface --> fake["FakeAdapter<br/>測試用"]

    claude --> sdk["官方 Python Agent SDK"]
    codex --> codexcli["Codex<br/>做法待任務 0 調查"]
    custom --> modelapi["模型 API<br/>自己的 agent 迴圈"]
```

一輪對話的資料流：

```mermaid
sequenceDiagram
    participant R as Runner
    participant A as Adapter
    participant G as Agent

    R->>A: run_turn(prompt, cwd, session_id, model, limits, policy)
    A->>G: 啟動或續接 session
    G-->>A: session 已建立
    A-->>R: session_started 事件
    Note over R: 立刻保存 session id
    loop agent 工作中
        G-->>A: 訊息、工具呼叫、指令
        A-->>R: 有型別的 AgentEvent
        Note over R: 寫進 events
    end
    G-->>A: 這一輪結束
    A-->>R: TurnResult（結果、用量、花費）
```

---

## 4. 資料表關係

```mermaid
erDiagram
    projects ||--o{ tasks : "有多個"
    tasks ||--o{ runs : "有多次執行"
    tasks ||--o{ events : "有多筆紀錄"
    runs ||--o{ events : "產生"

    projects {
        int id
        string name
        string repo_path
        string github_repo
        string default_branch
        bool require_review
    }
    tasks {
        int id
        int project_id
        string title
        string prompt
        string agent_type
        string model
        string session_id
        string branch
        string worktree_path
        int pr_number
        string pr_state
        string head_sha
        string ci_state
        string review_state
        string last_handled_head_sha
        int ci_fix_attempts
        datetime archived_at
    }
    runs {
        int id
        int task_id
        string trigger
        string input_text
        string source_head_sha
        datetime started_at
        datetime ended_at
        string exit_status
        int tokens_in
        int tokens_out
        float cost_usd
    }
    events {
        int id
        int task_id
        int run_id
        datetime ts
        string type
        json payload
    }
```

- `tasks` 上的 `pr_state`、`head_sha`、`ci_state`、`review_state` 是 Poller 對 GitHub 的快取。
- `events` 只新增，不修改、不刪除。`run_id` 可以是空的（例如 Poller 寫的事件）。
- 沒有「看板欄位」這個欄位。

---

## 5. 一張卡片的生命週期

```mermaid
stateDiagram-v2
    state "Working" as Working
    state "In review" as InReview
    state "Needs you" as NeedsYou
    state "Ready to merge" as Ready
    state "Archive 區" as Archive

    [*] --> Working: 開任務
    Working --> InReview: agent 做完，工具 push 並開 PR
    Working --> NeedsYou: 出錯、被中止、撞到上限、沒有任何變更
    InReview --> Working: CI 失敗，自動修復
    InReview --> Ready: CI 通過（要求 review 時還要核准）
    InReview --> NeedsYou: CI 失敗但不能再自動修、reviewer 要求修改
    NeedsYou --> Working: 我送訊息讓它繼續
    Ready --> Working: 我送訊息追加修改
    Ready --> Archive: 我按合併
    InReview --> Archive: PR 被合併或關閉
    NeedsYou --> Archive: 我封存任務
    Archive --> [*]
```

---

## 6. 看板欄位怎麼推導

由上往下判斷，先符合的優先。這張圖就是 `status.py` 要實作的邏輯。

```mermaid
flowchart TD
    start(["一個任務"]) --> q0{"已封存？"}
    q0 -->|"是"| archive["Archive 區"]
    q0 -->|"否"| q1{"有 run 正在執行？"}
    q1 -->|"是"| working["Working"]
    q1 -->|"否"| q2{"PR 已合併或關閉？"}
    q2 -->|"是"| archive
    q2 -->|"否"| q3{"符合自動修復條件？"}
    q3 -->|"是"| working
    q3 -->|"否"| q4{"最後一次 run 是 success？"}
    q4 -->|"否"| needs["Needs you"]
    q4 -->|"是"| q5{"有 PR？"}
    q5 -->|"否，agent 沒有產生變更"| needs
    q5 -->|"是"| q6{"CI 失敗？"}
    q6 -->|"是，且不能再自動修"| needs
    q6 -->|"否"| q7{"reviewer 要求修改？"}
    q7 -->|"是"| needs
    q7 -->|"否"| q8{"CI 通過？"}
    q8 -->|"否，還在跑或沒有檢查"| review["In review"]
    q8 -->|"是"| q9{"專案要求 review？"}
    q9 -->|"否"| ready["Ready to merge"]
    q9 -->|"是"| q10{"review 已核准？"}
    q10 -->|"是"| ready
    q10 -->|"否"| review
```

---

## 7. 自動修復條件

八項全部成立，Dispatcher 才會自動開一個修復的 run。`status.py` 與 Dispatcher 共用同一個函式。

```mermaid
flowchart TD
    start(["Poller 更新完一個任務"]) --> c1{"最新 commit 的 CI 檢查<br/>全部結束且有失敗？"}
    c1 -->|"否"| no["不自動修復"]
    c1 -->|"是"| c2{"這個 commit 還沒處理過？"}
    c2 -->|"否"| no
    c2 -->|"是"| c3{"修復次數小於 3？"}
    c3 -->|"否"| no
    c3 -->|"是"| c4{"剩餘預算大於零？"}
    c4 -->|"否"| no
    c4 -->|"是"| c5{"沒有 run 正在執行？"}
    c5 -->|"否"| no
    c5 -->|"是"| c6{"PR 開著且任務未封存？"}
    c6 -->|"否"| no
    c6 -->|"是"| c7{"最後一次 run 是 success？"}
    c7 -->|"否"| no
    c7 -->|"是"| c8{"reviewer 沒有要求修改？"}
    c8 -->|"否"| no
    c8 -->|"是"| yes["自動修復"]
```

不自動修復不代表卡片一定進 Needs you：CI 沒失敗的任務本來就不需要修。CI 失敗卻不能自動修的任務才會進 Needs you。

---

## 8. 開任務到開 PR

```mermaid
sequenceDiagram
    actor U as 使用者
    participant API as API
    participant DB as SQLite
    participant R as Runner
    participant W as worktree.py
    participant A as Adapter 與 Agent
    participant GH as GitHub

    U->>API: 開任務（標題、描述、agent）
    API->>DB: 同一個交易內建立任務與 initial run
    API-->>U: 卡片出現在 Working
    API->>R: 交給 Runner
    R->>W: 建立 branch 與 worktree
    R->>A: run_turn（任務描述）
    A-->>R: session_started
    R->>DB: 保存 session id
    loop agent 工作中
        A-->>R: 事件
        R->>DB: 寫入 events
    end
    A-->>R: TurnResult
    R->>DB: 回填用量與花費
    Note over R: 開始收尾
    R->>W: 有未 commit 的變更就補 commit
    R->>GH: push
    R->>DB: 更新 head_sha，ci_state 設為 pending
    R->>GH: 開 PR
    R->>DB: 回填 pr_number，pr_state 設為 open
    R->>DB: run 結案為 success
    Note over U: 卡片移到 In review
```

---

## 9. CI 自動修復迴圈

```mermaid
sequenceDiagram
    participant GH as GitHub
    participant P as Poller
    participant DB as SQLite
    participant D as Dispatcher
    participant R as Runner
    participant A as Adapter 與 Agent

    loop 每 30 秒
        P->>GH: 查詢 PR、CI、review
        GH-->>P: 最新 commit 的 CI 失敗
        P->>DB: 更新快取（head_sha 沒變才寫入）
    end
    P->>D: 通知有任務更新
    D->>DB: 交易開始：重新檢查自動修復條件
    D->>DB: 建立 ci_failure run，記下已處理的 commit，修復次數加一
    Note over D,DB: 交易結束，卡片在 Working
    D->>GH: 抓失敗檢查的 log
    Note over D: 只擷取失敗的片段
    D->>R: 交給 Runner
    R->>A: run_turn（失敗 log，續接同一個 session）
    A-->>R: 事件與 TurnResult
    R->>GH: 收尾：push 修復的 commit
    R->>DB: 更新 head_sha，ci_state 設為 pending，run 結案
    Note over GH: CI 對新的 commit 重跑
    alt CI 通過
        Note over P: 卡片到 Ready to merge 或 In review
    else CI 又失敗且還能再修
        Note over P: 回到最上面，再修一次
    else 已修 3 次、超過預算、或沒有產生新 commit
        Note over P: 卡片到 Needs you
    end
```

防止無限迴圈的五道防線：

```mermaid
flowchart LR
    loop_start(["CI 失敗"]) --> g1["同一個 commit<br/>只餵回一次"]
    g1 --> g2["自動修復<br/>最多 3 次"]
    g2 --> g3["任務花費<br/>有上限"]
    g3 --> run["開始一次 run"]
    run --> g4["單次 run 的<br/>輪數與花費上限<br/>adapter 負責"]
    run --> g5["單次 run 的<br/>執行時間上限<br/>Runner 負責"]
```

---

## 10. Run 的生命週期

一個 run 由同一個可取消的工作從頭負責到尾。四段都算「正在執行」，全部做完才結案。

```mermaid
stateDiagram-v2
    state "建立" as Created
    state "準備" as Preparing
    state "agent 執行" as Running
    state "收尾" as Finalizing

    [*] --> Created: 開任務、CI 失敗、使用者送訊息
    Created --> Preparing: 交易完成
    Preparing --> Running: worktree 或 CI log 準備好
    Running --> Finalizing: agent 正常結束
    Finalizing --> success: commit、push、開 PR 都完成

    Preparing --> error: 準備失敗
    Running --> error: agent 或 adapter 出錯
    Finalizing --> error: commit、push 或開 PR 失敗

    Preparing --> stopped: Stop
    Running --> stopped: Stop
    Finalizing --> stopped: Stop

    Running --> limit_reached: 輪數或花費到頂
    Preparing --> limit_reached: 執行時間到頂
    Running --> limit_reached: 執行時間到頂
    Finalizing --> limit_reached: 執行時間到頂

    Preparing --> interrupted: daemon 當掉
    Running --> interrupted: daemon 當掉
    Finalizing --> interrupted: daemon 當掉

    success --> [*]
    error --> [*]
    stopped --> [*]
    limit_reached --> [*]
    interrupted --> [*]
```

只有 `success` 會做完收尾並發布成果。其他四種結束方式都不自動發布，worktree 裡的變更原樣保留，卡片進 Needs you。

---

## 11. Run 結束後的收尾

每一步都先查現況再動手，所以重做一次不會重複 push 或重複開 PR。

```mermaid
flowchart TD
    start(["agent 正常結束"]) --> q1{"worktree 有<br/>未 commit 的變更？"}
    q1 -->|"是"| commit["supervisor 補一個 commit"]
    q1 -->|"否"| q2
    commit --> q2{"branch 有還沒 push 的 commit？"}
    q2 -->|"是"| push["push<br/>更新 head_sha<br/>ci_state 設為 pending<br/>review_state 設為 none"]
    q2 -->|"否"| q3
    push --> q3{"已經有 PR？"}
    q3 -->|"是"| done["run 結案為 success"]
    q3 -->|"否"| q4{"branch 相對於基底<br/>有 commit？"}
    q4 -->|"是"| pr["開 PR<br/>回填 pr_number<br/>pr_state 設為 open"]
    q4 -->|"否"| nochange["run 結案為 success<br/>卡片進 Needs you（No changes）"]
    pr --> done

    commit -.->|"失敗"| fail["run 結案為 error<br/>卡片進 Needs you"]
    push -.->|"失敗"| fail
    pr -.->|"失敗"| fail
```

---

## 12. 中止與封存

建立 run、Stop、封存都要先拿到該任務的鎖，所以三者不會同時進行。

**按 Stop**

```mermaid
flowchart TD
    start(["使用者按 Stop"]) --> q1{"有 run 正在執行？"}
    q1 -->|"否"| reject["回報錯誤<br/>沒有東西可以停"]
    q1 -->|"是"| cancel["取消負責這個 run 的工作<br/>不再開始下一個外部動作"]
    cancel --> kill["停掉 agent 與它的所有子程序<br/>以及執行中的 git / gh"]
    kill --> q2{"都結束了？"}
    q2 -->|"還沒"| force["等一小段時間後強制終止"]
    force --> q2
    q2 -->|"是"| done["run 結案為 stopped<br/>卡片進 Needs you"]
    done --> keep["已經 push 的 commit 不會撤回<br/>worktree 裡的變更原樣保留"]
```

**按 Archive**

```mermaid
flowchart TD
    start(["使用者按 Archive"]) --> q1{"有 run 正在執行？"}
    q1 -->|"是"| reject["拒絕，要先 Stop"]
    q1 -->|"否"| q2{"worktree 有<br/>未 commit 的變更？"}
    q2 -->|"是"| commit["supervisor commit 到任務的 branch<br/>不 push"]
    q2 -->|"否"| remove
    commit -->|"成功"| remove["清掉 worktree<br/>branch 保留"]
    commit -->|"失敗"| fail["拒絕封存並回報原因<br/>worktree 不刪"]
    remove --> done["寫入 archived_at<br/>卡片進 Archive 區"]
```

---

## 13. Daemon 啟動與復原

```mermaid
flowchart TD
    start(["daemon 啟動"]) --> q0{"已經有另一個<br/>daemon 在跑？"}
    q0 -->|"是"| exit["直接結束"]
    q0 -->|"否"| q1{"有沒有<br/>未結束的 run？"}
    q1 -->|"沒有"| serve["開始接受新工作"]
    q1 -->|"有"| each["逐一處理每個未結束的 run"]
    each --> kill["找出上次遺留的程序並停掉"]
    kill --> q2{"確認都停了？"}
    q2 -->|"是"| mark["run 標成 interrupted<br/>寫入事件<br/>卡片進 Needs you"]
    q2 -->|"否"| hold["run 維持未結束<br/>任務不能建立新的 run<br/>在啟動訊息中列出"]
    mark --> serve
    hold --> serve
```

不自動重跑被中斷的 run，由我決定要不要送訊息讓它繼續。

---

## 14. 路線圖

```mermaid
flowchart LR
    p0["階段 0<br/>準備"] --> t0["任務 0<br/>Codex 介面調查"]
    t0 --> p1["階段 1<br/>核心迴圈"]
    p1 --> p15["階段 1.5<br/>Codex adapter"]
    p15 --> p2["階段 2<br/>看板介面<br/>0.1 MVP"]
    p2 --> check{"檢查點<br/>重新評估順序"}
    check --> p3["階段 3<br/>回饋迴圈補齊<br/>0.2"]
    p3 --> p4["階段 4<br/>自動派工<br/>0.3"]
    p4 --> p5["階段 5<br/>治理<br/>0.4"]
    p5 --> p6["階段 6<br/>自製 API agent<br/>0.5"]
    p6 --> p7["階段 7<br/>產品化<br/>1.0"]
```

階段 1 內部的三步：

```mermaid
flowchart LR
    s1["任務 1<br/>Adapter + Runner<br/>權限、上限、復原"] --> s2["任務 2<br/>Worktree + 收尾<br/>commit、push、開 PR"]
    s2 --> s3["任務 3<br/>Poller + Dispatcher<br/>CI 自動修復"]
    s3 --> done(["不用人介入<br/>CI 從紅變綠"])
```
