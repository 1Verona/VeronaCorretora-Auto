import { useState, useEffect, useRef, useCallback } from 'react'

const STATUS_LABELS = {
  idle: 'Desligado',
  running: 'Buscando...',
  captcha: 'Aguardando CAPTCHA',
  completed: 'Concluído',
  stopped: 'Interrompido',
  failed: 'Erro',
}

export default function App() {
  const [connected, setConnected] = useState(null)
  const [active, setActive] = useState(false)
  const [captcha, setCaptcha] = useState(false)
  const [status, setStatus] = useState('idle')
  const [logs, setLogs] = useState([])
  const [progress, setProgress] = useState({ current: 0, total: 0 })
  const [found, setFound] = useState(0)
  const [error, setError] = useState(null)
  const pollRef = useRef(null)
  const logsRef = useRef(null)

  useEffect(() => {
    fetch('/status')
      .then((r) => r.json())
      .then((data) => {
        setConnected(data.connected)
        if (data.total_white_rows) {
          setProgress((p) => ({ ...p, total: data.total_white_rows }))
        }
      })
      .catch(() => setConnected(false))

    fetch('/job')
      .then((r) => r.json())
      .then((data) => {
        const job = data.job || data.last_job
        if (job) applyJob(job)
      })
      .catch(() => {})
  }, [])

  const applyJob = useCallback((job) => {
    setLogs(job.logs || [])
    setCaptcha(!!job.captcha_pending)

    if (job.progress?.total) {
      setProgress(job.progress)
    }

    const summary = job.summary || {}
    if (job.found != null) setFound(job.found)
    else if (summary.found != null) setFound(summary.found)

    if (job.status === 'running') {
      setActive(true)
      setStatus(job.captcha_pending ? 'captcha' : 'running')
    } else {
      setActive(false)
      setCaptcha(false)
      setStatus(job.status || 'idle')
      if (job.status === 'failed') setError(job.message)
    }
  }, [])

  useEffect(() => {
    if (logsRef.current) {
      logsRef.current.scrollTop = logsRef.current.scrollHeight
    }
  }, [logs])

  const startPolling = useCallback(() => {
    if (pollRef.current) clearInterval(pollRef.current)
    pollRef.current = setInterval(async () => {
      try {
        const res = await fetch('/job')
        const data = await res.json()
        const job = data.job || data.last_job
        if (job) applyJob(job)
        if (job && job.status !== 'running') {
          clearInterval(pollRef.current)
          pollRef.current = null
        }
      } catch {}
    }, 2000)
  }, [applyJob])

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [])

  const handleToggle = async () => {
    setError(null)

    if (active) {
      try {
        await fetch('/stop', { method: 'POST' })
        setActive(false)
        setCaptcha(false)
        setStatus('stopped')
        if (pollRef.current) {
          clearInterval(pollRef.current)
          pollRef.current = null
        }
      } catch {
        setError('Falha ao parar')
      }
    } else {
      try {
        const res = await fetch('/run', { method: 'POST' })
        const data = await res.json()
        if (data.error) {
          setError(data.error)
          return
        }
        setActive(true)
        setStatus('running')
        setLogs([])
        setFound(0)
        setProgress({ current: 0, total: 0 })
        startPolling()
      } catch {
        setError('Falha ao iniciar')
      }
    }
  }

  if (connected === null) {
    return (
      <div className="app">
        <div className="center-message">
          <div className="spinner" />
        </div>
      </div>
    )
  }

  if (connected === false) {
    return (
      <div className="app">
        <div className="center-message">
          <h1>OABPrev</h1>
          <p className="muted">Credenciais nao configuradas.</p>
          <p className="muted small">
            Rode <code>python app.py</code> e acesse{' '}
            <code>localhost:5000</code> para enviar o credentials.json
          </p>
        </div>
      </div>
    )
  }

  const showStats = active || status === 'completed' || status === 'stopped' || progress.current > 0

  return (
    <div className="app">
      <header>
        <h1>OABPrev</h1>
        <span className="subtitle">Busca de telefones</span>
      </header>

      <main>
        <div className="toggle-area">
          <button
            className={`toggle-btn ${active ? 'on' : ''} ${captcha ? 'captcha' : ''}`}
            onClick={handleToggle}
          >
            <span className="track">
              <span className="thumb" />
            </span>
          </button>
          <span className={`toggle-label ${captcha ? 'amber' : active ? 'green' : ''}`}>
            {STATUS_LABELS[status] || status}
          </span>
        </div>

        {captcha && (
          <div className="alert amber">
            <span className="alert-badge">CAPTCHA</span>
            <span>Resolva no navegador Chrome que abriu</span>
          </div>
        )}

        {error && (
          <div className="alert red">
            <span className="alert-badge">ERRO</span>
            <span>{error}</span>
          </div>
        )}

        {showStats && (
          <div className="stats">
            <div className="stat">
              <span className="stat-val">{progress.current}</span>
              <span className="stat-lbl">processados</span>
            </div>
            <div className="stat-sep" />
            <div className="stat">
              <span className="stat-val">{found}</span>
              <span className="stat-lbl">telefones</span>
            </div>
            <div className="stat-sep" />
            <div className="stat">
              <span className="stat-val">{progress.total}</span>
              <span className="stat-lbl">total</span>
            </div>
          </div>
        )}

        {active && progress.total > 0 && (
          <div className="progress-bar">
            <div
              className="progress-fill"
              style={{ width: `${(progress.current / progress.total) * 100}%` }}
            />
          </div>
        )}

        {logs.length > 0 && (
          <div className="logs" ref={logsRef}>
            {logs.map((line, i) => (
              <div
                key={i}
                className={`log-line${
                  line.includes('CAPTCHA') && !line.toLowerCase().includes('resolvido')
                    ? ' log-captcha'
                    : line.includes('✓')
                    ? ' log-ok'
                    : line.includes('✗')
                    ? ' log-err'
                    : ''
                }`}
              >
                {line}
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  )
}
