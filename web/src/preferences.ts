/** Read an optional UI preference even when browser storage is unavailable. */
export function readPreference(key: string): string | null {
  try {
    return localStorage.getItem(`agsv.${key}`)
  } catch {
    return null
  }
}

/** Save a UI preference without making storage a requirement for using the app. */
export function savePreference(key: string, value: string): void {
  try {
    localStorage.setItem(`agsv.${key}`, value)
  } catch {
    // Private browser contexts may disallow storage; the current view still works.
  }
}
