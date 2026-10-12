import { useEffect, useState } from 'react'
import api from '../api.js'
import Badge from '../components/Badge.jsx'
import ProgressBar from '../components/ProgressBar.jsx'

export default function TasksView({ toast }) {
  const [tasks, setTasks] = useState([])
  const [detail, setDetail] = useState(null)

  async function refresh() {
    try {
      setTasks(await api('/api/tasks'))
    } catch (e) {
      toast(`加载任务失败: ${e.message}`)
    }
  }

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 5000)
    return () => clearInterval(t)
  }, [])

  async function submit(ev) {
    ev.preventDefault()
    const fd = new FormData(ev.target)
    const items = String(fd.get('items') || '')
      .split('\n')
      .filter((x) => x.trim())
    const prompt = String(fd.get('prompt') || '')
    if (!fd.get('name') || !prompt || !items.length) {
      toast('name / prompt / items 都要填')
      return
    }
    if (!prompt.includes('{{item}}')) {
      toast('prompt 必须包含 {{item}}')
      return
    }
    try {
      const r = await api('/api/tasks', {
        method: 'POST',
        body: JSON.stringify({
          name: fd.get('name'),
          prompt_template: prompt,
          items,
          concurrency: parseInt(fd.get('concurrency')) || 8,
          backend: fd.get('backend') || 'local',
        }),
      })
      toast(`任务 ${r.task_id} 已提交 (${r.num_items} items)`)
      ev.target.reset()
      refresh()
    } catch (e) {
      toast(`提交失败: ${e.message}`)
    }
  }

  async function stop(id) {
    try {
      await api(`/api/tasks/${id}/stop`, { method: 'POST' })
      toast(`任务 ${id} 停止中`)
      refresh()
    } catch (e) {
      toast(e.message)
    }
  }

  async function openDetail(id) {
    setDetail(await api(`/api/tasks/${id}`))
  }

  return (
    <>
      <form className="toolbar" onSubmit={submit}>
        <input name="name" placeholder="任务名" />
        <input name="concurrency" type="number" defaultValue={8} title="沙箱+Codex 并发度" style={{ maxWidth: 110 }} />
        <input name="backend" defaultValue="local" style={{ maxWidth: 120 }} title="backend: local | e2b" />
        <button className="primary" type="submit">提交任务</button>
      </form>
      <div className="toolbar">
        <input name="prompt" placeholder="prompt 模板，必须含 {{item}}" style={{ flex: 1 }} form="noop" />
      </div>
      <div className="toolbar">
        <textarea name="items" placeholder="items：每行一条训练数据" form="noop" />
      </div>
      <table>
        <thead>
          <tr>
            <th>ID</th><th>名称</th><th>状态</th><th>进度</th><th>完成</th>
            <th>并发</th><th>backend</th><th></th>
          </tr>
        </thead>
        <tbody>
          {tasks.length === 0 && (
            <tr><td colSpan={8} className="muted">暂无任务</td></tr>
          )}
          {tasks.map((t) => (
            <tr key={t.id} onClick={() => openDetail(t.id)} className="clickable">
              <td>{t.id}</td>
              <td>{t.name}</td>
              <td><Badge value={t.status} /></td>
              <td><ProgressBar items={t.items} /></td>
              <td className="muted">{t.items.done || 0}/{t.total}</td>
              <td>{t.concurrency}</td>
              <td>{t.backend}</td>
              <td>
                <button
                  className="danger"
                  disabled={t.status !== 'running'}
                  onClick={(e) => { e.stopPropagation(); stop(t.id) }}
                >
                  stop
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {detail && (
        <div className="modal" onClick={() => setDetail(null)}>
          <div className="modal-body" onClick={(e) => e.stopPropagation()}>
            <h3>
              任务 {detail.id} · {detail.name} <Badge value={detail.status} />
            </h3>
            <pre className="prompt-box">{detail.prompt_template}</pre>
            <table>
              <thead>
                <tr><th>seq</th><th>状态</th><th>token</th><th>耗时</th><th>错误</th></tr>
              </thead>
              <tbody>
                {detail.items.map((it) => (
                  <tr key={it.id}>
                    <td>{it.seq}</td>
                    <td><Badge value={it.status} /></td>
                    <td>{it.token_id ?? '-'}</td>
                    <td>{it.duration_s ? `${it.duration_s.toFixed(1)}s` : '-'}</td>
                    <td className="muted err">{it.error || ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <button onClick={() => setDetail(null)}>关闭</button>
          </div>
        </div>
      )}
    </>
  )
}
