import { FormEvent, useEffect, useMemo, useState } from 'react'
import { Ban, CalendarClock, Check, ChevronLeft, ChevronRight, LoaderCircle, PauseCircle, PlayCircle, Plus, RefreshCw, Search, ShieldOff, UserRound, X } from 'lucide-react'
import { formatBeijingDate, formatBeijingDateTime } from '../utils/beijingTime'
import { API_BASE } from '../utils/api'

type AdminUser = { id: string; email: string; display_name: string; role: string; status: string; created_at: string; entitlement_expires_at: string | null; entitlement_active: boolean }
type Entitlement = { id: string; starts_at: string; expires_at: string; status: string; active: boolean }
type UserDetail = { user: AdminUser; entitlement: Entitlement | null; projects: number; threads: number; requests: number }
type LoadState = 'loading' | 'ready' | 'error'
const PAGE_SIZE = 20

export default function AdminUsersPage({ onNotice }: { onNotice: (message: string) => void }) {
  const [users, setUsers] = useState<AdminUser[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('')
  const [loading, setLoading] = useState(true)
  const [loadState, setLoadState] = useState<LoadState>('loading')
  const [selected, setSelected] = useState<AdminUser | null>(null)
  const [detail, setDetail] = useState<UserDetail | null>(null)
  const [grantOpen, setGrantOpen] = useState(false)
  const [months, setMonths] = useState(1)
  const [entitlementBusy, setEntitlementBusy] = useState(false)
  const visible = useMemo(() => users, [users])
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))

  const load = async (requestedPage = page) => {
    setLoading(true)
    setLoadState('loading')
    try {
      const params = new URLSearchParams({ page: String(requestedPage), page_size: String(PAGE_SIZE) })
      if (query.trim()) params.set('q', query.trim())
      if (status) params.set('status', status)
      const response = await fetch(`${API_BASE}/admin/users?${params}`, { headers: headers() })
      if (!response.ok) throw new Error()
      const body = await response.json() as { items: AdminUser[]; total: number }
      setUsers(body.items); setTotal(body.total); setPage(requestedPage); setLoadState('ready')
    } catch {
      setUsers([]); setTotal(0); setLoadState('error'); onNotice('用户数据加载失败，请检查后端服务')
    } finally { setLoading(false) }
  }
  useEffect(() => { void load(1) }, [status])

  const openDetail = async (user: AdminUser) => {
    setSelected(null); setDetail(null)
    try {
      const response = await fetch(`${API_BASE}/admin/users/${user.id}`, { headers: headers() })
      if (!response.ok) throw new Error()
      const loadedDetail = await response.json() as UserDetail
      setSelected(user); setDetail(loadedDetail)
    } catch { onNotice('用户详情加载失败，请检查后端服务') }
  }

  const grant = async (event: FormEvent) => {
    event.preventDefault(); if (!selected) return
    try {
      const response = await fetch(`${API_BASE}/admin/users/${selected.id}/entitlements`, { method: 'POST', headers: { ...headers(), 'Content-Type': 'application/json' }, body: JSON.stringify({ months }) })
      if (!response.ok) { onNotice('授权失败'); return }
      onNotice(`已开通 ${months} 个月使用权限`); setGrantOpen(false); await load(page); await openDetail(selected)
    } catch { onNotice('授权失败，请检查后端服务') }
  }

  const updateEntitlement = async (action: 'pause' | 'activate' | 'revoke') => {
    if (!selected || !detail?.entitlement) return
    const labels = { pause: '暂停', activate: '恢复', revoke: '撤销' }
    if (action === 'revoke' && !window.confirm(`确认撤销 ${selected.display_name} 的当前权限？`)) return
    setEntitlementBusy(true)
    try {
      const response = await fetch(`${API_BASE}/admin/entitlements/${detail.entitlement.id}?action=${action}`, { method: 'PATCH', headers: headers() })
      if (!response.ok) throw new Error()
      onNotice(`已${labels[action]}使用权限`); await load(page); await openDetail(selected)
    } catch { onNotice(`${labels[action]}权限失败`) } finally { setEntitlementBusy(false) }
  }

  const toggleStatus = async (user: AdminUser) => {
    const next = user.status === 'active' ? 'disabled' : 'active'
    if (!window.confirm(`确认${next === 'active' ? '启用' : '停用'} ${user.display_name}？`)) return
    try {
      const response = await fetch(`${API_BASE}/admin/users/${user.id}/status?status=${next}`, { method: 'PATCH', headers: headers() })
      if (!response.ok) { onNotice('状态更新失败'); return }
      onNotice(next === 'active' ? '用户已启用' : '用户已停用'); await load(page)
    } catch { onNotice('状态更新失败，请检查后端服务') }
  }

  return <section className="admin-page-content">
    <div className="admin-page-toolbar"><div className="admin-search"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void load(1) }} placeholder="搜索邮箱或名称" /></div><select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">全部状态</option><option value="active">正常</option><option value="disabled">已停用</option></select><button className="icon-button" title="刷新" aria-label="刷新" onClick={() => void load(page)} disabled={loading}>{loading ? <LoaderCircle size={17} className="spin" /> : <RefreshCw size={17} />}</button></div>
    {loadState === 'loading' && <div className="user-empty" role="status">正在加载用户…</div>}
    {loadState === 'error' && <div className="user-empty" role="status"><span>用户数据加载失败</span><button type="button" className="secondary-button" onClick={() => void load(page)}>重试</button></div>}
    {loadState === 'ready' && <><div className="admin-stat-strip"><div><span>用户总数</span><strong>{total}</strong></div><div><span>本页已授权</span><strong>{users.filter((item) => item.entitlement_active).length}</strong></div><div><span>本页已停用</span><strong>{users.filter((item) => item.status !== 'active').length}</strong></div></div>
    <div className="user-table" role="table">
      <div className="admin-user-row admin-user-row-head" role="row">
        <span role="columnheader">用户</span>
        <span role="columnheader">角色</span>
        <span role="columnheader">授权到期（北京时间）</span>
        <span role="columnheader">状态</span>
        <span className="admin-user-actions-label" role="columnheader">操作</span>
      </div>
      {visible.map((user) => (
        <div className="admin-user-row" key={user.id} role="row">
          <div className="user-cell" role="cell">
            <span className="user-avatar"><UserRound size={16} /></span>
            <span className="user-cell-text">
              <strong>{user.display_name}</strong>
              <small>{user.email}</small>
            </span>
          </div>
          <span className="role-label" role="cell">{user.role === 'admin' ? '管理员' : '用户'}</span>
          <span className={user.entitlement_active ? 'expiry active' : 'expiry'} role="cell">{user.entitlement_expires_at ? formatBeijingDate(user.entitlement_expires_at) : '未授权'}</span>
          <span className={`user-status ${user.status}`} role="cell">{user.status === 'active' ? '正常' : '已停用'}</span>
          <div className="user-actions" role="cell">
            <button className="text-action" onClick={() => void openDetail(user)}>详情</button>
            <button className="text-action primary" onClick={() => { setSelected(user); setDetail(null); setGrantOpen(true) }}><Plus size={14} />授权</button>
            <button className="icon-button" title={user.status === 'active' ? '停用' : '启用'} aria-label={user.status === 'active' ? '停用' : '启用'} onClick={() => void toggleStatus(user)}>{user.status === 'active' ? <ShieldOff size={15} /> : <Check size={15} />}</button>
          </div>
        </div>
      ))}
      {!visible.length && <div className="user-empty">暂无用户</div>}
    </div>
    <div className="table-pagination"><span>第 {page} / {pageCount} 页，共 {total} 位用户</span><div><button className="icon-button" title="上一页" aria-label="上一页" disabled={page <= 1 || loading} onClick={() => void load(page - 1)}><ChevronLeft size={17} /></button><button className="icon-button" title="下一页" aria-label="下一页" disabled={page >= pageCount || loading} onClick={() => void load(page + 1)}><ChevronRight size={17} /></button></div></div></>}

    {selected && detail && <div className="modal-backdrop" onMouseDown={() => { setSelected(null); setDetail(null) }}><section className="user-detail-dialog" role="dialog" aria-modal="true" aria-labelledby="user-detail-title" onMouseDown={(event) => event.stopPropagation()}><header><div><span className="eyebrow"><UserRound size={14} />用户详情</span><h2 id="user-detail-title">{detail.user.display_name}</h2><p>{detail.user.email}</p></div><button className="icon-button" aria-label="关闭" onClick={() => { setSelected(null); setDetail(null) }}><X size={18} /></button></header><div className="detail-stats"><div><span>项目</span><strong>{detail.projects}</strong></div><div><span>对话</span><strong>{detail.threads}</strong></div><div><span>模型请求</span><strong>{detail.requests}</strong></div></div><div className="detail-entitlement"><CalendarClock size={18} /><span><strong>{detail.entitlement?.active ? '权限有效' : detail.entitlement?.status === 'paused' ? '权限已暂停' : detail.entitlement ? '权限已撤销' : '当前未授权'}</strong><small>{detail.entitlement ? `到期（北京时间）：${formatBeijingDateTime(detail.entitlement.expires_at)}` : '可通过授权按钮开通使用期限'}</small></span></div><footer className="detail-actions"><button className="secondary-button" onClick={() => setSelected(null)}>关闭</button>{detail.entitlement?.status === 'active' && <button className="secondary-button" disabled={entitlementBusy} onClick={() => void updateEntitlement('pause')}><PauseCircle size={15} />暂停</button>}{detail.entitlement?.status === 'paused' && <button className="secondary-button" disabled={entitlementBusy} onClick={() => void updateEntitlement('activate')}><PlayCircle size={15} />恢复</button>}{detail.entitlement && ['active', 'paused'].includes(detail.entitlement.status) && <button className="danger-button" disabled={entitlementBusy} onClick={() => void updateEntitlement('revoke')}><Ban size={15} />撤销</button>}<button className="workspace-primary" onClick={() => setGrantOpen(true)}><Plus size={16} />开通权限</button></footer></section></div>}
    {grantOpen && selected && <div className="modal-backdrop" onMouseDown={() => setGrantOpen(false)}><form className="grant-dialog" onSubmit={grant} onMouseDown={(event) => event.stopPropagation()}><header><div><span className="eyebrow"><CalendarClock size={14} />权限管理</span><h2>开通使用权限</h2><p>{selected.display_name} · {selected.email}</p></div><button type="button" className="icon-button" aria-label="关闭" onClick={() => setGrantOpen(false)}><X size={18} /></button></header><label>授权时长<select value={months} onChange={(event) => setMonths(Number(event.target.value))}><option value={1}>1 个月</option><option value={3}>3 个月</option><option value={6}>6 个月</option><option value={12}>12 个月</option></select></label><p className="grant-hint">新的授权会替换该用户当前有效授权，立即生效。</p><footer><button type="button" className="secondary-button" onClick={() => setGrantOpen(false)}>取消</button><button className="workspace-primary" type="submit">确认开通</button></footer></form></div>}
  </section>
}

function headers(): Record<string, string> { const token = localStorage.getItem('access_token'); return token ? { Authorization: `Bearer ${token}` } : {} }
