import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, Check, KeyRound, List, LoaderCircle, Pencil, Plus, RefreshCw, Save, Server, Trash2, X } from 'lucide-react'
import { formatBeijingTime } from '../utils/beijingTime'

type Modality = 'text' | 'image' | 'both'
type ChannelType = 'official' | 'codex'

interface ModelChannel {
  id: string
  name: string
  provider: string
  protocol: string
  channel_type: ChannelType
  base_url: string
  api_key_masked: string
  modality: Modality
  enabled: boolean
  priority: number
  models: string[]
  capabilities?: Record<string, unknown>
  models_synced_at?: string | null
  last_sync_error?: string | null
  created_at: string
  updated_at: string
}

interface ChannelDraft {
  name: string
  base_url: string
  api_key: string
  modality: Modality
  channel_type: ChannelType
  priority: number
  enabled: boolean
  capabilities: string
}

interface ChannelSyncResult {
  ok?: boolean
  message?: string
  models?: string[]
  capabilities?: Record<string, unknown>
}

const emptyDraft: ChannelDraft = { name: '', base_url: '', api_key: '', modality: 'text', channel_type: 'official', priority: 100, enabled: true, capabilities: '{}' }
const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000/api/v1'
const MODEL_SYNC_INTERVAL_MS = 60_000

export default function AdminModelChannelsPage({ onBack, onNotice }: { onBack: () => void; onNotice: (message: string) => void }) {
  const [channels, setChannels] = useState<ModelChannel[]>([])
  const [filter, setFilter] = useState<'all' | Modality>('all')
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [modalOpen, setModalOpen] = useState(false)
  const [editing, setEditing] = useState<ModelChannel | null>(null)
  const [draft, setDraft] = useState<ChannelDraft>(emptyDraft)
  const [testingId, setTestingId] = useState<string | null>(null)
  const [modelManagerChannel, setModelManagerChannel] = useState<ModelChannel | null>(null)
  const [managerModels, setManagerModels] = useState<string[]>([])
  const [managerSelected, setManagerSelected] = useState<Set<string>>(() => new Set())
  const [managerLoading, setManagerLoading] = useState(false)
  const [managerSaving, setManagerSaving] = useState(false)
  const [syncingIds, setSyncingIds] = useState<Set<string>>(() => new Set())
  const [autoSyncing, setAutoSyncing] = useState(false)
  const [lastAutoSyncAt, setLastAutoSyncAt] = useState<string | null>(null)
  const syncTasksRef = useRef<Map<string, Promise<ChannelSyncResult>>>(new Map())
  const pollInFlightRef = useRef(false)
  const onNoticeRef = useRef(onNotice)

  const visibleChannels = useMemo(() => filter === 'all' ? channels : channels.filter((channel) => channel.modality === filter || channel.modality === 'both'), [channels, filter])

  useEffect(() => { onNoticeRef.current = onNotice }, [onNotice])

  const runChannelSync = useCallback((channelId: string, signal?: AbortSignal): Promise<ChannelSyncResult> => {
    const existing = syncTasksRef.current.get(channelId)
    if (existing) return existing

    const task = requestChannelModelSync(channelId, signal)
    syncTasksRef.current.set(channelId, task)
    setSyncingIds((current) => new Set(current).add(channelId))

    const finish = () => {
      // A later task may already occupy this key; only the owner may clear it.
      if (syncTasksRef.current.get(channelId) !== task) return
      syncTasksRef.current.delete(channelId)
      setSyncingIds((current) => {
        const next = new Set(current)
        next.delete(channelId)
        return next
      })
    }
    void task.then(finish, finish)
    return task
  }, [])

  const loadChannels = async () => {
    setLoading(true)
    setLoadError('')
    try {
      const response = await fetch(`${API_BASE}/admin/model-channels`, { headers: authHeaders() })
      if (!response.ok) throw new Error(await apiErrorMessage(response, '渠道数据加载失败'))
      const result: unknown = await response.json()
      if (!Array.isArray(result)) throw new Error('渠道接口返回格式错误')
      setChannels(result as ModelChannel[])
    } catch (error) {
      setChannels([])
      const message = error instanceof Error ? error.message : '渠道数据加载失败'
      setLoadError(message)
      onNotice(message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    let disposed = false
    const controllers = new Set<AbortController>()

    const pollModels = async (initial: boolean) => {
      if (pollInFlightRef.current) return
      pollInFlightRef.current = true
      const controller = new AbortController()
      controllers.add(controller)
      if (!disposed) {
        setAutoSyncing(true)
        if (initial) {
          setLoading(true)
          setLoadError('')
        }
      }
      try {
        const current = await fetchChannelList(controller.signal)
        if (!disposed) setChannels(current)
        await Promise.allSettled(
          current
            .filter((channel) => channel.enabled)
            .map((channel) => runChannelSync(channel.id, controller.signal)),
        )
        const refreshed = await fetchChannelList(controller.signal)
        if (!disposed) {
          setChannels(refreshed)
          setLoadError('')
          setLastAutoSyncAt(new Date().toISOString())
        }
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') return
        if (!disposed) {
          const message = error instanceof Error ? error.message : '渠道模型自动同步失败'
          setLoadError(message)
          if (initial) onNoticeRef.current(message)
        }
      } finally {
        controllers.delete(controller)
        pollInFlightRef.current = false
        if (!disposed) {
          setAutoSyncing(false)
          if (initial) setLoading(false)
        }
      }
    }

    // StrictMode runs an extra setup/cleanup cycle in development. Deferring
    // the first request lets that rehearsal cancel before any POST reaches the API.
    const initialSyncId = window.setTimeout(() => { void pollModels(true) }, 0)
    const intervalId = window.setInterval(() => { void pollModels(false) }, MODEL_SYNC_INTERVAL_MS)
    return () => {
      disposed = true
      window.clearTimeout(initialSyncId)
      window.clearInterval(intervalId)
      controllers.forEach((controller) => controller.abort())
      controllers.clear()
    }
  }, [runChannelSync])

  const openCreate = () => { setEditing(null); setDraft(emptyDraft); setModalOpen(true) }
  const openEdit = (channel: ModelChannel) => {
    setEditing(channel)
    setDraft({ name: channel.name, base_url: channel.base_url, api_key: '', modality: channel.modality, channel_type: channel.channel_type ?? 'official', priority: channel.priority, enabled: channel.enabled, capabilities: JSON.stringify(channel.capabilities ?? {}, null, 2) })
    setModalOpen(true)
  }

  const save = async (event: FormEvent) => {
    event.preventDefault()
    let capabilities: Record<string, unknown>
    try {
      const parsed: unknown = JSON.parse(draft.capabilities.trim() || '{}')
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('object required')
      capabilities = parsed as Record<string, unknown>
    } catch {
      onNotice('能力映射必须是 JSON 对象')
      return
    }
    const body = { name: draft.name.trim(), base_url: draft.base_url.trim(), api_key: draft.api_key, modality: draft.modality, channel_type: draft.channel_type, priority: draft.priority, enabled: draft.enabled, capabilities, ...(editing ? {} : { models: [] }) }
    if (!body.name || !body.base_url) { onNotice('请填写名称和 Base URL'); return }
    const endpoint = editing ? `${API_BASE}/admin/model-channels/${editing.id}` : `${API_BASE}/admin/model-channels`
    try {
      const response = await fetch(endpoint, { method: editing ? 'PATCH' : 'POST', headers: { ...authHeaders(), 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      if (!response.ok) {
        const error = await response.json().catch(() => ({})) as { message?: string; detail?: string }
        onNotice(error.message ?? error.detail ?? '渠道保存失败')
        return
      }
      const saved = await response.json() as ModelChannel
      setChannels((items) => editing ? items.map((item) => item.id === saved.id ? saved : item) : [saved, ...items])
      setModalOpen(false)
      try {
        const syncResult = await runChannelSync(saved.id)
        const refreshed = await fetchChannelList()
        setChannels(refreshed)
        onNotice(syncResult.ok ? `${editing ? '渠道已更新' : '渠道已创建'}，已同步 ${syncResult.models?.length ?? 0} 个模型` : `${editing ? '渠道已更新' : '渠道已创建'}，模型同步失败`)
      } catch (error) {
        onNotice(`${editing ? '渠道已更新' : '渠道已创建'}，模型同步失败：${error instanceof Error ? error.message : '请检查渠道连接'}`)
      }
    } catch (error) {
      onNotice(error instanceof Error ? error.message : '渠道保存失败，请检查接口连接')
    }
  }

  const toggle = async (channel: ModelChannel) => {
    const next = { ...channel, enabled: !channel.enabled }
    setChannels((items) => items.map((item) => item.id === channel.id ? next : item))
    try {
      const response = await fetch(`${API_BASE}/admin/model-channels/${channel.id}`, { method: 'PATCH', headers: { ...authHeaders(), 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: next.enabled }) })
      if (!response.ok) throw new Error()
    } catch {
      setChannels((items) => items.map((item) => item.id === channel.id ? channel : item))
      onNotice('渠道状态更新失败')
    }
  }

  const test = async (channel: ModelChannel) => {
    setTestingId(channel.id)
    try {
      const response = await fetch(`${API_BASE}/admin/model-channels/${channel.id}/test`, { method: 'POST', headers: authHeaders() })
      if (!response.ok) {
        const error = await response.json().catch(() => ({})) as { message?: string; detail?: string }
        onNotice(error.message ?? error.detail ?? '渠道测试失败')
        return
      }
      const result = await response.json() as { message: string }
      onNotice(result.message)
    } catch { onNotice('渠道测试请求失败，请检查接口连接') } finally { setTestingId(null) }
  }

  const syncModels = async (channel: ModelChannel) => {
    try {
      const result = await runChannelSync(channel.id)
      try {
        // The sync response does not carry the persisted models_synced_at value.
        // Reload the channel record instead of fabricating that business timestamp
        // from the browser clock.
        setChannels(await fetchChannelList())
      } catch {
        onNotice(result.ok ? `${result.message ?? '模型已同步'}，但渠道数据刷新失败` : result.message ?? '模型同步失败')
        return
      }
      if (!result.ok) {
        onNotice(result.message ?? '模型同步失败')
        return
      }
      onNotice(result.message ?? '模型已同步')
    } catch (error) { onNotice(error instanceof Error ? error.message : '模型同步失败，请检查渠道连接') }
  }

  const remove = async (channel: ModelChannel) => {
    if (!window.confirm(`确认删除“${channel.name}”？`)) return
    setChannels((items) => items.filter((item) => item.id !== channel.id))
    try {
      const response = await fetch(`${API_BASE}/admin/model-channels/${channel.id}`, { method: 'DELETE', headers: authHeaders() })
      if (!response.ok) throw new Error()
      onNotice('渠道已删除或停用')
    } catch {
      setChannels((items) => [channel, ...items])
      onNotice('渠道删除失败')
    }
  }

  const openModelManager = async (channel: ModelChannel) => {
    setModelManagerChannel(channel)
    setManagerModels(channel.models)
    setManagerSelected(new Set(channel.models))
    setManagerLoading(true)
    try {
      const result = await requestRemoteModels(channel.id)
      if (result.ok) {
        setManagerModels(Array.from(new Set([...channel.models, ...(result.models ?? [])])))
      } else onNotice(result.message ?? '模型获取失败，当前显示已有模型')
    } catch (error) {
      onNotice(error instanceof Error ? error.message : '模型同步失败，当前显示已有模型')
    } finally {
      setManagerLoading(false)
    }
  }

  const saveManagedModels = async () => {
    if (!modelManagerChannel) return
    setManagerSaving(true)
    const models = managerModels.filter((model) => managerSelected.has(model))
    try {
      const response = await fetch(`${API_BASE}/admin/model-channels/${modelManagerChannel.id}`, {
        method: 'PATCH',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({ models }),
      })
      if (!response.ok) throw new Error(await apiErrorMessage(response, '模型列表保存失败'))
      const saved = await response.json() as ModelChannel
      setChannels((items) => items.map((item) => item.id === saved.id ? saved : item))
      setModelManagerChannel(saved)
      onNotice(`已保存 ${saved.models.length} 个模型`)
      setModelManagerChannel(null)
    } catch (error) {
      onNotice(error instanceof Error ? error.message : '模型列表保存失败')
    } finally {
      setManagerSaving(false)
    }
  }

  return <section className="workspace-page channel-page">
    <header><button className="icon-button" aria-label="返回对话" onClick={onBack}><ArrowLeft size={19} /></button><div className="page-heading"><span className="eyebrow"><Server size={14} />管理端</span><h1>模型渠道</h1></div><button className="workspace-primary" onClick={openCreate}><Plus size={17} />新增渠道</button></header>
    <div className="channel-toolbar"><div className="channel-tabs">{(['all', 'text', 'image', 'both'] as const).map((value) => <button key={value} className={filter === value ? 'selected' : ''} onClick={() => setFilter(value)}>{value === 'all' ? '全部' : value === 'text' ? '文本模型' : value === 'image' ? '图片模型' : '文本 + 图片'}</button>)}</div><button className="icon-button" title="刷新" aria-label="刷新" onClick={() => void loadChannels()} disabled={loading}>{loading ? <LoaderCircle size={17} className="spin" /> : <RefreshCw size={17} />}</button></div>
    <div className="channel-note"><KeyRound size={16} /><span>渠道采用 OpenAI 协议。API Key 仅用于服务端请求，列表中始终以掩码展示；时间均为北京时间。</span><span className={`channel-auto-sync ${loadError ? 'has-error' : ''}`}>{autoSyncing ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}每 60 秒自动同步{lastAutoSyncAt ? ` · 最近 ${formatBeijingTime(lastAutoSyncAt)}` : ''}{loadError ? ` · ${loadError}` : ''}</span></div>
    <div className="channel-table"><div className="channel-row channel-row-head"><span>渠道</span><span>类型 / 模态 / 模型</span><span>优先级</span><span>状态</span><span>操作</span></div>{visibleChannels.map((channel) => { const syncing = syncingIds.has(channel.id); return <div className="channel-row" key={channel.id}><div className="channel-name"><span className="channel-mark"><Server size={16} /></span><span><strong>{channel.name}</strong><small>{channel.base_url}</small></span></div><div><span className={`channel-type-pill ${channel.channel_type === 'codex' ? 'codex' : 'official'}`}>{channel.channel_type === 'codex' ? 'Codex GPT' : '官网版'}</span><span className={`modality-pill ${channel.modality}`}>{channel.modality === 'text' ? '文本' : channel.modality === 'image' ? '图片' : '文本 + 图片'}</span><small className="model-list">{channel.models.join(' · ')}</small>{channel.models_synced_at && <small className="model-sync-time">已同步 {formatBeijingTime(channel.models_synced_at)}</small>}{channel.last_sync_error && <small className="model-sync-error">同步失败</small>}</div><span className="priority-value">{channel.priority}</span><button className={`status-toggle ${channel.enabled ? 'on' : ''}`} role="switch" aria-checked={channel.enabled} onClick={() => void toggle(channel)} disabled={syncing}><i /><span>{channel.enabled ? '已启用' : '已停用'}</span></button><div className="channel-actions"><button className="icon-button" title="管理模型" aria-label="管理模型" onClick={() => void openModelManager(channel)} disabled={syncing}><List size={16} /></button><button className="icon-button" title="同步模型" aria-label="同步模型" onClick={() => void syncModels(channel)} disabled={syncing}>{syncing ? <LoaderCircle size={16} className="spin" /> : <RefreshCw size={16} />}</button><button className="icon-button" title="测试连接" aria-label="测试连接" onClick={() => void test(channel)} disabled={syncing}>{testingId === channel.id ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />}</button><button className="icon-button" title="编辑" aria-label="编辑" onClick={() => openEdit(channel)} disabled={syncing}><Pencil size={16} /></button><button className="icon-button danger-icon" title="删除" aria-label="删除" onClick={() => void remove(channel)} disabled={syncing}><Trash2 size={16} /></button></div></div>})}{!visibleChannels.length && <div className="channel-empty">{loading ? '正在加载渠道…' : loadError || (filter === 'all' ? '暂无渠道，请点击“新增渠道”进行配置' : '当前筛选没有渠道')}</div>}</div>
    {modalOpen && <div className="modal-backdrop" onMouseDown={() => setModalOpen(false)}><form className="channel-dialog" onSubmit={save} onMouseDown={(event) => event.stopPropagation()}><header><div><span className="eyebrow"><Server size={14} />OpenAI-compatible</span><h2>{editing ? '编辑模型渠道' : '新增模型渠道'}</h2></div><button type="button" className="icon-button" aria-label="关闭" onClick={() => setModalOpen(false)}><X size={18} /></button></header><label>渠道名称<input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} placeholder="例如：主文本渠道" /></label><label>渠道类型<select value={draft.channel_type} onChange={(event) => setDraft({ ...draft, channel_type: event.target.value as ChannelType })}><option value="official">官网版（支持联网等能力）</option><option value="codex">Codex GPT（联网能力由上游配置）</option></select></label><label>Base URL<input type="url" value={draft.base_url} onChange={(event) => setDraft({ ...draft, base_url: event.target.value })} placeholder="https://api.example.com/v1" /></label><label>API Key <small>同一地址可留空；更换服务地址时需重新输入</small><input type="password" value={draft.api_key} onChange={(event) => setDraft({ ...draft, api_key: event.target.value })} placeholder={editing ? '••••••••••••' : 'sk-...'}/></label><div className="field-grid"><label>模态<select value={draft.modality} onChange={(event) => setDraft({ ...draft, modality: event.target.value as Modality })}><option value="text">文本模型</option><option value="image">图片模型</option><option value="both">文本 + 图片</option></select></label><label>优先级<input type="number" min="0" max="10000" value={draft.priority} onChange={(event) => setDraft({ ...draft, priority: Number(event.target.value) })} /></label></div><div className="channel-model-hint">模型列表由保存后的渠道自动请求 <code>/v1/models</code> 获取；名称包含 <code>image</code> 的模型归类为图片模型，其余归类为文本模型。</div><label>能力映射 <small>JSON；例如 {`{"gpt-image-2":["image"],"_text_endpoint":"responses"}`}</small><textarea className="capabilities-editor" value={draft.capabilities} onChange={(event) => setDraft({ ...draft, capabilities: event.target.value })} spellCheck={false} rows={5} /></label><label className="check-field"><input type="checkbox" checked={draft.enabled} onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })} />创建后启用该渠道</label><footer><button type="button" className="secondary-button" onClick={() => setModalOpen(false)}>取消</button><button type="submit" className="workspace-primary">保存并同步模型</button></footer></form></div>}
    {modelManagerChannel && <div className="modal-backdrop" onMouseDown={() => !managerSaving && setModelManagerChannel(null)}><section className="channel-dialog model-manager-dialog" role="dialog" aria-modal="true" aria-labelledby="model-manager-title" onMouseDown={(event) => event.stopPropagation()}><header><div><span className="eyebrow"><List size={14} />模型管理</span><h2 id="model-manager-title">{modelManagerChannel.name}</h2><p className="dialog-subtitle">从渠道 `/v1/models` 选择要在用户端展示的模型</p></div><button type="button" className="icon-button" aria-label="关闭" onClick={() => setModelManagerChannel(null)} disabled={managerSaving}><X size={18} /></button></header><div className="model-manager-toolbar"><span>{managerLoading ? '正在获取远端模型…' : `已选择 ${managerSelected.size} / ${managerModels.length}`}</span><button type="button" className="secondary-button" onClick={() => void openModelManager(modelManagerChannel)} disabled={managerLoading || managerSaving}><RefreshCw size={15} />重新获取</button></div><div className="model-manager-list">{managerModels.map((model) => { const image = model.toLowerCase().includes('image'); return <label className="model-manager-item" key={model}><input type="checkbox" checked={managerSelected.has(model)} onChange={(event) => setManagerSelected((current) => { const next = new Set(current); if (event.target.checked) next.add(model); else next.delete(model); return next })} disabled={managerLoading || managerSaving} /><span><strong>{model}</strong><small className={image ? 'image' : 'text'}>{image ? '图片模型' : '文本模型'}</small></span></label> })}{!managerModels.length && <div className="channel-empty">{managerLoading ? '正在获取模型…' : '渠道暂未返回模型'}</div>}</div><footer><button type="button" className="secondary-button" onClick={() => setModelManagerChannel(null)} disabled={managerSaving}>取消</button><button type="button" className="workspace-primary" onClick={() => void saveManagedModels()} disabled={managerLoading || managerSaving || !managerModels.length}><Save size={15} />{managerSaving ? '保存中…' : '保存模型配置'}</button></footer></section></div>}
  </section>
}

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('access_token')
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function apiErrorMessage(response: Response, fallback: string): Promise<string> {
  const error = await response.json().catch(() => null) as { message?: string; detail?: string } | null
  return error?.message ?? error?.detail ?? fallback
}

async function fetchChannelList(signal?: AbortSignal): Promise<ModelChannel[]> {
  const response = await fetch(`${API_BASE}/admin/model-channels`, { headers: authHeaders(), signal })
  if (!response.ok) throw new Error(await apiErrorMessage(response, '渠道数据加载失败'))
  const result: unknown = await response.json()
  if (!Array.isArray(result)) throw new Error('渠道接口返回格式错误')
  return result as ModelChannel[]
}

async function requestChannelModelSync(channelId: string, signal?: AbortSignal): Promise<ChannelSyncResult> {
  const response = await fetch(`${API_BASE}/admin/model-channels/${channelId}/sync-models`, { method: 'POST', headers: authHeaders(), signal })
  const result = await response.json().catch(() => ({})) as ChannelSyncResult
  if (!response.ok) throw new Error(result.message ?? '模型同步失败')
  return result
}

async function requestRemoteModels(channelId: string): Promise<ChannelSyncResult> {
  const response = await fetch(`${API_BASE}/admin/model-channels/${channelId}/remote-models`, { headers: authHeaders() })
  const result = await response.json().catch(() => ({})) as ChannelSyncResult
  if (!response.ok) throw new Error(result.message ?? '远端模型获取失败')
  return result
}
