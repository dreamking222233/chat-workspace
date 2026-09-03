// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import AdminUsagePage from './AdminUsagePage'

describe('AdminUsagePage resource state', () => {
  beforeEach(() => {
    const values = new Map<string, string>([['access_token', 'test-access-token']])
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value) },
      removeItem: (key: string) => { values.delete(key) },
      clear: () => { values.clear() },
    })
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('shows a retryable error instead of fabricated zero usage statistics', async () => {
    const onNotice = vi.fn()
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 503 } as Response)))

    render(<AdminUsagePage onNotice={onNotice} />)

    expect(await screen.findByText('使用记录加载失败')).toBeTruthy()
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy()
    expect(screen.queryByText('还没有模型调用记录')).toBeNull()
    expect(screen.queryByText('本页请求次数')).toBeNull()
    expect(screen.queryByText('第 1 页 · 已到末页')).toBeNull()
  })

  it('renders usage in Beijing time and sends Beijing calendar-day filters', async () => {
    const row = {
      id: 'request-1', user_id: 'user-1', user_email: 'user@real.test', thread_id: 'thread-1',
      model: 'text-model', modality: 'text', status: 'completed', input_tokens: 10,
      output_tokens: 20, latency_ms: 30, created_at: '2026-09-02T00:01:02Z',
    }
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => ({ ok: true, json: async () => [row] } as Response))
    vi.stubGlobal('fetch', fetchMock)

    render(<AdminUsagePage onNotice={vi.fn()} />)
    expect(await screen.findByText('2026-09-02 08:01:02')).toBeTruthy()
    expect(screen.getByText('时间（北京时间）')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('开始日期（北京时间）'), { target: { value: '2026-09-02' } })
    fireEvent.change(screen.getByLabelText('结束日期（北京时间）'), { target: { value: '2026-09-03' } })
    fireEvent.click(screen.getByRole('button', { name: '应用日期筛选' }))

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    const url = new URL(String(fetchMock.mock.calls[1][0]))
    expect(url.searchParams.get('created_after')).toBe('2026-09-02T00:00:00.000+08:00')
    expect(url.searchParams.get('created_before')).toBe('2026-09-04T00:00:00.000+08:00')

    let csv = ''
    class CapturingBlob {
      constructor(parts: BlobPart[]) { csv = parts.map((part) => String(part)).join('') }
    }
    vi.stubGlobal('Blob', CapturingBlob)
    vi.stubGlobal('URL', { createObjectURL: () => 'blob:test', revokeObjectURL: vi.fn() })
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    fireEvent.click(screen.getByRole('button', { name: '导出 CSV' }))
    expect(csv).toContain('时间（北京时间）')
    expect(csv).toContain('2026-09-02 08:01:02')
  })
})
