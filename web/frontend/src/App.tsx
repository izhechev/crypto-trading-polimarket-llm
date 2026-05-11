import { useState, useEffect } from 'react'
import { Search, RefreshCw, TrendingUp, TrendingDown, Clock, Shield, Anchor } from 'lucide-react'
import './App.css'

interface Position {
  coin: string
  symbol: string
  coin_id: string
  amount: number
  entry_price: number
  current_price: number
  pnl_pct: number
  type: string
  status: string
  logo_url?: string
  stop_loss?: number
  take_profit?: number
  reasoning?: string
  date?: string
}

interface ScanStatus {
  is_running: boolean
  last_output: string
  last_scan_ts?: string
}

function App() {
  const [positions, setPositions] = useState<Position[]>([])
  const [scanStatus, setScanStatus] = useState<ScanStatus>({ is_running: false, last_output: '' })
  const [loading, setLoading] = useState(true)

  const fetchPositions = async () => {
    try {
      const res = await fetch('/api/positions')
      const data = await res.json()
      setPositions(data)
    } catch (e) {
      console.error("Failed to fetch positions", e)
    } finally {
      setLoading(false)
    }
  }

  const fetchScanStatus = async () => {
    try {
      const res = await fetch('/api/scan/status')
      const data = await res.json()
      setScanStatus(data)
    } catch (e) {
      console.error("Failed to fetch scan status", e)
    }
  }

  const runScan = async () => {
    try {
      await fetch('/api/scan', { method: 'POST' })
      fetchScanStatus()
    } catch (e) {
      console.error("Failed to start scan", e)
    }
  }

  useEffect(() => {
    fetchPositions()
    fetchScanStatus()
    const interval = setInterval(() => {
      fetchScanStatus()
      if (!scanStatus.is_running) {
        fetchPositions()
      }
    }, 5000)
    return () => clearInterval(interval)
  }, [scanStatus.is_running])

  const formatPrice = (val: number) => {
    const abs = Math.abs(val)
    const decimals = abs >= 1 ? 2 : abs >= 0.01 ? 4 : abs >= 0.0001 ? 6 : 8
    return `$${val.toFixed(decimals)}`
  }

  return (
    <div className="dashboard">
      <header className="header glass">
        <div className="logo">
          <TrendingUp className="icon-blue" />
          <h1>CryptoAdvisor <span className="sub">Trading Desk</span></h1>
        </div>
        <div className="actions">
          <button 
            className={`btn-scan ${scanStatus.is_running ? 'running' : ''}`} 
            onClick={runScan}
            disabled={scanStatus.is_running}
          >
            {scanStatus.is_running ? <RefreshCw className="spin" /> : <Search />}
            {scanStatus.is_running ? 'Scanning...' : 'Run Scan'}
          </button>
        </div>
      </header>

      <main className="content">
        <section className="positions-section">
          <h2>Open Positions</h2>
          {loading ? (
            <div className="loading">Loading positions...</div>
          ) : (
            <div className="positions-grid">
              {positions.map((pos, idx) => (
                <div key={idx} className="position-card glass">
                  <div className="card-header">
                    <div className="coin-info">
                      {pos.logo_url ? (
                        <img src={pos.logo_url} alt={pos.symbol} className="coin-logo" />
                      ) : (
                        <div className="coin-logo-placeholder">{pos.symbol[0]}</div>
                      )}
                      <div>
                        <h3>{pos.symbol}</h3>
                        <span className="coin-name">{pos.coin}</span>
                      </div>
                    </div>
                    <div className="tags">
                      <span className={`tag ${pos.type.toLowerCase()}`}>
                        {pos.type === 'WHALE_RIDE' ? <Anchor size={12} /> : <Shield size={12} />}
                        {pos.type}
                      </span>
                    </div>
                  </div>

                  <div className="card-body">
                    <div className="price-row">
                      <div className="price-item">
                        <label>Entry</label>
                        <span>{formatPrice(pos.entry_price)}</span>
                      </div>
                      <div className="price-item">
                        <label>Current</label>
                        <span className="current-price">{formatPrice(pos.current_price)}</span>
                      </div>
                    </div>

                    <div className="pnl-section">
                      <div className="pnl-header">
                        <label>P&L</label>
                        <span className={pos.pnl_pct >= 0 ? 'pos' : 'neg'}>
                          {pos.pnl_pct >= 0 ? <TrendingUp size={14} /> : <TrendingDown size={14} />}
                          {pos.pnl_pct.toFixed(2)}%
                        </span>
                      </div>
                      <div className="progress-track">
                        <div 
                          className={`progress-fill ${pos.pnl_pct >= 0 ? 'green' : 'red'}`}
                          style={{ width: `${Math.min(100, Math.max(0, 50 + pos.pnl_pct))}%` }}
                        ></div>
                      </div>
                    </div>

                    {(pos.stop_loss || pos.take_profit) && (
                      <div className="sl-tp">
                        {pos.stop_loss && <div className="sl">SL: {formatPrice(pos.stop_loss)}</div>}
                        {pos.take_profit && <div className="tp">TP: {formatPrice(pos.take_profit)}</div>}
                      </div>
                    )}
                  </div>

                  {pos.reasoning && (
                    <div className="card-footer">
                      <div className="reasoning">{pos.reasoning}</div>
                    </div>
                  )}
                  {pos.date && (
                    <div className="date-footer">
                      <Clock size={12} /> {new Date(pos.date).toLocaleString()}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="terminal-section">
          <h2>
            Scan Console 
            {scanStatus.last_scan_ts && (
              <span className="ts">Last: {new Date(scanStatus.last_scan_ts).toLocaleTimeString()}</span>
            )}
          </h2>
          <div className="terminal glass">
            {scanStatus.last_output || 'No scan data available. Click "Run Scan" to start.'}
          </div>
        </section>
      </main>
    </div>
  )
}

export default App
