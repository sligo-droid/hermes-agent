import { stringWidth } from '@hermes/ink'
import { describe, expect, it } from 'vitest'

import { statusRuleLayout } from '../components/appChrome.js'

describe('statusRuleLayout', () => {
  it('truncates cwd from the start by display width', () => {
    const layout = statusRuleLayout(60, '/home/droid/hermes/some/deep/path')

    expect(layout.cwdLabel.startsWith('…')).toBe(true)
    expect(layout.leftWidth + stringWidth(layout.cwdLabel) + 3).toBeLessThanOrEqual(60)
  })

  it('hides cwd on very narrow terminals', () => {
    expect(statusRuleLayout(30, '/home/droid/hermes')).toEqual({ cwdLabel: '', leftWidth: 30 })
  })
})
