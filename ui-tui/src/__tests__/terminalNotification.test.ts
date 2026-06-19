import { describe, expect, it, vi } from 'vitest'

import {
  isUnsafeCompletionNotificationSession,
  osc777Notify,
  sanitizeOscField,
  wrapForMultiplexer,
  writeCompletionNotification
} from '../lib/terminalNotification.js'

describe('terminalNotification', () => {
  it('builds OSC 777 completion notifications', () => {
    expect(osc777Notify()).toBe('\x1b]777;notify;Hermes;Response complete\x07')
  })

  it('sanitizes OSC fields', () => {
    expect(sanitizeOscField('Her;mes\x07')).toBe('Her,mes')
    expect(osc777Notify('Her;mes\x07', 'Done\x1b now')).toBe('\x1b]777;notify;Her,mes;Done  now\x07')
  })

  it('wraps notifications for tmux passthrough', () => {
    const wrapped = wrapForMultiplexer('\x1b]777;notify;Hermes;Done\x07', { TMUX: '/tmp/tmux' })

    expect(wrapped).toContain('\x1bPtmux;')
    expect(wrapped).toContain('\x1b\x1b]777')
    expect(wrapped.endsWith('\x1b\\')).toBe(true)
  })

  it('detects terminal sessions where completion notifications are unsafe', () => {
    expect(isUnsafeCompletionNotificationSession({})).toBe(false)
    expect(isUnsafeCompletionNotificationSession({ TMUX: '/tmp/tmux' })).toBe(true)
    expect(isUnsafeCompletionNotificationSession({ STY: 'screen' })).toBe(true)
    expect(isUnsafeCompletionNotificationSession({ CMUX_WORKSPACE_ID: 'workspace' })).toBe(true)
    expect(isUnsafeCompletionNotificationSession({ CMUX_SURFACE_ID: 'surface' })).toBe(true)
    expect(isUnsafeCompletionNotificationSession({ CMUX_TAB_ID: 'tab' })).toBe(true)
    expect(isUnsafeCompletionNotificationSession({ CMUX_PANEL_ID: 'panel' })).toBe(true)
    expect(isUnsafeCompletionNotificationSession({ CMUX_SOCKET_PATH: '/tmp/cmux.sock' })).toBe(true)
    expect(isUnsafeCompletionNotificationSession({ __CFBundleIdentifier: 'com.cmuxterm.app' })).toBe(true)
    expect(isUnsafeCompletionNotificationSession({ SSH_CONNECTION: 'client server' })).toBe(true)
    expect(isUnsafeCompletionNotificationSession({ SSH_TTY: '/dev/pts/9' })).toBe(true)
    expect(isUnsafeCompletionNotificationSession({ TERM_PROGRAM: 'ghostty' })).toBe(false)
    expect(isUnsafeCompletionNotificationSession({ TERM: 'xterm-ghostty' })).toBe(false)
    expect(isUnsafeCompletionNotificationSession({ TERM_PROGRAM: 'cmux' })).toBe(true)
  })

  it('writes only when enabled and attached to a TTY outside unsafe sessions', () => {
    const write = vi.fn()
    const stdout = { isTTY: true, write }

    expect(writeCompletionNotification(stdout, true, {})).toBe(true)
    expect(write).toHaveBeenCalledWith(osc777Notify())

    expect(writeCompletionNotification(stdout, false, {})).toBe(false)
    expect(writeCompletionNotification({ isTTY: false, write }, true, {})).toBe(false)
  })

  it('suppresses TUI completion notifications in unsafe terminal sessions', () => {
    const write = vi.fn()
    const stdout = { isTTY: true, write }

    expect(writeCompletionNotification(stdout, true, { TMUX: '/tmp/tmux' })).toBe(false)
    expect(writeCompletionNotification(stdout, true, { STY: 'screen' })).toBe(false)
    expect(writeCompletionNotification(stdout, true, { CMUX_WORKSPACE_ID: 'workspace' })).toBe(false)
    expect(writeCompletionNotification(stdout, true, { CMUX_SURFACE_ID: 'surface' })).toBe(false)
    expect(writeCompletionNotification(stdout, true, { CMUX_TAB_ID: 'tab' })).toBe(false)
    expect(writeCompletionNotification(stdout, true, { __CFBundleIdentifier: 'com.cmuxterm.app' })).toBe(false)
    expect(writeCompletionNotification(stdout, true, { SSH_CONNECTION: 'client server' })).toBe(false)
    expect(writeCompletionNotification(stdout, true, { SSH_TTY: '/dev/pts/9' })).toBe(false)
    expect(write).not.toHaveBeenCalled()

    expect(writeCompletionNotification(stdout, true, { TERM_PROGRAM: 'ghostty' })).toBe(true)
    expect(writeCompletionNotification(stdout, true, { TERM: 'xterm-ghostty' })).toBe(true)
  })
})
