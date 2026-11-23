import { useState } from 'react'

import { createTask, errorMessage } from '../api'
import { branchPreview } from '../format'
import { currentSubmitShortcutLabel, submitOnShortcut } from '../keys'
import type { Agent, Project, TaskDetail } from '../types'
import { Dialog } from './Dialog'

interface NewTaskDialogProps {
  projects: Project[]
  agents: Agent[]
  /** The project to preselect, e.g. the one the board is filtered to. */
  initialProjectId?: number | null
  onCreated: (task: TaskDetail) => void
  onClose: () => void
}

/**
 * The form for opening a task: project, agent, title and task description.
 *
 * Every agent is listed, but only those usable at the daemon's last check can be chosen; the
 * settings page says what the others are missing.
 */
export function NewTaskDialog({
  projects,
  agents,
  initialProjectId = null,
  onCreated,
  onClose,
}: NewTaskDialogProps) {
  const usableAgents = agents.filter((agent) => agent.usable)
  const [projectId, setProjectId] = useState(
    projects.find((project) => project.id === initialProjectId)?.id ?? projects[0]?.id ?? 0,
  )
  const [agentType, setAgentType] = useState(usableAgents[0]?.agent_type ?? '')
  const [title, setTitle] = useState('')
  const [prompt, setPrompt] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [isBusy, setIsBusy] = useState(false)
  const shortcut = currentSubmitShortcutLabel()
  const descriptionStart = prompt.trim().split('\n')[0].slice(0, 60)

  const submit = async (): Promise<void> => {
    setIsBusy(true)
    setError(null)
    try {
      const task = await createTask({
        project_id: projectId,
        agent_type: agentType,
        title: title.trim() || null,
        prompt: prompt.trim(),
      })
      onCreated(task)
    } catch (failure) {
      setError(errorMessage(failure))
      setIsBusy(false)
    }
  }

  return (
    <Dialog
      title="New task"
      submitLabel="Start task"
      isBusy={isBusy}
      canSubmit={usableAgents.length > 0}
      error={error}
      shortcutHint={`${shortcut} to start`}
      onSubmit={() => void submit()}
      onClose={onClose}
    >
      <label className="field">
        Project
        <select
          className="input"
          value={projectId}
          onChange={(event) => setProjectId(Number(event.target.value))}
        >
          {projects.map((project) => (
            <option key={project.id} value={project.id}>
              {project.name}
            </option>
          ))}
        </select>
      </label>
      <fieldset className="field">
        <legend className="mb-1.5">Agent</legend>
        <div className="agent-choices">
          {agents.map((agent) => (
            <label className="agent-choice" key={agent.agent_type}>
              <input
                type="radio"
                name="agent"
                value={agent.agent_type}
                checked={agentType === agent.agent_type}
                disabled={!agent.usable}
                onChange={() => setAgentType(agent.agent_type)}
              />
              <span className="text-[13px] font-medium">{agent.display_name}</span>
              <span className="text-xs font-normal text-text-muted">
                {agent.usable
                  ? agent.reports_cost
                    ? 'Ready · reports cost'
                    : 'Ready · reports tokens only'
                  : 'Not ready'}
              </span>
            </label>
          ))}
        </div>
        {usableAgents.length < agents.length && (
          <span className="mt-1.5 font-normal text-text-muted">
            {usableAgents.length === 0 ? 'No agent can be used yet. ' : 'Some agents are not ready. '}
            <a href="#/settings" onClick={onClose}>
              See Settings
            </a>
          </span>
        )}
      </fieldset>
      <label className="field">
        <span>
          Title <span className="field-label-note">· optional</span>
        </span>
        <input
          className="input"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder={descriptionStart || 'Leave empty to use the start of the description'}
        />
      </label>
      <label className="field">
        Description
        <textarea
          className="input"
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          onKeyDown={submitOnShortcut}
          aria-keyshortcuts="Meta+Enter Control+Enter"
          rows={6}
          placeholder="What should the agent do?"
          required
          autoFocus
        />
      </label>
      <p className="note">
        Starts right away in a new worktree on{' '}
        <code className="text-text">{branchPreview(title.trim() || prompt)}</code>. The supervisor
        commits, pushes and opens the PR when the agent finishes.
      </p>
    </Dialog>
  )
}
