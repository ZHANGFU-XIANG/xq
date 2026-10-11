import { useState } from 'react'
import { post, setToken } from '../lib/api'

export default function Login({ onDone }: { onDone: () => void }) {
  const [pw, setPw] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  return (
    <div className="flex min-h-screen items-center justify-center bg-nav px-4">
      <form
        className="w-full max-w-sm rounded-xl bg-paper p-6"
        onSubmit={async (e) => {
          e.preventDefault()
          setBusy(true)
          setErr('')
          try {
            const r = await post('/api/login', { password: pw })
            setToken(r.token ?? null)
            onDone()
          } catch (x) {
            setErr(x instanceof Error ? x.message : '登入失敗')
          } finally {
            setBusy(false)
          }
        }}
      >
        <h1 className="title mb-1 text-2xl">投資擂台研究台</h1>
        <p className="mb-5 text-sm text-muted">輸入隊伍共用密碼</p>
        <label htmlFor="pw" className="mb-1 block text-sm">密碼</label>
        <input id="pw" type="password" autoFocus value={pw} onChange={(e) => setPw(e.target.value)}
          className="mb-3 w-full rounded-md border border-line bg-paper px-3 py-2 text-ink outline-none focus:border-link" />
        {err && <p className="mb-3 text-sm text-up">{err}</p>}
        <button disabled={busy || !pw} className="w-full rounded-md bg-link py-2 font-medium text-white disabled:opacity-50">登入</button>
      </form>
    </div>
  )
}
