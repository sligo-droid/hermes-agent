import { describe, expect, it } from 'vitest'

import { pagerContentWidth, pagerLineFits, pagerVisibleRows, pagerVisualLines } from '../lib/pager.js'

describe('pager helpers', () => {
  it('wraps long lines into bounded visual rows', () => {
    const width = 12
    const lines = pagerVisualLines('alpha beta gamma delta', width)

    expect(lines.length).toBeGreaterThan(1)
    expect(lines.every(line => pagerLineFits(line, width))).toBe(true)
  })

  it('preserves blank rows between paragraphs', () => {
    expect(pagerVisualLines('one\n\nthree', 20)).toEqual(['one', '', 'three'])
  })

  it('keeps pager dimensions inside terminal constraints', () => {
    expect(pagerContentWidth(80)).toBe(74)
    expect(pagerContentWidth(200)).toBe(120)
    expect(pagerVisibleRows(24)).toBe(16)
    expect(pagerVisibleRows(8)).toBe(3)
  })
})
