import { useEffect, useRef, type FormEvent, type ReactNode } from 'react'

interface DialogProps {
  title: string
  submitLabel: string
  isBusy: boolean
  /** False keeps the submit button disabled, e.g. while a required choice has no options. */
  canSubmit?: boolean
  /** Shown above the buttons; forms that show an error under a field pass null. */
  error: string | null
  /** Shown at the left of the footer when a field in the form submits on a keyboard shortcut. */
  shortcutHint?: string
  onSubmit: () => void
  onClose: () => void
  children: ReactNode
}

/**
 * A form laid over the page: a title bar with a close button, the fields, and a footer with the
 * shortcut, Cancel and the main button. When the API refuses, the reason is shown above the footer.
 *
 * Uses the browser's native modal: focus stays in the form, the background is inert, and
 * Escape closes it.
 * It cannot be closed between submitting and the response, or the response would act on a
 * form that no longer exists.
 */
export function Dialog(props: DialogProps) {
  const element = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    const dialog = element.current
    if (dialog === null || dialog.open) return
    // The test environment (jsdom) has no showModal, so fall back to a plain open state.
    if (typeof dialog.showModal === 'function') dialog.showModal()
    else dialog.setAttribute('open', '')
  }, [])

  const close = (): void => {
    if (!props.isBusy) props.onClose()
  }
  const submit = (event: FormEvent): void => {
    event.preventDefault()
    props.onSubmit()
  }

  return (
    <dialog
      ref={element}
      className="dialog"
      aria-label={props.title}
      onCancel={(event) => {
        // Escape: we decide whether to close, instead of letting the browser close it directly.
        event.preventDefault()
        close()
      }}
      onMouseDown={(event) => event.target === event.currentTarget && close()}
    >
      <form onSubmit={submit}>
        <div className="dialog-head">
          <h2>{props.title}</h2>
          <button
            type="button"
            className="ghost"
            aria-label="Close"
            onClick={close}
            disabled={props.isBusy}
          >
            <span aria-hidden="true">✕</span>
          </button>
        </div>
        <div className="dialog-body">
          {props.children}
          {props.error && (
            <div className="alert" role="alert">
              {props.error}
            </div>
          )}
        </div>
        <div className="dialog-foot">
          <kbd className="key-hint">{props.shortcutHint}</kbd>
          <button type="button" className="btn" onClick={close} disabled={props.isBusy}>
            Cancel
          </button>
          <button
            type="submit"
            className="primary"
            disabled={props.isBusy || props.canSubmit === false}
          >
            {props.isBusy ? 'Working…' : props.submitLabel}
          </button>
        </div>
      </form>
    </dialog>
  )
}
