import { useEffect, useState } from 'react'

import { checkSetup, errorMessage, getConfig, getSetup, getUsage } from '../api'
import { formatCount, settingRows, timeAgo } from '../format'
import type { Agent, Card, Project, ServerConfig, Setup, ToolStatus, Usage } from '../types'

interface SettingsPageProps {
  /** Called after a fresh check, so the rest of the page can pick up which agents are usable. */
  onChecked: () => void
  projects?: Project[]
  cards?: Card[]
  agents?: Agent[]
  onAddProject?: () => void
}

/** Read-only settings: which agents and gh can be used, the projects, usage over all tasks, and the limits. */
export function SettingsPage({
  onChecked,
  projects = [],
  cards = [],
  agents = [],
  onAddProject = () => undefined,
}: SettingsPageProps) {
  const [setup, setSetup] = useState<Setup | null>(null)
  const [usage, setUsage] = useState<Usage | null>(null)
  const [config, setConfig] = useState<ServerConfig | null>(null)
  // Kept apart from `error`: a failed limits request should not hide the rest of the page.
  const [configError, setConfigError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isChecking, setIsChecking] = useState(false)

  useEffect(() => {
    let isCurrent = true
    getSetup()
      .then((loaded) => isCurrent && setSetup(loaded))
      .catch((failure: unknown) => isCurrent && setError(errorMessage(failure)))
    getUsage()
      .then((loaded) => isCurrent && setUsage(loaded))
      .catch((failure: unknown) => isCurrent && setError(errorMessage(failure)))
    getConfig()
      .then((loaded) => isCurrent && setConfig(loaded))
      .catch((failure: unknown) => isCurrent && setConfigError(errorMessage(failure)))
    return () => {
      isCurrent = false
    }
  }, [])

  const checkAgain = async (): Promise<void> => {
    setIsChecking(true)
    setError(null)
    try {
      setSetup(await checkSetup())
      onChecked()
    } catch (failure) {
      setError(errorMessage(failure))
    } finally {
      setIsChecking(false)
    }
  }
  const reportsCost = (agentType: string): boolean | undefined =>
    agents.find((agent) => agent.agent_type === agentType)?.reports_cost

  return (
    <div className="settings">
      <div className="settings-head">
        <h1>Settings</h1>
        {setup && <span className="text-xs text-text-muted">Checked {timeAgo(setup.checked_at)}</span>}
        <button className="btn ml-auto" onClick={() => void checkAgain()} disabled={isChecking}>
          {isChecking ? 'Checking…' : 'Check again'}
        </button>
      </div>
      <p className="max-w-[920px] text-xs text-text-muted">
        agent-supervisor runs the agents you have installed and signed in to yourself. It never signs
        you in and never stores keys or tokens. After fixing something in a terminal, check again;
        environment variables only take effect when agsv serve is started from a shell that has them.
      </p>
      {error && (
        <div className="alert m-0 max-w-[920px]" role="alert">
          {error}
        </div>
      )}
      <section className="settings-section" id="settings-agents">
        <h2>Agents</h2>
        <div className="settings-card">
          {setup?.agents.map((agent) => (
            <ToolSection
              key={agent.agent_type}
              name={agent.display_name}
              status={agent}
              reportsCost={reportsCost(agent.agent_type)}
            />
          )) ?? <p className="empty">Loading…</p>}
        </div>
      </section>
      <section className="settings-section" id="settings-github">
        <h2>GitHub</h2>
        <div className="settings-card">
          {setup ? <ToolSection name="gh" status={setup.github} /> : <p className="empty">Loading…</p>}
        </div>
      </section>
      <section className="settings-section" id="settings-projects">
        <h2>
          Projects
          <button className="btn ml-auto" onClick={onAddProject}>
            Add project
          </button>
        </h2>
        <div className="table-wrap">
          <table className="table" aria-label="Projects">
            <thead>
              <tr>
                <th>Name</th>
                <th>Local path</th>
                <th>GitHub</th>
                <th>Requires review</th>
                <th>Tasks</th>
              </tr>
            </thead>
            <tbody>
              {projects.length === 0 && (
                <tr>
                  <td colSpan={5} className="text-text-muted">
                    No projects yet.
                  </td>
                </tr>
              )}
              {projects.map((project) => {
                const tasks = cards.filter((card) => card.project_id === project.id)
                const archived = tasks.filter((card) => card.column === 'Archive').length
                return (
                  <tr key={project.id}>
                    <td className="font-medium">{project.name}</td>
                    <td className="font-mono break-all whitespace-normal text-text-muted">{project.repo_path}</td>
                    <td className="text-text-muted">{project.github_repo ?? '—'}</td>
                    <td className="text-text-muted">{project.require_review ? 'Yes' : 'No'}</td>
                    <td className="text-text-muted">
                      {tasks.length - archived} active · {archived} archived
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </section>
      <section className="settings-section" id="settings-usage">
        <h2>Usage</h2>
        <div className="stats">
          <div className="stat">
            <span className="stat-label">Cost</span>
            <span className="stat-value">
              {usage ? `~$${usage.cost_usd.toFixed(2)}${usage.usage_is_complete ? '' : '+'}` : '…'}
            </span>
          </div>
          <div className="stat">
            <span className="stat-label">Tokens in</span>
            <span className="stat-value">{usage ? formatCount(usage.tokens_in) : '…'}</span>
          </div>
          <div className="stat">
            <span className="stat-label">Tokens out</span>
            <span className="stat-value">{usage ? formatCount(usage.tokens_out) : '…'}</span>
          </div>
        </div>
        <p className="text-xs text-text-muted">
          Over every task, archived ones included. Cost is an estimate
          {usage?.has_unknown_cost && '; it leaves out agents that do not report cost'}.
        </p>
      </section>
      <section className="settings-section" id="settings-limits">
        <h2>Limits and permissions</h2>
        {configError ? (
          <div className="alert m-0" role="alert">
            {configError}
          </div>
        ) : (
          <div className="settings-card">
            {config ? (
              <>
                <dl className="setting-list" aria-label="Limits and permissions">
                  {settingRows(config).map((row) => (
                    <div key={row.label} className="setting-row">
                      <dt>
                        {row.label}
                        {row.fixed && <span className="setting-fixed">fixed</span>}
                      </dt>
                      <dd className={row.mono ? 'font-mono' : undefined}>{row.value}</dd>
                    </div>
                  ))}
                </dl>
                <p className="setting-note">
                  To change these, create or edit <code className="font-mono">{config.config_path}</code> and
                  restart{' '}
                  <code className="font-mono">agsv serve</code>. Values marked fixed are built in.
                </p>
              </>
            ) : (
              <p className="empty">Loading…</p>
            )}
          </div>
        )}
      </section>
    </div>
  )
}

function ToolSection({
  name,
  status,
  reportsCost,
}: {
  name: string
  status: ToolStatus
  reportsCost?: boolean
}) {
  const state = status.usable ? 'Ready' : status.installed ? 'Not ready' : 'Not installed'
  // The state already says "Not installed"; repeat the detail only when it adds something.
  const detail = status.detail?.toLowerCase() === state.toLowerCase() ? null : status.detail
  return (
    <section className="tool" aria-label={name}>
      <div className="tool-header">
        <strong className="font-semibold">{name}</strong>
        <span
          className={`text-xs font-medium ${status.usable ? 'text-success' : 'text-needs-you-text'}`}
        >
          {state}
        </span>
        {status.version && <span className="text-xs text-text-muted">{status.version}</span>}
        {reportsCost !== undefined && (
          <span className="ml-auto text-xs text-text-muted">
            {reportsCost ? 'Reports cost' : 'Reports tokens only'}
          </span>
        )}
      </div>
      {status.usable ? (
        <div className="text-xs text-text-muted">Signed in with {status.auth_label}</div>
      ) : (
        detail && <div className="text-xs text-text-muted">{detail}</div>
      )}
      {status.fix.length > 0 && (
        <>
          <div className="text-xs text-text-muted">Run in a terminal, then Check again:</div>
          {status.fix.map((command) => (
            <CommandLine key={command} command={command} />
          ))}
        </>
      )}
      {status.notice && <p className="notice">{status.notice}</p>}
    </section>
  )
}

function CommandLine({ command }: { command: string }) {
  const [copied, setCopied] = useState('')
  return (
    <div className="tool-fix">
      <code>{command}</code>
      <span role="status" className="text-xs text-text-muted">
        {copied}
      </span>
      <button
        className="btn h-6 px-2 text-xs"
        onClick={() => {
          navigator.clipboard.writeText(command).then(
            () => setCopied('Copied'),
            () => setCopied('Could not copy'),
          )
        }}
      >
        Copy
      </button>
    </div>
  )
}
