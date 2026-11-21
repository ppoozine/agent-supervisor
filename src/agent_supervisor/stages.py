"""The prepare and wrap-up stages of a run, and archiving a task.

- Prepare: create a dedicated worktree and branch for the task.
- Wrap-up: once the agent ends normally, the supervisor commits leftovers, pushes, opens the PR.
- Archive: keep the results, then remove the worktree.

Each wrap-up step checks the current state before acting, so redoing it never pushes twice or opens
a duplicate PR.
"""

import asyncio
from pathlib import Path

import aiosqlite

from agent_supervisor import ci_log, db, worktree
from agent_supervisor.config import Config
from agent_supervisor.enums import CiState, EventType, MergeState, ReviewState, RunTrigger
from agent_supervisor.github import GitHub
from agent_supervisor.runner import EventWriter


class StageError(RuntimeError):
    """The prepare or wrap-up stage cannot proceed."""


class RunStages:
    """The prepare, wrap-up, and archive actions provided to the Runner."""

    def __init__(self, conn: aiosqlite.Connection, config: Config, github: GitHub) -> None:
        self._conn = conn
        self._config = config
        self._github = github

    async def prepare(self, task: db.Task, run: db.Run, record: EventWriter) -> None:
        """Ensure the task's worktree, then prepare by run kind: fetch CI logs or start a merge."""
        await self._ensure_worktree(task)
        if run.trigger is RunTrigger.CI_FAILURE:
            await self._load_ci_failure(task, run)
        if run.trigger is RunTrigger.MERGE_CONFLICT:
            await self._start_merge(task, run)

    async def _start_merge(self, task: db.Task, run: db.Run) -> None:
        """Merge the base branch into the task's branch, stopping at the conflicts for the agent.

        The supervisor starts the merge and commits it at wrap-up: some agents cannot run git write
        commands in the worktree, but editing files is fine. Local git is the source of truth, not
        what GitHub reports.
        """
        current = await db.get_task(self._conn, task.id)
        if current.worktree_path is None:
            raise StageError(f"task {current.id} has no worktree to merge in")
        project = await db.get_project(self._conn, current.project_id)
        path = Path(current.worktree_path)
        if await worktree.is_merge_in_progress(path):
            raise StageError("a merge is already in progress in this worktree")
        if await worktree.has_uncommitted_changes(path):
            raise StageError("the worktree has uncommitted changes; cannot start a merge")
        conflicted = await worktree.start_merge(path, project.default_branch)
        if not conflicted:
            if await worktree.is_merge_in_progress(path):
                await worktree.abort_merge(path)
            raise StageError(
                f"merging {project.default_branch} produced no conflict locally; nothing to resolve"
            )
        await db.set_run_input_text(
            self._conn, run.id, _conflict_prompt(project.default_branch, conflicted)
        )

    async def _ensure_worktree(self, task: db.Task) -> None:
        current = await db.get_task(self._conn, task.id)
        if current.worktree_path is not None and current.branch is None:
            # Having no branch of its own means the working directory is not a worktree created by
            # this tool (e.g. it points at the user's own repo), so the agent must not run in it.
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
        """Fetch and clean up the failed checks' logs, saving them as this run's agent input."""
        current = await db.get_task(self._conn, task.id)
        project = await db.get_project(self._conn, current.project_id)
        if project.github_repo is None or current.pr_number is None:
            raise StageError(f"task {current.id} has no pull request to read CI results from")
        status = await self._github.get_pull_request_status(project.github_repo, current.pr_number)
        if status.head_sha != run.source_head_sha:
            raise StageError("the pull request moved to another commit since CI failed")
        if not status.failed_checks:
            # E.g. someone reran the failed job on GitHub: this commit no longer has a failed check
            # to fix.
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
        """Publish the agent's results: commit leftovers, push, and open a PR if needed."""
        current = await db.get_task(self._conn, task.id)
        if current.worktree_path is None or current.branch is None:
            raise StageError(f"task {current.id} has no worktree to publish")
        project = await db.get_project(self._conn, current.project_id)
        path = Path(current.worktree_path)

        # Commit even with no changes when a merge is in progress: if every conflict was resolved by
        # keeping "our side", the content is identical to before the merge, but without this commit
        # the merge is not complete.
        is_merging = await worktree.is_merge_in_progress(path)
        if is_merging or await worktree.has_uncommitted_changes(path):
            await self._commit(path, project, run)
        if await worktree.count_commits_ahead(path, project.default_branch) == 0:
            return
        if project.github_repo is None:
            raise StageError(f"project {project.name} has no GitHub repo to publish to")

        if await worktree.has_unpushed_commits(path, current.branch):
            await self._push(current, path, record)
        if current.pr_number is None:
            await self._ensure_pull_request(current, project, record)

    async def _commit(self, path: Path, project: db.Project, run: db.Run) -> None:
        """Commit leftovers. Mid-merge, first confirm the conflicts are really resolved.

        Conflict markers must not be published. Check this whatever started the run: a previous
        conflict-resolving run may have been stopped, leaving a half-merged working directory to
        this run.
        """
        if not await worktree.is_merge_in_progress(path):
            await worktree.commit_all(
                path, f"chore(agent): save uncommitted changes from run {run.id}"
            )
            return
        unresolved = await worktree.files_with_conflict_markers(path)
        if unresolved:
            raise StageError(f"conflict markers are still present in: {', '.join(unresolved)}")
        await worktree.commit_all(
            path,
            f"chore(agent): merge {project.default_branch} and resolve conflicts (run {run.id})",
        )

    async def archive(self, task: db.Task) -> None:
        """Remove the task's worktree. Uncommitted changes go into a branch commit, not pushed."""
        current = await db.get_task(self._conn, task.id)
        if current.worktree_path is None or current.branch is None:
            # No worktree, or the directory was not created by this tool: nothing to remove, and it
            # must not be touched.
            return
        project = await db.get_project(self._conn, current.project_id)
        path = Path(current.worktree_path)
        if not await asyncio.to_thread(path.exists):
            # An earlier archive removed the worktree but was cut off before marking the task.
            await worktree.prune(Path(project.repo_path))
            return
        if await worktree.is_merge_in_progress(path):
            # A half-done merge is not kept: committing it would store conflict markers in the
            # branch. The task's own commits are all still there.
            await worktree.abort_merge(path)
        if await worktree.has_uncommitted_changes(path):
            await worktree.commit_all(path, "chore(agent): save uncommitted changes before archive")
        await worktree.remove(Path(project.repo_path), path)

    async def _push(self, task: db.Task, path: Path, record: EventWriter) -> None:
        assert task.branch is not None
        await worktree.push(path, task.branch)
        sha = await worktree.head_sha(path)
        # Update the cache right away instead of waiting for the next poll, so the card is not
        # misjudged by an old commit's CI result or a stale approval.
        await db.update_task(
            self._conn,
            task.id,
            head_sha=sha,
            ci_state=CiState.PENDING,
            review_state=ReviewState.NONE,
            merge_state=MergeState.UNKNOWN,
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


def _conflict_prompt(base_branch: str, conflicted: list[str]) -> str:
    files = "\n".join(f"- {name}" for name in conflicted)
    return (
        f"The pull request for this task conflicts with the latest {base_branch} branch."
        f" A merge of {base_branch} into this branch has been started for you and stopped"
        " at the conflicts. These files contain conflict markers:\n\n"
        f"{files}\n\n"
        "Resolve every conflict by editing the files so that the intent of both sides is kept,"
        " and remove all conflict markers. Run the tests if you can."
        " Do not run git merge, git rebase, git commit, git push or git merge --abort;"
        " the merge is committed and pushed for you when you finish."
    )
