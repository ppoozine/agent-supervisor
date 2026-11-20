import { labelTone } from '../format'

/** A status label with a colored dot. The text is the `label` returned by the API. */
export function StatusLabel({ label, column }: { label: string; column: string }) {
  return (
    <span className={`status tone-${labelTone(label, column)}`}>
      <span className="dot" />
      {label}
    </span>
  )
}
