import { useState } from 'react'

import { createTask, errorMessage } from '../api'
import { currentSubmitShortcutLabel, submitOnShortcut } from '../keys'
import type { Agent, Project, TaskDetail } from '../types'
import { Dialog } from './Dialog'

interface NewTaskDialogProps {
  projects: Project[]
  agents: Agent[]
  onCreated: (task: TaskDetail) => void
  onClose: () => void
}

/**
 * The form for opening a task: project, agent, title and task description.
 *
 * Only agents that could be used at the daemon's last check are offered; the settings page says
 * what the others are missing.
 */
export function NewTaskDialog({ projects, agents, onCreated, onClose }: NewTaskDialogProps) {
  const usableAgents = agents.filter((agent) => agent.usable)
  const [projectId, setProjectId] = useState(projects[0]?.id ?? 0)
  const [agentType, setAgentType] = useState(usableAgents[0]?.agent_type ?? '')
  const [title, setTitle] = useState('')
  const [prompt, setPrompt] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [isBusy, setIsBusy] = useState(false)

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
      shortcutHint={currentSubmitShortcutLabel()}
      onSubmit={() => void submit()}
      onClose={onClose}
    >
      <div className="field-row">
        <label>
          Project
          <select value={projectId} onChange={(event) => setProjectId(Number(event.target.value))}>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Agent
          <select value={agentType} onChange={(event) => setAgentType(event.target.value)}>
            {usableAgents.map((agent) => (
              <option key={agent.agent_type} value={agent.agent_type}>
                {agent.display_name}
                {agent.reports_cost ? '' : ' (reports tokens only)'}
              </option>
            ))}
          </select>
        </label>
      </div>
      {usableAgents.length < agents.length && (
        <div className="muted">
          {usableAgents.length === 0 ? 'No agent can be used yet. ' : 'Some agents are not ready. '}
          <a href="#/settings" onClick={onClose}>
            See Settings
          </a>
        </div>
      )}
      <label>
        Title
        <input
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="Leave empty to use the start of the description"
        />
      </label>
      <label>
        Description
        <textarea
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          onKeyDown={submitOnShortcut}
          aria-keyshortcuts="Meta+Enter Control+Enter"
          rows={7}
          placeholder="What should the agent do?"
          required
          autoFocus
        />
      </label>
    </Dialog>
  )
}
