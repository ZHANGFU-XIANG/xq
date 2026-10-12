import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { get, post, setToken } from '../lib/api'
import { HealthDot } from './Health'
import ChatPanel, { useChat } from './Chat'
import { liveEvery } from '../lib/live'
import { MarketBar } from './LiveBits'

// 側欄圖示（線條 SVG 路徑，24×24）
const ICON: Record<string, string> = {
  '/': 'M3 12l9-8 9 8M5 10v10h5v-6h4v6h5V10',
  '/daily': 'M7 3v3M17 3v3M4 8h16M5 5h14a1 1 0 011 1v13a1 1 0 01-1 1H5a1 1 0 01-1-1V6a1 1 0 011-1z',
  '/alerts': 'M12 3a6 6 0 00-6 6v4l-2 3h16l-2-3V9a6 6 0 00-6-6zM10 19a2 2 0 004 0',
  '/stock': 'M4 19V5M4 19h16M8 15l3-4 3 2 5-6',
  '/futures': 'M4 17l5-5 4 4 7-8M15 8h5v5',
  '/daytrade': 'M13 3L5 14h6l-1 7 8-11h-6l1-7z',
  '/news': 'M5 4h11v16H5a1 1 0 01-1-1V5a1 1 0 011-1zM16 8h3a1 1 0 011 1v9a2 2 0 01-4 0M8 8h5M8 12h5M8 16h3',
  '/portfolio': 'M4 7h16v12H4zM9 7V5h6v2M4 12h16',
  '/model': 'M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3zM12 12l8-4.5M12 12v9M12 12L4 7.5',
  '/chat': 'M5 5h14v10H9l-4 4V5z',
}

const NAV = [
  { to: '/', label: '高報酬推薦', end: true },
  { to: '/daily', label: '每日建議' },
  { to: '/alerts', label: '警示提醒' },
  { to: '/stock', label: '個股' },
  { to: '/daytrade', label: '當沖' },
  { to: '/futures', label: '期貨' },
  { to: '/news', label: '新聞' },
  { to: '/portfolio', label: '持股與交易' },
  { to: '/model', label: '模型' },
  { to: '/chat', label: 'AI 助理' },
]

function NavIcon({ to }: { to: string }) {
  return (
    <svg viewBox="0 0 24 24" className="h-[18px] w-[18px] shrink-0" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d={ICON[to]} />
    </svg>
  )
}

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
  const urgent = alerts && !alerts.from_report ? (alerts.counts?.['嚴重'] ?? 0) + (alerts.counts?.['警告'] ?? 0) : 0
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
                className={({ isActive }) => `flex items-center gap-2.5 rounded-md px-3 py-2 text-[0.95rem] no-underline ${isActive ? 'bg-white/15 font-medium text-white shadow-[inset_3px_0_0_#7FA2FF]' : 'text-white/75 hover:bg-white/10 hover:text-white'}`}>
                <NavIcon to={n.to} />
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

      <main className={`min-w-0 flex-1 md:ml-56 ${drawer ? 'lg:mr-[26rem]' : ''}`}>
        <MarketBar />
        <div className="mx-auto max-w-7xl px-4 py-5 md:px-6">
          <Outlet />
        </div>
      </main>

      {!chat.open && !onChatPage && (
        <button onClick={() => chat.setOpen(true)}
          className="fixed bottom-5 right-5 z-30 rounded-full bg-link px-5 py-3 text-white shadow-lg hover:opacity-90">
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
