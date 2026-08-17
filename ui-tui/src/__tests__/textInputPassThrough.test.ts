import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { describe, expect, it } from 'vitest'

import {
  TextInput,
  shouldBufferInputAsPaste,
  shouldInsertPlainBurst,
  shouldPassThroughToGlobalHandler,
  shouldPreserveCtrlJNewline
} from '../components/textInput.js'
import { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } from '../lib/platform.js'

const key = (overrides: Record<string, unknown> = {}) => ({ ctrl: false, meta: false, ...overrides }) as any
const tick = () => new Promise<void>(resolve => setTimeout(resolve, 25))

function ttyStream() {
  const stream = new PassThrough() as PassThrough & {
    columns: number
    isRaw: boolean
    isTTY: boolean
    ref: () => typeof stream
    rows: number
    setRawMode: (mode: boolean) => typeof stream
    unref: () => typeof stream
  }

  stream.columns = 80
  stream.rows = 24
  stream.isTTY = true
  stream.isRaw = false
  stream.setRawMode = (mode: boolean) => {
    stream.isRaw = mode

    return stream
  }
  stream.ref = () => stream
  stream.unref = () => stream

  return stream
}

describe('shouldPreserveCtrlJNewline', () => {
  it('preserves Ctrl+J as newline in Ghostty even when tmux masks TERM/TERM_PROGRAM', () => {
    expect(
      shouldPreserveCtrlJNewline({
        GHOSTTY_RESOURCES_DIR: '/usr/share/ghostty',
        TERM: 'tmux-256color',
        TERM_PROGRAM: 'tmux'
      })
    ).toBe(true)
  })

  it('keeps bare local POSIX LF-compatible prompts submitting on Ctrl+J', () => {
    expect(shouldPreserveCtrlJNewline({ TERM: 'xterm-256color' })).toBe(false)
  })
})

describe('shouldPassThroughToGlobalHandler', () => {
  it('passes through the configured voice shortcut while composer is focused', () => {
    expect(shouldPassThroughToGlobalHandler('o', key({ ctrl: true }), parseVoiceRecordKey('ctrl+o'))).toBe(true)
    expect(shouldPassThroughToGlobalHandler('r', key({ meta: true }), parseVoiceRecordKey('alt+r'))).toBe(true)
    expect(shouldPassThroughToGlobalHandler(' ', key({ ctrl: true }), parseVoiceRecordKey('ctrl+space'))).toBe(true)
    expect(
      shouldPassThroughToGlobalHandler('', key({ ctrl: true, return: true }), parseVoiceRecordKey('ctrl+enter'))
    ).toBe(true)
  })

  it('keeps the legacy default pass-through when no custom key is provided', () => {
    expect(shouldPassThroughToGlobalHandler('b', key({ ctrl: true }), DEFAULT_VOICE_RECORD_KEY)).toBe(true)
    expect(shouldPassThroughToGlobalHandler('b', key({ ctrl: true }))).toBe(true)
  })

  it('does not swallow ordinary typing keys', () => {
    expect(shouldPassThroughToGlobalHandler('h', key(), parseVoiceRecordKey('ctrl+o'))).toBe(false)
    expect(shouldPassThroughToGlobalHandler('o', key(), parseVoiceRecordKey('ctrl+o'))).toBe(false)
  })

  it('always passes through non-voice global control keys', () => {
    expect(shouldPassThroughToGlobalHandler('c', key({ ctrl: true }))).toBe(true)
    expect(shouldPassThroughToGlobalHandler('x', key({ ctrl: true }))).toBe(true)
    expect(shouldPassThroughToGlobalHandler('o', key({ ctrl: true }))).toBe(true)
    expect(shouldPassThroughToGlobalHandler('', key({ escape: true }))).toBe(true)
    expect(shouldPassThroughToGlobalHandler('', key({ tab: true }))).toBe(true)
    expect(shouldPassThroughToGlobalHandler('', key({ pageUp: true }))).toBe(true)
    expect(shouldPassThroughToGlobalHandler('', key({ pageDown: true }))).toBe(true)
  })
})

describe('shouldBufferInputAsPaste', () => {
  it('does not buffer coalesced printable typing bursts', () => {
    expect(shouldBufferInputAsPaste('ab', false)).toBe(false)
  })

  it('buffers actual paste input', () => {
    expect(shouldBufferInputAsPaste('ab', true)).toBe(true)
    expect(shouldBufferInputAsPaste('a\nb', false)).toBe(true)
  })
})

describe('shouldInsertPlainBurst', () => {
  it('accepts same-line printable typing bursts only', () => {
    expect(shouldInsertPlainBurst('ab', false)).toBe(true)
    expect(shouldInsertPlainBurst('a', false)).toBe(false)
    expect(shouldInsertPlainBurst('a\nb', false)).toBe(false)
    expect(shouldInsertPlainBurst('ab', true)).toBe(false)
  })
})

describe('TextInput coalesced typing bursts', () => {
  it('inserts a printable burst once', async () => {
    const stdin = ttyStream()
    const stdout = ttyStream()
    const stderr = ttyStream()
    const changes: string[] = []

    const app = renderSync(
      React.createElement(TextInput, {
        onChange: (value: string) => changes.push(value),
        value: ''
      }),
      {
        exitOnCtrlC: false,
        patchConsole: false,
        stderr: stderr as unknown as NodeJS.WriteStream,
        stdin: stdin as unknown as NodeJS.ReadStream,
        stdout: stdout as unknown as NodeJS.WriteStream
      }
    )

    stdin.write('ab')
    await tick()
    app.unmount()

    expect(changes).toContain('ab')
    expect(changes).not.toContain('abab')
    expect(changes.at(-1)).toBe('ab')
  })
})
