import { FormEvent, useEffect, useState } from 'react'
import { BarChart3, LogIn, LogOut, MessageSquare, Plus, Server, ShieldCheck, Users } from 'lucide-react'
import { ChatWorkspace } from './App'
import AdminModelChannelsPage from './components/AdminModelChannelsPage'
import AdminUsersPage from './components/AdminUsersPage'
import AdminUsagePage from './components/AdminUsagePage'
import LandingPage from './components/LandingPage'
import { API_BASE } from './utils/api'

type User = { id: string; email: string; display_name: string; role: 'user' | 'admin'; status: string }
const ADMIN_PAGES = {
  '/admin/model-channels': { page: 'channels', title: '模型渠道' },
  '/admin/users': { page: 'users', title: '用户管理' },
  '/admin/usage': { page: 'usage', title: '使用记录' },
} as const
type AdminPath = keyof typeof ADMIN_PAGES

function navigate(path: string) {
  window.history.pushState({}, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

function readUser(): User | null {
  try { return JSON.parse(localStorage.getItem('auth_user') ?? 'null') as User | null } catch { return null }
}

function isAdminPath(path: string): path is AdminPath {
  return Object.prototype.hasOwnProperty.call(ADMIN_PAGES, path)
}

function Redirect({ to }: { to: string }) {
  useEffect(() => {
    // Defer until Root's popstate listener is mounted, otherwise a redirect
    // rendered on the first pass can update the URL without updating the view.
    const timer = window.setTimeout(() => navigate(to), 0)
    return () => window.clearTimeout(timer)
  }, [to])
  return null
}

export default function Root() {
  const [path, setPath] = useState(window.location.pathname)
  const [user, setUser] = useState<User | null>(readUser)
  useEffect(() => { const sync = () => { setPath(window.location.pathname); setUser(readUser()) }; window.addEventListener('popstate', sync); return () => window.removeEventListener('popstate', sync) }, [])
  useEffect(() => {
    const accessToken = localStorage.getItem('access_token')
    const refreshToken = localStorage.getItem('refresh_token')
    if (!accessToken) return
    let cancelled = false
    const restore = async () => {
      try {
        let response = await fetch(`${API_BASE}/auth/me`, { headers: { Authorization: `Bearer ${accessToken}` } })
        let data: { id: string; email: string; display_name: string; role: 'user' | 'admin'; status: string; access_token?: string; refresh_token?: string }
        if (response.ok) {
          data = await response.json() as typeof data
        } else if (refreshToken) {
          response = await fetch(`${API_BASE}/auth/refresh`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ refresh_token: refreshToken }) })
          if (!response.ok) throw new Error('session expired')
          const refreshed = await response.json() as { user: User; access_token: string; refresh_token: string }
          data = { ...refreshed.user, access_token: refreshed.access_token, refresh_token: refreshed.refresh_token }
        } else throw new Error('session expired')
        if (cancelled) return
        localStorage.setItem('auth_user', JSON.stringify({ id: data.id, email: data.email, display_name: data.display_name, role: data.role, status: data.status }))
        if (data.access_token) localStorage.setItem('access_token', data.access_token)
        if (data.refresh_token) localStorage.setItem('refresh_token', data.refresh_token)
        setUser({ id: data.id, email: data.email, display_name: data.display_name, role: data.role, status: data.status })
      } catch {
        if (cancelled) return
        localStorage.removeItem('access_token'); localStorage.removeItem('refresh_token'); localStorage.removeItem('auth_user'); setUser(null)
        if (window.location.pathname !== '/') navigate('/')
      }
    }
    void restore()
    return () => { cancelled = true }
  }, [])

  const onAuth = (nextUser: User, token: string, refreshToken?: string) => { localStorage.setItem('access_token', token); if (refreshToken) localStorage.setItem('refresh_token', refreshToken); localStorage.setItem('auth_user', JSON.stringify(nextUser)); setUser(nextUser); navigate(nextUser.role === 'admin' ? '/admin/model-channels' : '/chat') }
  const logout = () => { const refreshToken = localStorage.getItem('refresh_token'); if (refreshToken) void fetch(`${API_BASE}/auth/logout`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ refresh_token: refreshToken }) }).catch(() => undefined); localStorage.removeItem('access_token'); localStorage.removeItem('refresh_token'); localStorage.removeItem('auth_user'); setUser(null); navigate('/') }

  if (path === '/login') return <AuthPage mode="login" onAuth={onAuth} onNavigate={navigate} />
  if (path === '/register') return <AuthPage mode="register" onAuth={onAuth} onNavigate={navigate} />
  if (path === '/admin' || path.startsWith('/admin/')) {
    if (!isAdminPath(path)) return <Redirect to="/admin/model-channels" />
    if (!user) return <AuthPage mode="login" onAuth={onAuth} onNavigate={navigate} notice="请先登录管理员账户" />
    if (user.role !== 'admin') return <Redirect to="/chat" />
    return <AdminShell path={path} user={user} onLogout={logout} onNavigate={navigate} />
  }
  if (path === '/chat') {
    if (!user) return <Redirect to="/login" />
    return <ChatWorkspace />
  }
  return <LandingPage user={user} onNavigate={navigate} onLogout={logout} />
}

function authFormError(mode: 'login' | 'register', status: number, detail?: string, message?: string) {
  const raw = message ?? detail ?? ''
  if (mode === 'register' && (status === 409 || raw === 'email already registered' || raw === '该邮箱已被注册')) return '该邮箱已被注册'
  return raw || '请求失败'
}

function AuthPage({ mode, onAuth, onNavigate, notice }: { mode: 'login' | 'register'; onAuth: (user: User, token: string, refreshToken?: string) => void; onNavigate: (path: string) => void; notice?: string }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState(notice ?? '')
  const [loading, setLoading] = useState(false)
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setLoading(true); setError('')
    try {
      const response = await fetch(`${API_BASE}/auth/${mode}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(mode === 'login' ? { email, password } : { email, password, display_name: name }) })
      const data = await response.json() as { detail?: string; message?: string; access_token?: string; refresh_token?: string; user?: User }
      if (!response.ok || !data.access_token || !data.user) throw new Error(authFormError(mode, response.status, data.detail, data.message))
      onAuth(data.user, data.access_token, data.refresh_token)
    } catch (caught) { setError(caught instanceof Error ? caught.message : '请求失败') } finally { setLoading(false) }
  }
  return <div className="auth-shell"><button className="auth-brand" onClick={() => onNavigate('/')}>ChatGPT</button><form className="auth-card" onSubmit={submit}><span className="eyebrow"><ShieldCheck size={15} />账户中心</span><h1>{mode === 'login' ? '登录你的账户' : '创建账户'}</h1><p>{mode === 'login' ? '登录后进入你的 AI 工作空间。' : '注册后开始管理项目和对话。'}</p>{mode === 'register' && <label>显示名称<input required value={name} onChange={(event) => setName(event.target.value)} placeholder="你的名字" /></label>}<label>邮箱<input type="email" required value={email} onChange={(event) => setEmail(event.target.value)} placeholder="you@example.com" /></label><label>密码<input type="password" required minLength={8} value={password} onChange={(event) => setPassword(event.target.value)} placeholder="至少 8 位" /></label>{error && <div className="auth-error">{error}</div>}<button className="landing-cta auth-submit" disabled={loading}>{loading ? '处理中…' : mode === 'login' ? <><LogIn size={17} />登录</> : <><Plus size={17} />注册</>}</button><div className="auth-switch">{mode === 'login' ? <>还没有账户？<button type="button" onClick={() => onNavigate('/register')}>注册</button></> : <>已有账户？<button type="button" onClick={() => onNavigate('/login')}>登录</button></>}</div></form></div>
}

function AdminShell({ path, user, onLogout, onNavigate }: { path: AdminPath; user: User; onLogout: () => void; onNavigate: (path: string) => void }) {
  const [notice, setNotice] = useState('')
  const { page, title } = ADMIN_PAGES[path]
  useEffect(() => { if (!notice) return; const timer = window.setTimeout(() => setNotice(''), 2200); return () => window.clearTimeout(timer) }, [notice])
  return <div className="admin-shell"><aside className="admin-sidebar"><button className="admin-brand" onClick={() => onNavigate('/chat')}><span>ChatGPT</span><small>管理控制台</small></button><div className="admin-nav-label">工作台</div><button className={page === 'channels' ? 'active' : ''} onClick={() => onNavigate('/admin/model-channels')}><Server size={18} />模型渠道</button><button className={page === 'users' ? 'active' : ''} onClick={() => onNavigate('/admin/users')}><Users size={18} />用户管理</button><button className={page === 'usage' ? 'active' : ''} onClick={() => onNavigate('/admin/usage')}><BarChart3 size={18} />使用记录</button><div className="admin-sidebar-bottom"><button onClick={() => onNavigate('/chat')}><MessageSquare size={17} />返回用户端</button><button onClick={onLogout}><LogOut size={17} />退出登录</button></div></aside><main className="admin-main"><header className="admin-topbar"><div><span className="eyebrow"><ShieldCheck size={14} />管理员空间</span><h1>{title}</h1></div><div className="admin-user"><span>{user.display_name}</span></div></header>{page === 'channels' && <AdminModelChannelsPage onBack={() => onNavigate('/chat')} onNotice={setNotice} />}{page === 'users' && <AdminUsersPage onNotice={setNotice} />}{page === 'usage' && <AdminUsagePage onNotice={setNotice} />}{notice && <div className="toast" role="status">{notice}</div>}</main></div>
}
