import { describe, expect, it } from 'vitest'

import { isCmuxSession, isSynchronizedOutputSupported, needsAltScreenResizeScrollbackClear } from './terminal.js'

describe('terminal resize quirks', () => {
  it('uses a deeper alt-screen resize clear for Apple Terminal', () => {
    expect(needsAltScreenResizeScrollbackClear({ TERM_PROGRAM: 'Apple_Terminal' })).toBe(true)
    expect(needsAltScreenResizeScrollbackClear({ TERM_PROGRAM: ' Apple_Terminal ' })).toBe(true)
  })

  it('keeps the normal resize repaint path for modern terminals', () => {
    expect(needsAltScreenResizeScrollbackClear({ TERM_PROGRAM: 'vscode' })).toBe(false)
    expect(needsAltScreenResizeScrollbackClear({ TERM_PROGRAM: 'iTerm.app' })).toBe(false)
  })
})

describe('synchronized output support', () => {
  it('treats cmux as Ghostty-backed even when Ghostty env vars are missing', () => {
    expect(isCmuxSession({ CMUX_WORKSPACE_ID: 'workspace:1' })).toBe(true)
    expect(isSynchronizedOutputSupported({ CMUX_WORKSPACE_ID: 'workspace:1' })).toBe(true)
    expect(isSynchronizedOutputSupported({ CMUX_SURFACE_ID: 'surface:1' })).toBe(true)
  })

  it('keeps tmux out of synchronized output even inside cmux', () => {
    expect(isSynchronizedOutputSupported({ CMUX_WORKSPACE_ID: 'workspace:1', TMUX: '/tmp/tmux' })).toBe(false)
  })
})
