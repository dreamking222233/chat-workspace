import {
  ArrowRight,
  FolderKanban,
  Image,
  Layers,
  MessageSquare,
  Server,
  ShieldCheck,
  Sparkles,
  Workflow,
} from 'lucide-react'

type User = { id: string; email: string; display_name: string; role: 'user' | 'admin'; status: string }

const features = [
  { icon: MessageSquare, title: '自然对话', desc: '流式回复、上下文记忆与完整历史，让每一次讨论都能接上一次继续。', tone: 'blue' },
  { icon: FolderKanban, title: '项目管理', desc: '按项目归档对话，把调研、设计和交付材料沉淀在同一工作空间。', tone: 'violet' },
  { icon: Server, title: '多模型渠道', desc: '统一接入 OpenAI 兼容协议，按优先级调度文本和图片模型。', tone: 'green' },
  { icon: Image, title: '图片生成', desc: '在对话中切换文生图模式，生成结果直接保存在当前工作区。', tone: 'amber' },
  { icon: ShieldCheck, title: '授权管控', desc: '管理员按月开通权限，到期、暂停和撤销都有清晰状态。', tone: 'teal' },
  { icon: Layers, title: '使用可追溯', desc: '模型、模态、Token 和延迟进入用量记录，便于审计和成本观察。', tone: 'slate' },
]

const steps = [
  { step: '01', title: '进入工作区', desc: '注册或登录后，按账户授权进入专属对话空间。' },
  { step: '02', title: '组织项目', desc: '创建项目、新建对话，把主题和工作材料分开存放。' },
  { step: '03', title: '连接模型', desc: '选择文本或图片渠道，持续生成并沉淀可用内容。' },
]

export default function LandingPage({ user, onNavigate, onLogout }: { user: User | null; onNavigate: (path: string) => void; onLogout: () => void }) {
  const startPath = user ? '/chat' : '/register'
  const scrollTo = (id: string) => {
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    document.getElementById(id)?.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' })
  }

  return (
    <div className="landing-shell">
      <header className="landing-nav">
        <button className="landing-brand" type="button" onClick={() => onNavigate('/')}>ChatGPT</button>
        <nav className="landing-nav-links" aria-label="页面导航">
          <button type="button" onClick={() => scrollTo('features')}>能力</button>
          <button type="button" onClick={() => scrollTo('workflow')}>使用流程</button>
        </nav>
        <div className="landing-nav-actions">
          {user ? (
            <>
              <span className="landing-user">{user.display_name}</span>
              {user.role === 'admin' && <button className="landing-link" type="button" onClick={() => onNavigate('/admin/users')}>管理台</button>}
              <button className="landing-link" type="button" onClick={() => onNavigate('/chat')}>工作区</button>
              <button className="landing-link" type="button" onClick={onLogout}>退出</button>
            </>
          ) : (
            <>
              <button className="landing-link" type="button" onClick={() => onNavigate('/login')}>登录</button>
              <button className="landing-cta small" type="button" onClick={() => onNavigate('/register')}>注册</button>
            </>
          )}
        </div>
      </header>

      <section className="landing-hero">
        <div className="landing-hero-glow" aria-hidden="true" />
        <div className="landing-hero-grid" aria-hidden="true" />
        <div className="landing-orb landing-orb-left" aria-hidden="true" />
        <div className="landing-orb landing-orb-right" aria-hidden="true" />

        <div className="landing-hero-copy">
          <span className="landing-badge"><ShieldCheck size={15} />智能工作空间</span>
          <h1>你的专属 AI 对话工作区</h1>
          <p>在项目中组织对话，连接文本和图片模型，把讨论、草稿和生成结果持续沉淀为可复用的工作内容。</p>
          <div className="landing-hero-actions">
            <button className="landing-cta" type="button" onClick={() => onNavigate(startPath)}>
              {user ? '进入工作区' : '开始使用'} <ArrowRight size={18} />
            </button>
            <button className="landing-ghost" type="button" onClick={() => scrollTo('features')}>了解功能</button>
          </div>
          <ul className="landing-chips">
            <li><Sparkles size={14} />流式回复</li>
            <li><FolderKanban size={14} />项目归档</li>
            <li><Image size={14} />文本 / 图片</li>
            <li><Server size={14} />OpenAI 协议</li>
          </ul>
        </div>
      </section>

      <section className="landing-stats" aria-label="产品亮点">
        <div>
          <strong>双模态</strong>
          <span>文本对话与图片生成共用同一工作区</span>
        </div>
        <div>
          <strong>项目制</strong>
          <span>按主题归档，对话不再散落在历史列表</span>
        </div>
        <div>
          <strong>可管理</strong>
          <span>渠道、授权和用量由管理员统一配置</span>
        </div>
      </section>

      <section className="landing-section" id="features">
        <div className="landing-section-head">
          <span className="landing-kicker">核心能力</span>
          <h2>从对话到交付，都在一个空间完成</h2>
          <p>不是一个孤立的聊天框，而是围绕项目、模型和权限组织起来的工作台。</p>
        </div>
        <div className="landing-feature-grid">
          {features.map((feature) => {
            const Icon = feature.icon
            return (
              <article className={`landing-feature-card tone-${feature.tone}`} key={feature.title}>
                <span className="landing-feature-icon"><Icon size={18} /></span>
                <h3>{feature.title}</h3>
                <p>{feature.desc}</p>
              </article>
            )
          })}
        </div>
      </section>

      <section className="landing-section landing-workflow" id="workflow">
        <div className="landing-section-head">
          <span className="landing-kicker">使用流程</span>
          <h2>三步进入持续产出的节奏</h2>
          <p>授权、组织、生成。把 AI 对话嵌进日常项目，而不是每次从空白开始。</p>
        </div>
        <ol className="landing-steps">
          {steps.map((item) => (
            <li key={item.step}>
              <em>{item.step}</em>
              <div>
                <strong>{item.title}</strong>
                <span>{item.desc}</span>
              </div>
            </li>
          ))}
        </ol>
      </section>

      <section className="landing-section landing-workspace">
        <div className="landing-section-head">
          <span className="landing-kicker">工作区体验</span>
          <h2>文本推理与图像生成，按任务切换</h2>
        </div>
        <div className="landing-split">
          <article>
            <span className="landing-feature-icon tone-blue"><MessageSquare size={18} /></span>
            <h3>文本对话</h3>
            <p>支持流式回复、重新生成、归档与导出。指定模型没有启用渠道时会明确提示，不会悄悄换到其他模型。</p>
            <ul>
              <li><Workflow size={14} />按项目创建会话</li>
              <li><Sparkles size={14} />上下文连续沉淀</li>
              <li><Layers size={14} />Markdown / JSON 导出</li>
            </ul>
          </article>
          <article>
            <span className="landing-feature-icon tone-amber"><Image size={18} /></span>
            <h3>图片生成</h3>
            <p>在输入框切换图片模式，选择已启用的图像模型。生成资源按用户隔离保存，可在对话中继续迭代。</p>
            <ul>
              <li><Image size={14} />文生图直接落在会话里</li>
              <li><Server size={14} />兼容 OpenAI 图像接口</li>
              <li><ShieldCheck size={14} />有效授权后才发起生成</li>
            </ul>
          </article>
        </div>
      </section>

      <section className="landing-banner">
        <div>
          <h2>把对话变成可复用的工作资产</h2>
          <p>从今天开始组织项目、连接模型，让每次提问都留下可继续使用的结果。</p>
        </div>
        <button className="landing-cta" type="button" onClick={() => onNavigate(startPath)}>
          {user ? '进入工作区' : '免费注册'} <ArrowRight size={18} />
        </button>
      </section>

      <footer className="landing-footer">
        <strong>ChatGPT</strong>
        <span>项目化 AI 对话工作区</span>
        <div>
          <button type="button" onClick={() => onNavigate(user ? '/chat' : '/login')}>{user ? '工作区' : '登录'}</button>
          {!user && <button type="button" onClick={() => onNavigate('/register')}>注册</button>}
          {user?.role === 'admin' && <button type="button" onClick={() => onNavigate('/admin/users')}>管理控制台</button>}
        </div>
      </footer>
    </div>
  )
}
