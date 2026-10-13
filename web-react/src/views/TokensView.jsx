import { useEffect, useRef, useState } from 'react'
import api from '../api.js'
import Badge from '../components/Badge.jsx'

export default function TokensView({ toast }) {
  const [tokens, setTokens] = useState([])
  const fileRef = useRef(null)

  async function refresh() {
    try {
      setTokens(await api('/api/tokens'))
    } catch (e) {
      toast(`加载 token 失败: ${e.message}`)
    }
  }

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 5000)
    return () => clearInterval(t)
  }, [])

  async function bulkAdd(ev) {
    ev.preventDefault()
    const fd = new FormData(ev.target)
    const blob = String(fd.get('blob') || '').trim()
    if (!blob) {
      toast('先粘贴 tokens')
      return
    }
    try {
      const r = await api('/api/tokens/bulk', {
        method: 'POST',
        body: JSON.stringify({
          blob,
          label_prefix: fd.get('prefix') || 'bulk',
        }),
      })
      toast(`已导入 ${r.created} 个 token`)
      ev.target.reset()
      refresh()
    } catch (e) {
      toast(`导入失败: ${e.message}`)
    }
  }

  async function uploadXlsx(ev) {
    ev.preventDefault()
    const f = fileRef.current?.files?.[0]
    if (!f) {
      toast('先选择 xlsx 文件')
      return
    }
    const b64 = await new Promise((res, rej) => {
      const r = new FileReader()
      r.onload = () => res(String(r.result).split(',')[1])
      r.onerror = rej
      r.readAsDataURL(f)
    })
    try {
      const r = await api('/api/tokens/import-xlsx', {
        method: 'POST',
        body: JSON.stringify({ blob: b64 }),
      })
      const skipped = r.skipped?.length ? `，跳过 ${r.skipped.length}` : ''
      toast(`已导入 ${r.added.length} 个账户${skipped}`)
      if (fileRef.current) fileRef.current.value = ''
      refresh()
    } catch (e) {
      toast(`导入失败: ${e.message}`)
    }
  }

  async function toggle(id, status) {
    try {
      await api(`/api/tokens/${id}/status`, {
        method: 'POST',
        body: JSON.stringify({ status }),
      })
      refresh()
    } catch (e) {
      toast(e.message)
    }
  }

  return (
    <>
      <form className="toolbar" onSubmit={uploadXlsx}>
        <input ref={fileRef} type="file" accept=".xlsx" style={{ maxWidth: 320 }} />
        <button className="primary" type="submit">导入 xlsx 账户</button>
        <span className="muted">自动解析每行的 refresh token / token bundle</span>
      </form>
      <form className="toolbar" onSubmit={bulkAdd}>
        <input name="prefix" defaultValue="bulk" placeholder="label 前缀" style={{ maxWidth: 140 }} />
        <button className="primary" type="submit">批量导入</button>
        <span className="muted">或粘贴裸 token，每行一个</span>
      </form>
      <div className="toolbar">
        <textarea name="blob" placeholder={'token1\ntoken2\ntoken3 ...'} />
      </div>
      <table>
        <thead>
          <tr>
            <th>ID</th><th>标签</th><th>指纹</th><th>状态</th><th>并发</th>
            <th>失败次数</th><th>失效原因</th><th></th>
          </tr>
        </thead>
        <tbody>
          {tokens.length === 0 && (
            <tr><td colSpan={8} className="muted">token 池为空</td></tr>
          )}
          {tokens.map((t) => (
            <tr key={t.id}>
              <td>{t.id}</td>
              <td>{t.label}</td>
              <td className="fp">{t.fingerprint}</td>
              <td><Badge value={t.status} /></td>
              <td>{t.in_use}/{t.max_concurrency}</td>
              <td>{t.fail_count}</td>
              <td className="muted err">{t.banned_reason || ''}</td>
              <td>
                <button
                  className="danger"
                  onClick={() => toggle(t.id, t.status === 'disabled' ? 'active' : 'disabled')}
                >
                  {t.status === 'disabled' ? '启用' : '禁用'}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
