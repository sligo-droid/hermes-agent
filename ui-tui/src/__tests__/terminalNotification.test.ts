import { describe, expect, it, vi } from 'vitest'

import {
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

  it('writes only when enabled and attached to a TTY', () => {
    const write = vi.fn()
    const stdout = { isTTY: true, write }

    expect(writeCompletionNotification(stdout, true, {})).toBe(true)
    expect(write).toHaveBeenCalledWith(osc777Notify())

    expect(writeCompletionNotification(stdout, false, {})).toBe(false)
    expect(writeCompletionNotification({ isTTY: false, write }, true, {})).toBe(false)
  })
})
