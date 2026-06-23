const PAGER_MAX_WIDTH = 120
const PAGER_MIN_WIDTH = 10

export const pagerContentWidth = (cols: number): number =>
  Math.min(PAGER_MAX_WIDTH, Math.max(PAGER_MIN_WIDTH, Math.max(1, cols) - 6))
