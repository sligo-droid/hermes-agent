import type { ScrollBoxHandle } from '@hermes/ink'
import type { RefObject } from 'react'

import type { TurnState } from './turnStore.js'

export type LiveTailFollowSignal = string

export const liveTailFollowSignal = (state: TurnState): LiveTailFollowSignal => {
  const segmentKey = state.streamSegments
    .map(segment => `${segment.kind ?? ''}:${segment.role}:${segment.text.length}:${segment.thinking?.length ?? 0}:${segment.tools?.length ?? 0}`)
    .join('|')

  const active = Boolean(
    state.streaming ||
      state.streamPendingTools.length ||
      state.streamSegments.length ||
      state.reasoning.trim() ||
      state.reasoningActive ||
      state.tools.length ||
      state.subagents.length ||
      state.todos.length
  )

  return active
    ? [
        state.streaming,
        state.streamPendingTools.join('\u0000'),
        segmentKey,
        state.reasoning,
        state.reasoningActive ? 'reasoning-active' : '',
        state.tools.length,
        state.subagents.length,
        state.todos.length
      ].join('\u0001')
    : ''
}

export const shouldFollowLiveTail = (
  previous: LiveTailFollowSignal | null,
  next: LiveTailFollowSignal,
  sticky: boolean
): boolean => sticky && next !== '' && previous !== next

export const scheduleStickyTailFollow = ({
  requestPaint,
  scrollRef,
  setTimer
}: {
  requestPaint?: () => void
  scrollRef: RefObject<ScrollBoxHandle | null>
  setTimer?: (cb: () => void) => ReturnType<typeof setTimeout>
}) => {
  const schedule = setTimer ?? (cb => setTimeout(cb, 0))

  return schedule(() => {
    if (!scrollRef.current?.isSticky()) {
      return
    }

    scrollRef.current.scrollToBottom()
    requestPaint?.()
  })
}
