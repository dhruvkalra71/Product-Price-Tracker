const API_BASE = import.meta.env.VITE_API_BASE_URL || '';

async function request(endpoint, options = {}) {
  const url = `${API_BASE}${endpoint}`;
  const headers = {
    'Content-Type': 'application/json',
    ...options.headers,
  };

  const response = await fetch(url, { ...options, headers });
  if (!response.ok) {
    const errorBody = await response.text();
    let message = `Request failed: ${response.status} ${response.statusText}`;
    try {
      const parsed = JSON.parse(errorBody);
      if (parsed.error) message = parsed.error;
    } catch {}
    throw new Error(message);
  }
  return response.json();
}

export const api = {
  getProducts: () => request('/api/products'),
  getProductDetail: (id) => request(`/api/products/${id}`),
  getProductHistory: (id) => request(`/api/products/${id}/history`),
  getProductLogs: (id) => request(`/api/products/${id}/logs`),
  searchCatalog: (q) => request(`/api/search?q=${encodeURIComponent(q)}`),
  trackProduct: (sourceProductId, interval = 120) =>
    request('/api/products/track', {
      method: 'POST',
      body: JSON.stringify({ source_product_id: sourceProductId, scrape_interval_minutes: interval, scrape_now: true }),
    }),
  untrackProduct: (id) =>
    request(`/api/products/${id}/track`, {
      method: 'DELETE',
    }),
  triggerScrape: (id) =>
    request(`/api/scrape/single/${id}`, {
      method: 'POST',
    }),
  addAlert: (id, type, threshold) =>
    request(`/api/products/${id}/alerts`, {
      method: 'POST',
      body: JSON.stringify({ type, threshold }),
    }),
  updateProductInterval: (id, intervalMinutes) =>
    request(`/api/products/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ scrape_interval_minutes: intervalMinutes }),
    }),
};
