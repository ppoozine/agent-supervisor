import { useEffect, useState } from 'react'

import { checkSetup, errorMessage, getSetup } from '../api'
import { timeAgo } from '../format'
import type { Setup, ToolStatus } from '../types'

interface SettingsPageProps {
  /** Called after a fresh check, so the rest of the page can pick up which agents are usable. */
  onChecked: () => void
}

/** Which agents and whether gh can be used, and what to run in a terminal for those that cannot. */
export function SettingsPage({ onChecked }: SettingsPageProps) {
  const [setup, setSetup] = useState<Setup | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [isChecking, setIsChecking] = useState(false)

  useEffect(() => {
    let isCurrent = true
    getSetup()
      .then((loaded) => isCurrent && setSetup(loaded))
      .catch((failure: unknown) => isCurrent && setError(errorMessage(failure)))
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

  return (
    <div className="settings">
      <div className="settings-header">
        <h2>Agents</h2>
        {setup && <span className="muted">Checked {timeAgo(setup.checked_at)}</span>}
        <button onClick={() => void checkAgain()} disabled={isChecking}>
          {isChecking ? 'Checking…' : 'Check again'}
        </button>
      </div>
      <p className="muted">
        agent-supervisor runs the agents you have installed and signed in to yourself. It never signs
        you in and never stores keys or tokens. After fixing something in a terminal, check again;
        environment variables only take effect when agsv serve is started from a shell that has them.
      </p>
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      {setup && (
        <>
          {setup.agents.map((agent) => (
            <ToolSection key={agent.agent_type} name={agent.display_name} status={agent} />
          ))}
          <h2>GitHub</h2>
          <ToolSection name="gh" status={setup.github} />
        </>
      )}
    </div>
  )
}

function ToolSection({ name, status }: { name: string; status: ToolStatus }) {
  const state = status.usable ? 'Ready' : status.installed ? 'Not ready' : 'Not installed'
  // The badge already says "Not installed"; repeat the detail only when it adds something.
  const detail = status.detail?.toLowerCase() === state.toLowerCase() ? null : status.detail
  return (
    <section className="tool" aria-label={name}>
      <div className="tool-header">
        <strong>{name}</strong>
        <span className={`tool-state ${status.usable ? 'is-usable' : 'is-unusable'}`}>{state}</span>
        {status.version && <span className="muted">{status.version}</span>}
      </div>
      {status.usable ? (
        <div>Signed in with {status.auth_label}</div>
      ) : (
        detail && <div>{detail}</div>
      )}
      {status.fix.length > 0 && (
        <>
          <div className="muted">Run in a terminal:</div>
          <pre className="tool-fix">
            <code>{status.fix.join('\n')}</code>
          </pre>
        </>
      )}
      {status.notice && <p className="notice">{status.notice}</p>}
    </section>
  )
}
