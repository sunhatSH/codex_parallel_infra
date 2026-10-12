import { useCallback, useEffect, useRef, useState } from 'react'
import api from './api.js'
import TasksView from './views/TasksView.jsx'
import TokensView from './views/TokensView.jsx'
import './App.css'

function App() {
  const [view, setView] = useState('tasks')
  const [hubId, setHubId] = useState('')
  const [stats, setStats] = useState(null)
  const [down, setDown] = useState(false)
  const [toastMsg, setToastMsg] = useState('')
  const toastTimer = useRef(null)

  const toast = useCallback((msg) => {
    setToastMsg(msg)
    clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToastMsg(''), 2500)
  }, [])

  async function refreshHeader() {
    try {
      const h = await api('/api/health')
      setHubId(h.hub_id || '')
      setDown(false)
      const [tokens, tasks] = await Promise.all([
        api('/api/tokens/stats'),
        api('/api/tasks'),
      ])
      setStats({ tokens, tasks })
    } catch {
      setDown(true)
    }
  }

  useEffect(() => {
    refreshHeader()
    const t = setInterval(refreshHeader, 5000)
    return () => clearInterval(t)
  }, [])

  const s = stats
  const runningTasks = s ? s.tasks.filter((t) => t.status === 'running').length : 0
  const doneItems = s ? s.tasks.reduce((a, t) => a + (t.items.done || 0), 0) : 0
  const totalItems = s ? s.tasks.reduce((a, t) => a + t.total, 0) : 0

  return (
    <>
      <header>
        <h1>Codex Parallel Hub</h1>
        <span className="hub-id">{hubId}</span>
        <nav>
          <button className={view === 'tasks' ? 'active' : ''} onClick={() => setView('tasks')}>任务</button>
          <button className={view === 'tokens' ? 'active' : ''} onClick={() => setView('tokens')}>Token 池</button>
        </nav>
      </header>

      <main>
        {down ? (
          <pre className="down-box">
            {`Hub 不可达\n\n先启动后端服务:\n  codex-parallel-infra: python -m codex_parallel.cli serve`}
          </pre>
        ) : (
          <>
            {s && (
              <div className="stats">
                <div className="stat"><div className="num">{runningTasks}/{s.tasks.length}</div><div className="lbl">运行中任务</div></div>
                <div className="stat"><div className="num">{doneItems}/{totalItems}</div><div className="lbl">已完成 items</div></div>
                <div className="stat"><div className="num">{s.tokens.active}</div><div className="lbl">可用 token</div></div>
                <div className="stat"><div className="num">{s.tokens.in_use}</div><div className="lbl">使用中</div></div>
                <div className="stat"><div className="num bad">{s.tokens.banned + s.tokens.discarded}</div><div className="lbl">失效/丢弃</div></div>
              </div>
            )}
            {view === 'tasks'
              ? <TasksView toast={toast} />
              : <TokensView toast={toast} />}
          </>
        )}
      </main>

      {toastMsg && <div id="toast">{toastMsg}</div>}
    </>
  )
}

export default App
