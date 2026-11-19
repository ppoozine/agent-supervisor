import { useState } from 'react'

import { createTask, errorMessage } from '../api'
import type { Agent, Project, TaskDetail } from '../types'
import { Dialog } from './Dialog'
import { agentName } from './TaskCard'

interface NewTaskDialogProps {
  projects: Project[]
  agents: Agent[]
  onCreated: (task: TaskDetail) => void
  onClose: () => void
}

/** 開任務的表單：選專案、選 agent、標題與任務描述。 */
export function NewTaskDialog({ projects, agents, onCreated, onClose }: NewTaskDialogProps) {
  const [projectId, setProjectId] = useState(projects[0]?.id ?? 0)
  const [agentType, setAgentType] = useState(agents[0]?.agent_type ?? '')
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
      error={error}
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
            {agents.map((agent) => (
              <option key={agent.agent_type} value={agent.agent_type}>
                {agentName(agent.agent_type)}
                {agent.reports_cost ? '' : '（只回報 token）'}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label>
        Title
        <input
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="留空就用描述的開頭"
        />
      </label>
      <label>
        Description
        <textarea
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          rows={7}
          placeholder="要 agent 做什麼"
          required
          autoFocus
        />
      </label>
    </Dialog>
  )
}
