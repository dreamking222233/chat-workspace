// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./App', () => ({ ChatWorkspace: () => <div>聊天工作区</div> }))
vi.mock('./components/LandingPage', () => ({ default: () => <div>落地页</div> }))
vi.mock('./components/AdminModelChannelsPage', () => ({ default: () => <div>模型渠道页面</div> }))
vi.mock('./components/AdminUsersPage', () => ({ default: () => <div>用户管理页面</div> }))
vi.mock('./components/AdminUsagePage', () => ({ default: () => <div>使用记录页面</div> }))

import Root from './Root'

const admin = {
  id: 'admin-1',
  email: 'admin@real.test',
  display_name: '真实管理员',
  role: 'admin',
  status: 'active',
}

describe('Root routes and authentication forms', () => {
  beforeEach(() => {
    const values = new Map<string, string>()
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value) },
      removeItem: (key: string) => { values.delete(key) },
      clear: () => { values.clear() },
    })
    window.history.replaceState({}, '', '/')
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('renders login credentials with empty initial values', () => {
    window.history.replaceState({}, '', '/login')
    render(<Root />)

    expect((screen.getByLabelText('邮箱') as HTMLInputElement).value).toBe('')
    expect((screen.getByLabelText('密码') as HTMLInputElement).value).toBe('')
  })

  it('requires the registration display name and sends the entered value', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({
      ok: true,
      json: async () => ({ access_token: 'access', refresh_token: 'refresh', user: { ...admin, role: 'user' } }),
    } as Response))
    vi.stubGlobal('fetch', fetchMock)
    window.history.replaceState({}, '', '/register')
    render(<Root />)

    const name = screen.getByLabelText('显示名称') as HTMLInputElement
    expect(name.required).toBe(true)
    expect(name.value).toBe('')

    fireEvent.change(name, { target: { value: '真实用户名称' } })
    fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'user@real.test' } })
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
    fireEvent.submit(screen.getByRole('button', { name: '注册' }).closest('form')!)

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      email: 'user@real.test',
      password: 'password123',
      display_name: '真实用户名称',
    })
  })

  it('shows a Chinese notice when the register email is already used', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 409,
      json: async () => ({ detail: 'email already registered' }),
    } as Response))
    vi.stubGlobal('fetch', fetchMock)
    window.history.replaceState({}, '', '/register')
    render(<Root />)

    fireEvent.change(screen.getByLabelText('显示名称'), { target: { value: 'dream' } })
    fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'owner@example.net' } })
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'password123' } })
    fireEvent.submit(screen.getByRole('button', { name: '注册' }).closest('form')!)

    expect(await screen.findByText('该邮箱已被注册')).toBeTruthy()
    expect(screen.queryByText('email already registered')).toBeNull()
  })

  it('redirects an unknown admin path to a real admin page and hides settings', async () => {
    localStorage.setItem('auth_user', JSON.stringify(admin))
    window.history.replaceState({}, '', '/admin/not-a-page')
    render(<Root />)

    await waitFor(() => expect(window.location.pathname).toBe('/admin/model-channels'))
    expect(await screen.findByText('模型渠道页面')).toBeTruthy()
    expect(screen.queryByText('使用记录页面')).toBeNull()
    expect(screen.queryByRole('button', { name: '设置' })).toBeNull()
  })
})
