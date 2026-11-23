import { useState } from 'react'

import { addProject, errorMessage } from '../api'
import { currentSubmitShortcutLabel } from '../keys'
import type { Project } from '../types'
import { Dialog } from './Dialog'

interface AddProjectFormProps {
  onAdded: (project: Project) => void
  onClose: () => void
}

/** Add a local repo. The daemon detects the GitHub repo and base branch from the repo. */
export function AddProjectForm({ onAdded, onClose }: AddProjectFormProps) {
  const [repoPath, setRepoPath] = useState('')
  const [requireReview, setRequireReview] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [isBusy, setIsBusy] = useState(false)

  const submit = async (): Promise<void> => {
    setIsBusy(true)
    setError(null)
    try {
      onAdded(await addProject({ repo_path: repoPath.trim(), require_review: requireReview }))
    } catch (failure) {
      setError(errorMessage(failure))
      setIsBusy(false)
    }
  }

  return (
    <Dialog
      title="Add project"
      submitLabel="Add project"
      isBusy={isBusy}
      error={null}
      shortcutHint={`${currentSubmitShortcutLabel()} to add`}
      onSubmit={() => void submit()}
      onClose={onClose}
    >
      <label className="field">
        Repo path
        <input
          className="input font-mono"
          value={repoPath}
          onChange={(event) => setRepoPath(event.target.value)}
          aria-invalid={error !== null}
          aria-describedby={error ? 'repo-path-error' : undefined}
          placeholder="/Users/you/code/my-repo"
          required
          autoFocus
        />
        {error && (
          <span id="repo-path-error" className="field-error" role="alert">
            {error}
          </span>
        )}
      </label>
      <div className="note">
        The folder needs:
        <ul className="mt-1 list-disc pl-5">
          <li>to be the top of a local git repository</li>
          <li>a GitHub remote that gh can reach; tasks open their pull requests there</li>
          <li>no other project already using it</li>
        </ul>
      </div>
      <label className="check">
        <input
          type="checkbox"
          checked={requireReview}
          onChange={(event) => setRequireReview(event.target.checked)}
        />
        <span>
          <span className="font-medium">Require an approving review</span>
          <span className="block text-xs text-text-muted">
            Tasks reach Ready to merge only after someone approves the PR.
          </span>
        </span>
      </label>
    </Dialog>
  )
}
