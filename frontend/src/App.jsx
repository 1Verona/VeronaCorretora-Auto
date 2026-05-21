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
  const [sources, setSources] = useState([])
  const [sourcesOpen, setSourcesOpen] = useState(false)
  const [sourcesDirty, setSourcesDirty] = useState(false)
  const [sourcesSaving, setSourcesSaving] = useState(false)
  const [dragIndex, setDragIndex] = useState(null)
  const [outreachOpen, setOutreachOpen] = useState(false)
  const [outreachConfig, setOutreachConfig] = useState(null)
  const [outreachStatus, setOutreachStatus] = useState(null)
  const [outreachDirty, setOutreachDirty] = useState(false)
  const [outreachSaving, setOutreachSaving] = useState(false)
  const [conversations, setConversations] = useState([])
  const [agentOpen, setAgentOpen] = useState(false)
  const [agentConfig, setAgentConfig] = useState(null)
  const [agentTools, setAgentTools] = useState([])
  const [agentDirty, setAgentDirty] = useState(false)
  const [agentSaving, setAgentSaving] = useState(false)
  const [sheetsOpen, setSheetsOpen] = useState(false)
  const [sheetsConfig, setSheetsConfig] = useState(null)
  const [sheetsDirty, setSheetsDirty] = useState(false)
  const [sheetsSaving, setSheetsSaving] = useState(false)
  const pollRef = useRef(null)
  const logsRef = useRef(null)
  const outreachPollRef = useRef(null)

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

  const loadSources = useCallback(async () => {
    try {
      const res = await fetch('/sources')
      const data = await res.json()
      if (Array.isArray(data.sources)) {
        setSources(data.sources)
        setSourcesDirty(false)
      }
    } catch {}
  }, [])

  useEffect(() => {
    if (connected) loadSources()
  }, [connected, loadSources])

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

  const toggleSourceEnabled = (index) => {
    setSources((prev) => {
      const next = [...prev]
      next[index] = { ...next[index], enabled: !next[index].enabled }
      return next
    })
    setSourcesDirty(true)
  }

  const handleDragStart = (index) => (e) => {
    setDragIndex(index)
    e.dataTransfer.effectAllowed = 'move'
  }

  const handleDragOver = (index) => (e) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    if (dragIndex === null || dragIndex === index) return
    setSources((prev) => {
      const next = [...prev]
      const [moved] = next.splice(dragIndex, 1)
      next.splice(index, 0, moved)
      return next
    })
    setDragIndex(index)
    setSourcesDirty(true)
  }

  const handleDragEnd = () => {
    setDragIndex(null)
  }

  const saveSources = async () => {
    setSourcesSaving(true)
    try {
      const res = await fetch('/sources', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sources }),
      })
      const data = await res.json()
      if (data.ok) {
        setSourcesDirty(false)
      } else {
        setError(data.error || 'Falha ao salvar fontes')
      }
    } catch {
      setError('Falha ao salvar fontes')
    } finally {
      setSourcesSaving(false)
    }
  }

  const loadOutreachConfig = useCallback(async () => {
    try {
      const res = await fetch('/outreach/config')
      const data = await res.json()
      setOutreachConfig(data)
      setOutreachDirty(false)
    } catch {}
  }, [])

  const loadOutreachStatus = useCallback(async () => {
    try {
      const [statusRes, convRes] = await Promise.all([
        fetch('/outreach/status').then((r) => r.json()),
        fetch('/outreach/conversations').then((r) => r.json()),
      ])
      setOutreachStatus(statusRes)
      setConversations(convRes.conversations || [])
    } catch {}
  }, [])

  useEffect(() => {
    if (connected) loadOutreachConfig()
  }, [connected, loadOutreachConfig])

  useEffect(() => {
    if (!outreachOpen) {
      if (outreachPollRef.current) {
        clearInterval(outreachPollRef.current)
        outreachPollRef.current = null
      }
      return
    }
    loadOutreachStatus()
    outreachPollRef.current = setInterval(loadOutreachStatus, 5000)
    return () => {
      if (outreachPollRef.current) clearInterval(outreachPollRef.current)
    }
  }, [outreachOpen, loadOutreachStatus])

  const updateOutreachField = (field, value) => {
    setOutreachConfig((prev) => ({ ...(prev || {}), [field]: value }))
    setOutreachDirty(true)
  }

  const updateTemplate = (i, value) => {
    setOutreachConfig((prev) => {
      const tpls = [...(prev?.templates || [])]
      tpls[i] = value
      return { ...prev, templates: tpls }
    })
    setOutreachDirty(true)
  }

  const addTemplate = () => {
    setOutreachConfig((prev) => ({
      ...prev,
      templates: [...(prev?.templates || []), 'Olá {nome}, '],
    }))
    setOutreachDirty(true)
  }

  const removeTemplate = (i) => {
    setOutreachConfig((prev) => ({
      ...prev,
      templates: (prev?.templates || []).filter((_, idx) => idx !== i),
    }))
    setOutreachDirty(true)
  }

  const saveOutreachConfig = async () => {
    if (!outreachConfig) return
    setOutreachSaving(true)
    try {
      const res = await fetch('/outreach/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(outreachConfig),
      })
      const data = await res.json()
      if (data.config) {
        setOutreachConfig(data.config)
        setOutreachDirty(false)
      }
    } finally {
      setOutreachSaving(false)
    }
  }

  const toggleOutreach = async () => {
    const path = outreachConfig?.enabled ? '/outreach/stop' : '/outreach/start'
    try {
      const res = await fetch(path, { method: 'POST' })
      const data = await res.json()
      if (data.config) setOutreachConfig(data.config)
      loadOutreachStatus()
    } catch {}
  }

  const dispatchNow = async () => {
    try {
      await fetch('/outreach/dispatch-now', { method: 'POST' })
      loadOutreachStatus()
    } catch {}
  }

  const pauseConversation = async (phone, paused) => {
    const path = paused ? 'resume' : 'pause'
    try {
      await fetch(`/outreach/conversations/${phone}/${path}`, { method: 'POST' })
      loadOutreachStatus()
    } catch {}
  }

  const loadAgentConfig = useCallback(async () => {
    try {
      const [cfgRes, toolsRes] = await Promise.all([
        fetch('/agent/config').then((r) => r.json()),
        fetch('/agent/tools').then((r) => r.json()),
      ])
      setAgentConfig(cfgRes)
      setAgentTools(toolsRes.tools || [])
      setAgentDirty(false)
    } catch {}
  }, [])

  useEffect(() => {
    if (connected && agentOpen && !agentConfig) loadAgentConfig()
  }, [connected, agentOpen, agentConfig, loadAgentConfig])

  const updateAgentField = (field, value) => {
    setAgentConfig((prev) => ({ ...(prev || {}), [field]: value }))
    setAgentDirty(true)
  }

  const toggleAgentTool = (name) => {
    setAgentConfig((prev) => {
      const enabled = { ...(prev?.enabled_tools || {}) }
      enabled[name] = !enabled[name]
      return { ...prev, enabled_tools: enabled }
    })
    setAgentTools((prev) =>
      prev.map((t) => (t.name === name && !t.required ? { ...t, enabled: !t.enabled } : t)),
    )
    setAgentDirty(true)
  }

  const saveAgentConfig = async () => {
    if (!agentConfig) return
    setAgentSaving(true)
    try {
      const res = await fetch('/agent/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(agentConfig),
      })
      const data = await res.json()
      if (data.config) {
        setAgentConfig(data.config)
        setAgentDirty(false)
        const toolsRes = await fetch('/agent/tools').then((r) => r.json())
        setAgentTools(toolsRes.tools || [])
      } else if (data.error) {
        setError(data.error)
      }
    } catch {
      setError('Falha ao salvar identidade')
    } finally {
      setAgentSaving(false)
    }
  }

  const loadSheetsConfig = useCallback(async () => {
    try {
      const data = await fetch('/sheets/config').then((r) => r.json())
      setSheetsConfig(data)
      setSheetsDirty(false)
    } catch {}
  }, [])

  useEffect(() => {
    if (connected && sheetsOpen && !sheetsConfig) loadSheetsConfig()
  }, [connected, sheetsOpen, sheetsConfig, loadSheetsConfig])

  const updateSheetsField = (field, value) => {
    setSheetsConfig((prev) => ({ ...(prev || {}), [field]: value }))
    setSheetsDirty(true)
  }

  const saveSheetsConfig = async () => {
    if (!sheetsConfig) return
    setSheetsSaving(true)
    try {
      const res = await fetch('/sheets/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sheetsConfig),
      })
      const data = await res.json()
      if (data.config) {
        setSheetsConfig(data.config)
        setSheetsDirty(false)
        loadSources()
      } else if (data.error) {
        setError(data.error)
      }
    } catch {
      setError('Falha ao salvar planilha')
    } finally {
      setSheetsSaving(false)
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
  const enabledCount = sources.filter((s) => s.enabled).length

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

        <div className="cards">
        <section className={`sources ${sourcesOpen ? 'open' : ''}`}>
          <button
            className="sources-header"
            onClick={() => setSourcesOpen((v) => !v)}
          >
            <span className="sources-title-group">
              <span className="sources-icon">📋</span>
              <span className="sources-title">Fontes de leads</span>
            </span>
            <span className="sources-summary">
              {sources.length === 0 ? (
                <span className="status-pill off">sem abas</span>
              ) : (
                <span className={`status-pill ${enabledCount > 0 ? 'on' : 'off'}`}>
                  {enabledCount}/{sources.length} ativas
                </span>
              )}
              <span className={`chevron ${sourcesOpen ? 'open' : ''}`}>▾</span>
            </span>
          </button>

          {sourcesOpen && (
            <div className="sources-body">
              {sources.length === 0 ? (
                <p className="muted small">Nenhuma aba encontrada.</p>
              ) : (
                <>
                  <p className="hint">Arraste para reordenar. Desmarque para ignorar.</p>
                  <ul className="source-list" onDragEnd={handleDragEnd}>
                    {sources.map((src, idx) => (
                      <li
                        key={src.name}
                        className={`source-item ${dragIndex === idx ? 'dragging' : ''} ${!src.enabled ? 'disabled' : ''}`}
                        draggable
                        onDragStart={handleDragStart(idx)}
                        onDragOver={handleDragOver(idx)}
                      >
                        <span className="grip">⋮⋮</span>
                        <span className="rank">{idx + 1}</span>
                        <label className="source-toggle">
                          <input
                            type="checkbox"
                            checked={src.enabled}
                            onChange={() => toggleSourceEnabled(idx)}
                          />
                          <span className="source-name">{src.name}</span>
                        </label>
                        <span className="lead-count">{src.lead_count}</span>
                      </li>
                    ))}
                  </ul>
                  <div className="sources-actions">
                    <button
                      className="btn-secondary"
                      onClick={loadSources}
                      disabled={sourcesSaving}
                    >
                      Recarregar
                    </button>
                    <button
                      className="btn-primary"
                      onClick={saveSources}
                      disabled={!sourcesDirty || sourcesSaving}
                    >
                      {sourcesSaving ? 'Salvando…' : sourcesDirty ? 'Salvar' : 'Salvo'}
                    </button>
                  </div>
                </>
              )}
            </div>
          )}
        </section>

        <section className={`sources ${outreachOpen ? 'open' : ''}`}>
          <button
            className="sources-header"
            onClick={() => setOutreachOpen((v) => !v)}
          >
            <span className="sources-title-group">
              <span className="sources-icon">💬</span>
              <span className="sources-title">Disparo WhatsApp</span>
            </span>
            <span className="sources-summary">
              {outreachStatus?.test_mode && <span className="status-pill warn">teste</span>}
              <span className={`status-pill ${outreachConfig?.enabled ? 'on' : 'off'}`}>
                {outreachConfig?.enabled ? 'ligado' : 'desligado'}
              </span>
              {outreachStatus?.queue_size != null && outreachStatus.queue_size >= 0 && (
                <span className="status-pill info">fila {outreachStatus.queue_size}</span>
              )}
              <span className={`chevron ${outreachOpen ? 'open' : ''}`}>▾</span>
            </span>
          </button>

          {outreachOpen && outreachConfig && (
            <div className="sources-body">
              <div className="outreach-toggle-row">
                <button
                  className={`toggle-btn ${outreachConfig.enabled ? 'on' : ''}`}
                  onClick={toggleOutreach}
                >
                  <span className="track">
                    <span className="thumb" />
                  </span>
                </button>
                <span className={`toggle-label ${outreachConfig.enabled ? 'green' : ''}`}>
                  {outreachConfig.enabled ? 'Disparo ligado' : 'Disparo desligado'}
                </span>
                <button className="btn-secondary" onClick={dispatchNow} style={{ marginLeft: 'auto' }}>
                  Enviar 1 agora
                </button>
              </div>

              {outreachStatus && (
                <div className="outreach-status">
                  <div>📤 Enviados hoje: <b>{outreachStatus.sent_today}</b>/{outreachStatus.daily_limit}</div>
                  <div>📋 Fila: <b>{outreachStatus.queue_size}</b></div>
                  <div>⏱️ Janela: <b>{outreachStatus.in_window ? 'dentro' : 'fora'}</b></div>
                  {outreachStatus.next_dispatch_in_seconds != null && (
                    <div>➡️ Próximo: {outreachStatus.next_dispatch_in_seconds}s</div>
                  )}
                  {outreachStatus.paused_reason && (
                    <div className="alert amber" style={{ marginTop: 6 }}>
                      ⏸️ {outreachStatus.paused_reason}
                    </div>
                  )}
                  {!outreachStatus.evolution_configured && (
                    <div className="alert red" style={{ marginTop: 6 }}>
                      ⚠️ Evolution não configurado no .env
                    </div>
                  )}
                </div>
              )}

              <div className="outreach-grid">
                <label>
                  Hora início
                  <input
                    type="number"
                    min="0"
                    max="23"
                    value={outreachConfig.hour_start}
                    onChange={(e) => updateOutreachField('hour_start', Number(e.target.value))}
                  />
                </label>
                <label>
                  Hora fim
                  <input
                    type="number"
                    min="0"
                    max="23"
                    value={outreachConfig.hour_end}
                    onChange={(e) => updateOutreachField('hour_end', Number(e.target.value))}
                  />
                </label>
                <label>
                  Limite diário
                  <input
                    type="number"
                    min="1"
                    value={outreachConfig.daily_limit}
                    onChange={(e) => updateOutreachField('daily_limit', Number(e.target.value))}
                  />
                </label>
                <label>
                  Delay mín (s)
                  <input
                    type="number"
                    min="1"
                    value={outreachConfig.min_delay}
                    onChange={(e) => updateOutreachField('min_delay', Number(e.target.value))}
                  />
                </label>
                <label>
                  Delay máx (s)
                  <input
                    type="number"
                    min="1"
                    value={outreachConfig.max_delay}
                    onChange={(e) => updateOutreachField('max_delay', Number(e.target.value))}
                  />
                </label>
                <label className="checkbox">
                  <input
                    type="checkbox"
                    checked={outreachConfig.weekdays_only}
                    onChange={(e) => updateOutreachField('weekdays_only', e.target.checked)}
                  />
                  <span>Só dias úteis</span>
                </label>
              </div>

              <div className="outreach-templates">
                <div className="outreach-templates-header">
                  <h4>Templates (use <code>{'{nome}'}</code>)</h4>
                  <button className="btn-secondary small" onClick={addTemplate}>+ Adicionar</button>
                </div>
                {(outreachConfig.templates || []).map((tpl, i) => (
                  <div key={i} className="template-row">
                    <textarea
                      rows={3}
                      value={tpl}
                      onChange={(e) => updateTemplate(i, e.target.value)}
                    />
                    <button className="btn-icon" onClick={() => removeTemplate(i)} title="Remover">✕</button>
                  </div>
                ))}
              </div>

              <div className="sources-actions">
                <button className="btn-secondary" onClick={loadOutreachConfig} disabled={outreachSaving}>
                  Recarregar
                </button>
                <button
                  className="btn-primary"
                  onClick={saveOutreachConfig}
                  disabled={!outreachDirty || outreachSaving}
                >
                  {outreachSaving ? 'Salvando…' : outreachDirty ? 'Salvar' : 'Salvo'}
                </button>
              </div>

              {conversations.length > 0 && (
                <div className="conversations">
                  <h4>Conversas ({conversations.length})</h4>
                  <ul className="conversation-list">
                    {conversations.slice(0, 20).map((c) => (
                      <li key={c.phone} className={`conversation-item ${c.paused_by_broker ? 'paused' : ''}`}>
                        <span className="conv-badge">{c.paused_by_broker ? '⏸️' : '🤖'}</span>
                        <span className="conv-name">{c.nome || c.phone}</span>
                        <span className="conv-stage">{c.stage}</span>
                        <button
                          className="btn-icon"
                          onClick={() => pauseConversation(c.phone, c.paused_by_broker)}
                          title={c.paused_by_broker ? 'Retomar bot' : 'Pausar bot'}
                        >
                          {c.paused_by_broker ? '▶' : '⏸'}
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </section>

        <section className={`sources ${agentOpen ? 'open' : ''}`}>
          <button
            className="sources-header"
            onClick={() => setAgentOpen((v) => !v)}
          >
            <span className="sources-title-group">
              <span className="sources-icon">🤖</span>
              <span className="sources-title">Identidade do agente</span>
            </span>
            <span className="sources-summary">
              {agentConfig ? (
                <>
                  {agentConfig.test_mode && <span className="status-pill warn">teste</span>}
                  <span className="status-pill info">{agentConfig.model}</span>
                  <span className="status-pill on">
                    {agentTools.filter((t) => t.enabled).length}/{agentTools.length} funções
                  </span>
                </>
              ) : (
                <span className="status-pill off">—</span>
              )}
              <span className={`chevron ${agentOpen ? 'open' : ''}`}>▾</span>
            </span>
          </button>

          {agentOpen && agentConfig && (
            <div className="sources-body">
              <div className={`test-mode-box ${agentConfig.test_mode ? 'on' : ''}`}>
                <div className="test-mode-row">
                  <label className="test-mode-toggle">
                    <input
                      type="checkbox"
                      checked={!!agentConfig.test_mode}
                      onChange={(e) => updateAgentField('test_mode', e.target.checked)}
                    />
                    <div>
                      <span className="test-mode-title">🧪 Modo de teste</span>
                      <span className="test-mode-desc">
                        Quando ligado, o sistema só envia disparos e responde mensagens do número configurado.
                      </span>
                    </div>
                  </label>
                </div>
                {agentConfig.test_mode && (
                  <label className="field" style={{ marginTop: 10, marginBottom: 0 }}>
                    <span>Número de teste (somente dígitos, com DDI 55)</span>
                    <input
                      type="text"
                      value={agentConfig.test_phone || ''}
                      onChange={(e) => updateAgentField('test_phone', e.target.value)}
                      placeholder="5548999320268"
                    />
                  </label>
                )}
              </div>

              <label className="field">
                <span>Prompt do sistema (personalidade, regras, escopo)</span>
                <textarea
                  className="prompt-area"
                  rows={14}
                  value={agentConfig.system_prompt || ''}
                  onChange={(e) => updateAgentField('system_prompt', e.target.value)}
                />
              </label>

              <div className="outreach-grid">
                <label>
                  Modelo
                  <input
                    type="text"
                    value={agentConfig.model || ''}
                    onChange={(e) => updateAgentField('model', e.target.value)}
                    placeholder="gpt-4o-mini"
                  />
                </label>
                <label>
                  Temperatura
                  <input
                    type="number"
                    min="0"
                    max="2"
                    step="0.1"
                    value={agentConfig.temperature ?? 0.4}
                    onChange={(e) => updateAgentField('temperature', Number(e.target.value))}
                  />
                </label>
                <label>
                  Memória (últimas msgs)
                  <input
                    type="number"
                    min="2"
                    max="100"
                    value={agentConfig.history_limit ?? 20}
                    onChange={(e) => updateAgentField('history_limit', Number(e.target.value))}
                  />
                </label>
              </div>

              <div className="tools-section">
                <h4>Funções disponíveis ao agente</h4>
                <p className="hint">Desmarque para impedir que o agente use a função. "Responder" é obrigatória.</p>
                <ul className="tools-list">
                  {agentTools.map((tool) => (
                    <li key={tool.name} className={`tool-item ${tool.enabled ? '' : 'off'} ${tool.required ? 'required' : ''}`}>
                      <label className="tool-toggle">
                        <input
                          type="checkbox"
                          checked={tool.enabled}
                          disabled={tool.required}
                          onChange={() => toggleAgentTool(tool.name)}
                        />
                        <div className="tool-info">
                          <span className="tool-name">{tool.label}</span>
                          <span className="tool-desc">{tool.description}</span>
                        </div>
                        {tool.required && <span className="tool-badge">obrigatória</span>}
                      </label>
                    </li>
                  ))}
                </ul>
              </div>

              <div className="sources-actions">
                <button className="btn-secondary" onClick={loadAgentConfig} disabled={agentSaving}>
                  Recarregar
                </button>
                <button
                  className="btn-primary"
                  onClick={saveAgentConfig}
                  disabled={!agentDirty || agentSaving}
                >
                  {agentSaving ? 'Salvando…' : agentDirty ? 'Salvar' : 'Salvo'}
                </button>
              </div>
            </div>
          )}
        </section>

        <section className={`sources ${sheetsOpen ? 'open' : ''}`}>
          <button
            className="sources-header"
            onClick={() => setSheetsOpen((v) => !v)}
          >
            <span className="sources-title-group">
              <span className="sources-icon">📊</span>
              <span className="sources-title">Planilha de leads</span>
            </span>
            <span className="sources-summary">
              <span className="status-pill info">
                {sheetsConfig ? (sheetsConfig.source_sheet ? `aba: ${sheetsConfig.source_sheet}` : 'todas as abas') : '—'}
              </span>
              <span className={`chevron ${sheetsOpen ? 'open' : ''}`}>▾</span>
            </span>
          </button>

          {sheetsOpen && sheetsConfig && (
            <div className="sources-body">
              <p className="hint">Aponta de qual Google Sheet o sistema lê e escreve. Trocar aqui muda scraper, outreach e o bot.</p>

              <label className="field">
                <span>Spreadsheet ID (ou URL completa)</span>
                <input
                  type="text"
                  value={sheetsConfig.spreadsheet_id || ''}
                  onChange={(e) => updateSheetsField('spreadsheet_id', e.target.value)}
                  placeholder="1QRnMXp8lTm..."
                />
              </label>

              <div className="outreach-grid">
                <label>
                  Aba de saída do scraper
                  <input
                    type="text"
                    value={sheetsConfig.output_sheet_name || ''}
                    onChange={(e) => updateSheetsField('output_sheet_name', e.target.value)}
                    placeholder="Leads_OAB_Scraper"
                  />
                </label>
                <label>
                  Seccional
                  <input
                    type="text"
                    value={sheetsConfig.seccional || ''}
                    onChange={(e) => updateSheetsField('seccional', e.target.value)}
                    placeholder="Santa Catarina"
                  />
                </label>
                <label>
                  Aba fonte do disparo (vazio = todas)
                  <input
                    type="text"
                    value={sheetsConfig.source_sheet || ''}
                    onChange={(e) => updateSheetsField('source_sheet', e.target.value)}
                    placeholder="(opcional)"
                  />
                </label>
              </div>

              <div className="sources-actions">
                <button className="btn-secondary" onClick={loadSheetsConfig} disabled={sheetsSaving}>
                  Recarregar
                </button>
                <button
                  className="btn-primary"
                  onClick={saveSheetsConfig}
                  disabled={!sheetsDirty || sheetsSaving}
                >
                  {sheetsSaving ? 'Salvando…' : sheetsDirty ? 'Salvar' : 'Salvo'}
                </button>
              </div>
            </div>
          )}
        </section>
        </div>
      </main>
    </div>
  )
}
