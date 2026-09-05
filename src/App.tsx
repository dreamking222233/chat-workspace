import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  ArrowLeft,
  ArrowUp,
  Check,
  Copy,
  Download,
  Ellipsis,
  FileText,
  Folder,
  Globe2,
  Image,
  LogOut,
  Menu,
  PanelLeftClose,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Server,
  ShieldOff,
  Square,
  Trash2,
  X,
} from 'lucide-react'
import EffortSlider, { type ReasoningEffort } from './components/EffortSlider'
import ProjectWorkspacePage from './components/ProjectWorkspacePage'
import SelectMenu from './components/SelectMenu'
import { encodeVisionImageBlob, VISION_MAX_TOTAL_ENCODED_CHARS, type VisionMimeType } from './utils/mediaEncode'
import { API_BASE } from './utils/api'

type Role = 'user' | 'assistant'
type WorkspaceView = '项目' | '已归档'
type LoadState = 'loading' | 'ready' | 'error'

interface Message {
  id: string
  role: Role
  content: string
  contentType?: string
  assetIds?: string[]
  activities?: Array<{ type: 'search'; label: string }>
}

interface ReferenceAsset {
  id: string
  url: string
  mimeType: string
  sizeBytes: number
  file?: File
}

interface MediaInput {
  type: 'image'
  data_url: string
  asset_id: string
  mime_type: 'image/jpeg' | 'image/png'
  width: number
  height: number
  detail: 'auto' | 'low' | 'high'
}

interface ModelOption {
  model: string
  modality: string
  channel_name: string
  channel_id?: string | null
  channel_type?: 'official' | 'codex'
  capabilities?: string[]
  supports_input_image?: boolean
  max_input_images?: number
  input_image_max_bytes?: number
  input_image_detail?: 'auto' | 'low' | 'high'
  supported_input_image_mime_types?: string[]
}

interface Thread {
  id: string
  title: string
  messages: Message[]
  projectId?: string | null
  model?: string
}

const navItems: Array<{ label: '新聊天' | WorkspaceView; icon: typeof Pencil }> = [
  { label: '新聊天', icon: Pencil },
  { label: '项目', icon: Folder },
  { label: '已归档', icon: Folder },
]
const wait = (milliseconds: number) => new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds))
function apiHeaders(json = false): Record<string, string> {
  const token = localStorage.getItem('access_token')
  return { ...(json ? { 'Content-Type': 'application/json' } : {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) }
}

async function apiErrorMessage(response: Response, fallback: string) {
  const raw = await response.text()
  try {
    const parsed = JSON.parse(raw) as { message?: unknown; detail?: unknown }
    const message = typeof parsed.message === 'string' ? parsed.message : typeof parsed.detail === 'string' ? parsed.detail : ''
    if (message.trim()) return message.trim()
  } catch { /* Non-JSON gateways can still return a useful plain-text response. */ }
  return raw.trim() || fallback
}

function apiMessage(value: { id: string; role: Role; content: string; content_type?: string; asset_ids?: string[] }): Message {
  return { id: value.id, role: value.role, content: value.content, contentType: value.content_type, assetIds: value.asset_ids ?? [] }
}

function apiThread(value: { id: string; title: string; project_id?: string | null; model?: string; messages?: Array<{ id: string; role: Role; content: string; content_type?: string; asset_ids?: string[] }> }): Thread {
  return { id: value.id, title: value.title, projectId: value.project_id, model: value.model, messages: (value.messages ?? []).map(apiMessage) }
}

function supportsCapability(option: ModelOption, capability: 'text' | 'image') {
  return (option.capabilities ?? option.modality.split('+')).includes(capability)
}

function modelOptionValue(option: ModelOption) {
  return option.channel_id ? `${option.channel_id}::${option.model}` : option.model
}

function modelOptionLabel(option: ModelOption) {
  return `${option.channel_type === 'codex' ? 'Codex' : '网页版'}·${option.model}`
}

function parseModelValue(value: string) {
  if (!value) return { model: '', channelId: null as string | null }
  const separator = value.indexOf('::')
  if (separator < 0) return { model: value, channelId: null as string | null }
  return { model: value.slice(separator + 2), channelId: value.slice(0, separator) }
}

export function ChatWorkspace() {
  const signedInUser = (() => { try { return JSON.parse(localStorage.getItem('auth_user') ?? 'null') as { display_name?: string; role?: string } | null } catch { return null } })()
  const [threads, setThreads] = useState<Thread[]>([])
  const [threadsState, setThreadsState] = useState<LoadState>('loading')
  const [projects, setProjects] = useState<Array<{ id: string; name: string; description: string; thread_count: number }>>([])
  const [projectsState, setProjectsState] = useState<LoadState>('loading')
  const [archivedThreads, setArchivedThreads] = useState<Thread[]>([])
  const [archivedState, setArchivedState] = useState<LoadState>('loading')
  const [activeId, setActiveId] = useState('')
  const [draft, setDraft] = useState('')
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth > 760)
  const [searching, setSearching] = useState(false)
  const [query, setQuery] = useState('')
  const [profileOpen, setProfileOpen] = useState(false)
  const [topMenuOpen, setTopMenuOpen] = useState(false)
  const [attachmentOpen, setAttachmentOpen] = useState(false)
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort | null>(null)
  const [reasoningMenuOpen, setReasoningMenuOpen] = useState(false)
  const [imageMode, setImageMode] = useState(false)
  const [modelOptions, setModelOptions] = useState<ModelOption[]>([])
  const [modelsState, setModelsState] = useState<LoadState>('loading')
  const [selectedModel, setSelectedModel] = useState('')
  const [selectedChannelId, setSelectedChannelId] = useState<string | null>(null)
  const [referenceAssets, setReferenceAssets] = useState<ReferenceAsset[]>([])
  const [uploading, setUploading] = useState(false)
  const [encodingVision, setEncodingVision] = useState(false)
  const [creatingThread, setCreatingThread] = useState(false)
  const [workspaceView, setWorkspaceView] = useState<WorkspaceView | null>(null)
  const [notice, setNotice] = useState('')
  const [streamingMessages, setStreamingMessages] = useState<Record<string, string>>({})
  const [stoppingThreadId, setStoppingThreadId] = useState<string | null>(null)
  const [entitlementActive, setEntitlementActive] = useState(false)
  const [entitlementState, setEntitlementState] = useState<LoadState>(signedInUser?.role === 'admin' ? 'ready' : 'loading')
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const streamRefs = useRef<Map<string, AbortController>>(new Map())
  const endRef = useRef<HTMLDivElement>(null)
  const lastVisibleThreadRef = useRef<string | null>(null)

  const activeThread = threads.find((thread) => thread.id === activeId)
  const activeStreamingMessageId = activeThread ? streamingMessages[activeThread.id] : undefined
  const activeStreaming = activeStreamingMessageId !== undefined
  const visibleThreads = useMemo(() => threads.filter((thread) => thread.title.toLowerCase().includes(query.trim().toLowerCase())), [threads, query])
  const textModelOptions = useMemo(() => modelOptions.filter((item) => supportsCapability(item, 'text')), [modelOptions])
  const imageModelOptions = useMemo(() => modelOptions.filter((item) => supportsCapability(item, 'image')), [modelOptions])
  const availableModelOptions = imageMode ? imageModelOptions : textModelOptions

  const selectedTextTarget = useMemo(() => {
    if (!selectedModel) return null
    return textModelOptions.find((item) => item.model === selectedModel && (!selectedChannelId || item.channel_id === selectedChannelId)) ?? null
  }, [selectedChannelId, selectedModel, textModelOptions])

  const activeThreadTextModel = useMemo(() => {
    if (!activeThread?.model) return undefined
    return textModelOptions.some((item) => item.model === activeThread.model) ? activeThread.model : undefined
  }, [activeThread?.model, textModelOptions])

  const loadThreads = useCallback(async () => {
    setThreadsState('loading')
    try {
      const response = await fetch(`${API_BASE}/threads`, { headers: apiHeaders() })
      if (!response.ok) throw new Error()
      const loaded = (await response.json() as Array<{ id: string; title: string; project_id?: string | null; model?: string; messages?: Array<{ id: string; role: Role; content: string; content_type?: string; asset_ids?: string[] }> }>).map(apiThread)
      setThreads(loaded)
      setActiveId((current) => loaded.some((thread) => thread.id === current) ? current : loaded[0]?.id ?? '')
      setThreadsState('ready')
    } catch {
      setThreads([])
      setActiveId('')
      setThreadsState('error')
    }
  }, [])

  const loadArchivedThreads = useCallback(async () => {
    setArchivedState('loading')
    try {
      const response = await fetch(`${API_BASE}/threads?include_archived=true`, { headers: apiHeaders() })
      if (!response.ok) throw new Error()
      const loaded = await response.json() as Array<{ id: string; title: string; project_id?: string | null; model?: string; archived_at?: string | null; messages?: Array<{ id: string; role: Role; content: string; content_type?: string; asset_ids?: string[] }> }>
      setArchivedThreads(loaded.filter((thread) => thread.archived_at).map(apiThread))
      setArchivedState('ready')
    } catch {
      setArchivedThreads([])
      setArchivedState('error')
    }
  }, [])

  const loadProjects = useCallback(async () => {
    setProjectsState('loading')
    try {
      const response = await fetch(`${API_BASE}/projects`, { headers: apiHeaders() })
      if (!response.ok) throw new Error()
      setProjects(await response.json() as Array<{ id: string; name: string; description: string; thread_count: number }>)
      setProjectsState('ready')
    } catch {
      setProjects([])
      setProjectsState('error')
    }
  }, [])

  const loadModels = useCallback(async () => {
    setModelsState('loading')
    try {
      const response = await fetch(`${API_BASE}/models`, { headers: apiHeaders() })
      if (!response.ok) throw new Error()
      setModelOptions(await response.json() as ModelOption[])
      setModelsState('ready')
    } catch {
      setModelOptions([])
      setModelsState('error')
    }
  }, [])

  const loadEntitlement = useCallback(async () => {
    if (signedInUser?.role === 'admin') {
      setEntitlementActive(true)
      setEntitlementState('ready')
      return
    }
    setEntitlementState('loading')
    try {
      const response = await fetch(`${API_BASE}/me/entitlement`, { headers: apiHeaders() })
      if (!response.ok) throw new Error()
      const entitlement = await response.json() as { active?: boolean } | null
      setEntitlementActive(Boolean(entitlement?.active))
      setEntitlementState('ready')
    } catch {
      setEntitlementActive(false)
      setEntitlementState('error')
    }
  }, [signedInUser?.role])

  useEffect(() => () => streamRefs.current.forEach((controller) => controller.abort()), [])

  useEffect(() => {
    void loadThreads()
    void loadArchivedThreads()
    void loadProjects()
    void loadModels()
    void loadEntitlement()
  }, [loadArchivedThreads, loadEntitlement, loadModels, loadProjects, loadThreads])

  useEffect(() => {
    if (!selectedModel) return
    const current = modelOptions.find((item) => item.model === selectedModel && (!selectedChannelId || item.channel_id === selectedChannelId))
    if (!current) { if (selectedChannelId) { setSelectedModel(''); setSelectedChannelId(null) }; return }
    if (!supportsCapability(current, imageMode ? 'image' : 'text')) { setSelectedModel(''); setSelectedChannelId(null) }
  }, [imageMode, modelOptions, selectedChannelId, selectedModel])

  useEffect(() => {
    if (threadsState !== 'ready') return
    setActiveId((current) => current && threads.some((thread) => thread.id === current) ? current : threads[0]?.id ?? '')
  }, [threads, threadsState])

  useEffect(() => {
    // Reference images belong to the active draft. Never carry them into a
    // different thread where the user did not select them.
    setReferenceAssets([])
  }, [activeId])

  useEffect(() => {
    const media = window.matchMedia('(max-width: 760px)')
    const syncSidebar = (event: MediaQueryListEvent) => setSidebarOpen(!event.matches)
    media.addEventListener('change', syncSidebar)
    return () => media.removeEventListener('change', syncSidebar)
  }, [])

  useEffect(() => {
    if (!activeThread) return
    const changedThread = lastVisibleThreadRef.current !== activeId
    lastVisibleThreadRef.current = activeId
    if (changedThread || activeStreamingMessageId !== undefined) endRef.current?.scrollIntoView({ behavior: changedThread ? 'auto' : 'smooth' })
  }, [activeId, activeThread, activeStreamingMessageId])

  useEffect(() => {
    if (!textareaRef.current) return
    textareaRef.current.style.height = '40px'
    textareaRef.current.style.height = `${Math.min(Math.max(textareaRef.current.scrollHeight, 40), 140)}px`
  }, [draft])

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(''), 1800)
    return () => window.clearTimeout(timer)
  }, [notice])

  const startRemoteStream = async (
    threadId: string,
    assistantId: string,
    prompt: string,
    model?: string,
    localUserId?: string,
    options: { regenerateMessageId?: string; restoreContent?: string; assetIds?: string[]; mediaInputs?: MediaInput[]; channelId?: string | null } = {},
  ) => {
    const controller = new AbortController()
    streamRefs.current.set(threadId, controller)
    setStreamingMessages((current) => ({ ...current, [threadId]: assistantId }))
    let requestId: string | null = null
    const idempotencyKey = typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : `send-${Date.now()}-${Math.random().toString(16).slice(2)}`
    let lastEventId = 0
    let serverMessageId = assistantId
    let serverUserMessageId = localUserId
    let completed = false
    let attempt = 0
    const applyMessageId = (id: string) => {
      if (id === serverMessageId) return
      // React may execute functional state updaters after this handler returns.
      // Capture the placeholder ID before mutating the stream cursor, otherwise
      // the updater searches for the new server ID and drops every later delta.
      const previousMessageId = serverMessageId
      serverMessageId = id
      setThreads((current) => current.map((thread) => thread.id === threadId ? { ...thread, messages: thread.messages.map((message) => message.id === previousMessageId ? { ...message, id } : message) } : thread))
      setStreamingMessages((current) => ({ ...current, [threadId]: id }))
    }
    const handle = (block: string) => {
      const eventId = Number(block.match(/^id:\s*(\d+)$/m)?.[1] ?? 0)
      if (eventId && eventId <= lastEventId) return
      const event = block.match(/^event:\s*(.+)$/m)?.[1]
      const raw = block.match(/^data:\s*(.+)$/m)?.[1]
      if (!event || !raw) return
      let data: { message_id?: string; user_message_id?: string; request_id?: string; delta?: string; content?: string; url?: string; asset_id?: string; tool_name?: string; tool_call_id?: string; mime_type?: string; arguments?: string; error?: string; ok?: boolean; query?: string }
      try { data = JSON.parse(raw) as typeof data } catch { return }
      if (data.request_id) requestId = data.request_id
      if (data.user_message_id && serverUserMessageId) {
        const previousUserMessageId = serverUserMessageId
        serverUserMessageId = data.user_message_id
        setThreads((current) => current.map((thread) => thread.id === threadId ? { ...thread, messages: thread.messages.map((message) => message.id === previousUserMessageId ? { ...message, id: data.user_message_id! } : message) } : thread))
      }
      if ((event === 'message.created' || event === 'message.delta' || event === 'message.completed') && data.message_id) applyMessageId(data.message_id)
      if (event === 'message.created' && data.user_message_id) setThreads((current) => current.map((thread) => thread.id === threadId && thread.title === '新聊天' ? { ...thread, title: prompt.slice(0, 60) } : thread))
      if (event === 'message.delta' && data.delta) setThreads((current) => current.map((thread) => thread.id === threadId ? { ...thread, messages: thread.messages.map((message) => message.id === serverMessageId ? { ...message, content: message.content + data.delta, contentType: 'text' } : message) } : thread))
      if (event === 'message.completed' && data.content !== undefined) { completed = true; setThreads((current) => current.map((thread) => thread.id === threadId ? { ...thread, messages: thread.messages.map((message) => message.id === serverMessageId ? { ...message, content: data.content!, contentType: data.content ? 'text' : message.contentType } : message) } : thread)) }
      if (event === 'search.started' && data.query) {
        const label = data.query.trim()
        if (label) setThreads((current) => current.map((thread) => thread.id === threadId ? {
          ...thread,
          messages: thread.messages.map((message) => message.id === (data.message_id ?? serverMessageId) ? {
            ...message,
            activities: [...(message.activities ?? []).filter((activity) => activity.type !== 'search' || activity.label !== label), { type: 'search' as const, label }],
          } : message),
        } : thread))
      }
      if (event === 'tool.started') {
        setNotice(`正在调用${data.tool_name ?? '工具'}`)
        if (data.tool_name === 'generate_image') setThreads((current) => current.map((thread) => thread.id === threadId ? { ...thread, messages: thread.messages.map((message) => message.id === serverMessageId ? { ...message, contentType: 'image_pending' } : message) } : thread))
      }
      if (event === 'tool.completed' && data.tool_name) {
        setNotice(data.ok === false ? data.error?.trim() || `${data.tool_name}调用失败` : `${data.tool_name}已完成`)
        if (data.tool_name === 'generate_image' && data.ok === false) setThreads((current) => current.map((thread) => thread.id === threadId ? { ...thread, messages: thread.messages.map((message) => message.id === serverMessageId && message.contentType === 'image_pending' ? { ...message, contentType: 'text' } : message) } : thread))
      }
      if (event === 'image.created' && data.message_id && data.url) {
        setThreads((current) => current.map((thread) => {
          if (thread.id !== threadId) return thread
          const messages = thread.messages.map((message) => message.id === serverMessageId && message.contentType === 'image_pending' ? { ...message, contentType: 'tool_pending' } : message)
          if (messages.some((message) => message.id === data.message_id)) return { ...thread, messages }
          return { ...thread, messages: [...messages, { id: data.message_id!, role: 'assistant' as const, content: data.url!, contentType: 'image', assetIds: data.asset_id ? [data.asset_id] : [] }] }
        }))
        setNotice('图片已生成')
      }
      if (event === 'error') {
        completed = true
        if (options.restoreContent !== undefined) setThreads((current) => current.map((thread) => thread.id === threadId ? { ...thread, messages: thread.messages.map((message) => message.id === serverMessageId ? { ...message, content: options.restoreContent! } : message) } : thread))
        else {
          const failedIds = new Set([assistantId, serverMessageId, localUserId, serverUserMessageId].filter((value): value is string => Boolean(value)))
          setThreads((current) => current.map((thread) => thread.id === threadId ? { ...thread, messages: thread.messages.filter((message) => !failedIds.has(message.id)) } : thread))
          void loadThreads()
        }
        setNotice('模型请求失败，请检查渠道配置后重试')
      }
      // A reconnect cursor acknowledges only a complete, parsed frame. Moving
      // it before JSON parsing would skip a truncated event after EOF/retry.
      if (eventId) lastEventId = eventId
    }
    try {
      while (!completed && !controller.signal.aborted) {
        try {
          const endpoint = new URL(`${API_BASE}/threads/${threadId}/${options.regenerateMessageId ? 'regenerate' : 'messages/stream'}`, window.location.origin)
          if (requestId) endpoint.searchParams.set('request_id', requestId)
          const headers = { ...apiHeaders(true), 'Idempotency-Key': idempotencyKey, ...(lastEventId ? { 'Last-Event-ID': String(lastEventId) } : {}) }
          const body = options.regenerateMessageId
            ? { assistant_message_id: options.regenerateMessageId, model: model || undefined, channel_id: options.channelId || undefined, reasoning_effort: reasoningEffort ?? undefined }
            : { content: prompt, model: model || undefined, channel_id: options.channelId || undefined, modality: 'text', asset_ids: options.assetIds ?? [], media_inputs: options.mediaInputs ?? [], enable_tools: true, reasoning_effort: reasoningEffort ?? undefined }
          const response = await fetch(endpoint.toString(), { method: 'POST', headers, body: JSON.stringify(body), signal: controller.signal })
          if (!response.ok || !response.body) { const failure = new Error(await response.text()); (failure as Error & { status?: number }).status = response.status; throw failure }
          const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''
            while (true) {
              const { value, done } = await reader.read()
              buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done })
              const blocks = buffer.split('\n\n'); buffer = blocks.pop() ?? ''; blocks.forEach(handle)
              if (done && buffer.trim()) handle(buffer)
              if (done) break
            }
          if (!completed && attempt < 2) { attempt += 1; await wait(350 * attempt); continue }
          break
        } catch (error) {
          if (controller.signal.aborted) break
          if (attempt < 2) { attempt += 1; await wait(350 * attempt); continue }
          throw error
        }
      }
      if (!completed && !controller.signal.aborted) throw new Error('stream ended before completion')
      setStreamingMessages((current) => { const next = { ...current }; delete next[threadId]; return next })
      streamRefs.current.delete(threadId)
    } catch (error) {
      streamRefs.current.delete(threadId)
      setStreamingMessages((current) => { const next = { ...current }; delete next[threadId]; return next })
      if (!controller.signal.aborted) {
        const status = (error as Error & { status?: number }).status
        const denied = status === 403 || (error instanceof Error && error.message.includes('403'))
        if (options.regenerateMessageId && options.restoreContent !== undefined) {
          setThreads((current) => current.map((thread) => thread.id === threadId ? { ...thread, messages: thread.messages.map((message) => message.id === assistantId || message.id === serverMessageId ? { ...message, content: options.restoreContent! } : message) } : thread))
          setNotice('重新生成失败，请检查模型渠道后重试')
        } else {
          const failedIds = new Set([assistantId, serverMessageId, localUserId, serverUserMessageId].filter((value): value is string => Boolean(value)))
          setThreads((current) => current.map((thread) => thread.id === threadId ? { ...thread, messages: thread.messages.filter((message) => !failedIds.has(message.id)) } : thread))
          if (requestId) void loadThreads()
          setNotice(denied ? '当前账户没有有效使用权限' : '模型服务暂不可用，请检查渠道配置后重试')
        }
        if (denied) {
          setEntitlementActive(false)
          setEntitlementState('ready')
        }
      }
    }
  }

  const selectThread = (id: string) => {
    setActiveId(id)
    setWorkspaceView(null)
    setSidebarOpen(window.innerWidth > 760)
  }

  const createThread = async (projectId?: string) => {
    if (creatingThread) return
    setCreatingThread(true)
    try {
      const response = await fetch(`${API_BASE}/threads`, { method: 'POST', headers: apiHeaders(true), body: JSON.stringify({ title: '新聊天', project_id: projectId ?? null }) })
      if (!response.ok) throw new Error()
      const created = apiThread(await response.json())
      setThreads((current) => [created, ...current.filter((thread) => thread.id !== created.id)])
      setThreadsState('ready')
      if (projectId) setProjects((current) => current.map((project) => project.id === projectId ? { ...project, thread_count: project.thread_count + 1 } : project))
      setActiveId(created.id)
      setWorkspaceView(null)
      setDraft('')
      setSearching(false)
      setQuery('')
      setSidebarOpen(window.innerWidth > 760)
      window.setTimeout(() => textareaRef.current?.focus(), 30)
    } catch {
      setNotice('会话创建失败，请稍后重试')
    } finally {
      setCreatingThread(false)
    }
  }

  const renameThread = async (thread: Thread) => {
    const title = window.prompt('重命名对话', thread.title)
    if (!title?.trim() || title.trim() === thread.title) return
    const nextTitle = title.trim().slice(0, 200)
    try {
      const response = await fetch(`${API_BASE}/threads/${thread.id}`, { method: 'PATCH', headers: apiHeaders(true), body: JSON.stringify({ title: nextTitle }) })
      if (!response.ok) throw new Error()
      const updated = apiThread(await response.json())
      setThreads((current) => current.map((item) => item.id === thread.id ? { ...item, title: updated.title } : item))
      setNotice('对话已重命名')
    } catch {
      setNotice('对话重命名失败')
    }
  }

  const openWorkspace = (view: WorkspaceView) => {
    setWorkspaceView(view)
    setSidebarOpen(window.innerWidth > 760)
  }

  const createProject = async (name: string, description: string) => {
    try {
      const response = await fetch(`${API_BASE}/projects`, { method: 'POST', headers: apiHeaders(true), body: JSON.stringify({ name, description }) })
      if (!response.ok) throw new Error()
      const project = await response.json() as { id: string; name: string; description: string; thread_count: number }
      setProjects((current) => [project, ...current]); setProjectsState('ready'); setNotice('项目已创建')
    } catch { setNotice('项目创建失败，请检查登录状态') }
  }

  const toggleSearch = () => {
    setSearching((current) => {
      if (current) setQuery('')
      return !current
    })
  }

  const send = async (event?: FormEvent) => {
    event?.preventDefault()
    const content = draft.trim()
    if (!content || activeStreaming || uploading || encodingVision || !activeThread) return
    if (entitlementState === 'loading') { setNotice('正在加载权限状态'); return }
    if (entitlementState === 'error') { setNotice('权限状态加载失败，请重试'); return }
    if (!entitlementActive) { setNotice('当前账户尚未开通使用权限'); return }
    if (modelsState === 'loading') { setNotice('正在加载模型列表'); return }
    if (modelsState === 'error') { setNotice('模型列表加载失败，请重试'); return }
    if (!availableModelOptions.length) { setNotice(`未配置可用${imageMode ? '图片' : '文本'}模型`); return }
    const threadId = activeThread.id
    const pendingId = typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`
    const userMessageId = `pending-user-${pendingId}`
    const assistantId = `pending-assistant-${pendingId}`
    const selectedAssets = [...referenceAssets]
    const assetIds = selectedAssets.map((asset) => asset.id)
    if (!imageMode && selectedAssets.length && selectedTextTarget?.supports_input_image === false) {
      setNotice('当前选择的文本模型不支持图片理解，请切换支持视觉输入的模型')
      return
    }
    if (!imageMode && selectedTextTarget && selectedAssets.length > Math.max(1, Math.min(8, selectedTextTarget.max_input_images ?? 8))) {
      setNotice(`当前文本模型最多支持 ${Math.max(1, Math.min(8, selectedTextTarget.max_input_images ?? 8))} 张图片`)
      return
    }
    let mediaInputs: MediaInput[] = []
    if (!imageMode && selectedAssets.length) {
      setEncodingVision(true)
      try {
        const inputImageDetail = selectedTextTarget?.input_image_detail ?? 'auto'
        const configuredMimeTypes = selectedTextTarget?.supported_input_image_mime_types ?? []
        const preferredMimeTypes = configuredMimeTypes
          .map((value) => String(value).trim().toLowerCase())
          .filter((value): value is VisionMimeType => value === 'image/jpeg' || value === 'image/png')
        const encodedImages = await Promise.all(selectedAssets.map(async (asset) => {
          let blob: Blob
          if (asset.file) {
            blob = asset.file
          } else {
            const response = await fetch(assetRequestUrl(asset.url), { headers: apiHeaders() })
            if (!response.ok) throw new Error('图片读取失败')
            blob = await response.blob()
          }
          return await encodeVisionImageBlob(blob, undefined, preferredMimeTypes.length ? preferredMimeTypes : undefined)
        }))
        const maxImageBytes = Math.max(0, Number(selectedTextTarget?.input_image_max_bytes ?? 0))
        if (maxImageBytes && encodedImages.some((item) => item.decodedSize > maxImageBytes)) {
          throw new Error(`图片超过当前文本模型的视觉输入大小限制（${maxImageBytes} 字节）`)
        }
        if (encodedImages.reduce((total, item) => total + item.encodedLength, 0) > VISION_MAX_TOTAL_ENCODED_CHARS) {
          throw new Error('本轮视觉输入超过 3 MiB 编码限制')
        }
        mediaInputs = encodedImages.map((encoded, index) => ({
          type: 'image' as const,
          data_url: encoded.dataUrl,
          asset_id: selectedAssets[index].id,
          mime_type: encoded.mimeType,
          width: encoded.width,
          height: encoded.height,
          detail: inputImageDetail,
        }))
      } catch (error) {
        setNotice(error instanceof Error ? error.message : '图片处理失败，请重试')
        setEncodingVision(false)
        return
      }
      setEncodingVision(false)
    }
    setThreads((current) => current.map((thread) => thread.id === threadId ? {
      ...thread,
      messages: [...thread.messages, { id: userMessageId, role: 'user', content, assetIds }, { id: assistantId, role: 'assistant', content: '', contentType: imageMode ? 'image_pending' : 'text' }],
    } : thread))
    setDraft('')
    if (imageMode) {
      setStreamingMessages((current) => ({ ...current, [threadId]: assistantId }))
      void generateImage(threadId, assistantId, userMessageId, content)
      return
    }
    setReferenceAssets([])
    // A direct image request records its image model on the thread. When the
    // composer returns to text/automatic mode, never submit that image-only
    // model as an explicit text target; let the backend resolve a text channel.
    // With the automatic model selector, a previous thread model is only a
    // preference. Let the server fall back to another enabled vision-capable
    // text model when that previous model is text-only; an explicitly selected
    // model/channel remains a strict routing choice.
    const requestTextModel = selectedTextTarget?.model ?? (selectedAssets.length ? undefined : activeThreadTextModel)
    void startRemoteStream(threadId, assistantId, content, requestTextModel, userMessageId, { assetIds, mediaInputs, channelId: selectedTextTarget?.channel_id ?? null })
  }

  const generateImage = async (threadId: string, assistantId: string, localUserId: string, prompt: string) => {
    const controller = new AbortController()
    streamRefs.current.set(threadId, controller)
    try {
      const key = typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : `image-${Date.now()}-${Math.random().toString(16).slice(2)}`
      const response = await fetch(`${API_BASE}/threads/${threadId}/image-generations`, { method: 'POST', headers: { ...apiHeaders(true), 'Idempotency-Key': key }, body: JSON.stringify({ prompt, model: selectedModel || undefined, channel_id: selectedChannelId || undefined, asset_ids: referenceAssets.map((asset) => asset.id), response_format: 'b64_json' }), signal: controller.signal })
      if (!response.ok) {
        const failure = new Error(await apiErrorMessage(response, '图片生成失败，请检查图片模型渠道')) as Error & { status?: number }
        failure.status = response.status
        throw failure
      }
      const result = await response.json() as { message_id: string; user_message_id: string | null; asset_id?: string; url: string; model?: string }
      setThreads((current) => current.map((thread) => thread.id === threadId ? { ...thread, title: thread.title === '新聊天' ? prompt.slice(0, 60) : thread.title, model: result.model ?? thread.model, messages: thread.messages.map((message) => message.id === localUserId && result.user_message_id ? { ...message, id: result.user_message_id } : message.id === assistantId ? { ...message, id: result.message_id, content: result.url, contentType: 'image', assetIds: result.asset_id ? [result.asset_id] : [] } : message) } : thread))
      setReferenceAssets([])
      setStreamingMessages((current) => { const next = { ...current }; delete next[threadId]; return next })
      setNotice('图片已生成')
    } catch (error) {
      setStreamingMessages((current) => { const next = { ...current }; delete next[threadId]; return next })
      setThreads((current) => current.map((thread) => thread.id === threadId ? {
        ...thread,
        // A user-initiated stop keeps the prompt in the conversation and only
        // removes the optimistic image placeholder. The non-abort branch
        // reloads below because the server persists its failure message.
        messages: thread.messages.filter((message) => message.id !== assistantId && (controller.signal.aborted || message.id !== localUserId)),
      } : thread))
      if (!controller.signal.aborted) {
        const status = error instanceof Error ? (error as Error & { status?: number }).status : undefined
        if (status === 403) { setEntitlementActive(false); setEntitlementState('ready') }
        setNotice(status === 403 ? '当前账户没有有效使用权限' : error instanceof Error && error.message ? error.message : '图片生成失败，请检查图片模型渠道')
        void loadThreads()
      }
    }
    finally { streamRefs.current.delete(threadId) }
  }

  const stopStreaming = async () => {
    if (!activeThread) return
    const threadId = activeThread.id
    if (stoppingThreadId === threadId) return
    setStoppingThreadId(threadId)
    try {
      const response = await fetch(`${API_BASE}/threads/${threadId}/messages/stop`, { method: 'POST', headers: apiHeaders() })
      if (!response.ok) throw new Error()
      const result = await response.json() as { stopped?: boolean }
      if (!result.stopped) { setNotice('当前没有可停止的模型请求'); return }
      const controller = streamRefs.current.get(threadId)
      if (controller) controller.abort()
      streamRefs.current.delete(threadId)
      setStreamingMessages((current) => { const next = { ...current }; delete next[threadId]; return next })
      setNotice('已停止生成')
    } catch {
      setNotice('停止生成失败，请重试')
    } finally {
      setStoppingThreadId((current) => current === threadId ? null : current)
    }
  }

  const uploadAttachment = async (files: FileList | File[]) => {
    const incoming = Array.from(files).filter(Boolean)
    if (!incoming.length) return
    const availableSlots = Math.max(0, 8 - referenceAssets.length)
    if (!availableSlots) { setNotice('本轮最多添加 8 张图片'); setAttachmentOpen(false); return }
    const selected = incoming.slice(0, availableSlots)
    const skipped = incoming.length - selected.length
    setUploading(true)
    let uploadedCount = 0
    let failedCount = 0
    try {
      // Upload one at a time so the browser does not hold several large
      // multipart bodies in memory while the vision encoder may run next.
      for (const file of selected) {
        if (!file.type.toLowerCase().startsWith('image/')) { failedCount += 1; continue }
        try {
          const form = new FormData(); form.append('file', file)
          const response = await fetch(`${API_BASE}/assets/upload`, { method: 'POST', headers: apiHeaders(), body: form })
          if (!response.ok) throw new Error()
          const uploaded = await response.json() as { id: string; url: string; mime_type: string; size_bytes: number }
          if (!uploaded.id || !uploaded.mime_type?.toLowerCase().startsWith('image/')) throw new Error()
          setReferenceAssets((current) => current.some((item) => item.id === uploaded.id) ? current : [...current, { id: uploaded.id, url: uploaded.url, mimeType: uploaded.mime_type, sizeBytes: uploaded.size_bytes, file }].slice(-8))
          uploadedCount += 1
        } catch {
          failedCount += 1
        }
      }
      const suffix = skipped ? `（已跳过 ${skipped} 张，单轮最多 8 张）` : ''
      if (uploadedCount) setNotice(imageMode ? `已上传 ${uploadedCount} 张参考图${suffix}` : `已上传 ${uploadedCount} 张图片，发送后将由文本模型读取${suffix}`)
      else setNotice(failedCount ? '附件上传失败，请重试' : '没有可上传的图片')
    } finally {
      setUploading(false)
      setAttachmentOpen(false)
    }
  }

  const regenerate = (messageId: string) => {
    if (activeStreaming || !activeThread) return
    if (entitlementState !== 'ready' || !entitlementActive) { setNotice(entitlementState === 'error' ? '权限状态加载失败，请重试' : '当前账户尚未开通使用权限'); return }
    if (modelsState !== 'ready' || !textModelOptions.length) { setNotice(modelsState === 'error' ? '模型列表加载失败，请重试' : '未配置可用文本模型'); return }
    const target = activeThread.messages.find((message) => message.id === messageId)
    if (!target || target.contentType === 'image') return
    const previousContent = target?.content ?? ''
    setThreads((current) => current.map((thread) => thread.id === activeThread.id ? {
      ...thread,
      messages: thread.messages.map((message) => message.id === messageId ? { ...message, content: '' } : message),
    } : thread))
    const previousUser = [...activeThread.messages].reverse().find((message) => message.role === 'user')
    void startRemoteStream(activeThread.id, messageId, previousUser?.content ?? '', selectedTextTarget?.model ?? activeThreadTextModel, undefined, { regenerateMessageId: messageId, restoreContent: previousContent, channelId: selectedTextTarget?.channel_id ?? null })
  }

  const removeActiveThread = async (message: string, archive = false) => {
    if (!activeThread) return
    const target = activeThread
    try {
      const response = await fetch(`${API_BASE}/threads/${target.id}`, { method: archive ? 'PATCH' : 'DELETE', headers: apiHeaders(true), body: archive ? JSON.stringify({ archived: true }) : undefined })
      if (!response.ok) throw new Error()
      const controller = streamRefs.current.get(target.id)
      if (controller) controller.abort()
      streamRefs.current.delete(target.id)
      setStreamingMessages((current) => { const next = { ...current }; delete next[target.id]; return next })
      if (archive) {
        setArchivedThreads((current) => [target, ...current.filter((thread) => thread.id !== target.id)])
        setArchivedState('ready')
      }
      setThreads((current) => current.filter((thread) => thread.id !== target.id))
      setActiveId((current) => current === target.id ? '' : current)
      setTopMenuOpen(false)
      setNotice(message)
    } catch {
      setNotice(archive ? '对话归档失败' : '对话删除失败')
    }
  }

  const restoreThread = async (threadId: string) => {
    const restored = archivedThreads.find((thread) => thread.id === threadId)
    if (!restored) return
    try {
      const response = await fetch(`${API_BASE}/threads/${restored.id}`, { method: 'PATCH', headers: apiHeaders(true), body: JSON.stringify({ archived: false }) })
      if (!response.ok) throw new Error()
      const updated = apiThread(await response.json())
      setArchivedThreads((current) => current.filter((thread) => thread.id !== threadId))
      setThreads((current) => [updated, ...current.filter((thread) => thread.id !== threadId)])
      setThreadsState('ready')
      setActiveId(updated.id)
      setWorkspaceView(null)
      setNotice('对话已恢复')
    } catch {
      setNotice('对话恢复失败')
    }
  }

  const logout = () => {
    const refreshToken = localStorage.getItem('refresh_token')
    if (refreshToken) void fetch(`${API_BASE}/auth/logout`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ refresh_token: refreshToken }) }).catch(() => undefined)
    localStorage.removeItem('access_token'); localStorage.removeItem('refresh_token'); localStorage.removeItem('auth_user')
    window.history.pushState({}, '', '/'); window.dispatchEvent(new PopStateEvent('popstate'))
  }

  const exportActive = async (format: 'json' | 'markdown' | 'txt') => {
    if (!activeThread) return
    try {
      const response = await fetch(`${API_BASE}/threads/${activeThread.id}/export?format=${format}`, { headers: apiHeaders() })
      if (!response.ok) throw new Error()
      const blob = await response.blob(); const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = `chat-${activeThread.id}.${format === 'markdown' ? 'md' : format}`; link.click(); URL.revokeObjectURL(url); setNotice('对话已导出')
    } catch { setNotice('导出失败，请稍后重试') }
  }

  return (
    <div className="app-shell">
      {sidebarOpen && <button className="drawer-mask" aria-label="关闭侧边栏" onClick={() => setSidebarOpen(false)} />}
      <aside className={`sidebar ${sidebarOpen ? 'open' : ''}`}>
        <header className="side-head">
          <button className="wordmark" title="首页" onClick={() => { window.history.pushState({}, '', '/'); window.dispatchEvent(new PopStateEvent('popstate')) }}><strong>ChatGPT</strong></button>
          <div className="side-head-actions">
            <button className="icon-button" title="搜索" aria-label="搜索" onClick={toggleSearch}><Search size={19} /></button>
            <button className="icon-button" title="收起边栏" aria-label="收起边栏" onClick={() => setSidebarOpen(false)}><PanelLeftClose size={19} /></button>
          </div>
        </header>

        {searching && <div className="side-search"><Search size={16} /><input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索聊天" /><button aria-label="关闭搜索" onClick={() => { setSearching(false); setQuery('') }}><X size={15} /></button></div>}

        <nav className="side-nav">
          {navItems.map((item) => {
            const Icon = item.icon
            const active = item.label !== '新聊天' && workspaceView === item.label
            return <button key={item.label} className={active ? 'active' : ''} disabled={item.label === '新聊天' && creatingThread} onClick={() => item.label === '新聊天' ? void createThread() : openWorkspace(item.label)}><Icon size={18} strokeWidth={1.85} /><span>{item.label}</span></button>
          })}
        </nav>

        <div className="recent-list">
          <div className="recent-label">最近</div>
          {threadsState === 'ready' && visibleThreads.map((thread) => <button key={thread.id} className={!workspaceView && thread.id === activeId ? 'active' : ''} onClick={() => selectThread(thread.id)}><span>{thread.title}</span><Ellipsis className="recent-more" size={16} role="button" tabIndex={0} aria-label="重命名对话" onClick={(event) => { event.stopPropagation(); void renameThread(thread) }} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); event.stopPropagation(); void renameThread(thread) } }} /></button>)}
          {threadsState === 'loading' && <p>正在加载对话…</p>}
          {threadsState === 'error' && <p><button className="inline-retry" onClick={() => void loadThreads()}>对话加载失败，重试</button></p>}
          {threadsState === 'ready' && !visibleThreads.length && <p>{query.trim() ? '没有找到对话' : '暂无对话'}</p>}
        </div>

        {signedInUser && <footer className="account-area">
          <button className="account-button" aria-expanded={profileOpen} onClick={() => setProfileOpen((value) => !value)}><span className="avatar account-avatar" aria-hidden="true">{signedInUser.display_name?.trim().slice(0, 1).toUpperCase()}</span><span><strong>{signedInUser.display_name}</strong><small>个人账户</small></span></button>
          {profileOpen && <div className="account-menu">{signedInUser.role === 'admin' && <button onClick={() => { window.history.pushState({}, '', '/admin/model-channels'); window.dispatchEvent(new PopStateEvent('popstate')); setProfileOpen(false) }}><Server size={16} />管理端：模型渠道</button>}<button onClick={logout}><LogOut size={16} />退出登录</button></div>}
        </footer>}
      </aside>

      <main className="main-area">
        <header className="top-actions">
          {!sidebarOpen && <button className="icon-button menu-button" title="打开边栏" aria-label="打开边栏" onClick={() => setSidebarOpen(true)}><Menu size={20} /></button>}
          {!workspaceView && activeThread && <div className="top-right">
            <div className="top-menu-wrap"><button className="icon-button" title="更多" aria-label="更多" aria-expanded={topMenuOpen} onClick={() => setTopMenuOpen((value) => !value)}><Ellipsis size={20} /></button>{topMenuOpen && <div className="top-menu"><button onClick={() => void removeActiveThread('对话已归档', true)}><Folder size={16} />归档</button><button onClick={() => void exportActive('markdown')}><FileText size={16} />导出 Markdown</button><button onClick={() => void exportActive('json')}><FileText size={16} />导出 JSON</button><button className="danger" onClick={() => void removeActiveThread('对话已删除')}><Trash2 size={16} />删除</button></div>}</div>
          </div>}
        </header>

        {workspaceView === '项目' ? (projectsState === 'ready' ? <ProjectWorkspacePage projects={projects} onBack={() => setWorkspaceView(null)} onCreateProject={createProject} onCreateThread={(projectId) => void createThread(projectId)} /> : <WorkspaceResourceState title="项目" state={projectsState} onBack={() => setWorkspaceView(null)} onRetry={() => void loadProjects()} />) : workspaceView === '已归档' ? <ArchivedThreadsPage threads={archivedThreads} state={archivedState} onBack={() => setWorkspaceView(null)} onRetry={() => void loadArchivedThreads()} onRestore={(id) => void restoreThread(id)} /> : (
          <section className="chat-stage">
            <div className="message-scroll"><div className={`messages ${!activeThread || !activeThread.messages.length ? 'empty' : ''}`}>
              {threadsState === 'loading' && <ChatResourceState title="正在加载对话…" />}
              {threadsState === 'error' && <ChatResourceState title="对话加载失败" action="重试" onAction={() => void loadThreads()} />}
              {threadsState === 'ready' && !activeThread && <ChatResourceState title="还没有对话" action={creatingThread ? '正在创建…' : '新聊天'} onAction={creatingThread ? undefined : () => void createThread()} />}
              {threadsState === 'ready' && activeThread && !activeThread.messages.length && <h1>有什么可以帮忙的？</h1>}
              {activeThread?.messages.map((message) => <MessageView key={message.id} message={message} streaming={activeStreamingMessageId === message.id} onRegenerate={() => regenerate(message.id)} />)}
              <div ref={endRef} />
            </div></div>

            {activeThread && <div className="composer-dock">
              {entitlementState === 'ready' && !entitlementActive ? (
                <p className="composer-status composer-status-locked" role="alert"><ShieldOff size={15} />当前账户尚未开通使用权限，请联系管理员。</p>
              ) : (
                <p className="composer-status">{entitlementState === 'loading' ? '正在加载权限状态…' : entitlementState === 'error' ? <><span>权限状态加载失败</span><button type="button" onClick={() => void loadEntitlement()}>重试</button></> : modelsState === 'loading' ? '正在加载模型列表…' : modelsState === 'error' ? <><span>模型列表加载失败</span><button type="button" onClick={() => void loadModels()}>重试</button></> : !availableModelOptions.length ? `未配置可用${imageMode ? '图片' : '文本'}模型` : 'ChatGPT 也可能会犯错。请核查重要信息。'}</p>
              )}
              {referenceAssets.length > 0 && <div className="reference-strip" aria-label="已添加的参考图片">{referenceAssets.map((asset) => <div className="reference-chip" key={asset.id}><AuthenticatedAssetImage path={asset.url} alt="参考图" /><button type="button" title="移除参考图" aria-label="移除参考图" onClick={() => setReferenceAssets((current) => current.filter((item) => item.id !== asset.id))}><X size={13} /></button></div>)}</div>}
              <form className="composer" onSubmit={send}>
                <div className="attachment-wrap"><button type="button" className={`composer-icon ${attachmentOpen ? 'active' : ''}`} title="添加图片" aria-label="添加图片" aria-expanded={attachmentOpen} disabled={uploading || encodingVision} onClick={() => setAttachmentOpen((value) => !value)}><Plus size={22} /></button>{attachmentOpen && <div className="attachment-menu"><button type="button" onClick={() => document.getElementById('image-upload-input')?.click()}><Image size={17} />{uploading ? '正在上传…' : imageMode ? '添加参考图' : '添加图片并识别'}</button></div>}<input id="image-upload-input" aria-label="选择参考图片" hidden type="file" accept="image/*" multiple onChange={(event) => { const files = event.currentTarget.files; if (files?.length) void uploadAttachment(files); event.currentTarget.value = '' }} /></div>
                <textarea ref={textareaRef} rows={1} value={draft} placeholder="有问题，随便问" aria-label="消息" onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send() } }} />
                <button type="button" className={`think-button ${imageMode ? 'active' : ''}`} aria-pressed={imageMode} onClick={() => { setImageMode((value) => !value); setReasoningMenuOpen(false); setSelectedModel(''); setSelectedChannelId(null) }}><Image size={19} /><span>图片</span></button>
                <EffortSlider value={reasoningEffort} open={reasoningMenuOpen} disabled={imageMode} onChange={setReasoningEffort} onOpenChange={setReasoningMenuOpen} />
                {((entitlementState === 'ready' && !entitlementActive) || availableModelOptions.length > 0) && <SelectMenu ariaLabel="选择模型" value={selectedChannelId && selectedModel ? `${selectedChannelId}::${selectedModel}` : selectedModel} placeholder="自动模型" header="自动模型" locked={!entitlementActive} options={entitlementActive ? availableModelOptions.map((item) => ({ value: modelOptionValue(item), label: modelOptionLabel(item) })) : []} onLocked={() => setNotice('当前账户尚未开通使用权限，请联系管理员。')} onOpenChange={(open) => { if (open) setReasoningMenuOpen(false) }} onChange={(value) => { const next = parseModelValue(value); setSelectedModel(next.model); setSelectedChannelId(next.channelId) }} />}
                <button type={activeStreaming ? 'button' : 'submit'} disabled={activeStreaming ? stoppingThreadId === activeThread.id : !draft.trim() || uploading || encodingVision || entitlementState !== 'ready' || !entitlementActive || modelsState !== 'ready' || !availableModelOptions.length} className={`voice-button ${draft.trim() ? 'send-ready' : ''}`} title={activeStreaming ? stoppingThreadId === activeThread.id ? '正在停止' : '停止生成' : uploading ? '正在上传图片' : encodingVision ? '正在处理图片' : '发送'} aria-label={activeStreaming ? stoppingThreadId === activeThread.id ? '正在停止' : '停止生成' : uploading ? '正在上传图片' : encodingVision ? '正在处理图片' : '发送'} onClick={() => { if (activeStreaming) void stopStreaming() }}>{activeStreaming ? <Square size={17} fill="currentColor" /> : uploading || encodingVision ? <RefreshCw size={17} className="spin" /> : <ArrowUp size={20} strokeWidth={2.4} />}</button>
              </form>
            </div>}
          </section>
        )}
      </main>

      {notice && <div className="toast" role="status">{notice}</div>}
    </div>
  )
}

function ChatResourceState({ title, action, onAction }: { title: string; action?: string; onAction?: () => void }) {
  return <div className="chat-resource-state" role="status"><h1>{title}</h1>{action && <button type="button" className="secondary-button" disabled={!onAction} onClick={onAction}>{action}</button>}</div>
}

function WorkspaceResourceState({ title, state, onBack, onRetry }: { title: string; state: LoadState; onBack: () => void; onRetry: () => void }) {
  return <section className="workspace-page"><header><button className="icon-button" aria-label="返回对话" onClick={onBack}><ArrowLeft size={19} /></button><h1>{title}</h1></header><div className="workspace-empty"><Folder size={28} /><h2>{state === 'loading' ? `正在加载${title}…` : `${title}加载失败`}</h2>{state === 'error' && <button type="button" onClick={onRetry}>重试</button>}</div></section>
}

function ArchivedThreadsPage({ threads, state, onBack, onRetry, onRestore }: { threads: Thread[]; state: LoadState; onBack: () => void; onRetry: () => void; onRestore: (id: string) => void }) {
  if (state !== 'ready') return <WorkspaceResourceState title="已归档" state={state} onBack={onBack} onRetry={onRetry} />
  return <section className="workspace-page"><header><button className="icon-button" aria-label="返回对话" onClick={onBack}><ArrowLeft size={19} /></button><h1>已归档</h1></header>{threads.length ? <div className="archive-list"><h2>已归档对话</h2>{threads.map((thread) => <div key={thread.id}><span>{thread.title}</span><button type="button" onClick={() => onRestore(thread.id)}>恢复</button></div>)}</div> : <div className="workspace-empty"><Folder size={28} /><h2>暂无已归档对话</h2></div>}</section>
}

function MessageView({ message, streaming, onRegenerate }: { message: Message; streaming: boolean; onRegenerate: () => void }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    if (!navigator.clipboard?.writeText) return
    try {
      await navigator.clipboard.writeText(message.content)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1200)
    } catch { /* Keep the neutral copy state when the browser rejects access. */ }
  }
  if (message.role === 'user') return <div className="user-row"><div className="user-bubble">{message.content}</div>{Boolean(message.assetIds?.length) && <div className="user-attachments" aria-label="已附加图片">{message.assetIds?.map((assetId) => <AuthenticatedAssetImage key={assetId} path={`/api/v1/assets/${assetId}`} alt="已附加图片" />)}</div>}</div>
  if (message.contentType === 'image_pending') return streaming ? <article className="assistant-row image-pending-row"><ImageGenerationPlaceholder /></article> : null
  if (message.contentType === 'tool_pending' && !message.content) return null
  const searchActivities = message.activities?.filter((activity) => activity.type === 'search') ?? []
  const activeSearch = searchActivities[searchActivities.length - 1]
  const waitingForFirstToken = streaming && !message.content && !activeSearch
  const isGeneratedImage = message.contentType === 'image' && Boolean(message.content) && Boolean(message.assetIds?.length) && isImageAssetPath(message.content)
  const content = isGeneratedImage ? <AssetPreview path={message.content} /> : <div className={`assistant-copy ${streaming ? 'streaming' : ''} ${waitingForFirstToken ? 'waiting' : ''}`} aria-busy={streaming}>
    {activeSearch && streaming && !message.content
      ? <SearchActivity label={activeSearch.label} />
      : waitingForFirstToken
        ? <span className="model-waiting" role="status" aria-label="正在等待模型回复"><i className="model-waiting-dot" aria-hidden="true" /></span>
        : message.content ? <MarkdownResponse content={message.content} /> : null}
    {streaming && Boolean(message.content) && <i className="cursor" aria-hidden="true" />}
  </div>
  return <article className="assistant-row">{content}{!streaming && message.content && <div className="response-actions">
    <button title="复制" aria-label="复制" onClick={copy}>{copied ? <Check size={17} /> : <Copy size={17} />}</button>
    {message.contentType !== 'image' && <button title="重新生成" aria-label="重新生成" onClick={onRegenerate}><RefreshCw size={17} /></button>}
  </div>}</article>
}

function SearchActivity({ label }: { label: string }) {
  return <div className="search-activity" role="status" aria-label={`正在搜索 ${label}`} aria-live="polite">
    <Globe2 size={20} aria-hidden="true" />
    <span>正在搜索 <strong>{label}</strong></span>
  </div>
}

function MarkdownResponse({ content }: { content: string }) {
  const normalized = content
    .replace(/^\s*```(?:markdown|md)[ \t]*\r?\n/i, '')
    .replace(/\r?\n```[ \t]*$/i, '')
  return <div className="assistant-markdown">
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      skipHtml
      components={{
        a: ({ href, children }) => <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>,
        img: ({ alt }) => <span className="markdown-image-omitted">外部图片：{alt || '未命名图片'}</span>,
      }}
    >{normalized}</ReactMarkdown>
  </div>
}

function ImageGenerationPlaceholder({ label = '正在创建图片' }: { label?: string }) {
  return <div className="image-generation-placeholder" role="status" aria-label={label} aria-live="polite">
    <strong>{label}</strong>
    <span className="image-generation-pattern" aria-hidden="true" />
  </div>
}

function isImageAssetPath(path: string) {
  return path.startsWith('/api/v1/assets/') || path.startsWith('data:image/')
}

function AssetPreview({ path }: { path: string }) {
  const [source, setSource] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let cancelled = false
    let objectUrl: string | null = null
    const load = async () => {
      try {
        setSource(null)
        setFailed(false)
        if (path.startsWith('data:image/')) { if (!cancelled) setSource(path); return }
        if (!path.startsWith('/api/v1/assets/')) throw new Error('invalid image asset path')
        const response = await fetch(assetRequestUrl(path), { headers: apiHeaders() })
        if (!response.ok) throw new Error()
        const contentType = response.headers?.get?.('content-type')?.toLowerCase() ?? ''
        if (!contentType.startsWith('image/')) throw new Error('invalid image response')
        objectUrl = URL.createObjectURL(await response.blob())
        if (!cancelled) setSource(objectUrl)
      } catch { if (!cancelled) { setSource(null); setFailed(true) } }
    }
    void load()
    return () => { cancelled = true; if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [path])
  if (failed) return <div className="assistant-copy" role="alert">图片资源加载失败，请稍后重试。</div>
  if (!source) return <ImageGenerationPlaceholder label="正在加载图片" />
  return <div className="image-result"><img className="assistant-image" src={source} alt="生成的图片" onError={() => { setSource(null); setFailed(true) }} /><a className="image-download" href={source} download title="下载图片"><Download size={15} />下载图片</a></div>
}

function AuthenticatedAssetImage({ path, alt }: { path: string; alt: string }) {
  const [source, setSource] = useState<string | null>(null)
  useEffect(() => {
    let cancelled = false
    let objectUrl: string | null = null
    const load = async () => {
      try {
        if (path.startsWith('http') || path.startsWith('data:')) { if (!cancelled) setSource(path); return }
        const response = await fetch(assetRequestUrl(path), { headers: apiHeaders() })
        if (!response.ok) throw new Error()
        objectUrl = URL.createObjectURL(await response.blob())
        if (!cancelled) setSource(objectUrl)
      } catch { if (!cancelled) setSource(null) }
    }
    void load()
    return () => { cancelled = true; if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [path])
  return source ? <img src={source} alt={alt} /> : <span className="image-loading" aria-label="图片加载中" />
}

function assetRequestUrl(value: string): string {
  if (value.startsWith('http') || value.startsWith('data:')) return value
  return `${API_BASE.replace(/\/api\/v1$/, '')}${value}`
}

export default ChatWorkspace
