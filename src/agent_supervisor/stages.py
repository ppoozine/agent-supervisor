"""Run 的準備與收尾階段，以及任務的封存。

- 準備：為任務建立專屬的 worktree 與 branch。
- 收尾：agent 正常結束後，由 supervisor 補 commit、push、開 PR。
- 封存：保留成果後清掉 worktree。

收尾的每一步都先查現況再動手，所以重做一次不會重複 push 或重複開 PR。
"""

from pathlib import Path

import aiosqlite

from agent_supervisor import ci_log, db, worktree
from agent_supervisor.config import Config
from agent_supervisor.enums import CiState, EventType, ReviewState, RunTrigger
from agent_supervisor.github import GitHub
from agent_supervisor.runner import EventWriter


class StageError(RuntimeError):
    """準備或收尾無法進行。"""


class RunStages:
    """提供給 Runner 的準備、收尾與封存動作。"""

    def __init__(self, conn: aiosqlite.Connection, config: Config, github: GitHub) -> None:
        self._conn = conn
        self._config = config
        self._github = github

    async def prepare(self, task: db.Task, run: db.Run, record: EventWriter) -> None:
        """確保任務有自己的 worktree；ci_failure run 另外要抓失敗的 CI log。"""
        await self._ensure_worktree(task)
        if run.trigger is RunTrigger.CI_FAILURE:
            await self._load_ci_failure(task, run)

    async def _ensure_worktree(self, task: db.Task) -> None:
        current = await db.get_task(self._conn, task.id)
        if current.worktree_path is not None and current.branch is None:
            # 沒有自己的 branch，表示工作目錄不是這個工具建立的 worktree
            # （例如指向使用者自己的 repo），不能讓 agent 在裡面執行。
            raise StageError(
                f"task {current.id} was created before worktrees existed; start a new task"
            )
        if current.worktree_path is not None:
            return
        project = await db.get_project(self._conn, current.project_id)
        branch = worktree.branch_name(current.id, current.title)
        path = self._config.worktrees_dir / project.name / f"task-{current.id}"
        await worktree.create(
            Path(project.repo_path), path, branch=branch, base_branch=project.default_branch
        )
        await db.update_task(self._conn, current.id, branch=branch, worktree_path=str(path))

    async def _load_ci_failure(self, task: db.Task, run: db.Run) -> None:
        """抓失敗檢查的 log，整理後寫成這個 run 實際要送給 agent 的內容。"""
        current = await db.get_task(self._conn, task.id)
        project = await db.get_project(self._conn, current.project_id)
        if project.github_repo is None or current.pr_number is None:
            raise StageError(f"task {current.id} has no pull request to read CI results from")
        status = await self._github.get_pull_request_status(project.github_repo, current.pr_number)
        if status.head_sha != run.source_head_sha:
            raise StageError("the pull request moved to another commit since CI failed")
        if not status.failed_checks:
            # 例如有人在 GitHub 上重跑了失敗的 job：這個 commit 上已經沒有失敗的檢查可以修。
            raise StageError(
                f"CI is no longer failing on this commit (now {status.ci_state}); nothing to fix"
            )

        logs = []
        for check in status.failed_checks:
            if check.job_id is None:
                continue
            text = await self._github.fetch_job_log(project.github_repo, check.job_id)
            logs.append(ci_log.CheckLog(check.name, text))
        if not logs:
            raise StageError("no CI log is available for the failed checks")
        without_log = [check.name for check in status.failed_checks if check.job_id is None]
        prompt = ci_log.build_fix_prompt(
            status.head_sha, logs, without_log, max_chars=self._config.ci_log_max_chars
        )
        await db.set_run_input_text(self._conn, run.id, prompt)

    async def finalize(self, task: db.Task, run: db.Run, record: EventWriter) -> None:
        """把 agent 的成果發布出去：補 commit、push、必要時開 PR。"""
        current = await db.get_task(self._conn, task.id)
        if current.worktree_path is None or current.branch is None:
            raise StageError(f"task {current.id} has no worktree to publish")
        project = await db.get_project(self._conn, current.project_id)
        path = Path(current.worktree_path)

        if await worktree.has_uncommitted_changes(path):
            await worktree.commit_all(
                path, f"chore(agent): save uncommitted changes from run {run.id}"
            )
        if await worktree.count_commits_ahead(path, project.default_branch) == 0:
            return
        if project.github_repo is None:
            raise StageError(f"project {project.name} has no GitHub repo to publish to")

        if await worktree.has_unpushed_commits(path, current.branch):
            await self._push(current, path, record)
        if current.pr_number is None:
            await self._ensure_pull_request(current, project, record)

    async def archive(self, task: db.Task) -> None:
        """清掉任務的 worktree。未 commit 的變更先 commit 到 branch，不 push。"""
        current = await db.get_task(self._conn, task.id)
        if current.worktree_path is None or current.branch is None:
            # 沒有 worktree，或目錄不是這個工具建立的：沒有東西要清，也不能去動它。
            return
        project = await db.get_project(self._conn, current.project_id)
        path = Path(current.worktree_path)
        if await worktree.has_uncommitted_changes(path):
            await worktree.commit_all(path, "chore(agent): save uncommitted changes before archive")
        await worktree.remove(Path(project.repo_path), path)

    async def _push(self, task: db.Task, path: Path, record: EventWriter) -> None:
        assert task.branch is not None
        await worktree.push(path, task.branch)
        sha = await worktree.head_sha(path)
        # 立刻更新快取：不用等下一次輪詢，卡片就不會被舊 commit 的 CI 結果或舊的核准誤判。
        await db.update_task(
            self._conn,
            task.id,
            head_sha=sha,
            ci_state=CiState.PENDING,
            review_state=ReviewState.NONE,
        )
        await record(EventType.PUSHED, {"branch": task.branch, "head_sha": sha})

    async def _ensure_pull_request(
        self, task: db.Task, project: db.Project, record: EventWriter
    ) -> None:
        assert task.branch is not None and project.github_repo is not None
        existing = await self._github.find_pull_request(project.github_repo, task.branch)
        if existing is not None:
            await db.update_task(
                self._conn, task.id, pr_number=existing.number, pr_state=existing.state
            )
            return
        created = await self._github.create_pull_request(
            project.github_repo,
            head=task.branch,
            base=project.default_branch,
            title=task.title,
            body=f"{task.prompt}\n\n---\nOpened by agent-supervisor for task {task.id}.",
        )
        await db.update_task(self._conn, task.id, pr_number=created.number, pr_state=created.state)
        await record(EventType.PR_OPENED, {"number": created.number, "url": created.url})
