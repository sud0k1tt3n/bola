/* АэроРоуд — интерфейс оператора (vanilla JS + Leaflet 1.9). */
'use strict';

const Api = window.AeroApi;

// ─── справочники ─────────────────────────────────────────────────────────
const LEVEL_COLOR = { red: '#d32f2f', yellow: '#fbc02d', green: '#388e3c' };
const LEVEL_LABEL = { red: 'Непроходимо', yellow: 'Риск', green: 'Проезд открыт' };
const CLASS_ORDER = ['flooding', 'bridge_collapse', 'fallen_tree'];
const CLASS_LABEL = { flooding: 'Подтопление', bridge_collapse: 'Обрушение моста', fallen_tree: 'Упавшее дерево' };
const GEO_SOURCE = { exif: 'EXIF', manual: 'Вручную', demo: 'Демо-точка' };
const PHOTO_STATUS = {
  done: { label: 'Готово', pill: 'green' },
  need_geo: { label: 'Нужна привязка', pill: 'red' },
  failed: { label: 'Ошибка', pill: 'red' },
};
const BASEMAPS = [
  { key: 'hybrid', name: 'Гибрид', note: 'спутник + подписи и дороги', sw: 'linear-gradient(135deg,#3c4a3d 60%,#f2d16b 60% 64%,#3c4a3d 64%)' },
  { key: 'satellite', name: 'Спутник', note: 'Esri World Imagery', sw: '#3c4a3d' },
  { key: 'scheme', name: 'Схема дорог', note: 'OpenStreetMap', sw: '#e4ded3' },
  { key: 'contrast', name: 'Контраст', note: 'ч/б спутник — видны цветные участки', sw: '#8a8a8a' },
];
const OVERLAYS = [
  { key: 'photos', name: 'Снимки БАС', note: 'ортофотослой, повёрнут по курсу съёмки' },
  { key: 'zones', name: 'Проблемные участки', note: 'полигоны с цветовой индикацией и номера' },
  { key: 'footprints', name: 'Контуры кадров', note: 'граница кадра на земле; зелёная заливка — норма' },
];
// Подложки по умолчанию; сервер может прислать свои в GET /api/config.
const FALLBACK_TILES = {
  satellite: { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', attribution: 'Спутник: Esri, Maxar, Earthstar Geographics', maxNativeZoom: 19 },
  labels: [
    { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', attribution: 'Подписи: Esri', maxNativeZoom: 19 },
    { url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}', attribution: '', maxNativeZoom: 19 },
  ],
  scheme: { url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png', attribution: '© участники OpenStreetMap', maxNativeZoom: 19 },
};

// ─── утилиты ─────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const fmtInt = (v) => Math.round(v).toLocaleString('ru-RU');
const fmtArea = (m2) => (m2 >= 10000 ? (m2 / 10000).toFixed(2).replace('.', ',') + ' га' : fmtInt(m2) + ' м²');
const fmtTime = (iso) => {
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
};
const fmtCoord = (lat, lon, n = 4) => `${lat.toFixed(n)}° N, ${lon.toFixed(n)}° E`;
const num = (v) => {
  const x = parseFloat(String(v == null ? '' : v).replace(',', '.').trim());
  return isFinite(x) ? x : null;
};
const lsGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* приватный режим */ } };

// ─── состояние ───────────────────────────────────────────────────────────
const S = {
  screen: 'map',
  theme: document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light',
  api: 'loading',
  health: null,
  config: { bbox: { west: 30, south: 50, east: 60, north: 62 }, tiles: FALLBACK_TILES, processingBudgetSec: 30 },
  settings: { minConfidence: 0.35, alerts: { blocked: true, nogeo: true } },
  projects: [],
  projectId: lsGet('aeroroad.project'),
  projectMenu: false,
  photos: [],
  zones: [],
  selected: null,
  filter: 'all',
  hidden: {},
  layers: { photos: true, zones: true, footprints: true },
  opacity: parseFloat(lsGet('aeroroad.opacity')) || 0.85,
  basemap: lsGet('aeroroad.basemap') || 'hybrid',
  results: null,
  place: null,
  geoOpen: false,
  geoPhotoId: null,
  pickMode: false,
  uploading: null,
  verifyStatus: '',
};

// ─── загрузка данных ─────────────────────────────────────────────────────
async function boot() {
  S.api = 'loading';
  render();
  try {
    S.health = await Api.health();
    S.api = 'ok';
  } catch (e) {
    S.api = 'error';
    render();
    return;
  }
  const soft = (p, fn) => p.then(fn).catch(() => {});
  await Promise.all([
    soft(Api.config(), (c) => { S.config = Object.assign({}, S.config, c); if (map) applyBasemap(true); }),
    soft(Api.settings(), (s) => { S.settings = s; }),
  ]);
  await loadProjects();
}

async function loadProjects(selectId) {
  try {
    S.projects = await Api.projects();
  } catch (e) {
    S.projects = [];
  }
  const ids = S.projects.map((p) => p.id);
  S.projectId = selectId || (ids.includes(S.projectId) ? S.projectId : ids[0] || null);
  if (S.projectId) lsSet('aeroroad.project', S.projectId);
  await refresh(true);
}

async function refresh(fit) {
  if (S.api !== 'ok') { render(); return; }
  const [photos, zones] = await Promise.all([
    Api.photos(S.projectId).catch(() => S.photos),
    Api.zones(S.projectId).catch(() => S.zones),
  ]);
  S.photos = photos;
  S.zones = zones;
  if (!S.zones.some((z) => z.id === S.selected)) S.selected = null;
  render();
  if (fit) fitAll();
}

// ─── производные ─────────────────────────────────────────────────────────
const threshold = () => S.settings.minConfidence || 0;
const selectedZone = () => S.zones.find((z) => z.id === S.selected) || null;
const photoById = (id) => S.photos.find((p) => p.id === id) || null;
const currentProject = () => S.projects.find((p) => p.id === S.projectId) || null;

function visibleZones() {
  return S.zones.filter((z) =>
    z.confidence >= threshold() &&
    (S.filter === 'all' || z.level === S.filter));
}

function mapZones() {
  if (!S.layers.zones) return [];
  return S.zones.filter((z) => z.confidence >= threshold() && !S.hidden[z.type]);
}

// ─── отрисовка интерфейса ────────────────────────────────────────────────
function render() {
  renderHeader();
  renderNav();
  renderLeft();
  renderSelection();
  renderPhotos();
  renderZonesScreen();
  renderLayers();
  renderSettings();
  renderMapChrome();
  syncMap();
}

function renderHeader() {
  const p = currentProject();
  $('project-label').textContent = p ? p.name : (S.projects.length ? 'Выберите проект' : 'Создать проект');
  $('project-meta').textContent = p
    ? [S.photos.length ? S.photos.length + ' сн.' : null, S.zones.length ? S.zones.length + ' уч.' : null].filter(Boolean).join(' · ')
    : '';
  $('project-menu').hidden = !S.projectMenu;
  $('project-list').innerHTML = S.projects.length
    ? S.projects.map((pr) => `
      <button class="project-item${pr.id === S.projectId ? ' active' : ''}" data-act="pickProject" data-id="${esc(pr.id)}">
        <div class="grow"><div class="name">${esc(pr.name)}</div>
          <div class="meta">${[pr.photoCount + ' сн.', pr.zoneCount + ' уч.', fmtTime(pr.createdAt)].join(' · ')}</div></div>
        <div class="tiny">${pr.id === S.projectId ? 'текущий' : ''}</div>
      </button>`).join('')
    : '<div class="hint" style="padding:14px 12px">Проектов пока нет. Создайте первый — снимки и найденные участки будут собираться в нём.</div>';

  const dot = $('api-dot');
  dot.className = 'dot ' + (S.api === 'ok' ? 'ok' : S.api === 'error' ? 'error' : '');
  $('api-label').textContent = S.api === 'ok' ? 'Сервер на связи' : S.api === 'error' ? 'Нет связи с сервером' : 'Подключение…';
  const m = S.health && S.health.model;
  const demo = $('demo-pill');
  demo.hidden = !(m && m.mode !== 'custom');
  if (m && m.mode !== 'custom') {
    demo.textContent = {
      demo: 'Демо-режим: тестовые участки',
      mock: 'Демо-режим: без нейросети',
    }[m.mode] || 'Демо-режим: COCO-заглушка';
    demo.title = m.warning || '';
  }
}

function renderNav() {
  document.querySelectorAll('.rail-item').forEach((b) => b.classList.toggle('on', b.dataset.screen === S.screen));
  document.querySelectorAll('[data-screen-id]').forEach((s) => { s.hidden = s.dataset.screenId !== S.screen; });
}

function thumbStyle(url) {
  return url ? ` style="background-image:url('${esc(Api.abs(url))}')"` : '';
}

function geoLine(p) {
  const src = GEO_SOURCE[p.geoSource] || 'Не найдена';
  return p.lat != null ? `${src} · ${p.lat.toFixed(4)}, ${p.lon.toFixed(4)}` : src;
}

function renderLeft() {
  const last = S.photos[0];
  $('last-photo').innerHTML = last
    ? `<button class="photo-card" data-act="openPhoto" data-id="${esc(last.id)}">
         <div class="thumb"${thumbStyle(last.thumbUrl)}></div>
         <div class="grow"><div class="name">${esc(last.name)}</div><div class="geo">${esc(geoLine(last))}</div></div>
         <div class="st">${esc((PHOTO_STATUS[last.status] || {}).label || last.status)}</div>
       </button>`
    : `<div class="empty-box">${S.api === 'error'
        ? 'Сервер не отвечает. Запустите backend: <b>uvicorn main:app --port 8000</b>'
        : 'Снимков пока нет. Загрузите JPG или PNG — обработка начнётся автоматически.'}</div>`;

  // ручная привязка
  $('geo-form').hidden = !S.geoOpen;
  $('geo-toggle').textContent = S.geoOpen ? 'Скрыть' : 'Ввести вручную';
  const target = geoTarget();
  $('geo-target').textContent = target
    ? (target.status === 'need_geo'
        ? `Снимок «${target.name}» без GPS — укажите центр кадра, курс и высоту.`
        : `Снимок «${target.name}» будет пересчитан с этими параметрами.`)
    : 'Впишите, где сделан снимок, затем загрузите его — или загрузите снимок без GPS, и эта панель откроется сама.';

  // очередь
  const up = S.uploading;
  $('queue-label').textContent = up
    ? `${up.stage === 'upload' ? 'Загрузка' : 'Анализ'} ${up.name} · ${up.stage === 'upload' ? Math.round(up.pct * 100) + '%' : 'YOLO'} (${up.index} из ${up.total})`
    : S.api === 'ok' ? 'Очередь пуста' : 'Сервер недоступен';
  $('queue-bar').style.width = up ? (up.stage === 'upload' ? Math.round(up.pct * 90) : 95) + '%' : '0%';

  // классы
  const m = S.health && S.health.model;
  $('model-label').textContent = m ? 'модель ' + (m.weights || m.mode) : '';
  const found = CLASS_ORDER.filter((c) => S.zones.some((z) => z.type === c));
  $('class-rows').innerHTML = found.length
    ? found.map((c) => {
        const list = S.zones.filter((z) => z.type === c);
        const area = list.reduce((a, z) => a + (z.areaM2 || 0), 0);
        const lvl = list[0].level;
        const off = !!S.hidden[c];
        return `<div class="cls-row${off ? ' off' : ''}">
          <span class="sw lv-${lvl}"></span><span class="nm">${CLASS_LABEL[c]}</span>
          <span class="ct">${list.length}</span><span class="ar">${fmtArea(area)}</span>
          <button class="eye" data-act="toggleClass" data-v="${c}" title="${off ? 'Показать' : 'Скрыть'} на карте">${off ? '◌' : '◉'}</button>
        </div>`;
      }).join('')
    : `<div class="hint">${emptyHint(true)}</div>`;

  const vz = visibleZones();
  $('zone-rows').innerHTML = vz.length
    ? vz.map((z) => `
      <button class="zone-row${z.id === S.selected ? ' active' : ''}" data-act="select" data-id="${esc(z.id)}">
        <span class="sw lv-${z.level}"></span>
        <span class="grow"><div class="ty">${esc(z.label)}</div><div class="pl">${esc(z.place || '')}</div></span>
        <span class="cf">${Math.round(z.confidence * 100)}%</span>
      </button>`).join('')
    : `<div class="hint">${emptyHint(false)}</div>`;
}

function emptyHint(forClasses) {
  if (S.api === 'error') return 'Нет связи с сервером.';
  if (!S.photos.length) return 'Загрузите снимки — найденные явления появятся здесь и на карте.';
  if (!S.zones.length) return forClasses ? 'На обработанных снимках опасных явлений нет.' : 'Участков нет — все снимки в норме.';
  return `Все участки ниже порога ${Math.round(threshold() * 100)}% или скрыты фильтром.`;
}

function geoTarget() {
  return photoById(S.geoPhotoId) || S.photos.find((p) => p.status === 'need_geo') || null;
}

function renderSelection() {
  const z = selectedZone();
  $('sel-panel').hidden = !z;
  if (!z) return;
  const photo = photoById(z.photoId);
  const g = photo && photo.georef;
  $('sel-id').textContent = 'УЧАСТОК ' + z.id.toUpperCase();
  const badge = $('sel-badge');
  badge.className = 'badge lv-' + z.level;
  badge.textContent = LEVEL_LABEL[z.level];
  $('sel-type').textContent = z.label;
  $('sel-place').textContent = z.place || '';
  const shot = $('sel-shot');
  shot.classList.toggle('has', !!z.cropUrl);
  shot.style.backgroundImage = z.cropUrl ? `url('${Api.abs(z.cropUrl)}')` : '';
  $('sel-frame').textContent = z.photoName || '—';

  const attrs = [
    ['Класс', `${z.label} (${z.type})`],
    ['Площадь', fmtArea(z.areaM2 || 0)],
    ['Координаты', z.centroid ? fmtCoord(z.centroid.lat, z.centroid.lon) : '—'],
    ['Снимок', z.photoName || '—'],
  ];
  if (g) {
    attrs.push(['Высота съёмки', `${g.altitude_m} м`]);
    attrs.push(['Курс кадра', `${g.yaw_deg}°`]);
    attrs.push(['Разрешение', `${g.gsd_cm_per_px} см/пикс`]);
  }
  attrs.push(['Обнаружено', fmtTime(z.detectedAt)]);
  attrs.push(['Вердикт оператора', z.verdict === 'confirmed' ? 'подтверждён' : z.verdict === 'false_positive' ? 'ложное срабатывание' : 'нет']);
  $('sel-attrs').innerHTML = attrs.map(([k, v]) => `<div class="attr"><span class="k">${k}</span><span class="v">${esc(v)}</span></div>`).join('');
  $('sel-conf').textContent = Math.round(z.confidence * 100) + '%';
  const bar = $('sel-conf-bar');
  bar.style.width = Math.round(z.confidence * 100) + '%';
  bar.style.background = LEVEL_COLOR[z.level];
  document.querySelectorAll('.sel-foot [data-act="verify"]').forEach((b) => b.classList.toggle('chosen', b.dataset.v === z.verdict));
  $('verify-status').textContent = S.verifyStatus;
}

function renderPhotos() {
  if (S.screen !== 'photos') return;
  const done = S.photos.filter((p) => p.status === 'done');
  const stats = [
    ['Загружено', S.photos.length, 'в текущем проекте'],
    ['Обработано', done.length, 'лимит ' + (S.config.processingBudgetSec || 30) + ' с на снимок'],
    ['Без геопривязки', S.photos.filter((p) => p.status === 'need_geo').length, 'нужен ручной ввод'],
    ['Найдено участков', S.zones.length, 'непроходимо: ' + S.zones.filter((z) => z.level === 'red').length],
  ];
  $('photo-stats').innerHTML = stats.map(([k, v, n]) =>
    `<div class="stat"><div class="k">${k}</div><div class="v">${v}</div><div class="n">${n}</div></div>`).join('');
  $('photo-rows').innerHTML = S.photos.length
    ? S.photos.map((p) => {
        const st = PHOTO_STATUS[p.status] || { label: p.status, pill: 'yellow' };
        const mb = p.sizeBytes ? (p.sizeBytes / 1048576).toFixed(1) + ' МБ' : '';
        const dim = p.width ? `${p.width}×${p.height}` : '';
        const found = p.status === 'done'
          ? (p.zoneTypes.length ? p.zoneTypes.map((t) => CLASS_LABEL[t] || t).join(', ') : 'Без явлений')
          : '—';
        return `<button class="tr" data-act="openPhoto" data-id="${esc(p.id)}">
          <div class="file"><div class="thumb"${thumbStyle(p.thumbUrl)}></div>
            <div class="grow"><div class="name">${esc(p.name)}</div><div class="size">${[mb, dim].filter(Boolean).join(' · ')}</div></div></div>
          <div style="color:var(--t-2)">${GEO_SOURCE[p.geoSource] || 'Не найдена'}</div>
          <div style="color:var(--ink-2)">${esc(found)}</div>
          <div><span class="pill lv-${st.pill}">${st.label}</span></div>
          <div class="ta-r muted">${p.processingMs != null ? (p.processingMs / 1000).toFixed(1).replace('.', ',') + ' с' : '—'}</div>
        </button>`;
      }).join('')
    : '<div class="empty">Список пуст. Перетащите снимки в окно или нажмите «Загрузить снимки».</div>';
}

function renderZonesScreen() {
  if (S.screen !== 'zones') return;
  const vz = visibleZones();
  $('zones-sub').textContent = `Показано ${vz.length} из ${S.zones.length} — порог уверенности ${Math.round(threshold() * 100)}%.`;
  document.querySelectorAll('[data-act="filter"]').forEach((b) => b.classList.toggle('on', b.dataset.v === S.filter));
  $('zone-cards').innerHTML = vz.length
    ? vz.map((z) => `
      <button class="zone-card" data-act="select" data-id="${esc(z.id)}" data-go="map">
        <span class="strip sw lv-${z.level}"></span>
        <span style="width:190px;flex:none"><div class="type">${esc(z.label)}</div><div class="sub2">${esc(z.id.toUpperCase())} · ${esc(z.photoName || '')}</div></span>
        <span class="grow"><div class="v" style="color:var(--ink-2)">${esc(z.place || '')}</div>
          <div class="sub2">${z.centroid ? fmtCoord(z.centroid.lat, z.centroid.lon) : 'координаты не заданы'}</div></span>
        <span style="width:110px;flex:none"><div class="k">Площадь</div><div class="v">${fmtArea(z.areaM2 || 0)}</div></span>
        <span style="width:100px;flex:none"><div class="k">Уверенность</div><div class="v">${Math.round(z.confidence * 100)}%</div></span>
        <span class="pill lv-${z.level}" style="flex:none">${LEVEL_LABEL[z.level]}</span>
        <span class="onmap">На карте</span>
      </button>`).join('')
    : `<div class="card empty">${emptyHint(false)}</div>`;
}

function renderLayers() {
  if (S.screen !== 'layers') return;
  $('basemap-list').innerHTML = BASEMAPS.map((b) => `
    <button class="opt${S.basemap === b.key ? ' on' : ''}" data-act="basemap" data-v="${b.key}">
      <span class="sw" style="background:${b.sw}"></span>
      <span class="grow"><div class="name">${b.name}</div><div class="desc">${b.note}</div></span>
      <span class="mark">${S.basemap === b.key ? 'выбрано' : ''}</span>
    </button>`).join('');
  $('overlay-list').innerHTML = OVERLAYS.map((o) => `
    <div class="setting">
      <div class="grow"><div class="name">${o.name}</div><div class="desc">${o.note}</div></div>
      <button class="toggle${S.layers[o.key] ? ' on' : ''}" data-act="toggleLayer" data-v="${o.key}" aria-label="${o.name}"></button>
    </div>`).join('');
  $('overlay-opacity').value = S.opacity;
  $('overlay-opacity-v').textContent = Math.round(S.opacity * 100) + '%';
}

function renderSettings() {
  if (S.screen !== 'settings') return;
  document.querySelectorAll('.theme-opt').forEach((b) => b.classList.toggle('on', b.dataset.v === S.theme));
  const classes = (S.config.classes || []).map((c) => `${c.label} ≥ ${Math.round(c.threshold * 100)}%`).join(', ');
  $('settings-rows').innerHTML = `
    <div class="setting"><div class="grow"><div class="name">Порог отображения</div>
      <div class="desc">участки ниже этой уверенности скрываются в списках и на карте</div></div>
      <button class="val" data-act="bumpThreshold">${Math.round(threshold() * 100)}%</button></div>
    <div class="setting"><div class="grow"><div class="name">Пороги детектора</div>
      <div class="desc">${esc(classes || 'задаются в backend/config.py (CLS_CONF)')}</div></div>
      <div class="val">config.py</div></div>
    <div class="setting"><div class="grow"><div class="name">Лимит на снимок</div>
      <div class="desc">требование ТЗ; фактическое время — в таблице «Снимки»</div></div>
      <div class="val">${S.config.processingBudgetSec || 30} с</div></div>
    <div class="setting"><div class="grow"><div class="name">Источник геопривязки</div>
      <div class="desc">сначала EXIF/XMP снимка; если GPS нет — спрашиваем оператора</div></div>
      <div class="val">EXIF → вручную</div></div>`;
  const alerts = [
    ['blocked', 'Новый участок «непроходимо»', 'заметное уведомление сразу после обработки'],
    ['nogeo', 'Снимок без геопривязки', 'напомнить, что нужен ручной ввод'],
  ];
  $('alert-rows').innerHTML = alerts.map(([k, n, d]) => `
    <div class="setting"><div class="grow"><div class="name">${n}</div><div class="desc">${d}</div></div>
      <button class="toggle${S.settings.alerts[k] ? ' on' : ''}" data-act="toggleAlert" data-v="${k}" aria-label="${n}"></button></div>`).join('');
  const inp = $('api-base');
  if (document.activeElement !== inp) inp.value = lsGet('aeroroad.apiBase') || '';
}

function renderMapChrome() {
  document.querySelectorAll('#basemap-seg button').forEach((b) => b.classList.toggle('on', b.dataset.v === S.basemap));
  const last = S.photos.find((p) => p.status === 'done');
  $('bas-chip').textContent = (S.layers.photos ? 'Слой снимков БАС' : 'Слой снимков выключен') + ' · ' + (last ? last.name : '—');
  $('pick-hint').hidden = !S.pickMode;
  document.querySelector('.map-screen').classList.toggle('picking', S.pickMode);
}

// ─── карта ───────────────────────────────────────────────────────────────
// Повёрнутый снимок: <img>, растянутый CSS-матрицей по трём углам footprint
// (штатный L.imageOverlay умеет только прямоугольник «север вверх»).
const RotatedImageOverlay = L.Layer.extend({
  options: { opacity: 0.85, pane: 'photosPane' },
  initialize(url, corners, options) {
    this._url = url;
    this._tl = L.latLng(corners.top_left);
    this._tr = L.latLng(corners.top_right);
    this._bl = L.latLng(corners.bottom_left);
    this._br = L.latLng(corners.bottom_right);
    L.setOptions(this, options);
  },
  onAdd() {
    if (!this._img) {
      const img = L.DomUtil.create('img', 'leaflet-image-layer leaflet-zoom-animated');
      img.alt = '';
      img.style.transformOrigin = '0 0';
      img.style.opacity = this.options.opacity;
      img.onload = () => this._reset();
      img.src = this._url;
      this._img = img;
    }
    this.getPane().appendChild(this._img);
    this._reset();
  },
  onRemove() { L.DomUtil.remove(this._img); },
  getEvents() {
    const ev = { zoom: this._reset, viewreset: this._reset };
    if (this._map && this._map._zoomAnimated) ev.zoomanim = this._animateZoom;
    return ev;
  },
  getBounds() { return L.latLngBounds([this._tl, this._tr, this._bl, this._br]); },
  setOpacity(v) { this.options.opacity = v; if (this._img) this._img.style.opacity = v; return this; },
  _apply(tl, tr, bl) {
    const img = this._img, w = img.naturalWidth, h = img.naturalHeight;
    if (!w || !h) return;
    img.style.width = w + 'px';
    img.style.height = h + 'px';
    img.style.transform = `matrix(${(tr.x - tl.x) / w}, ${(tr.y - tl.y) / w}, ${(bl.x - tl.x) / h}, ${(bl.y - tl.y) / h}, ${tl.x}, ${tl.y})`;
  },
  _reset() {
    if (!this._map || !this._img) return;
    const p = (ll) => this._map.latLngToLayerPoint(ll);
    this._apply(p(this._tl), p(this._tr), p(this._bl));
  },
  _animateZoom(e) {
    const p = (ll) => this._map._latLngToNewLayerPoint(ll, e.zoom, e.center);
    this._apply(p(this._tl), p(this._tr), p(this._bl));
  },
});

let map = null;
let base = {};
let baseKey = null;
const L_ = {};
const photoOverlays = new Map(); // id → RotatedImageOverlay (не пересоздаём, чтобы не мигали)

function initMap() {
  map = L.map('map', { zoomControl: false, attributionControl: true, minZoom: 2 });
  const b = S.config.bbox;
  map.fitBounds([[b.south, b.west], [b.north, b.east]]);
  L.control.scale({ imperial: false, position: 'bottomleft', maxWidth: 120 }).addTo(map);
  // Свои панели: подписи гибрида (340) и снимки БАС (350) лежат между подложкой
  // (200) и полигонами участков (overlayPane, 400) — снимок никогда не закроет участок.
  [['labelsPane', 340], ['photosPane', 350]].forEach(([name, z]) => {
    const pane = map.createPane(name);
    pane.style.zIndex = z;
    pane.style.pointerEvents = 'none';
  });
  L_.photos = L.layerGroup().addTo(map);
  L_.footprints = L.layerGroup().addTo(map);
  L_.zones = L.layerGroup().addTo(map);
  L_.pins = L.layerGroup().addTo(map);
  L_.place = L.layerGroup().addTo(map);
  applyBasemap(true);

  const readout = () => {
    const c = map.getCenter();
    $('c-lat').textContent = c.lat.toFixed(4) + '° N';
    $('c-lon').textContent = c.lng.toFixed(4) + '° E';
    $('zoom-label').textContent = 'z' + map.getZoom();
  };
  map.on('moveend zoomend', readout);
  readout();

  map.on('click', (e) => {
    if (!S.pickMode) return;
    S.pickMode = false;
    S.geoOpen = true;
    $('geo-lat').value = e.latlng.lat.toFixed(6);
    $('geo-lon').value = e.latlng.lng.toFixed(6);
    setGeoStatus('Точка снята с карты — проверьте и нажмите «Привязать».');
    render();
  });

  if (window.ResizeObserver) new ResizeObserver(() => map.invalidateSize()).observe($('map'));
}

function tile(def, extra) {
  return L.tileLayer(def.url, Object.assign({
    attribution: def.attribution || '', maxZoom: 21, maxNativeZoom: def.maxNativeZoom || 19,
  }, extra || {}));
}

function applyBasemap(rebuild) {
  if (!map) return;
  if (rebuild) {
    Object.values(base).flat().forEach((l) => map.removeLayer(l));
    const t = S.config.tiles || FALLBACK_TILES;
    base = {
      sat: tile(t.satellite),
      labels: (t.labels || []).map((d) => tile(d, { pane: 'labelsPane' })),
      osm: tile(t.scheme),
    };
    baseKey = null;
  }
  if (baseKey === S.basemap) return;
  const want = {
    satellite: [base.sat],
    hybrid: [base.sat, ...base.labels],
    scheme: [base.osm],
    contrast: [base.sat],
  }[S.basemap] || [base.sat];
  Object.values(base).flat().forEach((l) => { if (!want.includes(l)) map.removeLayer(l); });
  want.forEach((l) => { if (!map.hasLayer(l)) l.addTo(map); });
  map.getPane('tilePane').style.filter = S.basemap === 'contrast' ? 'grayscale(1) contrast(1.2) brightness(0.8)' : '';
  baseKey = S.basemap;
}

function toLatLngs(ring) {
  return ring.slice(0, -1).map(([lon, lat]) => [lat, lon]);
}

function syncMap() {
  if (!map) return;
  applyBasemap(false);

  // снимки БАС
  const done = S.photos.filter((p) => p.status === 'done' && p.corners);
  const keep = new Set(S.layers.photos ? done.map((p) => p.id) : []);
  photoOverlays.forEach((layer, id) => {
    if (!keep.has(id)) { L_.photos.removeLayer(layer); photoOverlays.delete(id); }
  });
  if (S.layers.photos) {
    // Порядок: старые снизу, свежие сверху.
    done.slice().reverse().forEach((p) => {
      let layer = photoOverlays.get(p.id);
      if (!layer) {
        layer = new RotatedImageOverlay(Api.abs(p.url), p.corners, { opacity: S.opacity });
        photoOverlays.set(p.id, layer);
        L_.photos.addLayer(layer);
      } else {
        layer.setOpacity(S.opacity);
      }
    });
  }

  // контуры кадров (+ зелёная заливка «норма» для кадров без детекций)
  L_.footprints.clearLayers();
  if (S.layers.footprints) {
    done.forEach((p) => {
      if (!p.footprint) return;
      const green = p.overallStatus === 'green';
      L.polygon(toLatLngs(p.footprint.coordinates[0]), green
        ? { color: LEVEL_COLOR.green, weight: 2, fillColor: LEVEL_COLOR.green, fillOpacity: 0.25 }
        : { color: '#ffffff', weight: 1.5, opacity: 0.8, dashArray: '6 5', fill: false, interactive: false },
      ).bindTooltip(green ? `${p.name} · норма, опасностей не найдено` : p.name, { sticky: true })
        .addTo(L_.footprints);
    });
  }

  // участки
  L_.zones.clearLayers();
  L_.pins.clearLayers();
  mapZones().forEach((z, i) => {
    const active = z.id === S.selected;
    const color = LEVEL_COLOR[z.level];
    if (z.polygon) {
      L.polygon(toLatLngs(z.polygon), {
        color: active ? '#ffffff' : color, weight: active ? 3 : 2, fillColor: color, fillOpacity: 0.4,
      }).bindPopup(() => zonePopup(z), { maxWidth: 300, minWidth: 260 })
        .on('click', () => select(z.id, false))
        .addTo(L_.zones);
    }
    if (z.centroid) {
      const size = active ? 26 : 20;
      L.marker([z.centroid.lat, z.centroid.lon], {
        icon: L.divIcon({
          className: '', iconSize: [size, size], iconAnchor: [size / 2, size / 2],
          html: `<div class="zone-pin lv-${z.level}${active ? ' active' : ''}" style="background:${color}">${i + 1}</div>`,
        }),
        title: `${z.label} · ${z.place || ''}`,
        zIndexOffset: active ? 1000 : 0,
      }).bindPopup(() => zonePopup(z), { maxWidth: 300, minWidth: 260 })
        .on('click', () => select(z.id, false))
        .addTo(L_.pins);
    }
  });

  // метка поиска
  L_.place.clearLayers();
  if (S.place) {
    L.marker([S.place.lat, S.place.lon], {
      icon: L.divIcon({ className: '', iconSize: [0, 0], html: `<div class="place-pin"><b>${esc(S.place.name)}</b><i></i></div>` }),
      interactive: false,
    }).addTo(L_.place);
  }
}

function zonePopup(z) {
  const wrap = document.createElement('div');
  wrap.className = 'zpop';
  if (z.cropUrl) {
    const ph = document.createElement('div');
    ph.className = 'ph';
    ph.style.backgroundImage = `url('${Api.abs(z.cropUrl)}')`;
    ph.title = 'Открыть кадр целиком';
    ph.onclick = (e) => { e.stopPropagation(); openLightbox(z.photoUrl, `${z.label} · ${z.photoName}`); };
    wrap.appendChild(ph);
  }
  const info = document.createElement('div');
  info.innerHTML = `
    <div class="hd"><i style="background:${LEVEL_COLOR[z.level]}"></i><b>${esc(z.label)}</b><span>${Math.round(z.confidence * 100)}%</span></div>
    <div class="pl">${esc(z.place || '')}</div>
    <div class="fr">${esc(LEVEL_LABEL[z.level])} · ${fmtArea(z.areaM2 || 0)} · ${esc(z.photoName || '')}</div>`;
  wrap.appendChild(info);
  return wrap;
}

function fitAll() {
  if (!map) return;
  const pts = [];
  S.photos.forEach((p) => { if (p.footprint) toLatLngs(p.footprint.coordinates[0]).forEach((c) => pts.push(c)); });
  if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.25), { maxZoom: 18 });
  else { const b = S.config.bbox; map.fitBounds([[b.south, b.west], [b.north, b.east]]); }
}

function focusZone(z) {
  if (!map || !z) return;
  if (z.polygon) map.fitBounds(L.latLngBounds(toLatLngs(z.polygon)).pad(0.8), { maxZoom: 19 });
  else if (z.centroid) map.setView([z.centroid.lat, z.centroid.lon], Math.max(map.getZoom(), 16));
}

function focusPhoto(p) {
  if (map && p && p.footprint) map.fitBounds(L.latLngBounds(toLatLngs(p.footprint.coordinates[0])).pad(0.15), { maxZoom: 19 });
}

// ─── действия ────────────────────────────────────────────────────────────
function go(screen) {
  S.screen = screen;
  S.projectMenu = false;
  render();
  if (screen === 'map' && map) setTimeout(() => map.invalidateSize(), 30);
}

function select(id, focus = true) {
  S.selected = id;
  S.verifyStatus = '';
  if (S.screen !== 'map') { S.screen = 'map'; }
  render();
  setTimeout(() => {
    if (!map) return;
    map.invalidateSize();
    const z = selectedZone();
    if (focus) focusZone(z);
  }, 40);
}

let toastTimer = null;
function toast(text, danger) {
  $('toast-text').textContent = text;
  $('toast').classList.toggle('danger', !!danger);
  $('toast').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, danger ? 9000 : 5000);
}

function openLightbox(url, title) {
  if (!url) { toast('Сервер не передал кадр для этого участка'); return; }
  $('lightbox-img').src = Api.abs(url);
  $('lightbox-title').textContent = title || '';
  $('lightbox').hidden = false;
}

function setGeoStatus(text) { $('geo-status').textContent = text || 'Координаты в градусах, WGS 84.'; }

async function handleFiles(list) {
  const files = Array.from(list || []).filter((f) => /image\/(jpeg|png)/.test(f.type) || /\.(jpe?g|png)$/i.test(f.name));
  if (!files.length) { toast('Поддерживаются только JPG и PNG'); return; }
  if (S.api !== 'ok') { toast('Сервер не отвечает — загрузка недоступна', true); return; }
  if (S.screen !== 'map') go('map');
  let lastOk = null;
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    S.uploading = { name: f.name, pct: 0, index: i + 1, total: files.length, stage: 'upload' };
    renderLeft();
    try {
      const res = await Api.analyze(f, { project_id: S.projectId, place: $('geo-place').value.trim() }, (pct) => {
        S.uploading.pct = pct;
        if (pct >= 1) S.uploading.stage = 'analyze';
        renderLeft();
      });
      lastOk = res;
      announce(res);
    } catch (e) {
      if (e.status === 422 && e.body && e.body.photo_id) {
        S.geoOpen = true;
        S.geoPhotoId = e.body.photo_id;
        setGeoStatus('');
        if (S.settings.alerts.nogeo) toast(`${f.name}: в снимке нет GPS — укажите координаты вручную или кликните по карте`);
      } else {
        toast(`${f.name}: ${e.message}`, true);
      }
    }
  }
  S.uploading = null;
  await refresh(false);
  if (lastOk) {
    const zones = lastOk.zones || [];
    if (zones.length) select(zones[0].id); else focusPhoto(lastOk.photo);
  }
  if (S.geoOpen && S.geoPhotoId) $('geo-lat').focus();
}

function announce(res) {
  const n = res.detections.length;
  const red = res.overall_status === 'red';
  const name = res.photo ? res.photo.name : 'Снимок';
  const secs = (res.processing_ms / 1000).toFixed(1).replace('.', ',');
  const text = n
    ? `${name}: найдено участков — ${n}${red ? ', есть НЕПРОХОДИМЫЕ' : ''}, обработка ${secs} с`
    : `${name}: опасных явлений не найдено, обработка ${secs} с`;
  toast(text, red && S.settings.alerts.blocked);
}

async function applyGeo() {
  const target = geoTarget() || S.photos.find((p) => p.status === 'done');
  const lat = num($('geo-lat').value), lon = num($('geo-lon').value);
  ['geo-lat', 'geo-lon'].forEach((id) => $(id).classList.toggle('invalid', num($(id).value) == null));
  if (lat == null || lon == null) { setGeoStatus('Укажите широту и долготу центра снимка.'); return; }
  if (!target) { setGeoStatus('Нет снимка для привязки — сначала загрузите его.'); return; }
  setGeoStatus('Пересчитываем снимок…');
  try {
    const res = await Api.setPhotoGeo(target.id, {
      lat, lon, yaw: num($('geo-yaw').value), altitude: num($('geo-alt').value),
      place: $('geo-place').value.trim() || null,
    });
    setGeoStatus(`Привязано: ${fmtCoord(lat, lon)}`);
    S.geoPhotoId = null;
    announce(res);
    await refresh(false);
    const zones = res.zones || [];
    if (zones.length) select(zones[0].id); else focusPhoto(res.photo);
  } catch (e) {
    setGeoStatus('Не удалось: ' + e.message);
  }
}

async function verify(verdict) {
  const z = selectedZone();
  if (!z) return;
  S.verifyStatus = 'Отправляем вердикт…';
  renderSelection();
  try {
    await Api.verify(z.id, verdict);
    S.verifyStatus = verdict === 'confirmed' ? 'Участок подтверждён оператором' : 'Отмечено как ложное срабатывание — пойдёт в дообучение';
    await refresh(false);
  } catch (e) {
    S.verifyStatus = 'Не удалось сохранить: ' + e.message;
    renderSelection();
  }
}

async function patchSettings(patch) {
  S.settings = Object.assign({}, S.settings, patch);
  render();
  try { S.settings = await Api.saveSettings(patch); } catch (e) { toast('Настройка не сохранена на сервере: ' + e.message); }
  render();
}

async function createProject() {
  const name = $('new-project').value.trim();
  const hint = $('project-hint');
  if (!name) { hint.hidden = false; hint.textContent = 'Введите название проекта.'; return; }
  try {
    const p = await Api.createProject(name);
    $('new-project').value = '';
    hint.hidden = true;
    S.projectMenu = false;
    S.selected = null;
    toast(`Проект «${name}» создан`);
    await loadProjects(p.id);
  } catch (e) {
    hint.hidden = false;
    hint.textContent = 'Не удалось создать: ' + e.message;
  }
}

// поиск: населённые пункты с сервера + участки проекта
let searchTimer = null;
function onSearch(value) {
  $('search-clear').hidden = !value;
  clearTimeout(searchTimer);
  const q = value.trim().toLowerCase();
  if (q.length < 2) { S.results = null; renderResults(); return; }
  searchTimer = setTimeout(async () => {
    let places = [];
    try { places = await Api.settlements(value.trim()); } catch (e) { /* участки ищем всё равно */ }
    const zones = S.zones.filter((z) => `${z.label} ${z.place} ${z.id}`.toLowerCase().includes(q)).slice(0, 3);
    S.results = [
      ...places.map((p) => ({ kind: 'place', name: p.name, note: [p.kind, p.region].filter(Boolean).join(', '), lat: p.lat, lon: p.lon })),
      ...zones.map((z) => ({ kind: 'zone', id: z.id, name: z.label, note: z.place })),
    ];
    renderResults();
  }, 250);
}

function renderResults() {
  const box = $('search-results');
  if (S.results == null) { box.hidden = true; return; }
  box.hidden = false;
  box.innerHTML = S.results.length
    ? S.results.map((r, i) => `
      <button class="result" data-act="pickResult" data-i="${i}">
        <span class="ic">${r.kind === 'place' ? '⌖' : '◧'}</span>
        <span class="grow"><div class="nm">${esc(r.name)}</div><div class="nt">${esc(r.note || '')}</div></span>
        <span class="cd">${r.kind === 'place' ? r.lat.toFixed(2) + ' N' : esc(r.id.toUpperCase())}</span>
      </button>`).join('')
    : '<div class="result none">Ничего не нашлось — попробуйте часть названия</div>';
}

function clearSearch() {
  $('search').value = '';
  $('search-clear').hidden = true;
  S.results = null;
  S.place = null;
  renderResults();
  syncMap();
}

const ACTIONS = {
  go: (d) => go(d.screen),
  toggleProjects: () => { S.projectMenu = !S.projectMenu; $('project-hint').hidden = true; renderHeader(); },
  pickProject: async (d) => {
    S.projectMenu = false;
    S.projectId = d.id;
    S.selected = null;
    lsSet('aeroroad.project', d.id);
    photoOverlays.forEach((l) => L_.photos && L_.photos.removeLayer(l));
    photoOverlays.clear();
    await refresh(true);
  },
  createProject,
  pickFiles: () => $('file-input').click(),
  pickFolder: () => $('folder-input').click(),
  openPhoto: (d) => {
    const p = photoById(d.id);
    if (!p) return;
    if (p.status === 'need_geo') {
      S.geoOpen = true; S.geoPhotoId = p.id; setGeoStatus('');
      go('map');
      $('geo-lat').focus();
    } else if (p.zoneIds && p.zoneIds.length) {
      select(p.zoneIds[0]);
    } else {
      go('map');
      setTimeout(() => focusPhoto(p), 50);
    }
  },
  select: (d) => select(d.id),
  closeSel: () => { S.selected = null; render(); setTimeout(() => map && map.invalidateSize(), 30); },
  focusSel: () => focusZone(selectedZone()),
  openFrame: () => { const z = selectedZone(); if (z) openLightbox(z.photoUrl, `${z.label} · ${z.photoName}`); },
  verify: (d) => verify(d.v),
  filter: (d) => { S.filter = d.v; render(); },
  toggleClass: (d) => { S.hidden[d.v] = !S.hidden[d.v]; render(); },
  basemap: (d) => { S.basemap = d.v; lsSet('aeroroad.basemap', d.v); render(); },
  toggleLayer: (d) => { S.layers[d.v] = !S.layers[d.v]; render(); },
  theme: (d) => {
    S.theme = d.v;
    lsSet('aeroroad.theme', d.v);
    if (d.v === 'dark') document.documentElement.dataset.theme = 'dark';
    else delete document.documentElement.dataset.theme;
    render();
  },
  toggleAlert: (d) => patchSettings({ alerts: Object.assign({}, S.settings.alerts, { [d.v]: !S.settings.alerts[d.v] }) }),
  saveApiBase: () => { Api.setBase($('api-base').value.trim()); toast('Адрес сохранён, переподключаемся…'); boot(); },
  toggleGeo: () => { S.geoOpen = !S.geoOpen; S.pickMode = false; render(); },
  openGeo: () => { S.geoOpen = true; go('map'); },
  pickOnMap: () => { S.pickMode = true; S.geoOpen = true; go('map'); setGeoStatus('Кликните по карте в точке центра снимка.'); },
  applyGeo,
  reload: () => { toast('Обновляем данные…'); boot(); },
  fit: () => fitAll(),
  zoomIn: () => map && map.zoomIn(),
  zoomOut: () => map && map.zoomOut(),
  clearSearch,
  pickResult: (d) => {
    const r = S.results[+d.i];
    if (!r) return;
    if (r.kind === 'zone') { clearSearch(); select(r.id); return; }
    S.place = { name: r.name, lat: r.lat, lon: r.lon };
    S.results = null;
    renderResults();
    $('geo-lat').value = r.lat.toFixed(4);
    $('geo-lon').value = r.lon.toFixed(4);
    toast(`${r.name} · ${fmtCoord(r.lat, r.lon)} — координаты подставлены в ручную привязку`);
    syncMap();
    map.setView([r.lat, r.lon], 12);
  },
  closeLightbox: () => { $('lightbox').hidden = true; },
  closeToast: () => { $('toast').hidden = true; },
};

// Порог: 35 → 40 → … → 90 → 20 → 25 → …
ACTIONS.bumpThreshold = () => {
  const cur = Math.round(threshold() * 100);
  const next = cur >= 90 ? 20 : Math.floor(cur / 5) * 5 + 5;
  patchSettings({ minConfidence: next / 100 });
};

// ─── события ─────────────────────────────────────────────────────────────
document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-act]');
  if (!el) {
    if (S.projectMenu && !e.target.closest('.project')) { S.projectMenu = false; renderHeader(); }
    if (S.results && !e.target.closest('.search')) { S.results = null; renderResults(); }
    return;
  }
  const fn = ACTIONS[el.dataset.act];
  if (fn) { e.preventDefault(); fn(el.dataset); }
});

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (!$('lightbox').hidden) $('lightbox').hidden = true;
  else if (S.pickMode) { S.pickMode = false; setGeoStatus(''); render(); }
  else if (S.results) clearSearch();
});

$('new-project').addEventListener('keydown', (e) => { if (e.key === 'Enter') createProject(); });
$('search').addEventListener('input', (e) => onSearch(e.target.value));
$('overlay-opacity').addEventListener('input', (e) => {
  S.opacity = parseFloat(e.target.value);
  lsSet('aeroroad.opacity', String(S.opacity));
  $('overlay-opacity-v').textContent = Math.round(S.opacity * 100) + '%';
  photoOverlays.forEach((l) => l.setOpacity(S.opacity));
});
['file-input', 'folder-input'].forEach((id) => $(id).addEventListener('change', (e) => {
  handleFiles(e.target.files);
  e.target.value = '';
}));

// Перетаскивание снимков в любое место окна.
let dragDepth = 0;
window.addEventListener('dragenter', (e) => {
  if (!e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes('Files')) return;
  dragDepth++; $('drop-veil').hidden = false;
});
window.addEventListener('dragleave', () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $('drop-veil').hidden = true; });
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('drop', (e) => {
  e.preventDefault();
  dragDepth = 0;
  $('drop-veil').hidden = true;
  if (e.dataTransfer && e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
});

// ─── старт ───────────────────────────────────────────────────────────────
initMap();
render();
boot();
