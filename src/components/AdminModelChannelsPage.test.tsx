// @vitest-environment jsdom

import { StrictMode } from 'react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import AdminModelChannelsPage from './AdminModelChannelsPage'

const channel = {
  id: 'channel-1',
  name: 'OpenAI-compatible',
  provider: 'openai-compatible',
  protocol: 'openai',
  channel_type: 'official',
  base_url: 'https://provider.example/v1',
  api_key_masked: 'TOKE••••••••OKEN',
  modality: 'both',
  enabled: true,
  priority: 10,
  models: ['text-model', 'image-model'],
  capabilities: { 'text-model': ['text'], 'image-model': ['image'] },
  models_synced_at: null,
  last_sync_error: null,
  created_at: '2026-09-02T00:00:00Z',
  updated_at: '2026-09-02T00:00:00Z',
}

function response(body: unknown, ok = true): Response {
  return { ok, json: async () => body } as Response
}

function requestMethod(input: RequestInfo | URL, init?: RequestInit): string {
  return init?.method ?? (typeof Request !== 'undefined' && input instanceof Request ? input.method : 'GET')
}

function requestUrl(input: RequestInfo | URL): string {
  return typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
}

async function flushCurrentTimers(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

describe('AdminModelChannelsPage model polling', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    const values = new Map<string, string>()
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value) },
      removeItem: (key: string) => { values.delete(key) },
      clear: () => { values.clear() },
    })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('runs once in StrictMode, repeats at 60 seconds, and stops after unmount', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (requestMethod(input, init) === 'POST') {
        return response({ ok: true, message: '已同步 2 个模型', models: channel.models, capabilities: channel.capabilities })
      }
      return response([channel])
    })
    vi.stubGlobal('fetch', fetchMock)

    const rendered = render(
      <StrictMode>
        <AdminModelChannelsPage onBack={vi.fn()} onNotice={vi.fn()} />
      </StrictMode>,
    )
    await flushCurrentTimers()

    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(fetchMock.mock.calls.filter(([input, init]) => requestMethod(input, init) === 'POST')).toHaveLength(1)
    expect(screen.getByText(/每 60 秒自动同步/)).toBeTruthy()

    await act(async () => { await vi.advanceTimersByTimeAsync(59_999) })
    expect(fetchMock).toHaveBeenCalledTimes(3)
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(fetchMock).toHaveBeenCalledTimes(6)
    expect(fetchMock.mock.calls.filter(([input, init]) => requestMethod(input, init) === 'POST')).toHaveLength(2)

    rendered.unmount()
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(fetchMock).toHaveBeenCalledTimes(6)
  })

  it('shares the per-channel lock with the manual sync button', async () => {
    let finishSync: ((value: Response) => void) | undefined
    const pendingSync = new Promise<Response>((resolve) => { finishSync = resolve })
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (requestMethod(input, init) === 'POST') return pendingSync
      return Promise.resolve(response([channel]))
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<AdminModelChannelsPage onBack={vi.fn()} onNotice={vi.fn()} />)
    await flushCurrentTimers()

    const syncButton = screen.getByRole('button', { name: '同步模型' }) as HTMLButtonElement
    expect(syncButton.disabled).toBe(true)
    fireEvent.click(syncButton)
    expect(fetchMock.mock.calls.filter(([input, init]) => requestMethod(input, init) === 'POST')).toHaveLength(1)

    await act(async () => {
      finishSync?.(response({ ok: true, message: '已同步 2 个模型', models: channel.models, capabilities: channel.capabilities }))
      await pendingSync
    })
    expect((screen.getByRole('button', { name: '同步模型' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('uses only the configured backend API routes', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      return requestMethod(input, init) === 'POST'
        ? response({ ok: true, models: channel.models })
        : response([channel])
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<AdminModelChannelsPage onBack={vi.fn()} onNotice={vi.fn()} />)
    await flushCurrentTimers()

    expect(fetchMock.mock.calls.every(([input]) => requestUrl(input).startsWith('http://localhost:8000/api/v1/admin/model-channels'))).toBe(true)
  })

  it('reloads the persisted channel after manual sync instead of inventing a sync timestamp', async () => {
    const idleChannel = { ...channel, enabled: false, models_synced_at: null }
    const persistedChannel = { ...idleChannel, models: [...idleChannel.models, 'new-model'], models_synced_at: '2026-09-02T01:02:03Z' }
    let listReads = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (requestMethod(input, init) === 'POST') {
        return response({ ok: true, message: '已同步 1 个模型', models: persistedChannel.models, capabilities: persistedChannel.capabilities })
      }
      listReads += 1
      return response(listReads >= 3 ? [persistedChannel] : [idleChannel])
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<AdminModelChannelsPage onBack={vi.fn()} onNotice={vi.fn()} />)
    await flushCurrentTimers()
    expect(listReads).toBe(2)

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '同步模型' }))
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(listReads).toBe(3)
    expect(screen.getByText(/new-model/)).toBeTruthy()
    expect(screen.getByText('已同步 09:02:03')).toBeTruthy()
    expect(screen.getByText(/时间均为北京时间/)).toBeTruthy()
  })

  it('opens the create form without a simulated Base URL value', async () => {
    const fetchMock = vi.fn(async () => response([]))
    vi.stubGlobal('fetch', fetchMock)

    render(<AdminModelChannelsPage onBack={vi.fn()} onNotice={vi.fn()} />)
    await flushCurrentTimers()
    fireEvent.click(screen.getByRole('button', { name: '新增渠道' }))

    const baseUrl = screen.getByLabelText('Base URL') as HTMLInputElement
    expect(baseUrl.value).toBe('')
    expect(baseUrl.placeholder).toBe('https://api.example.com/v1')
    expect((screen.getByLabelText('渠道类型') as HTMLSelectElement).value).toBe('official')
    expect(screen.queryByLabelText(/模型列表/)).toBeNull()
    expect(screen.getByText(/自动请求/)).toBeTruthy()
  })

  it('creates a channel without a manual model list and syncs remote models', async () => {
    const saved = { ...channel, id: 'channel-created', name: 'Remote models', models: [] }
    const synced = { ...saved, models: ['gpt-text', 'gpt-image-2'], capabilities: { 'gpt-text': ['text'], 'gpt-image-2': ['image'] } }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const method = requestMethod(input, init)
      const url = requestUrl(input)
      if (method === 'POST' && url.endsWith('/model-channels')) return response(saved)
      if (method === 'POST' && url.endsWith('/sync-models')) return response({ ok: true, message: '已同步 2 个模型', models: synced.models, capabilities: synced.capabilities })
      return response([synced])
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<AdminModelChannelsPage onBack={vi.fn()} onNotice={vi.fn()} />)
    await flushCurrentTimers()
    fireEvent.click(screen.getByRole('button', { name: '新增渠道' }))
    fireEvent.change(screen.getByLabelText('渠道名称'), { target: { value: 'Remote models' } })
    fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: 'https://provider.example/v1' } })
    fireEvent.change(screen.getByLabelText(/API Key/), { target: { value: 'TOKEN' } })
    fireEvent.click(screen.getByRole('button', { name: '保存并同步模型' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })

    const createCall = fetchMock.mock.calls.find(([input, init]) => requestMethod(input, init) === 'POST' && requestUrl(input).endsWith('/model-channels'))
    expect(createCall).toBeTruthy()
    expect(JSON.parse(String(createCall?.[1]?.body)).models).toEqual([])
    expect(fetchMock.mock.calls.some(([input, init]) => requestMethod(input, init) === 'POST' && requestUrl(input).endsWith('/sync-models'))).toBe(true)
    expect(screen.getByText(/gpt-image-2/)).toBeTruthy()
  })

  it('manages enabled models from the remote model list', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const method = requestMethod(input, init)
      const url = requestUrl(input)
      if (method === 'GET' && url.endsWith('/remote-models')) return response({ ok: true, message: '已获取 2 个远端模型', models: channel.models, capabilities: channel.capabilities })
      if (method === 'PATCH') return response({ ...channel, models: ['image-model'] })
      return response([channel])
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<AdminModelChannelsPage onBack={vi.fn()} onNotice={vi.fn()} />)
    await flushCurrentTimers()
    fireEvent.click(screen.getByRole('button', { name: '管理模型' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.getAllByText('文本模型').length).toBeGreaterThanOrEqual(2)
    const boxes = screen.getAllByRole('checkbox') as HTMLInputElement[]
    expect(boxes).toHaveLength(2)
    fireEvent.click(boxes[0])
    fireEvent.click(screen.getByRole('button', { name: '保存模型配置' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    const patchCall = fetchMock.mock.calls.find(([input, init]) => requestMethod(input, init) === 'PATCH')
    expect(JSON.parse(String(patchCall?.[1]?.body)).models).toEqual(['image-model'])
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
