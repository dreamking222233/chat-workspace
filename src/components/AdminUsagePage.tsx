import { useEffect, useState } from 'react'
import { BarChart3, ChevronLeft, ChevronRight, Download, LoaderCircle, RefreshCw, Search, X } from 'lucide-react'
import { beijingDateBoundary, formatBeijingDateTime } from '../utils/beijingTime'
import { API_BASE } from '../utils/api'

type Usage = { id: string; user_id: string; user_email?: string; thread_id?: string; model: string; modality: string; status: string; input_tokens: number | null; output_tokens: number | null; latency_ms: number | null; created_at: string }
type LoadState = 'loading' | 'ready' | 'error'
const PAGE_SIZE = 50

export default function AdminUsagePage({ onNotice }: { onNotice: (message: string) => void }) {
  const [rows, setRows] = useState<Usage[]>([])
  const [loading, setLoading] = useState(true)
  const [loadState, setLoadState] = useState<LoadState>('loading')
  const [modality, setModality] = useState('')
  const [model, setModel] = useState('')
  const [query, setQuery] = useState('')
  const [fromDate, setFromDate] = useState('')
  const [toDate, setToDate] = useState('')
  const [page, setPage] = useState(1)
  const [hasMore, setHasMore] = useState(false)

  const load = async (requestedPage = page, filterOverrides?: { modality?: string; model?: string; query?: string; fromDate?: string; toDate?: string }) => {
    setLoading(true)
    setLoadState('loading')
    try {
      const params = new URLSearchParams({ offset: String((requestedPage - 1) * PAGE_SIZE), limit: String(PAGE_SIZE) })
      const activeModality = filterOverrides?.modality ?? modality
      if (activeModality) params.set('modality', activeModality)
      const activeModel = filterOverrides?.model ?? model
      const activeQuery = filterOverrides?.query ?? query
      const activeFrom = filterOverrides?.fromDate ?? fromDate
      const activeTo = filterOverrides?.toDate ?? toDate
      if (activeModel.trim()) params.set('model', activeModel.trim())
      if (activeQuery.trim()) params.set('q', activeQuery.trim())
      const createdAfter = activeFrom ? beijingDateBoundary(activeFrom, 'start') : null
      const createdBefore = activeTo ? beijingDateBoundary(activeTo, 'end') : null
      if (createdAfter) params.set('created_after', createdAfter)
      if (createdBefore) params.set('created_before', createdBefore)
      const response = await fetch(`${API_BASE}/admin/usage?${params}`, { headers: headers() })
      if (!response.ok) throw new Error()
      const data = await response.json() as Usage[]
      setRows(data); setPage(requestedPage); setHasMore(data.length === PAGE_SIZE); setLoadState('ready')
    } catch {
      setRows([]); setHasMore(false); setLoadState('error'); onNotice('使用记录加载失败，请检查后端服务')
    } finally { setLoading(false) }
  }
  useEffect(() => { void load(1) }, [modality])

  const clearFilters = () => {
    const hadModality = Boolean(modality)
    setModality(''); setModel(''); setQuery(''); setFromDate(''); setToDate(''); setPage(1)
    if (!hadModality) void load(1, { modality: '', model: '', query: '', fromDate: '', toDate: '' })
  }
  const exportCsv = () => {
    if (!rows.length) { onNotice('当前没有可导出的记录'); return }
    const header = ['时间（北京时间）', '用户', '模型', '模态', '状态', '输入 token', '输出 token', '延迟(ms)']
    const body = rows.map((item) => [formatBeijingDateTime(item.created_at), item.user_email ?? item.user_id, item.model, item.modality, item.status, item.input_tokens ?? '', item.output_tokens ?? '', item.latency_ms ?? ''])
    const csv = [header, ...body].map((line) => line.map((value) => `"${String(value).replace(/"/g, '""')}"`).join(',')).join('\n')
    const blob = new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8' }); const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = `model-usage-page-${page}.csv`; link.click(); URL.revokeObjectURL(url); onNotice('使用记录已导出')
  }

  const input = rows.reduce((sum, item) => sum + (item.input_tokens ?? 0), 0); const output = rows.reduce((sum, item) => sum + (item.output_tokens ?? 0), 0)
  return <section className="admin-page-content">
    <div className="admin-page-toolbar usage-toolbar"><div className="admin-search"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void load(1) }} placeholder="搜索用户邮箱" /></div><div className="usage-filters"><input type="search" value={model} onChange={(event) => setModel(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void load(1) }} placeholder="模型名称" aria-label="模型名称" /><select value={modality} onChange={(event) => setModality(event.target.value)} aria-label="模态"><option value="">全部模态</option><option value="text">文本</option><option value="image">图片</option></select><label><span>从（北京时间）</span><input type="date" aria-label="开始日期（北京时间）" value={fromDate} onChange={(event) => setFromDate(event.target.value)} /></label><label><span>至（北京时间）</span><input type="date" aria-label="结束日期（北京时间）" value={toDate} onChange={(event) => setToDate(event.target.value)} /></label><button className="icon-button" title="应用日期筛选" aria-label="应用日期筛选" onClick={() => void load(1)}><Search size={16} /></button><button className="icon-button" title="清空筛选" aria-label="清空筛选" onClick={clearFilters}><X size={16} /></button></div><button className="icon-button" title="刷新" aria-label="刷新" onClick={() => void load(page)} disabled={loading}>{loading ? <LoaderCircle size={17} className="spin" /> : <RefreshCw size={17} />}</button><button className="secondary-button usage-export" onClick={exportCsv} disabled={loadState !== 'ready' || !rows.length}><Download size={15} />导出 CSV</button></div>
    {loadState === 'loading' && <div className="usage-empty" role="status"><LoaderCircle size={25} className="spin" /><span>正在加载使用记录…</span></div>}
    {loadState === 'error' && <div className="usage-empty" role="status"><BarChart3 size={25} /><span>使用记录加载失败</span><button type="button" className="secondary-button" onClick={() => void load(page)}>重试</button></div>}
    {loadState === 'ready' && <><div className="usage-stat-strip"><div><span>本页请求次数</span><strong>{rows.length}</strong></div><div><span>输入 token</span><strong>{input.toLocaleString()}</strong></div><div><span>输出 token</span><strong>{output.toLocaleString()}</strong></div><div><span>已完成</span><strong>{rows.filter((item) => item.status === 'completed').length}</strong></div></div>
    <div className="usage-table"><div className="usage-row usage-row-head"><span>时间（北京时间）</span><span>用户</span><span>模型 / 模态</span><span>状态</span><span>Token</span><span>延迟</span></div>{rows.map((item) => <div className="usage-row" key={item.id}><span className="usage-time">{formatBeijingDateTime(item.created_at)}</span><span className="usage-user">{item.user_email ?? item.user_id.slice(0, 8)}</span><span><strong>{item.model}</strong><small>{item.modality === 'text' ? '文本模型' : '图片模型'}</small></span><span className={`request-status ${item.status}`}>{item.status === 'completed' ? '已完成' : item.status === 'failed' ? '失败' : item.status === 'stopped' ? '已停止' : item.status}</span><span className="token-pair">{item.input_tokens ?? '—'} / {item.output_tokens ?? '—'}</span><span>{item.latency_ms !== null ? `${item.latency_ms} ms` : '—'}</span></div>)}{!rows.length && <div className="usage-empty"><BarChart3 size={25} /><span>还没有模型调用记录</span></div>}</div>
    <div className="table-pagination"><span>第 {page} 页{hasMore ? '' : ' · 已到末页'}</span><div><button className="icon-button" title="上一页" aria-label="上一页" disabled={page <= 1 || loading} onClick={() => void load(page - 1)}><ChevronLeft size={17} /></button><button className="icon-button" title="下一页" aria-label="下一页" disabled={!hasMore || loading} onClick={() => void load(page + 1)}><ChevronRight size={17} /></button></div></div></>}
  </section>
}

function headers(): Record<string, string> { const token = localStorage.getItem('access_token'); return token ? { Authorization: `Bearer ${token}` } : {} }
