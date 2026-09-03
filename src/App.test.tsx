// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ChatWorkspace } from './App'

function jsonResponse(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as Response
}

function errorResponse(status = 503): Response {
  return { ok: false, status, json: async () => ({ detail: 'unavailable' }), text: async () => 'unavailable' } as Response
}

function delayedSseResponse(content: string): { response: Response; release: () => void } {
  const encoded = new TextEncoder().encode(content)
  let delivered = false
  let release: () => void = () => {}
  const pending = new Promise<void>((resolve) => { release = resolve })
  return {
    release,
    response: {
      ok: true,
      status: 200,
      body: {
        getReader: () => ({
          read: async () => {
            if (delivered) return { done: true, value: undefined }
            delivered = true
            await pending
            return { done: false, value: encoded }
          },
        }),
      },
      text: async () => content,
    } as unknown as Response,
  }
}

function stagedSseResponse(first: string, rest: string): { response: Response; release: () => void } {
  const parts = [new TextEncoder().encode(first), new TextEncoder().encode(rest)]
  let index = 0
  let release: () => void = () => {}
  const pending = new Promise<void>((resolve) => { release = resolve })
  return {
    release,
    response: {
      ok: true,
      status: 200,
      body: {
        getReader: () => ({
          read: async () => {
            if (index >= parts.length) return { done: true, value: undefined }
            if (index === 1) await pending
            return { done: false, value: parts[index++] }
          },
        }),
      },
      text: async () => first + rest,
    } as unknown as Response,
  }
}

async function chooseModel(optionName: string | RegExp) {
  fireEvent.click(await screen.findByLabelText('选择模型'))
  fireEvent.click(await screen.findByRole('option', { name: optionName }))
}

function immediateSseResponse(content: string): Response {
  const encoded = new TextEncoder().encode(content)
  let delivered = false
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () => {
          if (delivered) return { done: true, value: undefined }
          delivered = true
          return { done: false, value: encoded }
        },
      }),
    },
    text: async () => content,
  } as unknown as Response
}

describe('ChatWorkspace remote stream', () => {
  beforeEach(() => {
    const values = new Map<string, string>([
      ['access_token', 'test-access-token'],
      ['auth_user', JSON.stringify({ display_name: 'Admin', role: 'admin' })],
    ])
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value) },
      removeItem: (key: string) => { values.delete(key) },
      clear: () => { values.clear() },
    })
    vi.stubGlobal('matchMedia', vi.fn(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })))
    Element.prototype.scrollIntoView = vi.fn()
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('replaces the optimistic assistant ID before applying SSE deltas', async () => {
    const thread = { id: 'thread-1', title: '新聊天', model: 'text-model', messages: [] }
    const frames = [
      'id: 1\nevent: message.created\ndata: {"message_id":"assistant-server","user_message_id":"user-server","request_id":"request-1","model":"text-model"}\n\n',
      'id: 2\nevent: message.delta\ndata: {"message_id":"assistant-server","delta":"模型"}\n\n',
      'id: 3\nevent: message.delta\ndata: {"message_id":"assistant-server","delta":"回复"}\n\n',
      'id: 4\nevent: message.completed\ndata: {"message_id":"assistant-server","content":"模型回复"}\n\n',
    ].join('')
    const delayedStream = delayedSseResponse(frames)

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST' && url.includes('/messages/stream')) return delayedStream.response
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return jsonResponse([thread])
      if (url.endsWith('/projects')) return jsonResponse([])
      if (url.endsWith('/models')) return jsonResponse([{ model: 'text-model', modality: 'text', channel_name: '渠道', channel_id: 'channel-1', capabilities: ['text'] }])
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)
    await waitFor(() => expect(screen.getByLabelText('选择模型')).toBeTruthy())
    await chooseModel('网页版·text-model')
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: '你好' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送' })) })

    expect(await screen.findByRole('status', { name: '正在等待模型回复' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '停止生成' })).toBeTruthy()
    await act(async () => { delayedStream.release() })
    expect(await screen.findByText('模型回复')).toBeTruthy()
    expect(screen.queryByRole('status', { name: '正在等待模型回复' })).toBeNull()
    expect(screen.getByText('你好', { selector: '.user-bubble' })).toBeTruthy()
    expect(fetchMock.mock.calls.filter(([input, init]) => init?.method === 'POST' && String(input).includes('/messages/stream'))).toHaveLength(1)
  })

  it('shows a search activity and renders the final answer as Markdown', async () => {
    const thread = { id: 'thread-search', title: '新聊天', model: 'text-model', messages: [] }
    const started = [
      'id: 1\nevent: message.created\ndata: {"message_id":"assistant-search","user_message_id":"user-search","request_id":"request-search"}\n\n',
      'id: 2\nevent: search.started\ndata: {"message_id":"assistant-search","query":"2026年9月3日 今日新闻"}\n\n',
    ].join('')
    const answer = '### 今日要闻\n\n- **市场**：保持关注。 [来源](https://example.com/news)'
    const completed = [
      `id: 3\nevent: message.delta\ndata: ${JSON.stringify({ message_id: 'assistant-search', delta: answer })}\n\n`,
      `id: 4\nevent: message.completed\ndata: ${JSON.stringify({ message_id: 'assistant-search', content: answer })}\n\n`,
    ].join('')
    const stagedStream = stagedSseResponse(started, completed)
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST' && url.includes('/messages/stream')) return stagedStream.response
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return jsonResponse([thread])
      if (url.endsWith('/projects')) return jsonResponse([])
      if (url.endsWith('/models')) return jsonResponse([{ model: 'text-model', modality: 'text', channel_name: '官网版渠道', channel_id: 'channel-1', channel_type: 'official', capabilities: ['text'] }])
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)
    await waitFor(() => expect(screen.getByLabelText('选择模型')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: '今天有什么新闻' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送' })) })

    expect(await screen.findByRole('status', { name: '正在搜索 2026年9月3日 今日新闻' })).toBeTruthy()
    expect(screen.queryByText(/search\(/)).toBeNull()

    await act(async () => { stagedStream.release() })
    expect(await screen.findByRole('heading', { name: '今日要闻', level: 3 })).toBeTruthy()
    expect(screen.getByRole('listitem').textContent).toBe('市场：保持关注。 来源')
    expect(screen.getByText('市场', { selector: 'strong' })).toBeTruthy()
    expect(screen.getByRole('link', { name: '来源' }).getAttribute('href')).toBe('https://example.com/news')
    expect(screen.queryByRole('status', { name: /正在搜索/ })).toBeNull()
  })

  it('replays a truncated SSE frame without acknowledging its event ID', async () => {
    const thread = { id: 'thread-reconnect', title: '新聊天', model: 'text-model', messages: [] }
    const firstAttempt = [
      'id: 1\nevent: message.created\ndata: {"message_id":"assistant-reconnect","user_message_id":"user-reconnect","request_id":"request-reconnect-1234567890"}\n\n',
      'id: 2\nevent: search.started\ndata: {"message_id":"assistant-reconnect","query":"重连测试"}\n\n',
      'id: 3\nevent: message.completed\ndata: {"message_id":"assistant-reconnect","content":"## 未完成',
    ].join('')
    const finalContent = '## 重连成功\n\n- **结果**：完整事件已恢复。'
    const secondAttempt = [
      `id: 3\nevent: message.completed\ndata: ${JSON.stringify({ message_id: 'assistant-reconnect', content: finalContent })}\n\n`,
      'id: 4\nevent: request.usage\ndata: {"request_id":"request-reconnect-1234567890"}\n\n',
    ].join('')
    let streamAttempt = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST' && url.includes('/messages/stream')) {
        streamAttempt += 1
        return immediateSseResponse(streamAttempt === 1 ? firstAttempt : secondAttempt)
      }
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return jsonResponse([thread])
      if (url.endsWith('/projects')) return jsonResponse([])
      if (url.endsWith('/models')) return jsonResponse([{ model: 'text-model', modality: 'text', channel_name: '官网版渠道', channel_id: 'channel-1', channel_type: 'official', capabilities: ['text'] }])
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)
    await waitFor(() => expect(screen.getByLabelText('选择模型')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: '测试重连' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送' })) })

    expect(await screen.findByRole('heading', { name: '重连成功', level: 2 }, { timeout: 3000 })).toBeTruthy()
    const requests = fetchMock.mock.calls.filter(([input, init]) => init?.method === 'POST' && String(input).includes('/messages/stream'))
    expect(requests).toHaveLength(2)
    expect(new URL(String(requests[1][0])).searchParams.get('request_id')).toBe('request-reconnect-1234567890')
    expect((requests[1][1]?.headers as Record<string, string>)['Last-Event-ID']).toBe('2')
  })

  it('blocks remote Markdown images and unsafe markup', async () => {
    const unsafe = '安全文字 ![tracking](https://tracker.example/pixel.png) [危险](javascript:alert(1)) <img src="https://tracker.example/html.png">'
    const thread = {
      id: 'thread-markdown-safety',
      title: '安全渲染',
      model: 'text-model',
      messages: [
        { id: 'user-safe', role: 'user', content: '展示内容', content_type: 'text' },
        { id: 'assistant-safe', role: 'assistant', content: unsafe, content_type: 'text' },
      ],
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return jsonResponse([thread])
      if (url.endsWith('/projects')) return jsonResponse([])
      if (url.endsWith('/models')) return jsonResponse([{ model: 'text-model', modality: 'text', channel_name: '渠道', channel_id: 'channel-1', capabilities: ['text'] }])
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)
    expect(await screen.findByText('外部图片：tracking')).toBeTruthy()
    expect(document.querySelector('.assistant-markdown img')).toBeNull()
    const unsafeLink = screen.getByText('危险').closest('a')
    expect(unsafeLink?.getAttribute('href')).not.toContain('javascript:')
    expect(screen.queryByText(/tracker\.example\/html/)).toBeNull()
  })

  it('restores the confirmed response after a failed cross-channel regeneration', async () => {
    const oldReply = '旧官网回复'
    const partialReply = 'search("term")slow|this is documentation|1\nCodex 部分正文'
    const thread = {
      id: 'thread-failed-regeneration',
      title: '失败重生成',
      model: 'official-model',
      messages: [
        { id: 'user-failed-regeneration', role: 'user', content: '问题', content_type: 'text' },
        { id: 'assistant-failed-regeneration', role: 'assistant', content: oldReply, content_type: 'text' },
      ],
    }
    const frames = [
      'id: 1\nevent: message.created\ndata: {"message_id":"assistant-failed-regeneration","request_id":"request-failed-regeneration"}\n\n',
      `id: 2\nevent: message.delta\ndata: ${JSON.stringify({ message_id: 'assistant-failed-regeneration', delta: partialReply })}\n\n`,
      'id: 3\nevent: error\ndata: {"request_id":"request-failed-regeneration","message":"模型请求失败"}\n\n',
    ].join('')
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST' && url.includes('/regenerate')) return immediateSseResponse(frames)
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return jsonResponse([thread])
      if (url.endsWith('/projects')) return jsonResponse([])
      if (url.endsWith('/models')) return jsonResponse([
        { model: 'official-model', modality: 'text', channel_name: '官网', channel_id: 'official-channel', channel_type: 'official', capabilities: ['text'] },
        { model: 'codex-model', modality: 'text', channel_name: 'Codex', channel_id: 'codex-channel', channel_type: 'codex', capabilities: ['text'] },
      ])
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)
    expect(await screen.findByText(oldReply)).toBeTruthy()
    await chooseModel('Codex·codex-model')
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '重新生成' })) })

    expect(await screen.findByText('模型请求失败，请检查渠道配置后重试')).toBeTruthy()
    expect(screen.getByText(oldReply)).toBeTruthy()
    expect(screen.queryByText(/Codex 部分正文/)).toBeNull()
  })

  it.each([
    { label: '低', effort: 'low', channelId: 'channel-1', model: 'text-model' },
    { label: '中', effort: 'medium', channelId: 'channel-2', model: 'codex-model' },
    { label: '高', effort: 'high', channelId: 'channel-1', model: 'text-model' },
    { label: '超高', effort: 'xhigh', channelId: 'channel-2', model: 'codex-model' },
  ])('selects $label reasoning and sends $effort to either channel type', async ({ label, effort, channelId, model }) => {
    const thread = { id: 'thread-reasoning', title: '新聊天', model: 'text-model', messages: [] }
    const frames = 'id: 1\nevent: message.created\ndata: {"message_id":"assistant-reasoning","user_message_id":"user-reasoning","request_id":"request-reasoning"}\n\nid: 2\nevent: message.completed\ndata: {"message_id":"assistant-reasoning","content":"完成"}\n\n'
    const delayedStream = delayedSseResponse(frames)
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST' && url.includes('/messages/stream')) return delayedStream.response
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return jsonResponse([thread])
      if (url.endsWith('/projects')) return jsonResponse([])
      if (url.endsWith('/models')) return jsonResponse([
        { model: 'text-model', modality: 'text', channel_name: '官网版渠道', channel_id: 'channel-1', channel_type: 'official', capabilities: ['text'] },
        { model: 'codex-model', modality: 'text', channel_name: 'Codex渠道', channel_id: 'channel-2', channel_type: 'codex', capabilities: ['text'] },
      ])
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)
    await waitFor(() => expect(screen.getByLabelText('选择模型')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: '思考等级：关闭' }))
    expect(screen.getByRole('menuitemradio', { name: /^低/ })).toBeTruthy()
    expect(screen.getByRole('menuitemradio', { name: /^中/ })).toBeTruthy()
    expect(screen.getByRole('menuitemradio', { name: /^高/ })).toBeTruthy()
    fireEvent.click(screen.getByRole('menuitemradio', { name: new RegExp(`^${label}`) }))
    expect(screen.getByRole('button', { name: `思考等级：${label}` })).toBeTruthy()
    await chooseModel(new RegExp(`·${model}$`))
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: '复杂问题' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送' })) })

    const request = fetchMock.mock.calls.find(([input, init]) => init?.method === 'POST' && String(input).includes('/messages/stream'))
    expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({ channel_id: channelId, model, reasoning_effort: effort })
    await act(async () => { delayedStream.release() })
    expect(await screen.findByText('完成')).toBeTruthy()
  })

  it('omits reasoning_effort after reasoning is turned off', async () => {
    const thread = { id: 'thread-reasoning-off', title: '新聊天', model: 'text-model', messages: [] }
    const frames = 'id: 1\nevent: message.created\ndata: {"message_id":"assistant-off","user_message_id":"user-off","request_id":"request-off"}\n\nid: 2\nevent: message.completed\ndata: {"message_id":"assistant-off","content":"完成"}\n\n'
    const delayedStream = delayedSseResponse(frames)
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST' && url.includes('/messages/stream')) return delayedStream.response
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return jsonResponse([thread])
      if (url.endsWith('/projects')) return jsonResponse([])
      if (url.endsWith('/models')) return jsonResponse([{ model: 'text-model', modality: 'text', channel_name: '官网版渠道', channel_id: 'channel-1', channel_type: 'official', capabilities: ['text'] }])
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)
    await waitFor(() => expect(screen.getByLabelText('选择模型')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: '思考等级：关闭' }))
    fireEvent.click(screen.getByRole('menuitemradio', { name: /^高/ }))
    fireEvent.click(screen.getByRole('button', { name: '思考等级：高' }))
    fireEvent.click(screen.getByRole('menuitemradio', { name: /^关闭思考/ }))
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: '普通问题' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送' })) })

    const request = fetchMock.mock.calls.find(([input, init]) => init?.method === 'POST' && String(input).includes('/messages/stream'))
    expect(JSON.parse(String(request?.[1]?.body))).not.toHaveProperty('reasoning_effort')
    await act(async () => { delayedStream.release() })
    expect(await screen.findByText('完成')).toBeTruthy()
  })

  it('keeps the stream active when the real stop API fails', async () => {
    const thread = { id: 'thread-stop-failure', title: '新聊天', model: 'text-model', messages: [] }
    const frames = 'id: 1\nevent: message.created\ndata: {"message_id":"assistant-server","user_message_id":"user-server","request_id":"request-stop"}\n\nid: 2\nevent: message.completed\ndata: {"message_id":"assistant-server","content":"最终回复"}\n\n'
    const delayedStream = delayedSseResponse(frames)
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST' && url.includes('/messages/stream')) return delayedStream.response
      if (init?.method === 'POST' && url.includes('/messages/stop')) return errorResponse(503)
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return jsonResponse([thread])
      if (url.endsWith('/projects')) return jsonResponse([])
      if (url.endsWith('/models')) return jsonResponse([{ model: 'text-model', modality: 'text', channel_name: '渠道', channel_id: 'channel-1', capabilities: ['text'] }])
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)
    await waitFor(() => expect(screen.getByLabelText('选择模型')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: '继续生成' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送' })) })
    expect(await screen.findByRole('status', { name: '正在等待模型回复' })).toBeTruthy()

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '停止生成' })) })
    expect(await screen.findByText('停止生成失败，请重试')).toBeTruthy()
    expect(screen.getByRole('button', { name: '停止生成' })).toBeTruthy()
    expect(screen.getByRole('status', { name: '正在等待模型回复' })).toBeTruthy()

    await act(async () => { delayedStream.release() })
    expect(await screen.findByText('最终回复')).toBeTruthy()
  })

  it('shows the image creation card while a direct image request is pending', async () => {
    const thread = { id: 'thread-image', title: '新聊天', model: '', messages: [] }
    let finishImageRequest: (response: Response) => void = () => {}
    const pendingImageRequest = new Promise<Response>((resolve) => { finishImageRequest = resolve })
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST' && url.includes('/image-generations')) return pendingImageRequest
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return jsonResponse([thread])
      if (url.endsWith('/projects')) return jsonResponse([])
      if (url.endsWith('/models')) return jsonResponse([{ model: 'gpt-image-2', modality: 'image', channel_name: '图片渠道', channel_id: 'channel-image', capabilities: ['image'] }])
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)
    fireEvent.click(await screen.findByRole('button', { name: '图片', pressed: false }))
    await waitFor(() => expect(screen.getByLabelText('选择模型')).toBeTruthy())
    await chooseModel('网页版·gpt-image-2')
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: '生成一张直播间截图 16:9 2K 高清' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送' })) })

    expect(await screen.findByRole('status', { name: '正在创建图片' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '停止生成' })).toBeTruthy()
    const request = fetchMock.mock.calls.find(([input, init]) => init?.method === 'POST' && String(input).includes('/image-generations'))
    expect(JSON.parse(String(request?.[1]?.body))).toMatchObject({ prompt: '生成一张直播间截图 16:9 2K 高清', model: 'gpt-image-2', channel_id: 'channel-image' })

    await act(async () => { finishImageRequest({ ok: false, status: 502, text: async () => 'test complete' } as Response) })
    await waitFor(() => expect(screen.queryByRole('status', { name: '正在创建图片' })).toBeNull())
  })

  it('keeps the user prompt and clears the image placeholder when stopped', async () => {
    const thread = { id: 'thread-stop-image', title: '新聊天', model: '', messages: [] }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST' && url.includes('/image-generations')) {
        return await new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
        })
      }
      if (init?.method === 'POST' && url.includes('/messages/stop')) return jsonResponse({ stopped: true })
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return jsonResponse([thread])
      if (url.endsWith('/projects')) return jsonResponse([])
      if (url.endsWith('/models')) return jsonResponse([{ model: 'gpt-image-2', modality: 'image', channel_name: '图片渠道', channel_id: 'channel-image', capabilities: ['image'] }])
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)
    fireEvent.click(await screen.findByRole('button', { name: '图片', pressed: false }))
    await waitFor(() => expect(screen.getByLabelText('选择模型')).toBeTruthy())
    await chooseModel('网页版·gpt-image-2')
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: '生成一张直播间截图' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送' })) })
    expect(await screen.findByRole('status', { name: '正在创建图片' })).toBeTruthy()

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '停止生成' })) })

    await waitFor(() => expect(screen.queryByRole('status', { name: '正在创建图片' })).toBeNull())
    expect(screen.getByText('生成一张直播间截图', { selector: '.user-bubble' })).toBeTruthy()
    expect(fetchMock.mock.calls.some(([input, init]) => init?.method === 'POST' && String(input).includes('/messages/stop'))).toBe(true)
  })

  it('does not reuse an image-only thread model after returning to automatic text mode', async () => {
    const thread = { id: 'thread-image-to-text', title: '新聊天', model: '', messages: [] }
    const textFrames = 'id: 1\nevent: message.created\ndata: {"message_id":"assistant-text","user_message_id":"user-text","request_id":"request-text"}\n\nid: 2\nevent: message.completed\ndata: {"message_id":"assistant-text","content":"文本回复"}\n\n'
    const delayedTextStream = delayedSseResponse(textFrames)
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST' && url.includes('/image-generations')) return jsonResponse({ message_id: 'image-message', user_message_id: 'image-user', url: 'data:image/png;base64,aW1hZ2U=', model: 'gpt-image-2' })
      if (init?.method === 'POST' && url.includes('/messages/stream')) return delayedTextStream.response
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return jsonResponse([thread])
      if (url.endsWith('/projects')) return jsonResponse([])
      if (url.endsWith('/models')) return jsonResponse([
        { model: 'text-model', modality: 'text', channel_name: '文本渠道', channel_id: 'channel-text', capabilities: ['text'] },
        { model: 'gpt-image-2', modality: 'image', channel_name: '图片渠道', channel_id: 'channel-image', capabilities: ['image'] },
      ])
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)
    fireEvent.click(await screen.findByRole('button', { name: '图片', pressed: false }))
    await chooseModel('网页版·gpt-image-2')
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: '先生成图片' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送' })) })
    expect(await screen.findByAltText('生成的图片')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: '图片', pressed: true }))
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: '继续文本对话' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送' })) })

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => init?.method === 'POST' && String(input).includes('/messages/stream'))).toBe(true))
    const request = fetchMock.mock.calls.find(([input, init]) => init?.method === 'POST' && String(input).includes('/messages/stream'))
    const body = JSON.parse(String(request?.[1]?.body)) as Record<string, unknown>
    expect(body).not.toHaveProperty('model')
    expect(body).not.toHaveProperty('channel_id')

    await act(async () => { delayedTextStream.release() })
    expect(await screen.findByText('文本回复')).toBeTruthy()
  })

  it('always resolves a text model when regenerating while image mode is open', async () => {
    const thread = {
      id: 'thread-regenerate-text',
      title: '已有对话',
      model: 'gpt-image-2',
      messages: [
        { id: 'user-old', role: 'user', content: '原问题', content_type: 'text' },
        { id: 'assistant-old', role: 'assistant', content: '原回复', content_type: 'text' },
      ],
    }
    const frames = 'id: 1\nevent: message.created\ndata: {"message_id":"assistant-old","request_id":"request-regenerate"}\n\nid: 2\nevent: message.completed\ndata: {"message_id":"assistant-old","content":"新回复"}\n\n'
    const delayedStream = delayedSseResponse(frames)
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST' && url.includes('/regenerate')) return delayedStream.response
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return jsonResponse([thread])
      if (url.endsWith('/projects')) return jsonResponse([])
      if (url.endsWith('/models')) return jsonResponse([
        { model: 'text-model', modality: 'text', channel_name: '文本渠道', channel_id: 'channel-text', capabilities: ['text'] },
        { model: 'gpt-image-2', modality: 'image', channel_name: '图片渠道', channel_id: 'channel-image', capabilities: ['image'] },
      ])
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)
    expect(await screen.findByText('原回复')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '思考等级：关闭' }))
    fireEvent.click(screen.getByRole('menuitemradio', { name: /^高/ }))
    fireEvent.click(screen.getByRole('button', { name: '图片', pressed: false }))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '重新生成' })) })

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => init?.method === 'POST' && String(input).includes('/regenerate'))).toBe(true))
    const request = fetchMock.mock.calls.find(([input, init]) => init?.method === 'POST' && String(input).includes('/regenerate'))
    const body = JSON.parse(String(request?.[1]?.body)) as Record<string, unknown>
    expect(body).toMatchObject({ assistant_message_id: 'assistant-old', reasoning_effort: 'high' })
    expect(body).not.toHaveProperty('model')
    expect(body).not.toHaveProperty('channel_id')

    await act(async () => { delayedStream.release() })
    expect(await screen.findByText('新回复')).toBeTruthy()
  })

  it('renders a real empty state without inserting built-in conversations', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return jsonResponse([])
      if (url.endsWith('/projects')) return jsonResponse([])
      if (url.endsWith('/models')) return jsonResponse([{ model: 'text-model', modality: 'text', channel_name: '渠道', channel_id: 'channel-1', capabilities: ['text'] }])
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)

    expect(await screen.findByText('还没有对话')).toBeTruthy()
    expect(screen.queryByText('问候交流')).toBeNull()
    expect(screen.queryByText('模型报价表生成')).toBeNull()
    expect(screen.queryByText(/很高兴又见到你/)).toBeNull()
    expect(screen.queryByLabelText('消息')).toBeNull()
  })

  it('distinguishes a thread API failure from an empty list and offers retry', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return errorResponse()
      if (url.endsWith('/projects') || url.endsWith('/models')) return jsonResponse([])
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)

    expect(await screen.findByRole('heading', { name: '对话加载失败' })).toBeTruthy()
    expect(screen.getAllByRole('button', { name: /重试/ }).length).toBeGreaterThan(0)
    expect(screen.queryByText('还没有对话')).toBeNull()
    expect(screen.queryByText('问候交流')).toBeNull()
  })

  it('does not create a client-side conversation when the create API fails', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST' && url.endsWith('/threads')) return errorResponse()
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads') || url.endsWith('/projects') || url.endsWith('/models')) return jsonResponse([])
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)
    expect(await screen.findByText('还没有对话')).toBeTruthy()
    const createButtons = screen.getAllByRole('button', { name: '新聊天' })
    await act(async () => { fireEvent.click(createButtons[createButtons.length - 1]) })

    expect(await screen.findByText('会话创建失败，请稍后重试')).toBeTruthy()
    expect(screen.getByText('还没有对话')).toBeTruthy()
    expect(screen.queryByText('有什么可以帮忙的？')).toBeNull()
  })

  it('hides controls without a real API while preserving real response actions', async () => {
    const thread = {
      id: 'thread-actions',
      title: '真实对话',
      model: 'text-model',
      messages: [{ id: 'assistant-1', role: 'assistant', content: '真实回复', content_type: 'text' }],
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return jsonResponse([thread])
      if (url.endsWith('/projects')) return jsonResponse([])
      if (url.endsWith('/models')) return jsonResponse([{ model: 'text-model', modality: 'text', channel_name: '渠道', channel_id: 'channel-1', capabilities: ['text'] }])
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)
    expect(await screen.findByText('真实回复')).toBeTruthy()

    expect(document.querySelector('img.avatar')).toBeNull()

    expect(screen.getByRole('button', { name: '复制' })).toBeTruthy()
    expect(screen.getByRole('button', { name: '重新生成' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: '分享' })).toBeNull()
    expect(screen.queryByRole('button', { name: '赞' })).toBeNull()
    expect(screen.queryByRole('button', { name: '踩' })).toBeNull()
    expect(screen.queryByRole('button', { name: '语音输入' })).toBeNull()
    expect(screen.queryByText('资料库')).toBeNull()
    expect(screen.queryByText('已安排')).toBeNull()
    expect(screen.queryByText('插件')).toBeNull()
    expect(screen.queryByText('Codex')).toBeNull()
    expect(screen.queryByText('上传文件')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /Admin/ }))
    expect(screen.queryByText('帮助与常见问题')).toBeNull()
    expect(screen.queryByText('设置')).toBeNull()
  })

  it('encodes an uploaded image for a text-model vision request', async () => {
    const thread = { id: 'thread-assets', title: '新聊天', model: 'text-model', messages: [] }
    const frames = 'id: 1\nevent: message.created\ndata: {"message_id":"assistant-server","user_message_id":"user-server","request_id":"request-asset"}\n\nid: 2\nevent: message.completed\ndata: {"message_id":"assistant-server","content":"已处理"}\n\n'
    const delayedStream = delayedSseResponse(frames)
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST' && url.endsWith('/assets/upload')) return jsonResponse({ id: 'asset-1', url: '/api/v1/assets/asset-1', mime_type: 'image/png', size_bytes: 3 })
      if (init?.method === 'POST' && url.includes('/messages/stream')) return delayedStream.response
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return jsonResponse([thread])
      if (url.endsWith('/projects')) return jsonResponse([])
      if (url.endsWith('/models')) return jsonResponse([{ model: 'text-model', modality: 'text', channel_name: '渠道', channel_id: 'channel-1', capabilities: ['text'] }])
      return errorResponse(404)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)
    await waitFor(() => expect(screen.getByLabelText('选择模型')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('选择参考图片'), { target: { files: [new File(['png'], 'reference.png', { type: 'image/png' })] } })
    expect(await screen.findByText(/已上传 1 张图片/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: '根据参考图生成新图' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送' })) })

    await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => init?.method === 'POST' && String(input).includes('/messages/stream'))).toBe(true))
    const request = fetchMock.mock.calls.find(([input, init]) => init?.method === 'POST' && String(input).includes('/messages/stream'))
    const body = JSON.parse(String(request?.[1]?.body))
    expect(body).toMatchObject({
      asset_ids: ['asset-1'],
      media_inputs: [{ type: 'image', asset_id: 'asset-1', mime_type: 'image/png', detail: 'auto' }],
    })
    expect(body).not.toHaveProperty('model')
    expect(body.media_inputs[0].data_url).toMatch(/^data:image\/png;base64,/)
    await act(async () => { delayedStream.release() })
    expect(await screen.findByText('已处理')).toBeTruthy()
  })

  it('checks a selected text model image-byte limit before opening the stream', async () => {
    const thread = { id: 'thread-asset-limit', title: '新聊天', model: 'text-model', messages: [] }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST' && url.endsWith('/assets/upload')) return jsonResponse({ id: 'asset-limit', url: '/api/v1/assets/asset-limit', mime_type: 'image/png', size_bytes: 3 })
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return jsonResponse([thread])
      if (url.endsWith('/projects')) return jsonResponse([])
      if (url.endsWith('/models')) return jsonResponse([{ model: 'text-model', modality: 'text', channel_name: '渠道', channel_id: 'channel-1', capabilities: ['text'], supports_input_image: true, input_image_max_bytes: 2 }])
      return errorResponse(404)
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)
    await waitFor(() => expect(screen.getByLabelText('选择模型')).toBeTruthy())
    await chooseModel('网页版·text-model')
    fireEvent.change(screen.getByLabelText('选择参考图片'), { target: { files: [new File(['png'], 'reference.png', { type: 'image/png' })] } })
    expect(await screen.findByText(/已上传 1 张图片/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('消息'), { target: { value: '识别图片' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: '发送' })) })

    expect(await screen.findByText(/视觉输入大小限制/)).toBeTruthy()
    expect(fetchMock.mock.calls.some(([input, init]) => init?.method === 'POST' && String(input).includes('/messages/stream'))).toBe(false)
    expect((screen.getByLabelText('消息') as HTMLTextAreaElement).value).toBe('识别图片')
  })

  it('does not reveal model names when the account is not entitled', async () => {
    const values = new Map<string, string>([
      ['access_token', 'test-access-token'],
      ['auth_user', JSON.stringify({ display_name: 'Dream', role: 'user' })],
    ])
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value) },
      removeItem: (key: string) => { values.delete(key) },
      clear: () => { values.clear() },
    })
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/threads?include_archived=true')) return jsonResponse([])
      if (url.endsWith('/threads')) return jsonResponse([{ id: 'thread-1', title: '新聊天', messages: [] }])
      if (url.endsWith('/projects')) return jsonResponse([])
      if (url.endsWith('/models')) return jsonResponse([{ model: 'secret-model', modality: 'text', channel_name: '渠道1', channel_id: 'channel-1', capabilities: ['text'] }])
      if (url.endsWith('/me/entitlement')) return jsonResponse({ active: false })
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatWorkspace />)
    expect((await screen.findByRole('alert')).textContent).toContain('当前账户尚未开通使用权限，请联系管理员。')
    fireEvent.click(await screen.findByLabelText('选择模型'))
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(screen.queryByRole('option')).toBeNull()
    expect(screen.queryByText(/secret-model/)).toBeNull()
  })
})
