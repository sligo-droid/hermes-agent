import { stringWidth, wrapAnsi } from '@hermes/ink'

const PAGER_MAX_WIDTH = 120
const PAGER_MIN_WIDTH = 10

export const pagerContentWidth = (cols: number): number =>
  Math.min(PAGER_MAX_WIDTH, Math.max(PAGER_MIN_WIDTH, Math.max(1, cols) - 6))

export const pagerVisibleRows = (rows: number): number => Math.max(3, Math.max(1, rows) - 8)

export const pagerVisualLines = (text: string, width: number): string[] => {
  const wrapWidth = Math.max(1, width)
  const out: string[] = []

  for (const line of text.split('\n')) {
    if (!line) {
      out.push('')

      continue
    }

    out.push(...wrapAnsi(line, wrapWidth, { hard: true, trim: false }).split('\n'))
  }

  return out
}

export const pagerLineFits = (line: string, width: number): boolean => stringWidth(line) <= Math.max(1, width)
