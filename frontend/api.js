/*
 * АэроРоуд — клиент REST API. Единственное место, где формируются запросы.
 *
 * Базовый адрес:
 *   1) window.AERO_ROAD_API_BASE (можно задать в index.html)
 *   2) localStorage["aeroroad.apiBase"] (Настройки → Подключение)
 *   3) тот же origin, если страницу отдал backend (порт 8000),
 *      иначе http://localhost:8000 (file:// или python -m http.server)
 */
(function () {
  'use strict';
  const LS_KEY = 'aeroroad.apiBase';

  class ApiError extends Error {
    constructor(message, status, body) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.body = body;
    }
  }

  function getBase() {
    if (window.AERO_ROAD_API_BASE) return window.AERO_ROAD_API_BASE;
    try { const v = localStorage.getItem(LS_KEY); if (v) return v; } catch (e) { /* приватный режим */ }
    if (location.protocol.startsWith('http') && location.port === '8000') return '';
    return 'http://localhost:8000';
  }

  function setBase(value) {
    try {
      if (value) localStorage.setItem(LS_KEY, value.replace(/\/$/, ''));
      else localStorage.removeItem(LS_KEY);
    } catch (e) { /* игнорируем */ }
  }

  /** Абсолютный адрес для путей вида /static/... из ответов сервера. */
  const abs = (path) => (!path || /^https?:/.test(path) ? path : getBase() + path);

  function url(path, query) {
    let u = getBase() + '/api' + path;
    if (query) {
      const qs = Object.keys(query)
        .filter((k) => query[k] !== undefined && query[k] !== null && query[k] !== '')
        .map((k) => encodeURIComponent(k) + '=' + encodeURIComponent(query[k])).join('&');
      if (qs) u += '?' + qs;
    }
    return u;
  }

  function errorMessage(body, status) {
    if (body && typeof body.detail === 'string') return body.detail;
    if (body && Array.isArray(body.detail)) return body.detail.map((d) => d.msg).join('; ');
    return 'Ошибка сервера ' + status;
  }

  async function request(method, path, { query, body, timeout = 60000 } = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    let res;
    try {
      res = await fetch(url(path, query), {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        signal: ctrl.signal,
      });
    } catch (e) {
      throw new ApiError('Нет связи с сервером', 0, null);
    } finally {
      clearTimeout(timer);
    }
    const text = await res.text();
    let parsed = null;
    if (text) { try { parsed = JSON.parse(text); } catch (e) { parsed = text; } }
    if (!res.ok) throw new ApiError(errorMessage(parsed, res.status), res.status, parsed);
    return parsed;
  }

  /** POST /api/analyze — multipart с прогрессом загрузки (0..1). */
  function analyze(file, meta, onProgress) {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      fd.append('image', file, file.name);
      Object.keys(meta || {}).forEach((k) => {
        if (meta[k] !== undefined && meta[k] !== null && meta[k] !== '') fd.append(k, String(meta[k]));
      });
      const xhr = new XMLHttpRequest();
      xhr.open('POST', url('/analyze'));
      if (onProgress && xhr.upload) {
        xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
      }
      xhr.onload = () => {
        let body = null;
        try { body = JSON.parse(xhr.responseText); } catch (e) { body = xhr.responseText; }
        if (xhr.status >= 200 && xhr.status < 300) resolve(body);
        else reject(new ApiError(errorMessage(body, xhr.status), xhr.status, body));
      };
      xhr.onerror = () => reject(new ApiError('Нет связи с сервером', 0, null));
      xhr.send(fd);
    });
  }

  window.AeroApi = {
    ApiError, getBase, setBase, abs, analyze,
    health: () => request('GET', '/health', { timeout: 8000 }),
    config: () => request('GET', '/config'),
    projects: () => request('GET', '/projects'),
    createProject: (name) => request('POST', '/projects', { body: { name } }),
    photos: (projectId) => request('GET', '/photos', { query: { projectId } }),
    zones: (projectId) => request('GET', '/zones', { query: { projectId } }),
    setPhotoGeo: (id, geo) => request('POST', '/photos/' + encodeURIComponent(id) + '/geo', { body: geo }),
    verify: (id, verdict, comment) =>
      request('POST', '/zones/' + encodeURIComponent(id) + '/verify', { body: { verdict, comment } }),
    settings: () => request('GET', '/settings'),
    saveSettings: (patch) => request('PATCH', '/settings', { body: patch }),
    settlements: (q) => request('GET', '/settlements', { query: { q, limit: 6 } }),
  };
})();
