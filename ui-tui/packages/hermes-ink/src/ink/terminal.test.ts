import { afterEach, describe, expect, it } from 'vitest'

import {
  enableSynchronizedOutputFromTerminalQuery,
  isCmuxSession,
  isRuntimeSynchronizedOutputSupported,
  isSynchronizedOutputSupported,
  needsAltScreenResizeScrollbackClear,
  resetSynchronizedOutputSupportForTest
} from './terminal.js'

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
  afterEach(() => {
    resetSynchronizedOutputSupportForTest(false)
  })

  it('treats cmux as Ghostty-backed even when Ghostty env vars are missing', () => {
    expect(isCmuxSession({ CMUX_WORKSPACE_ID: 'workspace:1' })).toBe(true)
    expect(isSynchronizedOutputSupported({ CMUX_WORKSPACE_ID: 'workspace:1' })).toBe(true)
    expect(isSynchronizedOutputSupported({ CMUX_SURFACE_ID: 'surface:1' })).toBe(true)
  })

  it('keeps tmux out of synchronized output even inside cmux', () => {
    expect(isSynchronizedOutputSupported({ CMUX_WORKSPACE_ID: 'workspace:1', TMUX: '/tmp/tmux' })).toBe(false)
  })

  it('starts disabled when SSH leaves only generic xterm TERM', () => {
    expect(isSynchronizedOutputSupported({ TERM: 'xterm-256color' })).toBe(false)
  })

  it('detects known synchronized-output terminals from environment', () => {
    expect(isSynchronizedOutputSupported({ TERM_PROGRAM: 'ghostty' })).toBe(true)
    expect(isSynchronizedOutputSupported({ TERM_PROGRAM: 'iTerm.app' })).toBe(true)
    expect(isSynchronizedOutputSupported({ TERM: 'xterm-kitty' })).toBe(true)
    expect(isSynchronizedOutputSupported({ TERM: 'xterm-ghostty' })).toBe(true)
  })

  it('keeps tmux out of synchronized output from TERM_PROGRAM', () => {
    expect(isSynchronizedOutputSupported({ TERM_PROGRAM: 'tmux' })).toBe(false)
  })

  it('allows a positive terminal query to enable synchronized output at runtime', () => {
    resetSynchronizedOutputSupportForTest(false)

    enableSynchronizedOutputFromTerminalQuery({ TERM: 'xterm-256color' })

    expect(isRuntimeSynchronizedOutputSupported()).toBe(true)
  })

  it('does not let terminal query override tmux safety gate', () => {
    resetSynchronizedOutputSupportForTest(false)

    enableSynchronizedOutputFromTerminalQuery({ TERM: 'xterm-256color', TMUX: '/tmp/tmux' })
    expect(isRuntimeSynchronizedOutputSupported()).toBe(false)

    enableSynchronizedOutputFromTerminalQuery({ TERM_PROGRAM: 'tmux' })
    expect(isRuntimeSynchronizedOutputSupported()).toBe(false)
  })

  it('preserves static env-positive support when runtime query is absent', () => {
    resetSynchronizedOutputSupportForTest(true)

    expect(isRuntimeSynchronizedOutputSupported()).toBe(true)
  })
})
