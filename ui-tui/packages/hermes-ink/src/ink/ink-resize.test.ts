import { EventEmitter } from 'events'

import React from 'react'
import { describe, expect, it } from 'vitest'

import Text from './components/Text.js'
import Ink from './ink.js'
import { CURSOR_HOME, ERASE_SCREEN, RESET_SCROLL_REGION } from './termio/csi.js'
import { ENTER_ALT_SCREEN } from './termio/dec.js'

class FakeTty extends EventEmitter {
  chunks: string[] = []
  columns = 20
  rows = 5
  isTTY = true

  write(chunk: string | Uint8Array, cb?: (err?: Error | null) => void): boolean {
    this.chunks.push(typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8'))
    cb?.()

    return true
  }
}

const tick = () => new Promise<void>(resolve => queueMicrotask(resolve))

describe('Ink resize healing', () => {
  it('reasserts the default scroll region before steady alt-screen repaints', async () => {
    const stdout = new FakeTty()
    const stdin = new FakeTty()
    const stderr = new FakeTty()

    const ink = new Ink({
      exitOnCtrlC: false,
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream
    })

    ink.setAltScreenActive(true)
    ink.render(React.createElement(Text, null, 'hello'))
    ink.onRender()
    await tick()

    expect(stdout.chunks.join('')).toContain(RESET_SCROLL_REGION + CURSOR_HOME)

    ink.unmount()
  })

  it('heals same-dimension alt-screen resize events with an erase before repaint', async () => {
    const stdout = new FakeTty()
    const stdin = new FakeTty()
    const stderr = new FakeTty()

    const ink = new Ink({
      exitOnCtrlC: false,
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream
    })

    ink.setAltScreenActive(true)
    ink.render(React.createElement(Text, null, 'hello'))
    ink.onRender()
    stdout.chunks = []

    stdout.emit('resize')
    ink.onRender()
    await tick()

    expect(stdout.chunks.join('')).toContain(RESET_SCROLL_REGION + ERASE_SCREEN + CURSOR_HOME)

    ink.unmount()
  })

  it('heals requested alt-screen repaints with an erase before repaint', async () => {
    const stdout = new FakeTty()
    const stdin = new FakeTty()
    const stderr = new FakeTty()

    const ink = new Ink({
      exitOnCtrlC: false,
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream
    })

    ink.setAltScreenActive(true)
    ink.render(React.createElement(Text, null, 'hello'))
    ink.onRender()
    stdout.chunks = []

    ink.requestRepaint()
    ink.render(React.createElement(Text, null, 'hello'))
    ink.onRender()
    await tick()

    expect(stdout.chunks.join('')).toContain(RESET_SCROLL_REGION + ERASE_SCREEN + CURSOR_HOME)

    ink.unmount()
  })

  it('resets scroll margins when re-entering alt-screen after a terminal-mode gap', async () => {
    const stdout = new FakeTty()
    const stdin = new FakeTty()
    const stderr = new FakeTty()

    const ink = new Ink({
      exitOnCtrlC: false,
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream
    })

    ink.setAltScreenActive(true)
    ink.render(React.createElement(Text, null, 'hello'))
    ink.onRender()
    stdout.chunks = []

    ink.reassertTerminalModes(true)
    await tick()

    expect(stdout.chunks.join('')).toContain(
      ENTER_ALT_SCREEN + RESET_SCROLL_REGION + ERASE_SCREEN + CURSOR_HOME
    )

    ink.unmount()
  })
})
