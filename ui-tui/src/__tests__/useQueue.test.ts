import { describe, expect, it } from 'vitest'

import { queueSummaryText, shouldRenderQueueDetails } from '../components/queuedMessages.js'
import { removeAtInPlace } from '../hooks/useQueue.js'

describe('removeAtInPlace', () => {
  it('removes the item at the given index in place', () => {
    const arr = ['a', 'b', 'c']

    removeAtInPlace(arr, 1)
    expect(arr).toEqual(['a', 'c'])
  })

  it('is a no-op when the index is out of bounds', () => {
    const arr = ['a', 'b']

    removeAtInPlace(arr, -1)
    removeAtInPlace(arr, 5)
    expect(arr).toEqual(['a', 'b'])
  })

  it('returns the same reference (mutates in place)', () => {
    const arr = ['x']
    const same = removeAtInPlace(arr, 0)

    expect(same).toBe(arr)
    expect(arr).toEqual([])
  })
})

describe('queue summary display', () => {
  it('uses one compact line for queued items', () => {
    expect(queueSummaryText(['also, what to do about emails?'], 80)).toBe(
      'queued (1) · next: also, what to do about emails?'
    )
  })

  it('truncates the preview to fit narrow composers', () => {
    expect(queueSummaryText(['abcdefghijklmnopqrstuvwxyz'], 32)).toBe('queued (1) · next: abcdefghijkl…')
  })

  it('only renders queue details while editing', () => {
    expect(shouldRenderQueueDetails(['a'], null)).toBe(false)
    expect(shouldRenderQueueDetails(['a'], 0)).toBe(true)
    expect(shouldRenderQueueDetails([], 0)).toBe(false)
  })
})
