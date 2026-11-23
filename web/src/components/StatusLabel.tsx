import { columnTone } from '../format'

/** A status label with a colored dot. The text is the `label` returned by the API. */
export function StatusLabel({
  label,
  column,
  active = false,
}: {
  label: string
  column: string
  active?: boolean
}) {
  return (
    <span className={`status tone-${columnTone(column)}`}>
      <span className={`dot${active ? ' is-live' : ''}`} />
      {label}
    </span>
  )
}
