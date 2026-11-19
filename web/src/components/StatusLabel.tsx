import { labelTone } from '../format'

/** 帶顏色圓點的狀態標籤。文字就是 API 回傳的 `label`。 */
export function StatusLabel({ label, column }: { label: string; column: string }) {
  return (
    <span className={`status tone-${labelTone(label, column)}`}>
      <span className="dot" />
      {label}
    </span>
  )
}
