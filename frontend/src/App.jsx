import React, { useState, useEffect, useRef } from 'react';
import { api } from './api';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  ReferenceLine,
} from 'recharts';

const INTERVAL_PRESETS = [
  { label: 'Every 15 min', value: 15 },
  { label: 'Every 30 min', value: 30 },
  { label: 'Every 1 hour', value: 60 },
  { label: 'Every 2 hours', value: 120 },
  { label: 'Every 6 hours', value: 360 },
  { label: 'Every 12 hours', value: 720 },
  { label: 'Every 24 hours', value: 1440 },
];

const formatInterval = (m = 120) =>
  m % 1440 === 0 ? `${m / 1440} day${m > 1440 ? 's' : ''}` :
  m % 60 === 0 ? `${m / 60} hour${m > 60 ? 's' : ''}` : `${m} min`;

function renderStockBadge(stockRaw, inStock) {
  const raw = (stockRaw || '').trim().toLowerCase();

  if (raw.includes('selling fast') || raw.includes('low stock') || (raw.includes('only') && raw.includes('left'))) {
    return (
      <span className="badge-stock badge-stock-urgent">
        <span>🔥</span>
        <span>{stockRaw || 'Selling Fast'}</span>
      </span>
    );
  }

  if (inStock === false || raw.includes('out of stock') || raw.includes('sold out')) {
    return (
      <span className="badge-stock badge-stock-out">
        <span>✕</span>
        <span>{stockRaw || 'Out of Stock'}</span>
      </span>
    );
  }

  return (
    <span className="badge-stock badge-stock-in">
      <span>●</span>
      <span>{stockRaw || (inStock ? 'In Stock' : 'Stock Unknown')}</span>
    </span>
  );
}

export default function App() {
  const [activeTab, setActiveTab] = useState('dashboard');
  const [trackedProducts, setTrackedProducts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Search state
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [trackingIds, setTrackingIds] = useState(new Set());

  // Selected product detail modal
  const [selectedProduct, setSelectedProduct] = useState(null);
  const [productDetail, setProductDetail] = useState(null);
  const [detailTab, setDetailTab] = useState('chart'); // 'chart' | 'table' | 'logs'
  const [scrapingId, setScrapingId] = useState(null);

  // Scrape interval modal state
  const [modalCustomInterval, setModalCustomInterval] = useState(120);
  const [showCustomInput, setShowCustomInput] = useState(false);

  // Alert form state
  const [alertType, setAlertType] = useState('price_drop'); // 'price_drop' | 'back_in_stock'
  const [alertThreshold, setAlertThreshold] = useState('');

  // Change detection & live polling state
  const [priceUpdates, setPriceUpdates] = useState({});
  const previousPricesRef = useRef({});

  // Theme state
  const [theme, setTheme] = useState(() => {
    const saved = localStorage.getItem('theme');
    if (saved) return saved;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
      ? 'dark'
      : 'light';
  });

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('theme', theme);
  }, [theme]);

  const toggleTheme = () => {
    setTheme((prev) => (prev === 'light' ? 'dark' : 'light'));
  };

  const loadTrackedProducts = async ({ silent = false } = {}) => {
    if (!silent) {
      setLoading(true);
      setError(null);
    }
    try {
      const data = await api.getProducts();

      // Price change detection
      const newUpdates = {};
      data.forEach((p) => {
        const prev = previousPricesRef.current[p.id];
        if (prev !== undefined && p.latest_price !== null) {
          if (prev !== null && p.latest_price !== prev) {
            const diff = p.latest_price - prev;
            newUpdates[p.id] = {
              type: diff < 0 ? 'drop' : 'hike',
              amount: Math.abs(diff),
              oldPrice: prev,
              newPrice: p.latest_price,
              time: Date.now(),
            };
          } else if (prev === null && p.latest_price !== null) {
            newUpdates[p.id] = {
              type: 'initial',
              amount: p.latest_price,
              oldPrice: null,
              newPrice: p.latest_price,
              time: Date.now(),
            };
          }
        }
        previousPricesRef.current[p.id] = p.latest_price;
      });

      if (Object.keys(newUpdates).length > 0) {
        setPriceUpdates((current) => ({ ...current, ...newUpdates }));
        setTimeout(() => {
          setPriceUpdates((current) => {
            const copy = { ...current };
            Object.keys(newUpdates).forEach((id) => delete copy[id]);
            return copy;
          });
        }, 15000);
      }

      setTrackedProducts(data);
    } catch (err) {
      if (!silent) setError(err.message);
    } finally {
      if (!silent) setLoading(false);
    }
  };

  useEffect(() => {
    loadTrackedProducts();
  }, []);

  // Live auto-polling every 8 seconds on dashboard tab for instant change detection
  useEffect(() => {
    if (activeTab !== 'dashboard') return;
    const interval = setInterval(() => {
      loadTrackedProducts({ silent: true });
    }, 8000);
    return () => clearInterval(interval);
  }, [activeTab]);

  // Search handler
  useEffect(() => {
    if (!searchQuery.trim()) {
      setSearchResults([]);
      return;
    }
    const timer = setTimeout(async () => {
      setSearchLoading(true);
      try {
        const res = await api.searchCatalog(searchQuery);
        setSearchResults(res);
      } catch (err) {
        console.error(err);
      } finally {
        setSearchLoading(false);
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  const handleTrack = async (sourceId) => {
    // 1. Optimistic UI feedback: instantly show as tracked in search results
    setSearchResults((prev) =>
      prev.map((item) => (item.id === sourceId ? { ...item, is_tracked: true } : item))
    );
    setTrackingIds((prev) => new Set(prev).add(sourceId));

    try {
      // 2. Fire tracking request (backend returns in ~10ms with async initial scrape)
      await api.trackProduct(sourceId);
      // 3. Immediately refresh tracked list so the card appears on the dashboard
      await loadTrackedProducts({ silent: true });
    } catch (err) {
      // Revert optimistic update on failure
      setSearchResults((prev) =>
        prev.map((item) => (item.id === sourceId ? { ...item, is_tracked: false } : item))
      );
      alert(`Error tracking product: ${err.message}`);
    } finally {
      setTrackingIds((prev) => {
        const next = new Set(prev);
        next.delete(sourceId);
        return next;
      });
    }
  };

  const handleUntrack = async (id) => {
    if (!confirm('Are you sure you want to stop tracking this product?')) return;
    try {
      await api.untrackProduct(id);
      if (selectedProduct?.id === id) {
        setSelectedProduct(null);
      }
      await loadTrackedProducts();
    } catch (err) {
      alert(`Error untracking: ${err.message}`);
    }
  };

  const handleOpenDetail = async (product) => {
    setSelectedProduct(product);
    setDetailTab('chart');
    setShowCustomInput(false);
    setModalCustomInterval(product.scrape_interval_minutes || 120);
    try {
      const detail = await api.getProductDetail(product.id);
      setProductDetail(detail);
      setModalCustomInterval(detail.scrape_interval_minutes || 120);
    } catch (err) {
      console.error(err);
    }
  };

  const handleUpdateInterval = async (productId, minutes) => {
    const val = parseInt(minutes, 10);
    if (isNaN(val) || val < 5) {
      alert('Scrape frequency must be at least 5 minutes.');
      return;
    }
    if (val > 43200) {
      alert('Scrape frequency cannot exceed 43,200 minutes (30 days).');
      return;
    }
    // Optimistic update in state
    setTrackedProducts((prev) =>
      prev.map((p) => (p.id === productId ? { ...p, scrape_interval_minutes: val } : p))
    );
    if (productDetail?.id === productId) {
      setProductDetail((prev) => ({ ...prev, scrape_interval_minutes: val }));
    }
    try {
      await api.updateProductInterval(productId, val);
    } catch (err) {
      alert(`Failed to update scrape interval: ${err.message}`);
      await loadTrackedProducts({ silent: true });
    }
  };

  const handleManualScrape = async (id) => {
    setScrapingId(id);
    try {
      await api.triggerScrape(id);
      await loadTrackedProducts();
      if (selectedProduct?.id === id) {
        const detail = await api.getProductDetail(id);
        setProductDetail(detail);
      }
    } catch (err) {
      alert(`Scrape error: ${err.message}`);
    } finally {
      setScrapingId(null);
    }
  };

  const handleSetAlert = async (e) => {
    e.preventDefault();
    if (!selectedProduct) return;
    if (alertType === 'price_drop' && (!alertThreshold || isNaN(alertThreshold))) {
      alert('Please enter a valid price threshold for the price drop alert.');
      return;
    }
    try {
      const thresholdVal = alertType === 'price_drop' ? parseFloat(alertThreshold) : null;
      await api.addAlert(selectedProduct.id, alertType, thresholdVal);
      const detail = await api.getProductDetail(selectedProduct.id);
      setProductDetail(detail);
      setAlertThreshold('');
      await loadTrackedProducts({ silent: true });
      alert(alertType === 'price_drop' ? 'Price drop alert configured!' : 'Back-in-stock alert configured!');
    } catch (err) {
      alert(`Failed to create alert: ${err.message}`);
    }
  };

  return (
    <div className="container">
      {/* Header */}
      <header className="app-header">
        <div className="logo-area">
          <div className="logo-icon">INE</div>
          <div>
            <h1 style={{ fontSize: '1.25rem', fontWeight: 700 }}>Product Price Tracker</h1>
            <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
              Monitoring <strong>demo.inelabteamdev.com</strong> • Automated Chaos-Resilient Scraper
            </p>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', flexWrap: 'wrap' }}>
          <nav className="nav-tabs">
            <button
              className={`tab-btn ${activeTab === 'dashboard' ? 'active' : ''}`}
              onClick={() => setActiveTab('dashboard')}
            >
              Tracked Dashboard ({trackedProducts.length})
            </button>
            <button
              className={`tab-btn ${activeTab === 'search' ? 'active' : ''}`}
              onClick={() => setActiveTab('search')}
            >
              Search Store
            </button>
          </nav>

          <button
            className="theme-toggle-btn"
            onClick={toggleTheme}
            title={`Switch to ${theme === 'light' ? 'dark' : 'light'} theme`}
            aria-label="Toggle theme"
          >
            {theme === 'light' ? '🌙 Dark' : '☀️ Light'}
          </button>
        </div>
      </header>

      {error && (
        <div style={{ padding: '1rem', background: 'var(--danger-bg)', color: 'var(--danger)', borderRadius: 'var(--radius)', marginBottom: '1.5rem' }}>
          <strong>Error:</strong> {error}
        </div>
      )}

      {/* DASHBOARD TAB */}
      {activeTab === 'dashboard' && (
        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.25rem', flexWrap: 'wrap', gap: '0.75rem' }}>
            <div className="live-sync-pill">
              <span className="pulse-dot"></span>
              <span>Live sync active (auto-updates every 8s)</span>
            </div>
            <button className="btn btn-secondary btn-sm" onClick={() => loadTrackedProducts()}>
              ↻ Refresh Now
            </button>
          </div>

          {loading && <p style={{ color: 'var(--text-muted)' }}>Loading tracked items...</p>}

          {!loading && trackedProducts.length === 0 && (
            <div style={{ textAlign: 'center', padding: '4rem 1rem', background: 'var(--bg-card)', borderRadius: 'var(--radius)', border: '1px solid var(--border)' }}>
              <h2 style={{ fontSize: '1.25rem', marginBottom: '0.5rem' }}>No Tracked Products Yet</h2>
              <p style={{ color: 'var(--text-muted)', marginBottom: '1.5rem' }}>
                Search INE's mock storefront catalog to pick products and start monitoring their price history.
              </p>
              <button className="btn btn-primary" onClick={() => setActiveTab('search')}>
                Browse Store Catalog
              </button>
            </div>
          )}

          <div className="grid-cards">
            {trackedProducts.map((p) => {
              const updateInfo = priceUpdates[p.id];
              const hasTriggeredAlert = p.alerts?.some((a) => a.triggered_at);
              const hasActiveAlert = p.alerts?.some((a) => !a.triggered_at);
              const alertCardClass = hasTriggeredAlert ? 'has-alert-triggered' : hasActiveAlert ? 'has-alert-active' : '';

              return (
                <div key={p.id} className={`card ${alertCardClass} ${updateInfo ? 'card-updated' : ''}`}>
                  <div>
                    <div className="card-header">
                      <span className="mono" style={{ color: 'var(--text-muted)' }}>
                        #{p.source_product_id}
                      </span>
                      {updateInfo && (
                        <span className={`badge flash-badge ${updateInfo.type === 'hike' ? 'badge-warning' : 'badge-success'}`}>
                          {updateInfo.type === 'drop'
                            ? `↓ Dropped by ₹${updateInfo.amount.toLocaleString('en-IN')}!`
                            : updateInfo.type === 'hike'
                            ? `↑ Updated: ₹${updateInfo.newPrice.toLocaleString('en-IN')}`
                            : `✨ Price ready: ₹${updateInfo.newPrice.toLocaleString('en-IN')}`}
                        </span>
                      )}
                    </div>

                    {/* Price-drop / stock alerts banner at fixed top position above price */}
                    {p.alerts && p.alerts.length > 0 && (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.3rem', marginBottom: '0.65rem' }}>
                        {p.alerts.map((alert) => (
                          <div
                            key={alert.id}
                            className={`alert-pill ${alert.triggered_at ? 'alert-pill-triggered' : 'alert-pill-active'}`}
                          >
                            {alert.type === 'price_drop' ? (
                              alert.triggered_at ? (
                                <span>🔔 Price dropped below ₹{Number(alert.threshold).toLocaleString('en-IN')}!</span>
                              ) : (
                                <span>🎯 Target: &le; ₹{Number(alert.threshold).toLocaleString('en-IN')}</span>
                              )
                            ) : (
                              alert.triggered_at ? (
                                <span>📦 Back in stock alert triggered!</span>
                              ) : (
                                <span>📦 Back-in-stock watch active</span>
                              )
                            )}
                          </div>
                        ))}
                      </div>
                    )}

                    <h3 style={{ fontSize: '1.05rem', fontWeight: 600, marginBottom: '0.5rem', lineHeight: 1.35 }}>{p.name}</h3>

                    {/* Price and Stock Status */}
                    <div style={{ margin: '0.75rem 0' }}>
                      <div className="price-tag">
                        {p.latest_price != null ? `₹${p.latest_price.toLocaleString('en-IN')}` : '—'}
                      </div>
                      <div style={{ marginTop: '0.45rem' }}>
                        {p.latest_price != null ? (
                          renderStockBadge(p.latest_stock_raw, p.latest_in_stock)
                        ) : (
                          <span style={{ fontSize: '0.8rem', color: 'var(--primary)', fontStyle: 'italic' }}>
                            ⚡ Initial scrape in progress...
                          </span>
                        )}
                      </div>
                    </div>
                  </div>

                  <div>
                    {/* Action buttons with clear hierarchy */}
                    <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', flexWrap: 'wrap' }}>
                      <button className="btn btn-primary btn-sm" onClick={() => handleOpenDetail(p)}>
                        View History & Logs
                      </button>
                      <button
                        className="btn btn-secondary btn-sm"
                        disabled={scrapingId === p.id}
                        onClick={() => handleManualScrape(p.id)}
                      >
                        {scrapingId === p.id ? 'Scraping...' : 'Scrape Now'}
                      </button>
                      <button
                        className="btn-ghost-danger"
                        onClick={() => handleUntrack(p.id)}
                        title="Stop tracking this product"
                      >
                        🗑️ Untrack
                      </button>
                    </div>

                    {/* Muted footer row for metadata */}
                    <div className="card-footer-meta">
                      <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                        <span>⏱️ Check:</span>
                        <select
                          className="interval-select"
                          style={{ fontSize: '0.7rem', padding: '0.15rem 0.35rem' }}
                          value={
                            INTERVAL_PRESETS.some((opt) => opt.value === p.scrape_interval_minutes)
                              ? p.scrape_interval_minutes
                              : 'custom'
                          }
                          onChange={(e) => {
                            const val = e.target.value;
                            if (val === 'custom') {
                              const input = prompt('Enter scrape interval in minutes (minimum 5):', p.scrape_interval_minutes || 120);
                              if (input !== null) handleUpdateInterval(p.id, input);
                            } else {
                              handleUpdateInterval(p.id, val);
                            }
                          }}
                        >
                          {INTERVAL_PRESETS.map((opt) => (
                            <option key={opt.value} value={opt.value}>
                              {opt.label.replace('Every ', '')}
                            </option>
                          ))}
                          <option value="custom">
                            {INTERVAL_PRESETS.some((opt) => opt.value === p.scrape_interval_minutes)
                              ? 'Custom...'
                              : `Custom (${p.scrape_interval_minutes}m)`}
                          </option>
                        </select>
                      </div>

                      <div>
                        Last updated:{' '}
                        {p.last_scraped_at ? new Date(p.last_scraped_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : 'Never'}
                      </div>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* SEARCH TAB */}
      {activeTab === 'search' && (
        <div>
          <div className="search-box">
            <input
              type="text"
              className="search-input"
              placeholder="Search products by title, brand, category, or SKU (e.g. headphones, copperpot)..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              autoFocus
            />
          </div>

          {searchLoading && <p style={{ color: 'var(--text-muted)' }}>Searching catalog...</p>}

          <div className="table-container">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Product</th>
                  <th>Brand</th>
                  <th>Category</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {searchResults.map((item) => (
                  <tr key={item.id}>
                    <td className="mono">#{item.id}</td>
                    <td>
                      <strong>{item.name}</strong>
                      <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>{item.description}</div>
                    </td>
                    <td>{item.brand}</td>
                    <td>{item.category}</td>
                    <td>
                      {item.is_tracked ? (
                        <span className="badge badge-tracked">Tracked ✓</span>
                      ) : trackingIds.has(item.id) ? (
                        <button className="btn btn-secondary btn-sm" disabled>
                          Tracking...
                        </button>
                      ) : (
                        <button className="btn btn-outline-primary btn-sm" onClick={() => handleTrack(item.id)}>
                          + Track
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
                {!searchLoading && searchResults.length === 0 && searchQuery && (
                  <tr>
                    <td colSpan="5" style={{ textAlign: 'center', color: 'var(--text-muted)', padding: '2rem' }}>
                      No matching products found.
                    </td>
                  </tr>
                )}
                {!searchQuery && (
                  <tr>
                    <td colSpan="5" style={{ textAlign: 'center', color: 'var(--text-muted)', padding: '2rem' }}>
                      Type a keyword above to explore 1,000 products from INE's live store.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* PRODUCT DETAIL MODAL */}
      {selectedProduct && productDetail && (
        <div className="modal-overlay" onClick={() => setSelectedProduct(null)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <div>
                <span className="mono" style={{ color: 'var(--text-muted)' }}>
                  Product #{selectedProduct.source_product_id}
                </span>
                <h2 style={{ fontSize: '1.4rem' }}>{selectedProduct.name}</h2>
                <a
                  href={`https://demo.inelabteamdev.com/product/${selectedProduct.source_product_id}`}
                  target="_blank"
                  rel="noreferrer"
                  style={{ fontSize: '0.8rem', color: 'var(--primary)', textDecoration: 'none' }}
                >
                  Open live page on INE storefront ↗
                </a>
              </div>
              <button className="close-btn" onClick={() => setSelectedProduct(null)}>
                &times;
              </button>
            </div>

            {/* Quick stats banner */}
            <div style={{ display: 'flex', gap: '2rem', padding: '1rem', background: 'var(--alert-item-bg)', border: '1px solid var(--border)', borderRadius: '8px', marginBottom: '1.5rem', flexWrap: 'wrap' }}>
              <div>
                <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Latest Price</div>
                <div style={{ fontSize: '1.5rem', fontWeight: 700 }}>
                  {productDetail.latest_price != null ? `₹${productDetail.latest_price.toLocaleString('en-IN')}` : '—'}
                </div>
              </div>
              <div>
                <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Live Stock</div>
                <div style={{ marginTop: '0.35rem' }}>
                  {renderStockBadge(productDetail.latest_stock_raw, productDetail.latest_in_stock)}
                </div>
              </div>
              <div>
                <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Scrape Frequency</div>
                <div style={{ fontSize: '1.1rem', fontWeight: 600, marginTop: '0.25rem' }}>
                  Every {productDetail.scrape_interval_minutes}m
                </div>
              </div>
              <div style={{ marginLeft: 'auto', alignSelf: 'center' }}>
                <button
                  className="btn btn-secondary btn-sm"
                  disabled={scrapingId === selectedProduct.id}
                  onClick={() => handleManualScrape(selectedProduct.id)}
                >
                  {scrapingId === selectedProduct.id ? 'Scraping...' : 'Trigger Scrape'}
                </button>
              </div>
            </div>

            {/* Detail Tabs */}
            <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '1rem', borderBottom: '1px solid var(--border)', paddingBottom: '0.5rem' }}>
              <button
                className={`btn btn-sm ${detailTab === 'chart' ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setDetailTab('chart')}
              >
                Price Chart
              </button>
              <button
                className={`btn btn-sm ${detailTab === 'table' ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setDetailTab('table')}
              >
                Price Table ({productDetail.price_history?.length || 0})
              </button>
              <button
                className={`btn btn-sm ${detailTab === 'logs' ? 'btn-primary' : 'btn-secondary'}`}
                onClick={() => setDetailTab('logs')}
              >
                Scrape Audit Logs ({productDetail.logs?.length || 0})
              </button>
            </div>

            {/* CHART VIEW */}
            {detailTab === 'chart' && (
              <div style={{ width: '100%', height: 300, margin: '1rem 0' }}>
                {productDetail.price_history?.length > 0 ? (
                  <ResponsiveContainer>
                    <LineChart
                      data={[...productDetail.price_history].reverse()}
                      margin={{ top: 20, right: 30, left: 15, bottom: 5 }}
                    >
                      <CartesianGrid
                        vertical={false}
                        strokeDasharray="3 3"
                        stroke={theme === 'dark' ? '#1f293d' : '#e2e8f0'}
                      />
                      <XAxis
                        dataKey="scraped_at"
                        tickFormatter={(t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                        stroke={theme === 'dark' ? '#94a3b8' : '#64748b'}
                        tick={{ fontSize: 12 }}
                      />
                      <YAxis
                        stroke={theme === 'dark' ? '#94a3b8' : '#64748b'}
                        tick={{ fontSize: 12 }}
                        domain={[
                          (dataMin) => {
                            const dropAlert = productDetail.alerts?.find((a) => a.type === 'price_drop' && a.threshold);
                            const minVal = dropAlert ? Math.min(dataMin, Number(dropAlert.threshold)) : dataMin;
                            return Math.max(0, Math.floor(minVal * 0.95));
                          },
                          (dataMax) => {
                            const dropAlert = productDetail.alerts?.find((a) => a.type === 'price_drop' && a.threshold);
                            const maxVal = dropAlert ? Math.max(dataMax, Number(dropAlert.threshold)) : dataMax;
                            return Math.ceil(maxVal * 1.05);
                          },
                        ]}
                        tickFormatter={(v) => `₹${v.toLocaleString('en-IN')}`}
                      />
                      <Tooltip
                        contentStyle={{
                          backgroundColor: theme === 'dark' ? '#111827' : '#ffffff',
                          borderColor: theme === 'dark' ? '#1f293d' : '#e2e8f0',
                          color: theme === 'dark' ? '#f1f5f9' : '#0f172a',
                          borderRadius: '8px',
                          boxShadow: '0 4px 12px rgba(0,0,0,0.15)',
                        }}
                        formatter={(val) => [`₹${Number(val).toLocaleString('en-IN')}`, 'Verified Price']}
                        labelFormatter={(lbl) => new Date(lbl).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })}
                      />
                      {productDetail.alerts
                        ?.filter((a) => a.type === 'price_drop' && a.threshold)
                        .map((a) => (
                          <ReferenceLine
                            key={a.id}
                            y={Number(a.threshold)}
                            stroke="#22c55e"
                            strokeDasharray="4 4"
                            strokeWidth={1.5}
                            label={{
                              value: `🎯 Alert Target: ₹${Number(a.threshold).toLocaleString('en-IN')}`,
                              fill: '#22c55e',
                              fontSize: 11,
                              fontWeight: 600,
                              position: 'insideTopRight',
                            }}
                          />
                        ))}
                      <Line
                        type="monotone"
                        dataKey="price"
                        stroke="#3b82f6"
                        strokeWidth={2.5}
                        dot={{ r: 4, fill: '#3b82f6' }}
                        activeDot={{ r: 6, stroke: '#3b82f6', strokeWidth: 2 }}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                ) : (
                  <div style={{ textAlign: 'center', padding: '3rem', color: 'var(--text-muted)' }}>
                    No verified price readings recorded yet.
                  </div>
                )}
              </div>
            )}

            {/* TABLE VIEW */}
            {detailTab === 'table' && (
              <div className="table-container" style={{ maxHeight: 300, overflowY: 'auto' }}>
                <table>
                  <thead>
                    <tr>
                      <th>Timestamp</th>
                      <th>Verified Price</th>
                      <th>Stock State</th>
                      <th>Raw Badge</th>
                    </tr>
                  </thead>
                  <tbody>
                    {productDetail.price_history?.map((entry) => (
                      <tr key={entry.id}>
                        <td className="mono">{new Date(entry.scraped_at).toLocaleString()}</td>
                        <td style={{ fontWeight: 700 }}>₹{parseFloat(entry.price).toLocaleString('en-IN')}</td>
                        <td>
                          <span className={`badge ${entry.in_stock ? 'badge-success' : 'badge-danger'}`}>
                            {entry.in_stock ? 'In Stock' : 'Out of Stock'}
                          </span>
                        </td>
                        <td>{entry.stock_raw || '—'}</td>
                      </tr>
                    ))}
                    {(!productDetail.price_history || productDetail.price_history.length === 0) && (
                      <tr>
                        <td colSpan="4" style={{ textAlign: 'center', color: 'var(--text-muted)', padding: '1.5rem' }}>
                          No price history records found.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}

            {/* SCRAPE LOGS VIEW (Honest logging requirement) */}
            {detailTab === 'logs' && (
              <div>
                <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: '0.5rem' }}>
                  Every attempt is honestly recorded below. Failed scrapes never write garbage to the price history table.
                </div>
                <div className="table-container" style={{ maxHeight: 300, overflowY: 'auto' }}>
                  <table>
                    <thead>
                      <tr>
                        <th>Started At</th>
                        <th>Outcome</th>
                        <th>Attempts</th>
                        <th>Duration</th>
                        <th>Details / Error Message</th>
                      </tr>
                    </thead>
                    <tbody>
                      {productDetail.logs?.map((l) => (
                        <tr key={l.id} className={l.status === 'failed' ? 'row-failed' : ''}>
                          <td className="mono">{new Date(l.started_at).toLocaleTimeString()}</td>
                          <td>
                            <span
                              className={`badge ${
                                l.status === 'success'
                                  ? 'badge-success'
                                  : l.status === 'retried_then_success'
                                  ? 'badge-warning'
                                  : 'badge-danger'
                              }`}
                            >
                              {l.status}
                            </span>
                          </td>
                          <td>{l.attempt_count}</td>
                          <td>
                            {l.http_or_dom_detail?.elapsed_seconds
                              ? `${l.http_or_dom_detail.elapsed_seconds}s`
                              : '—'}
                          </td>
                          <td style={{ fontSize: '0.75rem' }}>
                            {l.error_message ? (
                              <span style={{ color: 'var(--danger)', fontWeight: 600 }}>{l.error_message}</span>
                            ) : (
                              <span style={{ color: 'var(--success)' }}>Successfully extracted and validated</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* Scrape Schedule & Frequency */}
            <div style={{ marginTop: '1.5rem', paddingTop: '1rem', borderTop: '1px solid var(--border)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75rem', flexWrap: 'wrap', gap: '0.5rem' }}>
                <div>
                  <h4 style={{ fontSize: '0.95rem', fontWeight: 600 }}>Scrape Schedule & Frequency</h4>
                  <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                    How frequently this product is scraped for price changes and alert evaluation.
                  </p>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                  <select
                    className="interval-select"
                    style={{ padding: '0.35rem 0.65rem', fontSize: '0.85rem' }}
                    value={
                      INTERVAL_PRESETS.some((opt) => opt.value === productDetail.scrape_interval_minutes)
                        ? productDetail.scrape_interval_minutes
                        : 'custom'
                    }
                    onChange={(e) => {
                      const val = e.target.value;
                      if (val === 'custom') {
                        setModalCustomInterval(productDetail.scrape_interval_minutes || 120);
                        setShowCustomInput(true);
                      } else {
                        setShowCustomInput(false);
                        handleUpdateInterval(productDetail.id, val);
                      }
                    }}
                  >
                    {INTERVAL_PRESETS.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                    <option value="custom">
                      {INTERVAL_PRESETS.some((opt) => opt.value === productDetail.scrape_interval_minutes)
                        ? 'Custom...'
                        : `Custom (${productDetail.scrape_interval_minutes}m)`}
                    </option>
                  </select>
                </div>
              </div>

              {showCustomInput && (
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', background: 'var(--alert-item-bg)', padding: '0.65rem 0.85rem', borderRadius: '6px', border: '1px solid var(--border)', marginBottom: '0.75rem' }}>
                  <span style={{ fontSize: '0.85rem', color: 'var(--text-main)' }}>Custom interval in minutes (min 5):</span>
                  <input
                    type="number"
                    min="5"
                    max="43200"
                    className="search-input"
                    style={{ maxWidth: 110, padding: '0.3rem 0.5rem', fontSize: '0.85rem' }}
                    value={modalCustomInterval}
                    onChange={(e) => setModalCustomInterval(e.target.value)}
                  />
                  <button
                    className="btn btn-primary btn-sm"
                    onClick={() => {
                      handleUpdateInterval(productDetail.id, modalCustomInterval);
                      setShowCustomInput(false);
                    }}
                  >
                    Save
                  </button>
                  <button
                    className="btn btn-secondary btn-sm"
                    onClick={() => setShowCustomInput(false)}
                  >
                    Cancel
                  </button>
                </div>
              )}

              <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)', display: 'flex', gap: '1.5rem', flexWrap: 'wrap' }}>
                <span>
                  Current schedule: <strong>{formatInterval(productDetail.scrape_interval_minutes)}</strong> ({productDetail.scrape_interval_minutes} min)
                </span>
                {productDetail.last_scraped_at && (
                  <span>
                    Next projected check:{' '}
                    <strong>
                      {new Date(
                        new Date(productDetail.last_scraped_at).getTime() +
                          productDetail.scrape_interval_minutes * 60 * 1000
                      ).toLocaleTimeString()}
                    </strong>
                  </span>
                )}
              </div>
            </div>

            {/* Alert Configuration */}
            <div style={{ marginTop: '1.5rem', paddingTop: '1rem', borderTop: '1px solid var(--border)' }}>
              <h4 style={{ fontSize: '0.95rem', marginBottom: '0.75rem' }}>Price & Stock Alerts</h4>

              {/* Active & Triggered Alerts List */}
              {productDetail.alerts && productDetail.alerts.length > 0 && (
                <div className="alert-list" style={{ marginBottom: '1rem' }}>
                  {productDetail.alerts.map((a) => (
                    <div key={a.id} className="alert-item">
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                        <span>{a.type === 'price_drop' ? '🔔' : '📦'}</span>
                        <div>
                          <strong>{a.type === 'price_drop' ? 'Price Drop Alert' : 'Back-in-Stock Alert'}</strong>
                          {a.type === 'price_drop' && a.threshold && (
                            <span style={{ color: 'var(--text-muted)', marginLeft: '0.5rem' }}>
                              (Target: &le; ₹{Number(a.threshold).toLocaleString('en-IN')})
                            </span>
                          )}
                        </div>
                      </div>
                      <div>
                        {a.triggered_at ? (
                          <span className="badge badge-success">
                            Triggered {new Date(a.triggered_at).toLocaleTimeString()}
                          </span>
                        ) : (
                          <span className="badge badge-neutral">Active (Monitoring)</span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {/* Alert Creation Form */}
              <form onSubmit={handleSetAlert} style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
                <div style={{ display: 'flex', gap: '1rem', alignItems: 'center' }}>
                  <label style={{ fontSize: '0.85rem', display: 'flex', alignItems: 'center', gap: '0.35rem', cursor: 'pointer' }}>
                    <input
                      type="radio"
                      name="alertType"
                      value="price_drop"
                      checked={alertType === 'price_drop'}
                      onChange={() => setAlertType('price_drop')}
                    />
                    <span>Price Drop Alert</span>
                  </label>
                  <label style={{ fontSize: '0.85rem', display: 'flex', alignItems: 'center', gap: '0.35rem', cursor: 'pointer' }}>
                    <input
                      type="radio"
                      name="alertType"
                      value="back_in_stock"
                      checked={alertType === 'back_in_stock'}
                      onChange={() => setAlertType('back_in_stock')}
                    />
                    <span>Back in Stock Alert</span>
                  </label>
                </div>

                <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                  {alertType === 'price_drop' ? (
                    <input
                      type="number"
                      placeholder="Target price in ₹ (e.g. 10000)"
                      className="search-input"
                      style={{ maxWidth: 280 }}
                      value={alertThreshold}
                      onChange={(e) => setAlertThreshold(e.target.value)}
                    />
                  ) : (
                    <span style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>
                      You will be notified immediately when this product is back in stock.
                    </span>
                  )}
                  <button type="submit" className="btn btn-secondary btn-sm">
                    Set Alert
                  </button>
                </div>
              </form>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
