import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { describe, expect, it } from 'vitest'

import { MessageLine } from '../components/messageLine.js'
import { toTranscriptMessages } from '../domain/messages.js'
import { upsert } from '../lib/messages.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

describe('toTranscriptMessages', () => {
  it('preserves assistant tool-call rows so resume does not drop prior turns', () => {
    const rows = [
      { role: 'user', text: 'first prompt' },
      { role: 'tool', context: 'repo', name: 'search_files' },
      { role: 'assistant', text: 'first answer' },
      { role: 'user', text: 'second prompt' }
    ]

    expect(toTranscriptMessages(rows).map(msg => [msg.role, msg.text])).toEqual([
      ['user', 'first prompt'],
      ['assistant', 'first answer'],
      ['user', 'second prompt']
    ])
    expect(toTranscriptMessages(rows)[1]?.tools?.[0]).toContain('Search Files')
  })

  it('keeps tool result text visible when resuming sessions with output-only rows', () => {
    const rows = [
      { role: 'user', text: 'run pwd' },
      { role: 'tool', context: 'pwd', name: 'terminal', text: '/home/droid/hermes' }
    ]

    expect(toTranscriptMessages(rows).map(msg => [msg.role, msg.text])).toEqual([
      ['user', 'run pwd'],
      ['tool', '/home/droid/hermes'],
      ['system', '']
    ])
    expect(toTranscriptMessages(rows)[2]?.tools?.[0]).toContain('Terminal')
  })

  it('flushes trailing resumed tool calls into a collapsed details row', () => {
    const rows = [
      { role: 'user', text: 'inspect files' },
      { role: 'tool', context: 'src', name: 'search_files' }
    ]

    const messages = toTranscriptMessages(rows)

    expect(messages.map(msg => [msg.kind, msg.role, msg.text])).toEqual([
      [undefined, 'user', 'inspect files'],
      ['trail', 'system', '']
    ])
    expect(messages[1]?.tools?.[0]).toContain('Search Files')
  })

  it('preserves resumed assistant reasoning as collapsed details', () => {
    const messages = toTranscriptMessages([{ role: 'assistant', text: 'answer', thinking: 'private chain' }])

    expect(messages[0]).toMatchObject({
      role: 'assistant',
      text: 'answer',
      thinking: 'private chain',
      detailsCollapsedByDefault: true
    })
  })
})

describe('MessageLine', () => {
  it('preserves a separator after compound user prompt glyphs in transcript rows', () => {
    const stdout = new PassThrough()
    const stdin = new PassThrough()
    const stderr = new PassThrough()
    let output = ''

    Object.assign(stdout, { columns: 80, isTTY: false, rows: 24 })
    Object.assign(stdin, { isTTY: false })
    Object.assign(stderr, { isTTY: false })
    stdout.on('data', chunk => {
      output += chunk.toString()
    })

    const t = {
      ...DEFAULT_THEME,
      brand: { ...DEFAULT_THEME.brand, prompt: 'Ψ >' }
    }

    const instance = renderSync(
      React.createElement(MessageLine, {
        cols: 80,
        msg: { role: 'user', text: 'Okay' },
        t
      }),
      {
        patchConsole: false,
        stderr: stderr as NodeJS.WriteStream,
        stdin: stdin as NodeJS.ReadStream,
        stdout: stdout as NodeJS.WriteStream
      }
    )

    instance.unmount()
    instance.cleanup()

    const renderedLine = stripAnsi(output)
      .split('\n')
      .find(line => line.includes('Okay'))

    expect(renderedLine).toContain('Ψ > Okay')
  })
})

describe('upsert', () => {
  it('appends when last role differs', () => {
    expect(upsert([{ role: 'user', text: 'hi' }], 'assistant', 'hello')).toHaveLength(2)
  })

  it('replaces when last role matches', () => {
    expect(upsert([{ role: 'assistant', text: 'partial' }], 'assistant', 'full')[0]!.text).toBe('full')
  })

  it('appends to empty', () => {
    expect(upsert([], 'user', 'first')).toEqual([{ role: 'user', text: 'first' }])
  })

  it('does not mutate', () => {
    const prev = [{ role: 'user' as const, text: 'hi' }]
    upsert(prev, 'assistant', 'yo')
    expect(prev).toHaveLength(1)
  })
})
