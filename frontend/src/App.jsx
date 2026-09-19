import React, { useState, useEffect } from 'react';
import { api } from './api';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from 'recharts';

export default function App() {
  const [activeTab, setActiveTab] = useState('dashboard');
  const [trackedProducts, setTrackedProducts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Search state
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState([]);
  const [searchLoading, setSearchLoading] = useState(false);

  // Selected product detail modal
  const [selectedProduct, setSelectedProduct] = useState(null);
  const [productDetail, setProductDetail] = useState(null);
  const [detailTab, setDetailTab] = useState('chart'); // 'chart' | 'table' | 'logs'
  const [scrapingId, setScrapingId] = useState(null);

  // Alert form state
  const [alertThreshold, setAlertThreshold] = useState('');

  const loadTrackedProducts = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.getProducts();
      setTrackedProducts(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadTrackedProducts();
  }, []);

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
    try {
      await api.trackProduct(sourceId);
      await loadTrackedProducts();
      if (searchQuery) {
        const updated = await api.searchCatalog(searchQuery);
        setSearchResults(updated);
      }
    } catch (err) {
      alert(`Error tracking: ${err.message}`);
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
    try {
      const detail = await api.getProductDetail(product.id);
      setProductDetail(detail);
    } catch (err) {
      console.error(err);
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
    if (!selectedProduct || !alertThreshold) return;
    try {
      await api.addAlert(selectedProduct.id, 'price_drop', parseFloat(alertThreshold));
      const detail = await api.getProductDetail(selectedProduct.id);
      setProductDetail(detail);
      setAlertThreshold('');
      alert('Alert configured successfully!');
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
      </header>

      {error && (
        <div style={{ padding: '1rem', background: 'var(--danger-bg)', color: 'var(--danger)', borderRadius: 'var(--radius)', marginBottom: '1.5rem' }}>
          <strong>Error:</strong> {error}
        </div>
      )}

      {/* DASHBOARD TAB */}
      {activeTab === 'dashboard' && (
        <div>
          {loading && <p style={{ color: 'var(--text-muted)' }}>Loading tracked items...</p>}

          {!loading && trackedProducts.length === 0 && (
            <div style={{ textAlign: 'center', padding: '4rem 1rem', background: 'white', borderRadius: 'var(--radius)', border: '1px solid var(--border)' }}>
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
              const statusBadge =
                { success: 'badge-success', retried_then_success: 'badge-warning', failed: 'badge-danger' }[p.latest_status] || 'badge-neutral';

              return (
                <div key={p.id} className="card">
                  <div>
                    <div className="card-header">
                      <span className="mono" style={{ color: 'var(--text-muted)' }}>
                        #{p.source_product_id}
                      </span>
                      <span className={`badge ${statusBadge}`}>{p.latest_status.replace(/_/g, ' ')}</span>
                    </div>

                    <h3 style={{ fontSize: '1.1rem', marginBottom: '0.5rem' }}>{p.name}</h3>

                    <div style={{ margin: '1rem 0' }}>
                      <div className="price-tag">
                        {p.latest_price != null ? `₹${p.latest_price.toLocaleString('en-IN')}` : '—'}
                      </div>
                      <div style={{ fontSize: '0.85rem', color: 'var(--text-muted)', marginTop: '0.25rem' }}>
                        Stock: {p.latest_stock_raw || (p.latest_in_stock ? 'In Stock' : 'Out of Stock')}
                      </div>
                    </div>

                    <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginBottom: '1rem' }}>
                      Last scraped:{' '}
                      {p.last_scraped_at ? new Date(p.last_scraped_at).toLocaleTimeString() : 'Never'}
                    </div>
                  </div>

                  <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
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
                    <button className="btn btn-danger btn-sm" onClick={() => handleUntrack(p.id)}>
                      Untrack
                    </button>
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
                        <span className="badge badge-success">Tracked</span>
                      ) : (
                        <button className="btn btn-primary btn-sm" onClick={() => handleTrack(item.id)}>
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
            <div style={{ display: 'flex', gap: '2rem', padding: '1rem', background: '#f8fafc', borderRadius: '8px', marginBottom: '1.5rem' }}>
              <div>
                <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Latest Price</div>
                <div style={{ fontSize: '1.5rem', fontWeight: 700 }}>
                  {productDetail.latest_price != null ? `₹${productDetail.latest_price.toLocaleString('en-IN')}` : '—'}
                </div>
              </div>
              <div>
                <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>Live Stock</div>
                <div style={{ fontSize: '1.1rem', fontWeight: 600, marginTop: '0.25rem' }}>
                  {productDetail.latest_stock_raw || (productDetail.latest_in_stock ? 'In Stock' : 'Out of Stock')}
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
              <div style={{ width: '100%', height: 280, margin: '1rem 0' }}>
                {productDetail.price_history?.length > 0 ? (
                  <ResponsiveContainer>
                    <LineChart data={[...productDetail.price_history].reverse()}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                      <XAxis
                        dataKey="scraped_at"
                        tickFormatter={(t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                        stroke="#64748b"
                      />
                      <YAxis
                        stroke="#64748b"
                        domain={['auto', 'auto']}
                        tickFormatter={(v) => `₹${v}`}
                      />
                      <Tooltip
                        formatter={(val) => [`₹${val}`, 'Price']}
                        labelFormatter={(lbl) => new Date(lbl).toLocaleString()}
                      />
                      <Line
                        type="monotone"
                        dataKey="price"
                        stroke="#2563eb"
                        strokeWidth={2}
                        dot={{ r: 4 }}
                        activeDot={{ r: 6 }}
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

            {/* Alert Configuration */}
            <div style={{ marginTop: '1.5rem', paddingTop: '1rem', borderTop: '1px solid var(--border)' }}>
              <h4 style={{ fontSize: '0.9rem', marginBottom: '0.5rem' }}>Set Price Drop Alert</h4>
              <form onSubmit={handleSetAlert} style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                <input
                  type="number"
                  placeholder="Target price in ₹ (e.g. 10000)"
                  className="search-input"
                  style={{ maxWidth: 260 }}
                  value={alertThreshold}
                  onChange={(e) => setAlertThreshold(e.target.value)}
                />
                <button type="submit" className="btn btn-secondary btn-sm">
                  Save Alert
                </button>
              </form>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
