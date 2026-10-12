const COLORS = {
  done: 'var(--ok)',
  run: 'var(--accent)',
  fail: 'var(--bad)',
}

export default function ProgressBar({ items }) {
  const total = Object.values(items).reduce((a, b) => a + b, 0) || 1
  const seg = (cls, n) => (
    <i className={cls} style={{ width: `${(100 * n) / total}%` }} />
  )
  return (
    <div className="bar">
      {seg('done', items.done || 0)}
      {seg('run', (items.running || 0) + (items.assigned || 0) + (items.pending || 0))}
      {seg('fail', (items.failed || 0) + (items.timeout || 0) + (items.cancelled || 0))}
    </div>
  )
}
