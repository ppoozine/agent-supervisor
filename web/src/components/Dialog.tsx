import { useEffect, useRef, type FormEvent, type ReactNode } from 'react'

interface DialogProps {
  title: string
  submitLabel: string
  isBusy: boolean
  error: string | null
  onSubmit: () => void
  onClose: () => void
  children: ReactNode
}

/**
 * 蓋在畫面上的表單。API 拒絕時把原因顯示在按鈕上方。
 *
 * 用瀏覽器原生的 modal：焦點留在表單裡，背景不能操作，按 Escape 關閉。
 * 送出之後、回應回來之前不能關，否則回應會作用在已經不存在的表單上。
 */
export function Dialog(props: DialogProps) {
  const element = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    const dialog = element.current
    if (dialog === null || dialog.open) return
    // 測試環境（jsdom）沒有 showModal，退回成一般的開啟狀態。
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
        // Escape：由我們決定關不關，不讓瀏覽器直接關掉。
        event.preventDefault()
        close()
      }}
      onMouseDown={(event) => event.target === event.currentTarget && close()}
    >
      <form className="dialog-form" onSubmit={submit}>
        <h2>{props.title}</h2>
        {props.children}
        {props.error && (
          <div className="alert" role="alert">
            {props.error}
          </div>
        )}
        <div className="dialog-actions">
          <button type="button" className="ghost" onClick={close} disabled={props.isBusy}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={props.isBusy}>
            {props.isBusy ? 'Working…' : props.submitLabel}
          </button>
        </div>
      </form>
    </dialog>
  )
}
