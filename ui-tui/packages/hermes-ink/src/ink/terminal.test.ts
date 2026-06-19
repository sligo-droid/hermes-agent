import { afterEach, describe, expect, it } from 'vitest'

import {
  enableSynchronizedOutputFromTerminalQuery,
  isCmuxSession,
  isRuntimeSynchronizedOutputSupported,
  isSynchronizedOutputSupported,
  isSynchronizedOutputUnsafeSession,
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

  it('treats cmux as an unsafe synchronized-output transport layer', () => {
    expect(isCmuxSession({ CMUX_WORKSPACE_ID: 'workspace:1' })).toBe(true)
    expect(isCmuxSession({ CMUX_SURFACE_ID: 'surface:1' })).toBe(true)
    expect(isCmuxSession({ CMUX_TAB_ID: 'tab:1' })).toBe(true)
    expect(isCmuxSession({ __CFBundleIdentifier: 'com.cmuxterm.app' })).toBe(true)
    expect(isSynchronizedOutputUnsafeSession({ CMUX_WORKSPACE_ID: 'workspace:1' })).toBe(true)
    expect(isSynchronizedOutputSupported({ CMUX_WORKSPACE_ID: 'workspace:1' })).toBe(false)
    expect(isSynchronizedOutputSupported({ CMUX_SURFACE_ID: 'surface:1' })).toBe(false)
  })

  it('disables synchronized output across SSH transports', () => {
    expect(isSynchronizedOutputUnsafeSession({ SSH_CONNECTION: 'client server' })).toBe(true)
    expect(isSynchronizedOutputUnsafeSession({ SSH_TTY: '/dev/pts/9' })).toBe(true)
    expect(isSynchronizedOutputSupported({ TERM_PROGRAM: 'ghostty', SSH_CONNECTION: 'client server' })).toBe(false)
    expect(isSynchronizedOutputSupported({ TERM: 'xterm-ghostty', SSH_TTY: '/dev/pts/9' })).toBe(false)
  })

  it('keeps tmux and screen out of synchronized output', () => {
    expect(isSynchronizedOutputSupported({ CMUX_WORKSPACE_ID: 'workspace:1', TMUX: '/tmp/tmux' })).toBe(false)
    expect(isSynchronizedOutputSupported({ STY: 'screen' })).toBe(false)
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

  it('does not let terminal query override unsafe transport gates', () => {
    resetSynchronizedOutputSupportForTest(false)

    enableSynchronizedOutputFromTerminalQuery({ TERM: 'xterm-256color', TMUX: '/tmp/tmux' })
    expect(isRuntimeSynchronizedOutputSupported()).toBe(false)

    enableSynchronizedOutputFromTerminalQuery({ TERM_PROGRAM: 'tmux' })
    expect(isRuntimeSynchronizedOutputSupported()).toBe(false)

    enableSynchronizedOutputFromTerminalQuery({ CMUX_WORKSPACE_ID: 'workspace:1' })
    expect(isRuntimeSynchronizedOutputSupported()).toBe(false)

    enableSynchronizedOutputFromTerminalQuery({ SSH_TTY: '/dev/pts/9' })
    expect(isRuntimeSynchronizedOutputSupported()).toBe(false)

    enableSynchronizedOutputFromTerminalQuery({ STY: 'screen' })
    expect(isRuntimeSynchronizedOutputSupported()).toBe(false)
  })

  it('preserves static env-positive support when runtime query is absent', () => {
    resetSynchronizedOutputSupportForTest(true)

    expect(isRuntimeSynchronizedOutputSupported()).toBe(true)
  })
})
