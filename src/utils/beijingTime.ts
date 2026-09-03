const BEIJING_TIME_ZONE = 'Asia/Shanghai'
const OFFSET_SUFFIX = /(?:z|[+-]\d{2}:?\d{2})$/i
const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/

const dateFormatter = new Intl.DateTimeFormat('zh-CN-u-ca-iso8601-nu-latn', {
  timeZone: BEIJING_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
})

const dateTimeFormatter = new Intl.DateTimeFormat('zh-CN-u-ca-iso8601-nu-latn', {
  timeZone: BEIJING_TIME_ZONE,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
})

const timeFormatter = new Intl.DateTimeFormat('zh-CN-u-ca-iso8601-nu-latn', {
  timeZone: BEIJING_TIME_ZONE,
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hourCycle: 'h23',
})

export function parseApiDateTime(value: string | Date): Date | null {
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value
  const trimmed = value.trim()
  if (!trimmed) return null
  // Legacy MySQL DATETIME responses have no offset. Stored values use UTC,
  // so add the missing marker instead of letting the browser assume local time.
  const normalized = OFFSET_SUFFIX.test(trimmed) ? trimmed : `${trimmed}Z`
  const parsed = new Date(normalized)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

export function formatBeijingDate(value: string | Date): string {
  const date = parseApiDateTime(value)
  return date ? joinParts(dateFormatter, date, ['year', 'month', 'day'], '-') : '时间未知'
}

export function formatBeijingDateTime(value: string | Date): string {
  const date = parseApiDateTime(value)
  if (!date) return '时间未知'
  const datePart = joinParts(dateTimeFormatter, date, ['year', 'month', 'day'], '-')
  const timePart = joinParts(dateTimeFormatter, date, ['hour', 'minute', 'second'], ':')
  return `${datePart} ${timePart}`
}

export function formatBeijingTime(value: string | Date): string {
  const date = parseApiDateTime(value)
  return date ? joinParts(timeFormatter, date, ['hour', 'minute', 'second'], ':') : '时间未知'
}

export function beijingDateBoundary(value: string, boundary: 'start' | 'end'): string | null {
  const match = DATE_ONLY.exec(value)
  if (!match) return null
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const candidate = new Date(Date.UTC(year, month - 1, day))
  if (candidate.getUTCFullYear() !== year || candidate.getUTCMonth() !== month - 1 || candidate.getUTCDate() !== day) return null
  if (boundary === 'start') return `${value}T00:00:00.000+08:00`
  // Use the next Beijing midnight as an exclusive upper bound. This remains
  // correct when the database moves from second to microsecond precision.
  const next = new Date(Date.UTC(year, month - 1, day + 1))
  const nextDate = [next.getUTCFullYear(), String(next.getUTCMonth() + 1).padStart(2, '0'), String(next.getUTCDate()).padStart(2, '0')].join('-')
  return `${nextDate}T00:00:00.000+08:00`
}

function joinParts(
  formatter: Intl.DateTimeFormat,
  value: Date,
  names: Intl.DateTimeFormatPartTypes[],
  separator: string,
): string {
  const parts = new Map(formatter.formatToParts(value).map((part) => [part.type, part.value]))
  return names.map((name) => parts.get(name) ?? '').join(separator)
}
