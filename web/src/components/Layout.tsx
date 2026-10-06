import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { get, post, setToken } from '../lib/api'
import { HealthDot } from './Health'
import ChatPanel, { useChat } from './Chat'
import { liveEvery } from '../lib/live'

const NAV = [
  { to: '/', label: '總覽', end: true },
  { to: '/daily', label: '每日建議' },
  { to: '/alerts', label: '警示提醒' },
  { to: '/stock', label: '個股' },
  { to: '/futures', label: '期貨' },
  { to: '/news', label: '新聞' },
  { to: '/portfolio', label: '持股' },
  { to: '/trades', label: '交易紀錄' },
  { to: '/model', label: '模型' },
  { to: '/chat', label: 'AI 助理' },
]

function StockSearch({ onDone }: { onDone?: () => void }) {
  const [q, setQ] = useState('')
  const [open, setOpen] = useState(false)
  const nav = useNavigate()
  const { data } = useQuery<{ code: string; name: string; sector: string; kind?: string }[]>({
    queryKey: ['search', q],
    queryFn: () => get(`/api/stock/search?q=${encodeURIComponent(q)}`),
    enabled: q.trim().length > 0,
  })
  const go = (code: string, kind?: string) => {
    nav(kind === 'futures' ? `/futures/${code}` : `/stock/${code}`)
    setQ('')
    setOpen(false)
    onDone?.()
  }
  return (
    <div className="relative">
      <label htmlFor="stock-search" className="sr-only">搜尋股票</label>
      <input
        id="stock-search"
        value={q}
        onChange={(e) => {
          setQ(e.target.value)
          setOpen(true)
        }}
        onKeyDown={(e) => e.key === 'Enter' && data?.[0] && go(data[0].code, data[0].kind)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        placeholder="搜尋股票、ETF、期貨"
        className="w-full rounded-md border border-white/20 bg-white/10 px-3 py-1.5 text-sm text-white placeholder:text-white/60 outline-none focus:border-white/60"
      />
      {open && !!data?.length && (
        <ul className="absolute z-30 mt-1 w-full overflow-hidden rounded-md border border-line bg-paper text-ink shadow-lg">
          {data.map((s) => (
            <li key={s.code}>
              <button onMouseDown={() => go(s.code, s.kind)} className="flex w-full justify-between gap-2 px-3 py-1.5 text-left text-sm hover:bg-mist">
                <span><span className="num text-muted">{s.code}</span> {s.name}</span>
                <span className="text-xs text-muted">{s.sector}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function ThemeToggle() {
  const [theme, setTheme] = useState<string>(() => {
    try {
      return localStorage.getItem('theme') || ''
    } catch {
      return ''
    }
  })
  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme
    else delete document.documentElement.dataset.theme
    try {
      localStorage.setItem('theme', theme)
    } catch { /* 無法儲存時忽略 */ }
  }, [theme])
  const next = theme === '' ? 'dark' : theme === 'dark' ? 'light' : ''
  return (
    <button onClick={() => setTheme(next)} className="text-xs text-white/70 hover:text-white">
      外觀：{theme === 'dark' ? '深色' : theme === 'light' ? '淺色' : '跟隨系統'}
    </button>
  )
}

export default function Layout() {
  const chat = useChat()
  const { data: status } = useQuery({ queryKey: ['status'], queryFn: () => get('/api/status'), refetchInterval: 60_000 })
  const { data: alerts } = useQuery({ queryKey: ['alerts'], queryFn: () => get('/api/alerts'), refetchInterval: liveEvery(60_000, 300_000), enabled: !!status?.warm })
  const urgent = alerts ? (alerts.counts?.['嚴重'] ?? 0) + (alerts.counts?.['警告'] ?? 0) : 0
  const [menu, setMenu] = useState(false)
  const onChatPage = useLocation().pathname === '/chat'
  const drawer = chat.open && !onChatPage
  return (
    <div className="min-h-screen md:flex">
      <aside className="sticky top-0 z-20 bg-nav text-white md:fixed md:inset-y-0 md:left-0 md:w-56 md:flex md:flex-col">
        <div className="flex items-center justify-between px-4 py-3 md:block md:py-5">
          <NavLink to="/" className="title block text-lg text-white no-underline">投資擂台研究台</NavLink>
          <div className="hidden text-xs leading-relaxed text-white/60 md:mt-1 md:block">2026 德明X富達模擬投資競賽<br />分析目標日 {status?.target_date ?? '—'}</div>
          <div className="flex items-center gap-2 md:hidden">
            {urgent > 0 && <NavLink to="/alerts" className="rounded-full bg-up px-2.5 py-0.5 text-xs font-bold text-white no-underline">{urgent} 則警示</NavLink>}
            <button className="rounded px-2 py-1 text-sm" onClick={() => setMenu(!menu)} aria-expanded={menu}>選單</button>
          </div>
        </div>
        <div className={`${menu ? 'block' : 'hidden'} px-3 pb-3 md:block md:flex-1`}>
          <StockSearch onDone={() => setMenu(false)} />
          <nav className="mt-3 flex flex-col gap-0.5" aria-label="主選單">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to} end={n.end} onClick={() => setMenu(false)}
                className={({ isActive }) => `rounded-md px-3 py-2 text-[0.95rem] no-underline ${isActive ? 'bg-white/15 font-medium text-white' : 'text-white/75 hover:bg-white/10 hover:text-white'}`}>
                {n.label}
                {n.to === '/alerts' && urgent > 0 && <span className="ml-2 rounded-full bg-up px-1.5 text-xs font-bold text-white">{urgent}</span>}
              </NavLink>
            ))}
          </nav>
        </div>
        <div className="hidden space-y-2 px-4 py-4 text-xs md:block">
          {status && !status.warm && <div className="text-amber-200">正在載入全市場資料，個股分析約需 1 分鐘</div>}
          {status?.sync?.error && <div className="text-red-200">同步失敗：{status.sync.error}</div>}
          <HealthDot />
          <ThemeToggle />
          {status?.auth_required && (
            <button className="block text-white/70 hover:text-white" onClick={() => post('/api/logout').finally(() => { setToken(null); location.reload() })}>登出</button>
          )}
        </div>
      </aside>

      <main className={`min-w-0 flex-1 px-4 py-6 md:ml-56 md:px-8 ${drawer ? 'lg:mr-[26rem]' : ''}`}>
        <div className="mx-auto max-w-6xl">
          <Outlet />
        </div>
      </main>

      {!chat.open && !onChatPage && (
        <button onClick={() => chat.setOpen(true)}
          className="fixed bottom-5 right-5 z-30 rounded-full bg-ink px-5 py-3 text-paper shadow-lg hover:bg-ink-2">
          問 AI 助理
        </button>
      )}
      {drawer && (
        <aside className="fixed inset-0 z-40 flex flex-col bg-mist lg:inset-y-0 lg:left-auto lg:right-0 lg:w-[26rem] lg:border-l lg:border-line" aria-label="AI 助理">
          <div className="flex items-center justify-between border-b border-line bg-paper px-4 py-3">
            <div className="font-bold">AI 助理</div>
            <button onClick={() => chat.setOpen(false)} className="text-sm text-muted hover:text-ink">收起</button>
          </div>
          <div className="min-h-0 flex-1"><ChatPanel compact /></div>
        </aside>
      )}
    </div>
  )
}
