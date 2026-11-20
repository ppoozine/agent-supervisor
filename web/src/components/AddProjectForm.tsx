import { useState } from 'react'

import { addProject, errorMessage } from '../api'
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
      error={error}
      onSubmit={() => void submit()}
      onClose={onClose}
    >
      <label>
        Repo path
        <input
          value={repoPath}
          onChange={(event) => setRepoPath(event.target.value)}
          placeholder="/Users/you/code/my-repo"
          required
          autoFocus
        />
      </label>
      <label className="checkbox">
        <input
          type="checkbox"
          checked={requireReview}
          onChange={(event) => setRequireReview(event.target.checked)}
        />
        Require an approving review before Ready to merge
      </label>
    </Dialog>
  )
}
