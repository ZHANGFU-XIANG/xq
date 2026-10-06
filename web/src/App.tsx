import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import ChatPanel, { ChatProvider } from './components/Chat'
import Layout from './components/Layout'
import { Loading, PageTitle } from './components/ui'
import { get } from './lib/api'
import Alerts from './pages/Alerts'
import Daily from './pages/Daily'
import Login from './pages/Login'
import Model from './pages/Model'
import News from './pages/News'
import Overview from './pages/Overview'
import Portfolio from './pages/Portfolio'
import Stock from './pages/Stock'
import Trades from './pages/Trades'
import Futures from './pages/Futures'
import WeeklyReport from './components/WeeklyReport'

function ChatPage() {
  return (
    <>
      <div className="flex h-[calc(100vh-7rem)] flex-col">
        <PageTitle>AI 助理</PageTitle>
        <div className="min-h-0 flex-1"><ChatPanel /></div>
      </div>
      <WeeklyReport />
    </>
  )
}

export default function App() {
  const qc = useQueryClient()
  const { data: status, isLoading, refetch } = useQuery({ queryKey: ['status'], queryFn: () => get('/api/status'), refetchInterval: 5 * 60_000 })
  // 部署新版後自動換新：伺服器的前端版本與目前載入的不同就重新載入（手機加到主畫面的 App 也適用）
  useEffect(() => {
    if (!import.meta.env.PROD || !status?.build) return
    const mine = new URL(import.meta.url).pathname
    if (status.build === mine) return
    try {
      const key = 'reloaded_for'
      if (sessionStorage.getItem(key) === status.build) return  // 已為這個版本重新載入過，避免無限重載
      sessionStorage.setItem(key, status.build)
    } catch { /* 無法儲存時仍重新載入一次 */ }
    window.location.reload()
  }, [status?.build])
  useEffect(() => {
    const h = () => refetch()
    window.addEventListener('auth-required', h)
    return () => window.removeEventListener('auth-required', h)
  }, [refetch])
  if (isLoading) return <Loading label="連線中" />
  if (status?.auth_required && !status.authenticated)
    return <Login onDone={() => qc.invalidateQueries()} />
  return (
    <ChatProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<Overview />} />
            <Route path="daily" element={<Daily />} />
            <Route path="daily/:key" element={<Daily />} />
            <Route path="stock" element={<Stock />} />
            <Route path="stock/:code" element={<Stock />} />
            <Route path="futures" element={<Futures />} />
            <Route path="futures/:root" element={<Futures />} />
            <Route path="alerts" element={<Alerts />} />
            <Route path="news" element={<News />} />
            <Route path="portfolio" element={<Portfolio />} />
            <Route path="trades" element={<Trades />} />
            <Route path="model" element={<Model />} />
            <Route path="chat" element={<ChatPage />} />
            <Route path="*" element={<p className="text-muted">找不到這個頁面。</p>} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ChatProvider>
  )
}
