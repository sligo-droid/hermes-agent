import { describe, expect, it } from 'vitest'

import { shouldStickTranscriptToBottom } from '../components/appLayout.js'

describe('AppLayout transcript stickiness', () => {
  it('does not pin the startup intro-only transcript to the bottom', () => {
    expect(shouldStickTranscriptToBottom({ empty: true })).toBe(false)
  })

  it('keeps tail-follow enabled once real transcript content exists', () => {
    expect(shouldStickTranscriptToBottom({ empty: false })).toBe(true)
  })
})
