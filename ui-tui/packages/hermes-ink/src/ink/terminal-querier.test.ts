import { describe, expect, it } from 'vitest'

import { DECRPM_STATUS } from './parse-keypress.js'
import { isDecrpmModeSettable } from './terminal-querier.js'

describe('DECRPM settable-mode support', () => {
  it('treats settable DEC private modes as supported', () => {
    expect(isDecrpmModeSettable({ type: 'decrpm', mode: 2026, status: DECRPM_STATUS.SET })).toBe(true)
    expect(isDecrpmModeSettable({ type: 'decrpm', mode: 2026, status: DECRPM_STATUS.RESET })).toBe(true)
  })

  it('treats unrecognized, permanent, or missing DECRPM replies as unsupported', () => {
    expect(isDecrpmModeSettable({ type: 'decrpm', mode: 2026, status: DECRPM_STATUS.NOT_RECOGNIZED })).toBe(false)
    expect(isDecrpmModeSettable({ type: 'decrpm', mode: 2026, status: DECRPM_STATUS.PERMANENTLY_SET })).toBe(false)
    expect(isDecrpmModeSettable({ type: 'decrpm', mode: 2026, status: DECRPM_STATUS.PERMANENTLY_RESET })).toBe(false)
    expect(isDecrpmModeSettable(undefined)).toBe(false)
  })
})
