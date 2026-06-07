import { describe, expect, it } from 'vitest'

import { looksLikeSlashCommand, parseSlashCommand } from '../domain/slash.js'
import { expandPasteSnippets } from '../protocol/paste.js'

describe('paste expansion for slash commands', () => {
  it('expands a collapsed slash-command paste before command detection', () => {
    const label = '[[ /goal Goal Plan.. [80 lines] .. optional. ]]'
    const expanded = '/goal\n\nGoal Plan\n- keep the newlines\n- finish the work'

    const text = expandPasteSnippets(label, [{ label, text: expanded }])

    expect(text).toBe(expanded)
    expect(looksLikeSlashCommand(text)).toBe(true)
  })

  it('expands paste tokens inside slash-command arguments', () => {
    const label = '[[ Goal Plan.. [80 lines] .. optional. ]]'
    const expanded = 'Goal Plan\n- keep the newlines\n- finish the work'

    const text = expandPasteSnippets(`/goal ${label}`, [{ label, text: expanded }])
    const parsed = parseSlashCommand(text)

    expect(parsed.name).toBe('goal')
    expect(parsed.arg).toBe(expanded)
  })
})

describe('parseSlashCommand', () => {
  it('preserves multiline command arguments', () => {
    const parsed = parseSlashCommand('/goal\n\nGoal Plan\n- keep bullets')

    expect(parsed.name).toBe('goal')
    expect(parsed.arg).toBe('Goal Plan\n- keep bullets')
  })

  it('keeps ordinary one-line args unchanged', () => {
    const parsed = parseSlashCommand('/model x-model --global')

    expect(parsed.name).toBe('model')
    expect(parsed.arg).toBe('x-model --global')
  })
})
