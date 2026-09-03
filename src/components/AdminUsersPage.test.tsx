// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import AdminUsersPage from './AdminUsersPage'

const user = {
  id: 'user-1',
  email: 'user@real.test',
  display_name: '真实用户',
  role: 'user',
  status: 'active',
  created_at: '2026-09-02T00:00:00Z',
  entitlement_expires_at: null,
  entitlement_active: false,
}

describe('AdminUsersPage details', () => {
  beforeEach(() => {
    const values = new Map<string, string>()
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value) },
      removeItem: (key: string) => { values.delete(key) },
      clear: () => { values.clear() },
    })
    localStorage.setItem('access_token', 'test-access-token')
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('does not render fabricated zero statistics when detail loading fails', async () => {
    const onNotice = vi.fn()
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/admin/users?')) {
        return { ok: true, json: async () => ({ items: [user], total: 1 }) } as Response
      }
      if (url.endsWith('/admin/users/user-1')) {
        return { ok: false, status: 503 } as Response
      }
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<AdminUsersPage onNotice={onNotice} />)
    expect(await screen.findByText('user@real.test')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '详情' }))

    await waitFor(() => expect(onNotice).toHaveBeenCalledWith('用户详情加载失败，请检查后端服务'))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByText('模型请求')).toBeNull()
  })

  it('shows a retryable error instead of fabricated zero list statistics', async () => {
    const onNotice = vi.fn()
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 503 } as Response)))

    render(<AdminUsersPage onNotice={onNotice} />)

    expect(await screen.findByText('用户数据加载失败')).toBeTruthy()
    expect(screen.getByRole('button', { name: '重试' })).toBeTruthy()
    expect(screen.queryByText('暂无用户')).toBeNull()
    expect(screen.queryByText('用户总数')).toBeNull()
    expect(screen.queryByText(/共 0 位用户/)).toBeNull()
  })

  it('renders entitlement timestamps in Beijing time', async () => {
    const entitledUser = { ...user, entitlement_expires_at: '2026-09-02T16:00:00Z', entitlement_active: true }
    const detail = {
      user: entitledUser,
      entitlement: { id: 'entitlement-1', starts_at: '2026-09-01T00:00:00Z', expires_at: '2026-09-02T16:00:00Z', status: 'active', active: true },
      projects: 1,
      threads: 2,
      requests: 3,
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/admin/users?')) return { ok: true, json: async () => ({ items: [entitledUser], total: 1 }) } as Response
      if (url.endsWith('/admin/users/user-1')) return { ok: true, json: async () => detail } as Response
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<AdminUsersPage onNotice={vi.fn()} />)
    expect(await screen.findByText('2026-09-03')).toBeTruthy()
    expect(screen.getByText('授权到期（北京时间）')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '详情' }))
    expect(await screen.findByText('到期（北京时间）：2026-09-03 00:00:00')).toBeTruthy()
  })
})
