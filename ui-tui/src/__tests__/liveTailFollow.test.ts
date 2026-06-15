import { describe, expect, it, vi } from 'vitest'

import { liveTailFollowSignal, scheduleStickyTailFollow, shouldFollowLiveTail } from '../app/liveTailFollow.js'
import type { TurnState } from '../app/turnStore.js'

const baseTurnState = (): TurnState => ({
  activity: [],
  outcome: '',
  reasoning: '',
  reasoningActive: false,
  reasoningStreaming: false,
  reasoningTokens: 0,
  streamPendingTools: [],
  streamSegments: [],
  streaming: '',
  subagents: [],
  todoCollapsed: false,
  todos: [],
  toolTokens: 0,
  tools: [],
  turnTrail: []
})

describe('live tail bottom follow', () => {
  it('requests a follow when sticky live streaming text changes', () => {
    const first = liveTailFollowSignal({ ...baseTurnState(), streaming: 'hello' })
    const next = liveTailFollowSignal({ ...baseTurnState(), streaming: 'hello\nworld' })

    expect(shouldFollowLiveTail(first, next, true)).toBe(true)
  })

  it('does not follow when manual scrolling broke sticky mode', () => {
    const first = liveTailFollowSignal({ ...baseTurnState(), streaming: 'hello' })
    const next = liveTailFollowSignal({ ...baseTurnState(), streaming: 'hello\nworld' })

    expect(shouldFollowLiveTail(first, next, false)).toBe(false)
  })

  it('does not follow when the live tail is inactive', () => {
    const next = liveTailFollowSignal(baseTurnState())

    expect(shouldFollowLiveTail(null, next, true)).toBe(false)
  })

  it('defers the scroll-to-bottom until the latest layout can be measured', () => {
    const scrollToBottom = vi.fn()
    const requestPaint = vi.fn()

    const setTimer = vi.fn((cb: () => void) => {
      cb()

      return 1 as unknown as ReturnType<typeof setTimeout>
    })

    scheduleStickyTailFollow({
      requestPaint,
      scrollRef: { current: { isSticky: () => true, scrollToBottom } } as any,
      setTimer
    })

    expect(setTimer).toHaveBeenCalledOnce()
    expect(scrollToBottom).toHaveBeenCalledOnce()
    expect(requestPaint).toHaveBeenCalledOnce()
  })

  it('does not resnap if sticky mode is broken before the deferred follow runs', () => {
    const scrollToBottom = vi.fn()
    const requestPaint = vi.fn()

    scheduleStickyTailFollow({
      requestPaint,
      scrollRef: { current: { isSticky: () => false, scrollToBottom } } as any,
      setTimer: cb => {
        cb()

        return 1 as unknown as ReturnType<typeof setTimeout>
      }
    })

    expect(scrollToBottom).not.toHaveBeenCalled()
    expect(requestPaint).not.toHaveBeenCalled()
  })
})
