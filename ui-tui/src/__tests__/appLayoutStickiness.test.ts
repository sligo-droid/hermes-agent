import { describe, expect, it } from 'vitest'

import type { AppLayoutTranscriptProps } from '../app/interfaces.js'
import {
  shouldRenderTranscriptScrollBox,
  shouldStickTranscriptToBottom,
  transcriptRowsForMode
} from '../components/appLayout.js'

describe('AppLayout transcript stickiness', () => {
  it('does not pin the startup intro-only transcript to the bottom', () => {
    expect(shouldStickTranscriptToBottom({ empty: true })).toBe(false)
  })

  it('keeps tail-follow enabled once real transcript content exists', () => {
    expect(shouldStickTranscriptToBottom({ empty: false })).toBe(true)
  })
})

describe('AppLayout inline transcript rendering', () => {
  const rows = Array.from({ length: 5 }, (_, index) => ({
    index,
    key: `row-${index}`,
    msg: { role: 'assistant' as const, text: `message ${index}` }
  }))

  const transcript: Pick<AppLayoutTranscriptProps, 'virtualHistory' | 'virtualRows'> = {
    virtualHistory: {
      bottomSpacer: 0,
      end: 4,
      measureRef: () => () => {},
      offsets: [],
      start: 2,
      topSpacer: 0
    },
    virtualRows: rows
  }

  it('keeps ScrollBox virtualization outside inline mode', () => {
    expect(shouldRenderTranscriptScrollBox(false)).toBe(true)
    expect(transcriptRowsForMode(transcript, false).map(row => row.key)).toEqual(['row-2', 'row-3'])
  })

  it('renders every transcript row directly in inline mode', () => {
    expect(shouldRenderTranscriptScrollBox(true)).toBe(false)
    expect(transcriptRowsForMode(transcript, true).map(row => row.key)).toEqual([
      'row-0',
      'row-1',
      'row-2',
      'row-3',
      'row-4'
    ])
  })
})
