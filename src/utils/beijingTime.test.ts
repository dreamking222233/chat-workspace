import { describe, expect, it } from 'vitest'

import { beijingDateBoundary, formatBeijingDate, formatBeijingDateTime, formatBeijingTime, parseApiDateTime } from './beijingTime'

describe('Beijing time utilities', () => {
  it('renders UTC, offset-aware and legacy offset-free values in Asia/Shanghai', () => {
    expect(formatBeijingDateTime('2026-09-02T00:01:02Z')).toBe('2026-09-02 08:01:02')
    expect(formatBeijingDateTime('2026-09-02T08:01:02+08:00')).toBe('2026-09-02 08:01:02')
    expect(formatBeijingDateTime('2026-09-02T00:01:02')).toBe('2026-09-02 08:01:02')
    expect(formatBeijingDate('2026-09-02T16:01:02Z')).toBe('2026-09-03')
    expect(formatBeijingTime('2026-09-02T16:01:02Z')).toBe('00:01:02')
  })

  it('returns a stable fallback for invalid API timestamps', () => {
    expect(parseApiDateTime('')).toBeNull()
    expect(formatBeijingDateTime('not-a-time')).toBe('时间未知')
  })

  it('builds explicit +08 boundaries for a Beijing calendar day', () => {
    expect(beijingDateBoundary('2026-09-02', 'start')).toBe('2026-09-02T00:00:00.000+08:00')
    expect(beijingDateBoundary('2026-09-02', 'end')).toBe('2026-09-03T00:00:00.000+08:00')
    expect(beijingDateBoundary('2026-12-31', 'end')).toBe('2027-01-01T00:00:00.000+08:00')
    expect(beijingDateBoundary('2026-02-30', 'start')).toBeNull()
  })
})
