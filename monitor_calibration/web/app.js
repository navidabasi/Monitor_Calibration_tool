'use strict';

/* ---------- helpers ---------- */

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (v, d = 0) => (v == null || !isFinite(v) ? '–' : Number(v).toFixed(d));

function load(key, fallback) {
  try {
    const v = JSON.parse(localStorage.getItem('mc.' + key));
    return v ?? fallback;
  } catch {
    return fallback;
  }
}
function save(key, val) {
  try { localStorage.setItem('mc.' + key, JSON.stringify(val)); } catch { /* storage unavailable */ }
}

const S = {
  layout: null,
  cfg: Object.assign({
    target: '', reference: '', hasRef: true, gamma: '2.2',
    refSpec: { kind: 'preset', name: 'srgb' },
    rgb: { r: 100, g: 100, b: 100, max: 100 },
    // Monitor details for the shared database. Nicknames above stay on this computer; only these fixed fields are used.
    targetMon: null, refMon: null, targetTech: 'unknown', refTech: 'unknown', targetMode: '', refMode: '',
  }, load('cfg', {})),
  checks: load('checks', {}),
  session: null,
  devices: [],        // monitors read from this computer (EDID), without serial numbers
  settings: null,     // { share: true | false | null, apiConfigured }
  shareKey: '',       // which set of photos has already been offered for sharing
  recipe: null,       // suggested settings for this monitor model and picture mode, when enough people agree
  settingsSent: '',   // which photos and values were already offered for sharing
  settingsMsg: '',
  shareResult: '',    // what happened when the last session was offered for sharing
  busy: [],
  srgb: new URLSearchParams(location.search).get('srgb') === '1',
};

const tgtLabel = () => S.cfg.target.trim() || 'Monitor to calibrate';
const refLabel = () => S.cfg.reference.trim() || 'Reference monitor';
const saveCfg = () => save('cfg', S.cfg);

function qs() {
  const ref = S.cfg.hasRef ? S.cfg.reference.trim() || 'the reference monitor' : '';
  const spec = S.cfg.hasRef ? `&refspec=${encodeURIComponent(JSON.stringify(S.cfg.refSpec))}` : '';
  return `target=${encodeURIComponent(S.cfg.gamma)}&ref=${encodeURIComponent(ref)}&tgt=${encodeURIComponent(S.cfg.target.trim())}${spec}&mon=${encodeURIComponent(JSON.stringify(monitorInfo()))}`;
}

// The fixed monitor fields sent to the local server (never the nickname typed in Setup).
function monitorInfo() {
  const c = S.cfg;
  const info = { target: { ...(c.targetMon || {}), screen_tech: c.targetTech } };
  if (c.targetMode) info.target.picture_mode = c.targetMode;
  if (c.hasRef) {
    info.reference = { ...(c.refMon || {}), screen_tech: c.refTech };
    if (c.refMode) info.reference.picture_mode = c.refMode;
  }
  return info;
}

function banner(kind, html) {
  const ico = { ok: '✓', warn: '!', bad: '✕' }[kind];
  return `<div class="banner ${kind}"><span class="ico">${ico}</span><p>${html}</p></div>`;
}

/* ---------- shots ---------- */

function shotList() {
  return [
    { n: 1, type: 'bright', role: 'reference', monitor: refLabel(), shutter: '1/60 s', name: 'Bright chart', skipped: !S.cfg.hasRef,
      purpose: 'Calibrates the camera against the known colors of your reference monitor.' },
    { n: 2, type: 'white', role: 'target', monitor: tgtLabel(), shutter: '1/60 s', name: 'White chart',
      purpose: 'Maps how the camera lens darkens toward the edges of the frame, so every patch can be corrected.' },
    { n: 3, type: 'bright', role: 'target', monitor: tgtLabel(), shutter: '1/60 s', name: 'Bright chart',
      purpose: 'Measures white point, gamma, gray tint and colors. After each adjustment, retake only this shot.' },
    { n: 4, type: 'dark', role: 'target', monitor: tgtLabel(), shutter: '1/4 s', name: 'Dark chart',
      purpose: 'Checks near-black detail. The slower shutter lets the dark patches show up.' },
  ];
}

function shotLabel(type, role) {
  const s = shotList().find((x) => x.type === type && x.role === role);
  if (s) return `Shot ${s.n} · ${s.name.toLowerCase()} on ${s.monitor}`;
  return `${type} chart on the ${role} monitor (not used)`;
}

/* ---------- viewfinder reference images ---------- */

const VF = { w: 240, h: 180, x: 80, y: 60, cw: 80 }; // the chart fills the middle grid square

const hex2 = (v) => Math.round(Math.max(0, Math.min(255, v))).toString(16).padStart(2, '0');
const rgbHex = (c) => '#' + c.map(hex2).join('');

// What a display code value looks like in the phone preview at a longer exposure.
function exposed(code, gain) {
  if (gain === 1) return code;
  return Math.pow(Math.min(Math.pow(code / 255, 2.2) * gain, 1), 1 / 2.2) * 255;
}

function chartShapes(type, role) {
  const L = S.layout;
  const s = VF.cw / L.width;
  const c = L.charts[type];
  const gain = type === 'dark' ? 15 : 1; // 1/4 s vs 1/60 s
  const mark = rgbHex([0, 0, 0].map(() => exposed(c.mark, gain)));
  const rect = (x, y, w, h, fill) =>
    `<rect x="${(VF.x + x * s).toFixed(2)}" y="${(VF.y + y * s).toFixed(2)}" width="${(w * s).toFixed(2)}" height="${(h * s).toFixed(2)}" fill="${fill}"/>`;
  let out = '';
  L.fiducials.forEach(([x, y]) => (out += rect(x, y, L.fiducialSize, L.fiducialSize, mark)));
  const bits = L.codes[`${type}:${role}`];
  L.bits.forEach(([x, y, w, h], i) => { if (bits[i]) out += rect(x, y, w, h, mark); });
  L.patches.forEach(([x, y, w, h], i) => (out += rect(x, y, w, h, rgbHex(c.colors[i].map((v) => exposed(v, gain))))));
  return out;
}

function viewfinder(type, role, o = {}) {
  const tf = o.transform ? ` transform="${o.transform}"` : '';
  let s = `<svg viewBox="0 0 ${VF.w} ${VF.h}" role="img" aria-label="${esc(o.label || 'Phone camera preview with the chart in the middle grid square')}">`;
  s += `<rect width="${VF.w}" height="${VF.h}" fill="#141414"/>`;
  s += `<rect x="13" y="30" width="214" height="120" rx="2" fill="#030303" stroke="#2e2e2e" stroke-width="2"/>`;
  s += `<g${tf}>${chartShapes(type, role)}</g>`;
  if (o.glare) s += `<ellipse cx="72" cy="72" rx="48" ry="26" fill="#fff" opacity=".16"/><ellipse cx="64" cy="66" rx="15" ry="7" fill="#fff" opacity=".4"/>`;
  s += `<g stroke="#fff" stroke-opacity=".5" stroke-width=".6"><line x1="80" y1="0" x2="80" y2="180"/><line x1="160" y1="0" x2="160" y2="180"/><line x1="0" y1="60" x2="240" y2="60"/><line x1="0" y1="120" x2="240" y2="120"/></g>`;
  if (o.hist) {
    s += `<rect x="182" y="148" width="52" height="26" rx="2" fill="#000" opacity=".7"/>`;
    [2, 3, 2, 2, 3, 4, 3, 2, 3, 20].forEach((hh, i) => {
      s += `<rect x="${185 + i * 4.7}" y="${172 - hh}" width="3.6" height="${hh}" fill="${i === 9 ? '#ff5a4a' : '#cfcfcf'}"/>`;
    });
  }
  if (o.shutter) {
    s += `<rect width="${VF.w}" height="15" fill="#000" opacity=".65"/>`;
    s += `<text x="6" y="10.5" fill="#f2f2f2" font-size="9" font-family="Segoe UI, system-ui, sans-serif">RAW · ISO min · ${o.shutter} · focus ∞ · grid on</text>`;
  }
  return s + '</svg>';
}

/* ---------- full-screen chart ---------- */

let shown = null;

function drawChart() {
  if (!shown) return;
  const canvas = $('#chart-canvas');
  const dpr = window.devicePixelRatio || 1;
  const W = Math.round(window.innerWidth * dpr);
  const H = Math.round(window.innerHeight * dpr);
  canvas.width = W;
  canvas.height = H;
  canvas.style.width = window.innerWidth + 'px';
  canvas.style.height = window.innerHeight + 'px';
  const ctx = canvas.getContext('2d', { alpha: false });
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, W, H);

  const L = S.layout;
  const c = L.charts[shown.type];
  const cw = Math.round(Math.min(0.4 * W, 0.53 * H * (L.width / L.height)));
  const s = cw / L.width;
  const ox = Math.round((W - cw) / 2);
  const oy = Math.round((H - L.height * s) / 2);
  // Snap every rectangle to whole device pixels so edges are crisp and colors exact.
  const box = (x, y, w, h, rgb) => {
    const x0 = Math.round(ox + x * s), y0 = Math.round(oy + y * s);
    const x1 = Math.round(ox + (x + w) * s), y1 = Math.round(oy + (y + h) * s);
    ctx.fillStyle = `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
    ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
  };
  const mark = [c.mark, c.mark, c.mark];
  L.fiducials.forEach(([x, y]) => box(x, y, L.fiducialSize, L.fiducialSize, mark));
  L.codes[`${shown.type}:${shown.role}`].forEach((bit, i) => {
    if (bit) {
      const [x, y, w, h] = L.bits[i];
      box(x, y, w, h, mark);
    }
  });
  L.patches.forEach(([x, y, w, h], i) => box(x, y, w, h, c.colors[i]));
}

function showChart(type, role) {
  shown = { type, role };
  noteScreen(role);
  const ov = $('#chart-overlay');
  const hint = $('#chart-hint');
  ov.hidden = false;
  document.documentElement.style.overflow = 'hidden';
  hint.style.display = '';
  hint.classList.remove('gone');
  drawChart();
  const req = ov.requestFullscreen ? ov.requestFullscreen({ navigationUI: 'hide' }) : Promise.reject(new Error('no fullscreen'));
  req.catch(() => {}).finally(() => setTimeout(drawChart, 60));
  clearTimeout(showChart.timer);
  // The hint must be gone before the photo is taken.
  showChart.timer = setTimeout(() => {
    hint.classList.add('gone');
    setTimeout(() => { hint.style.display = 'none'; }, 700);
  }, 2500);
}

function hideChart() {
  shown = null;
  $('#chart-overlay').hidden = true;
  document.documentElement.style.overflow = '';
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
}

document.addEventListener('fullscreenchange', () => { if (!document.fullscreenElement && shown) hideChart(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && shown) hideChart(); });
window.addEventListener('resize', drawChart);
$('#chart-overlay').addEventListener('dblclick', hideChart);

/* ---------- page: setup ---------- */

function renderSetup() {
  const c = S.cfg;
  $('#setup').innerHTML = `
    <h1>Calibrate your monitor with your phone</h1>
    <p class="lead">You take four RAW photos of test charts with your phone. The analyzer measures white point, gamma, gray tint and near-black detail, then tells you which monitor settings to change.</p>
    <div class="grid-2" style="margin-top:24px">
      <div class="card">
        <h3 style="margin-bottom:16px">Your monitors</h3>
        <label class="field"><span>Monitor to calibrate</span>
          <input id="cfg-target" type="text" placeholder="ASUS XG27AQDMES" value="${esc(c.target)}"></label>
        ${monitorFields('target')}
        <div id="recipe-hint"></div>
        <div class="field" style="margin-bottom:16px"><span class="muted small" style="display:block;margin-bottom:6px">Do you have a reference monitor?</span>
          <div class="choice">
            <label><input type="radio" name="hasref" value="1" ${c.hasRef ? 'checked' : ''}> Yes, a factory-calibrated one</label>
            <label><input type="radio" name="hasref" value="0" ${c.hasRef ? '' : 'checked'}> No</label>
          </div>
        </div>
        <div id="ref-field" ${c.hasRef ? '' : 'hidden'}>
          <label class="field"><span>Reference monitor</span>
            <input id="cfg-ref" type="text" placeholder="Dell U2722DE" value="${esc(c.reference)}"></label>
          ${monitorFields('ref')}
          <div id="ref-profile-hint"></div>
          <label class="field"><span>What it shows</span>
            <select id="ref-kind">
              <option value="srgb">sRGB (factory-calibrated sRGB or standard mode)</option>
              <option value="p3">Display P3 (calibrated P3 mode)</option>
              <option value="adobe">Adobe RGB (calibrated Adobe RGB mode)</option>
              <option value="icc">Its ICC profile…</option>
              <option value="custom">Values from a calibration report</option>
            </select></label>
          <div id="ref-icc" class="field" hidden>
            <button id="icc-pick" type="button">Choose ICC profile (.icc, .icm)</button>
            <input type="file" id="icc-file" accept=".icc,.icm" hidden>
            <div id="icc-info" class="small" style="margin-top:8px"></div>
          </div>
          <div id="ref-custom" class="field" hidden>
            <div class="xy-grid">
              <span></span><span class="muted small">x</span><span class="muted small">y</span>
              ${['Red', 'Green', 'Blue', 'White'].map((n, i) => `<span class="small">${n}</span>
                <input type="number" step="0.0001" min="0" max="1" data-xy="${i}:0">
                <input type="number" step="0.0001" min="0" max="1" data-xy="${i}:1">`).join('')}
            </div>
            <div id="custom-err" class="small" style="color:var(--bad);margin-top:6px"></div>
          </div>
        </div>
        <p class="small muted" style="margin:0">A reference is a monitor whose colors are known, usually one calibrated to sRGB at the factory (many Dell UltraSharp, ASUS ProArt and BenQ SW models). If you measured it with a colorimeter, or downloaded an ICC profile for your exact model from a review site such as RTINGS, load that profile instead. A profile only fits the picture mode it was made in. Without a reference, white point and colors are rough estimates, but gamma and near-black are still measured well.</p>
      </div>
      <div class="card">
        <h3 style="margin-bottom:8px">What you need</h3>
        <ul class="muted" style="margin:0 0 16px;padding-left:20px">
          <li>A phone with a manual camera app that saves RAW (DNG). The next step lists free ones.</li>
          <li>A tripod or a stack of books.</li>
          <li>A room you can make dark.</li>
          <li>About 45 minutes, including a 30-minute warm-up.</li>
        </ul>
        <h3 style="margin-bottom:8px">How it works</h3>
        <ol class="muted" style="margin:0;padding-left:20px">
          <li>Prepare the room, the monitor, Windows and your phone.</li>
          <li>Show each chart full screen and photograph it.</li>
          <li>Drop the photos here to get measurements and the settings to change.</li>
          <li>Adjust the monitor, retake one photo, and repeat until it's on target.</li>
        </ol>
      </div>
    </div>
    ${S.srgb
      ? banner('ok', 'This browser window has color management turned off, so the charts reach your monitor unchanged.')
      : banner('warn', 'This window may adjust the chart colors. Close it and start the site with <code>python calibrate.py</code>, which opens a browser window with color management turned off.')}
    <div class="row next"><span class="spacer"></span><button class="primary" data-go="prep">Next: prepare</button></div>`;

  $('#cfg-target').addEventListener('input', (e) => { c.target = e.target.value; saveCfg(); });
  $('#cfg-ref').addEventListener('input', (e) => { c.reference = e.target.value; saveCfg(); });
  $$('input[name="hasref"]').forEach((r) => r.addEventListener('change', (e) => {
    c.hasRef = e.target.value === '1';
    $('#ref-field').hidden = !c.hasRef;
    saveCfg();
  }));
  wireRefSpec();
  wireMonitorFields();
  showRecipe('#recipe-hint');
  if (c.hasRef) showRefProfile();
}

/* ---------- monitor details (Setup) ---------- */

const TECHS = [
  ['unknown', 'I don’t know'],
  ['woled', 'OLED with white subpixels (WOLED)'],
  ['qdoled', 'QD-OLED'],
  ['oled_rgb', 'OLED with RGB stripes (phones, tablets, some laptops)'],
  ['ips_ksf', 'Wide-gamut IPS (KSF red phosphor LED)'],
  ['ips_wled', 'IPS with standard white LED'],
  ['va_qd', 'VA with quantum dots'],
  ['va_wled', 'VA with standard white LED'],
  ['mini_led_qd', 'Mini-LED with quantum dots'],
];
const MODES = [['', 'Not sure'], ['srgb', 'sRGB'], ['standard', 'Standard'], ['user', 'User'], ['native', 'Native / wide gamut'],
  ['cinema', 'Cinema'], ['game', 'Game'], ['other', 'Other']];
const devKey = (d) => (d ? `${d.manufacturer}:${d.product_code}` : '');
const devName = (d) => d.model_name || `Monitor ${d.manufacturer}`;

// Details about one monitor: which detected one it is, and what kind of screen it has. `role` is 'target' or 'ref'.
function monitorFields(role) {
  const c = S.cfg;
  const mon = role === 'target' ? c.targetMon : c.refMon;
  const tech = role === 'target' ? c.targetTech : c.refTech;
  const detected = S.devices.length
    ? `<label class="field"><span>Detected on this computer</span>
        <select data-det="${role}"><option value="">Not listed</option>
          ${S.devices.map((d) => `<option value="${devKey(d)}" ${devKey(d) === devKey(mon) ? 'selected' : ''}>${esc(devName(d))}${d.resolution ? ` · ${d.resolution.join('×')}` : ''}</option>`).join('')}
        </select></label>` : '';
  const modeNow = role === 'target' ? c.targetMode : c.refMode;
  const mode = `<label class="field"><span>${role === 'target' ? 'Picture mode you’ll measure in' : 'Picture mode it’s in'}</span>
        <select data-mode="${role}">${MODES.map(([v, l]) => `<option value="${v}" ${modeNow === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>`;
  return `<div class="mon-fields">${detected}
    <label class="field"><span>Screen type</span>
      <select data-tech="${role}">${TECHS.map(([v, l]) => `<option value="${v}" ${tech === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
    ${mode}</div>`;
}

function wireMonitorFields() {
  const c = S.cfg;
  $$('[data-det]').forEach((sel) => sel.addEventListener('change', () => {
    const role = sel.dataset.det;
    const d = S.devices.find((x) => devKey(x) === sel.value) || null;
    chooseMonitor(role, d);
    if (role === 'target') showRecipe('#recipe-hint'); else showRefProfile();
    const input = $(role === 'target' ? '#cfg-target' : '#cfg-ref');
    if (d && !input.value.trim()) { input.value = devName(d); c[role === 'target' ? 'target' : 'reference'] = input.value; }
    saveCfg();
  }));
  $$('[data-tech]').forEach((sel) => sel.addEventListener('change', () => {
    c[sel.dataset.tech === 'target' ? 'targetTech' : 'refTech'] = sel.value;
    saveCfg();
  }));
  $$('[data-mode]').forEach((mode) => mode.addEventListener('change', () => {
    const target = mode.dataset.mode === 'target';
    c[target ? 'targetMode' : 'refMode'] = mode.value;
    saveCfg();
    if (target) showRecipe('#recipe-hint'); else showRefProfile();
  }));
}

// Remember a detected monitor's fixed identity fields (never its serial number, which the server never sends).
function chooseMonitor(role, d) {
  const key = role === 'target' ? 'targetMon' : 'refMon';
  S.cfg[key] = d ? { manufacturer: d.manufacturer, product_code: d.product_code, model_name: d.model_name, year: d.year,
    chromaticity: d.chromaticity, resolution: d.resolution } : null;
}

async function loadDevices() {
  try {
    S.devices = (await api('/api/devices')).monitors || [];
  } catch { S.devices = []; }
  // Keep what was chosen only if that monitor is still connected.
  for (const [role, key] of [['target', 'targetMon'], ['ref', 'refMon']]) {
    if (S.cfg[key] && !S.devices.some((d) => devKey(d) === devKey(S.cfg[key]))) S.cfg[key] = null;
  }
  // One monitor connected and none chosen yet: the obvious pick for the monitor to calibrate.
  if (S.devices.length === 1 && !S.cfg.targetMon && !S.cfg.hasRef) chooseMonitor('target', S.devices[0]);
  saveCfg();
  if (!location.hash || location.hash === '#setup') renderSetup();
}

// Which of the connected monitors a screen (from the Window Management API) is.
function matchScreen(screen) {
  const label = (screen.label || '').toLowerCase();
  const byName = S.devices.filter((d) => d.model_name && label.includes(d.model_name.toLowerCase()));
  if (byName.length === 1) return byName[0];
  const dpr = screen.devicePixelRatio || 1;
  const size = [Math.round(screen.width * dpr), Math.round(screen.height * dpr)];
  const bySize = S.devices.filter((d) => d.resolution && d.resolution[0] === size[0] && d.resolution[1] === size[1]);
  return bySize.length === 1 ? bySize[0] : null;
}

// When a chart is shown, note which monitor it's on (one permission prompt). It fills in a monitor the user hasn't chosen.
async function noteScreen(role) {
  if (!window.getScreenDetails || !S.devices.length) return;
  try {
    const details = await window.getScreenDetails();
    const d = matchScreen(details.currentScreen);
    const key = role === 'reference' ? 'refMon' : 'targetMon';
    if (d && !S.cfg[key]) {
      chooseMonitor(role === 'reference' ? 'ref' : 'target', d);
      const nick = role === 'reference' ? 'reference' : 'target';
      if (!S.cfg[nick].trim()) S.cfg[nick] = devName(d);
      saveCfg();
    }
  } catch { /* permission declined or not supported: the user picks in Setup */ }
}

const PRESET_XY = {
  srgb: [[0.64, 0.33], [0.30, 0.60], [0.15, 0.06], [0.3127, 0.3290]],
  p3: [[0.680, 0.320], [0.265, 0.690], [0.150, 0.060], [0.3127, 0.3290]],
  adobe: [[0.64, 0.33], [0.21, 0.71], [0.15, 0.06], [0.3127, 0.3290]],
};

function specKind(spec) {
  if (spec.pendingIcc) return 'icc';
  if (spec.kind === 'custom') return spec.source === 'icc' ? 'icc' : 'custom';
  return spec.name in PRESET_XY ? spec.name : 'srgb';
}

function specPoints(spec) {
  return spec.kind === 'custom' ? [...spec.primaries, spec.white] : PRESET_XY[spec.name] || PRESET_XY.srgb;
}

function wireRefSpec() {
  const c = S.cfg;
  const kind = $('#ref-kind');
  const showParts = () => {
    $('#ref-icc').hidden = kind.value !== 'icc';
    $('#ref-custom').hidden = kind.value !== 'custom';
  };
  const showIcc = () => {
    const s = c.refSpec;
    $('#icc-info').innerHTML = s.kind === 'custom' && s.source === 'icc'
      ? `Using <b>${esc(s.name)}</b>: white ${esc(s.cct)} K · R ${s.primaries[0].join(', ')} · G ${s.primaries[1].join(', ')} · B ${s.primaries[2].join(', ')}`
      : '<span class="muted">No profile loaded yet. The reference is treated as sRGB until you load one.</span>';
  };
  const fillXY = () => {
    const pts = specPoints(c.refSpec);
    $$('[data-xy]').forEach((inp) => {
      const [i, j] = inp.dataset.xy.split(':').map(Number);
      inp.value = pts[i][j];
    });
  };

  kind.value = specKind(c.refSpec);
  showParts();
  showIcc();
  fillXY();

  kind.addEventListener('change', () => {
    if (kind.value in PRESET_XY) c.refSpec = { kind: 'preset', name: kind.value };
    else if (kind.value === 'icc' && c.refSpec.source !== 'icc') c.refSpec = { kind: 'preset', name: 'srgb', pendingIcc: true };
    else if (kind.value === 'custom') c.refSpec = { kind: 'custom', source: 'manual', name: 'values from a calibration report', primaries: specPoints(c.refSpec).slice(0, 3), white: specPoints(c.refSpec)[3] };
    saveCfg();
    showParts();
    showIcc();
    fillXY();
  });

  $('#icc-pick').addEventListener('click', () => $('#icc-file').click());
  $('#icc-file').addEventListener('change', async (e) => {
    const f = e.target.files[0];
    e.target.value = '';
    if (!f) return;
    try {
      const p = await api('/api/icc', { method: 'POST', body: f });
      c.refSpec = { kind: 'custom', source: 'icc', name: p.name || f.name, primaries: p.primaries, white: p.white, cct: p.cct };
      saveCfg();
      showIcc();
      fillXY();
    } catch (err) {
      $('#icc-info').innerHTML = `<span style="color:var(--bad)">${esc(err.message)}</span>`;
    }
  });

  $$('[data-xy]').forEach((inp) => inp.addEventListener('input', () => {
    const pts = specPoints(c.refSpec).map((p) => [...p]);
    $$('[data-xy]').forEach((el) => {
      const [i, j] = el.dataset.xy.split(':').map(Number);
      pts[i][j] = parseFloat(el.value);
    });
    const ok = pts.every(([x, y]) => x > 0 && x < 0.8 && y > 0 && y < 0.9);
    $('#custom-err').textContent = ok ? '' : 'Each x and y should be between 0 and 0.9.';
    if (!ok) return;
    c.refSpec = { kind: 'custom', source: 'manual', name: 'values from a calibration report', primaries: pts.slice(0, 3), white: pts[3] };
    saveCfg();
  }));
}

/* ---------- page: prepare ---------- */

function checklist() {
  return [
    { group: 'Room', items: [
      { id: 'dark-room', t: 'Make the room dark', d: 'Close the curtains and turn off lights and LED strips. A glossy screen shows every light source as a reflection.' },
      { id: 'phone-dim', t: 'Dim your phone screen', d: 'Its glow reflects off the monitor too.' },
    ] },
    { group: tgtLabel(), items: [
      { id: 'note-settings', t: 'Write down your current settings', d: 'Picture mode, brightness, color temperature and RGB values, so you can always go back.' },
      { id: 'srgb-mode', t: 'Choose the sRGB picture mode', d: 'Wide-gamut monitors, including most OLEDs, make normal content look oversaturated in other modes. On ASUS ROG monitors it’s GameVisual → sRGB.' },
      { id: 'uniform', tag: 'OLED', t: 'Turn on Uniform Brightness', d: 'It stops the screen dimming when the bright area changes size, which would make readings jump. On ASUS ROG OLEDs it only works in sRGB mode. Other brands may name it differently or not have it.' },
      { id: 'oled-care', tag: 'OLED', t: 'Pause OLED care features', d: 'Turn off the screen saver and static-image or logo dimming until you finish. If a pixel-cleaning cycle is due, run it before you start, not during.' },
      { id: 'rgb-controls', t: 'Find the white balance controls', d: 'Look for Color temperature → User (or Custom) with Red, Green and Blue gain. Some sRGB modes lock them. You can still measure, and the results will say whether the mode is accurate.' },
    ] },
    { group: refLabel(), ref: true, items: [
      { id: 'ref-reset', t: 'Reset it to its calibrated mode', d: 'Factory-reset it from its menu, then choose the mode its factory calibration applies to (Standard on Dell UltraSharp monitors). After that, change only its brightness.' },
    ] },
    { group: 'Windows', items: [
      { id: 'hdr-off', t: 'Turn HDR off', d: 'Settings → System → Display → select the monitor → Use HDR: off.' },
      { id: 'night-light', t: 'Turn off Night light', d: 'Settings → System → Display → Night light.' },
      { id: 'acm-off', t: 'Turn off Automatically manage color for apps', d: 'Settings → System → Display → Color profile (or Advanced display, depending on your Windows version). It would correct colors a second time on top of sRGB mode.' },
      { id: 'icc-default', t: 'Remove custom color profiles', d: 'Use the monitor’s default profile while you measure, not a downloaded or calibrated ICC profile.' },
      { id: 'awake', t: 'Keep the screens on', d: 'Settings → System → Power: set the screen to turn off after Never for this session, and turn off any screensaver.' },
    ] },
    { group: 'Graphics driver', items: [
      { id: 'nv-range', tag: 'NVIDIA', t: 'Full-range RGB output', d: 'NVIDIA Control Panel → Change resolution → Use NVIDIA color settings: Output color format RGB, Output dynamic range Full. Limited range loses dark detail and dulls whites.' },
      { id: 'nv-color', tag: 'NVIDIA', t: 'No color tweaks', d: 'Adjust desktop color settings: defaults, Digital Vibrance 50%. In the NVIDIA app, turn off Filters, RTX Dynamic Vibrance and RTX HDR.' },
      { id: 'amd-range', tag: 'AMD', t: 'Full-range RGB output', d: 'AMD Software → Display: Custom Color off, Pixel Format RGB 4:4:4 Full RGB.' },
    ] },
    { group: 'Before you start', items: [
      { id: 'browser', auto: S.srgb, t: 'Open this site with calibrate.py', d: 'It opens a browser window with color management turned off, so the charts reach the monitor unchanged.' },
      { id: 'warmup', t: 'Warm up the monitors for 30 minutes', d: 'Leave them on, showing normal content. Brightness and color drift while they warm up.' },
    ] },
    { group: 'Phone and camera app', items: [
      { id: 'app', t: 'Install a manual camera app that saves RAW', d: 'Android: Open Camera (free; turn on Camera2 API in its settings), or your camera’s Pro mode if it saves RAW/DNG. iPhone or Android: the camera inside Adobe Lightroom mobile (free), in Professional mode. The iPhone’s own Camera app can’t lock ISO and shutter speed.' },
      { id: 'lens', t: 'Use the main 1× lens', d: 'On iPhone Pro models, turn off Settings → Camera → Macro Control so the phone doesn’t switch lenses up close.' },
      { id: 'settings', t: 'Lock every camera setting', d: 'RAW/DNG on, lowest ISO, shutter 1/60 s, white balance Daylight, focus set by hand to infinity (a slight blur is right), and HDR, night mode, scene AI and stabilization off.' },
      { id: 'grid', t: 'Turn on the grid and a timer', d: 'The 3×3 grid lines frame the chart. A 3-second timer keeps you from shaking the phone.' },
      { id: 'mount', t: 'Mount the phone in landscape', d: 'Use a tripod or a stack of books. Put the lens level with the middle of the screen, pointing straight at it.' },
    ] },
  ].filter((g) => !g.ref || S.cfg.hasRef);
}

function renderPrep() {
  const groups = checklist();
  const items = groups.flatMap((g) => g.items);
  items.forEach((it) => { if (it.auto) S.checks[it.id] = true; });
  const done = () => items.filter((it) => S.checks[it.id]).length;

  $('#prep').innerHTML = `
    <h1>Prepare</h1>
    <p class="lead">Tick each item as you go. If something doesn’t apply to your setup, tick it anyway.</p>
    <div class="progress"><div class="bar"><div id="prep-bar"></div></div><span class="muted small" id="prep-count"></span></div>
    ${groups.map((g) => `
      <div class="group"><h2>${esc(g.group)}</h2>
        ${g.items.map((it) => `
          <label class="check ${S.checks[it.id] ? 'done' : ''}">
            <input type="checkbox" data-id="${it.id}" ${S.checks[it.id] ? 'checked' : ''}>
            <div><div class="t">${esc(it.t)}${it.tag ? `<span class="tag">${esc(it.tag)}</span>` : ''}</div><div class="d">${esc(it.d)}</div></div>
          </label>`).join('')}
      </div>`).join('')}
    <div class="row next"><button class="quiet" data-go="setup">Back</button><span class="spacer"></span><button class="primary" data-go="photos">Next: take photos</button></div>`;

  const update = () => {
    const n = done();
    $('#prep-bar').style.width = `${(n / items.length) * 100}%`;
    $('#prep-count').textContent = `${n} of ${items.length} done`;
  };
  $$('#prep input[type="checkbox"]').forEach((cb) => cb.addEventListener('change', () => {
    S.checks[cb.dataset.id] = cb.checked;
    cb.closest('.check').classList.toggle('done', cb.checked);
    save('checks', S.checks);
    update();
  }));
  update();
}

/* ---------- page: photos ---------- */

function renderPhotos() {
  const shots = shotList();
  const shotCard = (s) => `
    <div class="card shot ${s.skipped ? 'skipped' : ''}">
      <div class="vf">${viewfinder(s.type, s.role, { shutter: s.shutter })}</div>
      <div>
        <h3>Shot ${s.n} <span class="pill">${esc(s.monitor)}</span></h3>
        <p class="muted">${esc(s.purpose)}</p>
        <div class="settings"><span class="chip on">${esc(s.name)}</span><span class="chip on">Shutter ${s.shutter}</span></div>
        ${s.skipped
          ? '<p class="small muted" style="margin-top:12px">Skipped, because you don’t have a reference monitor. You can change that in Setup.</p>'
          : `<ol>
              <li>Move this window to <b>${esc(s.monitor)}</b>.</li>
              ${s.type === 'dark' ? '<li>Change only the shutter speed, to <b>1/4 s</b>. Switch it back to 1/60 s afterwards.</li>' : ''}
              ${s.role === 'reference' ? `<li>If ${esc(s.monitor)} stands in portrait, check the chart looks upright on it (Windows display orientation: Portrait), and still hold the phone in <b>landscape</b>.</li>
              <li>Reference monitors are often brighter. If the result says overexposed, retake at <b>1/120 s</b>, or lower only that monitor’s brightness.</li>` : ''}
              <li>Click <b>Show chart</b>. It fills the screen.</li>
              <li>Line up the corner squares with the middle grid square, then take the photo with the timer.</li>
              <li>Press <span class="kbd">Esc</span> to come back.</li>
            </ol>
            <button class="primary" data-chart="${s.type}:${s.role}">Show chart</button>`}
      </div>
    </div>`;

  $('#photos').innerHTML = `
    <h1>Take the photos</h1>
    <p class="lead">Show each chart on the right monitor and photograph it in RAW. Every chart carries an ID code, so the order and file names don’t matter.</p>
    <div class="card" style="margin:24px 0">
      <h3 style="margin-bottom:6px">Camera settings</h3>
      <p class="muted">Set these once and keep them for every shot. Only the shutter speed changes, as each shot says.</p>
      <div class="table-wrap"><table class="settings-table">
        ${[
          ['Mode', 'Professional / Pro / Manual. Not Auto, HDR or Night.'],
          ['File format', 'RAW (DNG). In Lightroom, tap the format at the top until it says DNG.'],
          ['Lens', 'Main 1× lens only. Not 0.5×, 2× or 5×.'],
          ['ISO', 'The lowest it goes (64 on recent iPhones).'],
          ['Shutter speed', '1/60 s. Shot 4: 1/4 s. If a shot says overexposed: 1/120 s.'],
          ['White balance', 'Daylight preset (or a fixed 5500 K). Never Auto.'],
          ['Focus', 'Manual, slid all the way to infinity (∞ or the mountain icon). Slight blur is right.'],
          ['Exposure compensation', '0'],
          ['Flash, HDR, Night mode', 'Off'],
          ['Grid', '3×3 grid on'],
          ['Timer', '2–5 seconds, so the phone is still when it fires'],
          ['Highlight clipping warning', 'On, if the app has it. If the white patches flash, use 1/120 s.'],
          ['Phone orientation', 'Landscape (sideways) for every shot, even if a monitor stands in portrait'],
        ].map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('')}
      </table></div>
      <h3 style="margin:20px 0 6px">How to frame every shot</h3>
      <p class="muted" style="margin:0">Move the phone until the chart’s four corner squares sit on the corners of the middle grid square. That keeps the distance, centering and angle the same for every shot, on both monitors. The monitor itself can extend past the edges of the frame.</p>
    </div>
    ${shotCard(shots[0])}
    ${S.cfg.hasRef ? `<div class="move-note">Move the phone to ${esc(tgtLabel())} and frame it the same way</div>` : ''}
    ${shots.slice(1).map(shotCard).join('')}
    <h2>Retake the shot if it looks like one of these</h2>
    <div class="mistakes">
      ${[
        ['Tilted', 'The corners miss the grid.', { transform: 'rotate(-9 120 90) translate(8 -6)', label: 'Tilted chart with corners off the grid' }],
        ['Too close', 'The chart spills past the middle square.', { transform: 'translate(120 90) scale(1.5) translate(-120 -90)', label: 'Chart too large for the middle grid square' }],
        ['Light on', 'Reflections on the glossy screen.', { glare: true, label: 'Reflection of a room light on the screen' }],
        ['Too bright', 'The histogram piles up on the right. Use 1/120 s.', { hist: true, label: 'Histogram piled up at the right edge' }],
      ].map(([t, d, o]) => `<div class="mistake">${viewfinder('bright', 'target', o)}<div class="t">${t}</div><div class="d">${d}</div></div>`).join('')}
    </div>
    <div class="row next"><button class="quiet" data-go="prep">Back</button><span class="spacer"></span><button class="primary" data-go="results">Next: add photos</button></div>`;

  $$('#photos [data-chart]').forEach((b) => b.addEventListener('click', () => {
    const [type, role] = b.dataset.chart.split(':');
    showChart(type, role);
  }));
}

/* ---------- page: results ---------- */

function renderResults() {
  $('#results').innerHTML = `
    <h1>Results</h1>
    <p class="lead">Copy the RAW files from your phone with a USB cable, then drop them here. Messaging apps and some cloud uploads convert or strip RAW files.</p>
    <div class="drop" id="drop">
      <p>Drop RAW photos (DNG) here</p>
      <button id="pick">Choose files</button>
      <input type="file" id="file" multiple hidden accept=".dng,.DNG">
    </div>
    <div id="busy"></div>
    <div id="results-body"></div>`;

  const drop = $('#drop');
  $('#pick').addEventListener('click', () => $('#file').click());
  $('#file').addEventListener('change', (e) => { upload([...e.target.files]); e.target.value = ''; });
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', (e) => {
    e.preventDefault();
    drop.classList.remove('over');
    upload([...e.dataTransfer.files]);
  });
  renderBusy();
  renderResultsBody();
  refresh();
  loadRecipe().then(renderResultsBody);
}

// Per-launch token the local server writes into index.html; it rejects API calls without it.
const TOKEN = document.querySelector('meta[name="mc-token"]')?.content || '';

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { ...(opts.headers || {}), 'X-MC-Token': TOKEN } });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

async function refresh() {
  try {
    S.session = await api(`/api/session?${qs()}`);
    renderResultsBody();
  } catch (e) {
    $('#results-body').innerHTML = banner('bad', `Couldn’t reach the analyzer: ${esc(e.message)}. Is <code>calibrate.py</code> still running?`);
  }
}

async function upload(files) {
  if (!files.length) return;
  S.busy.push(...files.map((f) => f.name));
  renderBusy();
  for (const f of files) {
    try {
      S.session = await api(`/api/upload?name=${encodeURIComponent(f.name)}&${qs()}`, { method: 'POST', body: f });
    } catch (e) {
      alert(`${f.name}: ${e.message}`);
    }
    S.busy.shift();
    renderBusy();
    renderResultsBody();
  }
}

function renderBusy() {
  const el = $('#busy');
  if (el) el.innerHTML = S.busy.length ? `<div class="busy">Analyzing ${esc(S.busy[0])}${S.busy.length > 1 ? ` (${S.busy.length - 1} more waiting)` : ''}…</div>` : '';
}

const shutterText = (t) => (!t ? '' : t >= 1 ? `${fmt(t, t < 10 ? 1 : 0)} s` : `1/${Math.round(1 / t)} s`);

function photoRow(p) {
  const what = p.error ? 'Couldn’t use this photo' : p.type ? shotLabel(p.type, p.role) : 'Unknown chart: pick which shot it is';
  const exp = [shutterText(p.shutter), p.iso ? `ISO ${Math.round(p.iso)}` : ''].filter(Boolean).join(' · ');
  const opts = [
    ['auto', p.detected ? 'Detected automatically' : 'Pick a shot…'],
    ['bright:reference', 'Shot 1 · bright, reference'],
    ['white:target', 'Shot 2 · white'],
    ['bright:target', 'Shot 3 · bright'],
    ['dark:target', 'Shot 4 · dark'],
  ];
  const cur = p.assigned ? `${p.type}:${p.role}` : 'auto';
  return `
    <div class="photo">
      <div>
        <div class="what ${p.error ? 'err' : ''}">${esc(what)}</div>
        <div class="name">${esc(p.name)}${exp ? ' · ' + esc(exp) : ''}</div>
        ${p.error ? `<p class="small muted" style="margin:6px 0 0">${esc(p.error)}</p>` : ''}
        ${p.warnings.length ? `<ul>${p.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
      </div>
      <div class="actions">
        ${p.error ? '' : `<select data-assign="${p.id}" aria-label="Which shot is this">${opts.map(([v, l]) => `<option value="${v}" ${v === cur ? 'selected' : ''}>${l}</option>`).join('')}</select>`}
        <button class="small-btn quiet" data-del="${p.id}">Remove</button>
      </div>
    </div>`;
}

const cls = (v, okMax, warnMax) => (v == null ? '' : v <= okMax ? 'ok' : v <= warnMax ? 'warn' : 'bad');

function metric(k, v, s, state) {
  return `<div class="metric ${state || ''}"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
}

function lineChart({ xs, series, yMin, yMax, ticks, fmtTick = (v) => v }) {
  const w = 520, h = 250, pl = 46, pr = 14, pt = 12, pb = 30;
  const X = (x) => pl + ((x - xs[0]) / (xs[xs.length - 1] - xs[0])) * (w - pl - pr);
  const Y = (y) => pt + (1 - (y - yMin) / (yMax - yMin)) * (h - pt - pb);
  let s = `<svg viewBox="0 0 ${w} ${h}" role="img">`;
  ticks.forEach((t) => {
    s += `<line x1="${pl}" x2="${w - pr}" y1="${Y(t)}" y2="${Y(t)}" stroke="#2b2b2b"/>`;
    s += `<text x="${pl - 8}" y="${Y(t) + 4}" fill="#8a8a8a" font-size="11" text-anchor="end">${fmtTick(t)}</text>`;
  });
  xs.forEach((x) => { s += `<text x="${X(x)}" y="${h - 10}" fill="#8a8a8a" font-size="11" text-anchor="middle">${x}</text>`; });
  series.forEach((ser) => {
    const pts = xs.map((x, i) => [x, ser.values[i]]).filter(([, v]) => v != null && isFinite(v));
    const clamp = (v) => Math.max(yMin, Math.min(yMax, v));
    s += `<polyline fill="none" stroke="${ser.color}" stroke-width="2" ${ser.dash ? 'stroke-dasharray="5 4"' : ''} points="${pts.map(([x, v]) => `${X(x)},${Y(clamp(v))}`).join(' ')}"/>`;
    if (!ser.dash) pts.forEach(([x, v]) => { s += `<circle cx="${X(x)}" cy="${Y(clamp(v))}" r="3" fill="${ser.color}"/>`; });
  });
  return s + '</svg>';
}

function range(vals, minSpan, pad) {
  const v = vals.filter((x) => x != null && isFinite(x));
  let lo = Math.min(...v), hi = Math.max(...v);
  const mid = (lo + hi) / 2;
  if (hi - lo < minSpan) { lo = mid - minSpan / 2; hi = mid + minSpan / 2; }
  return [lo - pad, hi + pad];
}

function ticksFor(lo, hi, step) {
  const out = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi + 1e-9; t += step) out.push(+t.toFixed(4));
  return out;
}

/* ---------- color math (mirrors calib/colorimetry.py) ---------- */

const CM = (() => {
  const D65 = [0.95047, 1.0, 1.08883];
  const uv = ([x, y, z]) => { const d = x + 15 * y + 3 * z; return [(4 * x) / d, (6 * y) / d]; };
  const planck = (t) => [
    (0.860117757 + 1.54118254e-4 * t + 1.28641212e-7 * t * t) / (1 + 8.42420235e-4 * t + 7.08145163e-7 * t * t),
    (0.317398726 + 4.22806245e-5 * t + 4.20481691e-8 * t * t) / (1 - 2.89741816e-5 * t + 1.61456053e-7 * t * t),
  ];
  const dist = (t, u, v) => { const [pu, pv] = planck(t); return Math.hypot(pu - u, pv - v); };

  function cctDuv(xyz) {
    const [u, v] = uv(xyz);
    const n = 600, at = (i) => 1500 * Math.pow(20000 / 1500, i / (n - 1));
    let best = 0, bd = Infinity;
    for (let i = 0; i < n; i++) { const d = dist(at(i), u, v); if (d < bd) { bd = d; best = i; } }
    let lo = at(Math.max(best - 1, 0)), hi = at(Math.min(best + 1, n - 1));
    const g = (Math.sqrt(5) - 1) / 2;
    for (let i = 0; i < 50; i++) {
      const a = hi - g * (hi - lo), b = lo + g * (hi - lo);
      if (dist(a, u, v) < dist(b, u, v)) hi = b; else lo = a;
    }
    const t = (lo + hi) / 2, [pu, pv] = planck(t), p1 = planck(t + 1), p0 = planck(t - 1);
    const cross = (p1[0] - p0[0]) * (v - pv) - (p1[1] - p0[1]) * (u - pu);
    return [t, Math.hypot(u - pu, v - pv) * -Math.sign(cross)];
  }

  function lab(xyz, white) {
    const d = 6 / 29;
    const f = xyz.map((c, i) => { const t = c / white[i]; return t > d ** 3 ? Math.cbrt(t) : t / (3 * d * d) + 4 / 29; });
    return [116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])];
  }

  function de2000([l1, a1, b1], [l2, a2, b2]) {
    const rad = Math.PI / 180, deg = 180 / Math.PI;
    const c1 = Math.hypot(a1, b1), c2 = Math.hypot(a2, b2), cm = (c1 + c2) / 2;
    const g = 0.5 * (1 - Math.sqrt(cm ** 7 / (cm ** 7 + 25 ** 7)));
    const a1p = (1 + g) * a1, a2p = (1 + g) * a2;
    const c1p = Math.hypot(a1p, b1), c2p = Math.hypot(a2p, b2);
    const h1p = ((Math.atan2(b1, a1p) * deg) % 360 + 360) % 360, h2p = ((Math.atan2(b2, a2p) * deg) % 360 + 360) % 360;
    let dhp = 0;
    if (c1p * c2p !== 0) { dhp = h2p - h1p; if (dhp > 180) dhp -= 360; else if (dhp < -180) dhp += 360; }
    const dl = l2 - l1, dc = c2p - c1p, dh = 2 * Math.sqrt(c1p * c2p) * Math.sin((dhp / 2) * rad);
    const lm = (l1 + l2) / 2, cmp = (c1p + c2p) / 2;
    let hm;
    if (c1p * c2p === 0) hm = h1p + h2p;
    else if (Math.abs(h1p - h2p) <= 180) hm = (h1p + h2p) / 2;
    else if (h1p + h2p < 360) hm = (h1p + h2p + 360) / 2;
    else hm = (h1p + h2p - 360) / 2;
    const t = 1 - 0.17 * Math.cos((hm - 30) * rad) + 0.24 * Math.cos(2 * hm * rad)
      + 0.32 * Math.cos((3 * hm + 6) * rad) - 0.2 * Math.cos((4 * hm - 63) * rad);
    const sl = 1 + (0.015 * (lm - 50) ** 2) / Math.sqrt(20 + (lm - 50) ** 2);
    const sc = 1 + 0.045 * cmp, sh = 1 + 0.015 * cmp * t;
    const rt = -2 * Math.sqrt(cmp ** 7 / (cmp ** 7 + 25 ** 7)) * Math.sin(60 * Math.exp(-(((hm - 275) / 25) ** 2)) * rad);
    return Math.sqrt((dl / sl) ** 2 + (dc / sc) ** 2 + (dh / sh) ** 2 + rt * (dc / sc) * (dh / sh));
  }

  // White point description, same fields as the analyzer's round.white.
  function describe(xyz) {
    const y = xyz[1];
    const de = de2000(lab(xyz, D65.map((c) => c * y)), [100, 0, 0]);
    const [cct, duv] = cctDuv(xyz);
    const [u, v] = uv(xyz), [u0, v0] = uv(D65);
    const w6 = planck(6000), w7 = planck(7000);
    let warmer = [w6[0] - w7[0], w6[1] - w7[1]];
    const nw = Math.hypot(...warmer);
    warmer = warmer.map((c) => c / nw);
    let greener = [-warmer[1], warmer[0]];
    if (greener[1] < 0) greener = greener.map((c) => -c);
    const d = [u - u0, v - v0];
    const ang = Math.atan2(d[0] * greener[0] + d[1] * greener[1], d[0] * warmer[0] + d[1] * warmer[1]);
    const offset = Math.hypot(...d) === 0 ? { warm: 0, green: 0 } : { warm: de * Math.cos(ang), green: de * Math.sin(ang) };
    return { Y: y, dE: de, cct, duv, offset };
  }

  return { describe };
})();

// White predicted at slider values `next`, given the values the photo was taken at (`cur`).
function predictWhite(gains, cur, next) {
  const f = next.map((v, i) => (cur[i] > 0 ? v / cur[i] : 1));
  const xyz = [0, 1, 2].map((j) => gains.basis.reduce((s, col, i) => s + col[j] * f[i], 0));
  return CM.describe(xyz);
}

/* ---------- white point panel ---------- */

const CH = [
  { key: 'r', name: 'Red', color: '#ff6b6b' },
  { key: 'g', name: 'Green', color: '#56c48e' },
  { key: 'b', name: 'Blue', color: '#6f9bff' },
];

function verdict(dE) {
  if (dE <= 1) return ['ok', 'Spot on', 'The difference from a perfect D65 white is invisible.'];
  if (dE <= 2) return ['ok', 'Accurate', 'You’d only notice a difference next to a perfect reference.'];
  if (dE <= 3) return ['warn', 'Close', 'Visible side by side, but fine for most uses.'];
  return ['bad', 'Off', `It has to come down by ${fmt(dE - 2, 1)} ΔE to get under 2, the usual limit for “accurate”.`];
}

// Where white sits around the D65 target. Rings are ΔE 2000 distances.
// `preview` (optional) is a predicted white for slider values being tried.
function bullseye(rounds, preview) {
  const size = 300, c = size / 2, radius = c - 34;
  const maxDE = Math.max(5, ...rounds.map((r) => r.white.dE)) * 1.1;
  const k = radius / maxDE;
  const pos = (w) => {
    let x = -w.offset.warm * k, y = -w.offset.green * k;
    const d = Math.hypot(x, y);
    if (d > radius) { x *= radius / d; y *= radius / d; }
    return [c + x, c + y];
  };
  let s = `<svg viewBox="0 0 ${size} ${size}" role="img" aria-label="Distance of the white point from D65">`;
  s += `<circle cx="${c}" cy="${c}" r="${Math.min(3 * k, radius)}" fill="#2c2414"/>`;
  s += `<circle cx="${c}" cy="${c}" r="${Math.min(2 * k, radius)}" fill="#15281f"/>`;
  s += `<circle cx="${c}" cy="${c}" r="${Math.min(1 * k, radius)}" fill="#1c3a2c"/>`;
  [1, 2, 3, 5, 10, 20, 40].filter((v) => v <= maxDE).forEach((v) => {
    s += `<circle cx="${c}" cy="${c}" r="${v * k}" fill="none" stroke="#3a3a3a" stroke-dasharray="${v <= 3 ? '0' : '3 3'}"/>`;
    s += `<text x="${c + 4}" y="${c - v * k - 3}" fill="#8a8a8a" font-size="10">${v}</text>`;
  });
  s += `<line x1="${c - radius}" x2="${c + radius}" y1="${c}" y2="${c}" stroke="#2b2b2b"/><line y1="${c - radius}" y2="${c + radius}" x1="${c}" x2="${c}" stroke="#2b2b2b"/>`;
  s += `<text x="8" y="${c + 4}" fill="#e8a24d" font-size="11">Warmer</text>`;
  s += `<text x="${size - 8}" y="${c + 4}" fill="#7aa7ff" font-size="11" text-anchor="end">Cooler</text>`;
  s += `<text x="${c}" y="14" fill="#56c48e" font-size="11" text-anchor="middle">Greener</text>`;
  s += `<text x="${c}" y="${size - 6}" fill="#e070c0" font-size="11" text-anchor="middle">Magenta</text>`;
  const pts = rounds.map((r) => pos(r.white));
  if (pts.length > 1) s += `<polyline points="${pts.map((p) => p.join(',')).join(' ')}" fill="none" stroke="#737373" stroke-width="1.5" stroke-dasharray="4 3"/>`;
  pts.slice(0, -1).forEach(([x, y], i) => {
    s += `<circle cx="${x}" cy="${y}" r="4" fill="#737373"/><text x="${x + 7}" y="${y + 4}" fill="#8a8a8a" font-size="10">${i + 1}</text>`;
  });
  const [lx, ly] = pts[pts.length - 1];
  if (!preview) s += `<line x1="${c}" y1="${c}" x2="${lx}" y2="${ly}" stroke="#ececec" stroke-opacity=".35"/>`;
  s += `<circle cx="${c}" cy="${c}" r="3" fill="#ececec"/><text x="${c + 6}" y="${c + 14}" fill="#a3a3a3" font-size="10">D65</text>`;
  s += `<circle cx="${lx}" cy="${ly}" r="7" fill="#ececec" stroke="#0e0e0e" stroke-width="2"/>`;
  if (preview) {
    const [px, py] = pos(preview);
    s += `<line x1="${lx}" y1="${ly}" x2="${px}" y2="${py}" stroke="#8ab4ff" stroke-width="1.5" stroke-dasharray="4 3"/>`;
    s += `<circle cx="${px}" cy="${py}" r="8" fill="none" stroke="#8ab4ff" stroke-width="2.5"/>`;
  }
  return s + '</svg>';
}

function newSliderValues(gains, rgb) {
  const cur = [rgb.r, rgb.g, rgb.b].map(Number);
  const max = Number(rgb.max) || 100;
  // Keep white about as bright as now, then make sure no slider goes past its maximum.
  const s = gains.y.reduce((a, b) => a + b, 0) / gains.y.reduce((a, y, i) => a + y * gains.k[i], 0);
  let next = cur.map((v, i) => v * gains.k[i] * s);
  const over = Math.max(...next) / max;
  if (over > 1) next = next.map((v) => v / over);
  return { cur, next: next.map((v) => Math.round(v)), max };
}

function deltaTag(next, cur) {
  const d = Math.round(next - cur);
  if (Math.abs(d) < 1) return '<span class="delta keep">Keep</span>';
  return d < 0 ? `<span class="delta down">Lower ${-d}</span>` : `<span class="delta up">Raise ${d}</span>`;
}

// Range thumbs are ~16px wide, so a value's spot on the track shifts by up to 8px at the ends.
const tickLeft = (v, max) => {
  const p = Math.max(0, Math.min(100, (v / max) * 100));
  return `calc(${p}% + ${(8 - p * 0.16).toFixed(1)}px)`;
};

function tryRowsHtml(trial, cur, sug, max) {
  return CH.map((ch, i) => `<div class="try-row">
      <div class="gain-name"><span class="dot" style="background:${ch.color}"></span>${ch.name}</div>
      <div class="slider-wrap">
        <input type="range" min="0" max="${max}" step="1" value="${trial[i]}" data-try="${i}" style="accent-color:${ch.color}" aria-label="${ch.name}">
        <div class="tick now" style="left:${tickLeft(cur[i], max)}" title="Now: ${cur[i]}"></div>
        <div class="tick sug" style="left:${tickLeft(sug[i], max)}" title="Suggested: ${sug[i]}"></div>
      </div>
      <div class="gain-val" data-val="${i}"><b>${trial[i]}</b> ${deltaTag(trial[i], cur[i])}</div>
    </div>`).join('');
}

function previewHtml(p) {
  const [state, title] = verdict(p.dE);
  const dK = 6504 - p.cct;
  const dDuv = p.duv - 0.0032;
  const temp = Math.abs(dK) < 150 ? `${fmt(p.cct)} K, on target` : `${fmt(p.cct)} K, ${dK > 0 ? 'still warm' : 'now cool'}`;
  const tint = Math.abs(dDuv) < 0.0015 ? 'neutral tint' : dDuv < 0 ? 'magenta tint' : 'green tint';
  const bright = Math.round((p.Y - 1) * 100) || 0; // `|| 0` turns -0 into 0
  return `<div class="verdict ${state}"><span class="muted small">With these values</span><span class="big">${fmt(p.dE, 1)} ΔE</span><span class="vt">${title}</span></div>
    <div class="small muted">${temp} · ${tint} · brightness ${bright > 0 ? '+' : ''}${bright}%</div>`;
}

function whitePanelHtml(a) {
  const r = a.rounds[a.rounds.length - 1];
  const w = r.white;
  const rough = a.method === 'camera';
  const [state, title, text] = verdict(w.dE);
  const dK = 6504 - w.cct;
  const dDuv = w.duv - 0.0032;
  const cctText = Math.abs(dK) < 150 ? 'on target' : `needs to be about ${Math.round(Math.abs(dK) / 50) * 50} K ${dK > 0 ? 'cooler' : 'warmer'}`;
  const tintText = Math.abs(dDuv) < 0.0015 ? 'neutral' : dDuv < 0 ? 'too magenta (pinkish): needs a little more green' : 'too green: needs a little more magenta';
  const rgb = S.cfg.rgb;
  return `<h2>White point</h2>
    <div class="card wp">
      <div class="wp-target"><div id="wp-svg">${bullseye(a.rounds)}</div>
        <div class="legend small"><span><i style="background:#1c3a2c;height:10px;width:10px"></i>Accurate (under 2)</span><span><i style="background:#2c2414;height:10px;width:10px"></i>Close (2–3)</span>
          ${rough ? '' : '<span><i style="background:transparent;border:2px solid #8ab4ff;border-radius:50%;height:10px;width:10px"></i>With your slider values</span>'}</div></div>
      <div class="wp-info">
        <div class="verdict ${state}"><span class="big">${fmt(w.dE, 1)} ΔE</span><span class="vt">${title}${rough ? ' (rough)' : ''}</span></div>
        <p class="muted" style="margin:4px 0 16px">${text}${rough ? ' Without shot 1 this reading can be off by several hundred kelvin.' : ''}</p>
        <dl class="details">
          <dt>Color temperature</dt><dd>${fmt(w.cct)} K → 6504 K, ${cctText}</dd>
          <dt>Tint</dt><dd>${tintText}</dd>
        </dl>
        ${rough
          ? '<p class="small muted" style="margin-top:16px">Take shot 1 on a reference monitor to get slider values.</p>'
          : `<h3 style="margin:20px 0 4px">Try Red, Green and Blue values</h3>
            <p class="small muted" style="margin:0 0 10px">First type what the monitor menu showed when you took shot 3. Then drag the sliders: the blue ring on the target shows where white would land, no new photo needed.</p>
            <div class="rgb-inputs">
              ${CH.map((ch) => `<label>${ch.name} now<input type="number" min="0" max="1000" step="1" data-rgb="${ch.key}" value="${esc(rgb[ch.key])}"></label>`).join('')}
              <label>Slider max<input type="number" min="1" max="1000" step="1" data-rgb="max" value="${esc(rgb.max)}"></label>
            </div>
            <div id="try-rows"></div>
            <div class="legend small" style="margin:2px 0 12px"><span><i class="tick-key now"></i>Now</span><span><i class="tick-key sug"></i>Suggested</span></div>
            <div class="row" style="margin-bottom:14px">
              <button class="small-btn" id="try-sug">Suggested values</button>
              <button class="small-btn quiet" id="try-now">Back to current</button>
            </div>
            <div class="preview" id="try-preview"></div>
            <p class="small muted" style="margin:12px 0 0">This is a prediction that assumes each slider scales its color evenly. When you like it, set the same values on the monitor and retake shot 3 once to confirm.${r.gains.method === 'srgb' ? ' It assumes the monitor uses sRGB primaries.' : ''}</p>`}
      </div>
    </div>`;
}

function wireWhitePanel(el, a) {
  const r = a.rounds[a.rounds.length - 1];
  const rowsEl = $('#try-rows', el);
  if (!rowsEl || a.method === 'camera') return;

  let base;   // { cur, next (suggested), max }
  let trial;  // values being tried

  const update = () => {
    const p = predictWhite(r.gains, base.cur, trial);
    $('#try-preview', el).innerHTML = previewHtml(p);
    $('#wp-svg', el).innerHTML = bullseye(a.rounds, p);
  };
  const drawRows = () => {
    rowsEl.innerHTML = tryRowsHtml(trial, base.cur, base.next, base.max);
    $$('[data-try]', rowsEl).forEach((inp) => inp.addEventListener('input', () => {
      const i = Number(inp.dataset.try);
      trial[i] = Number(inp.value);
      $(`[data-val="${i}"]`, rowsEl).innerHTML = `<b>${trial[i]}</b> ${deltaTag(trial[i], base.cur[i])}`;
      update();
    }));
    update();
  };
  const reset = () => {
    base = newSliderValues(r.gains, S.cfg.rgb);
    trial = [...base.next];
    drawRows();
  };

  $$('[data-rgb]', el).forEach((inp) => inp.addEventListener('input', () => {
    const v = parseFloat(inp.value);
    if (!isFinite(v) || v < 0 || (inp.dataset.rgb === 'max' && v < 1)) return;
    S.cfg.rgb[inp.dataset.rgb] = v;
    saveCfg();
    reset();
  }));
  $('#try-sug', el).addEventListener('click', () => { trial = [...base.next]; drawRows(); });
  $('#try-now', el).addEventListener('click', () => { trial = [...base.cur]; drawRows(); });
  reset();
}

const NB_STATUS = { ok: ['Visible', 'ok'], dark: ['Dim', 'warn'], crushed: ['Lost', 'bad'], raised: ['Too bright', 'warn'], faint: ['Too faint to measure', ''] };

function analysisHtml(a) {
  const r = a.rounds[a.rounds.length - 1];
  const tg = S.cfg.gamma === 'srgb' ? 2.2 : +S.cfg.gamma;
  const w = r.white;
  const nb = a.nearBlack;
  const lost = nb ? nb.steps.filter((s) => s.status === 'crushed') : [];
  let h = '';

  h += `<div class="row" style="margin-top:32px"><h2 style="margin:0">Round ${r.round}${a.rounds.length > 1 ? ` of ${a.rounds.length}` : ''}</h2><span class="spacer"></span>
    <label class="muted small">Target&nbsp;<select id="target-sel">
      ${[['2.2', 'Gamma 2.2'], ['srgb', 'sRGB curve'], ['2.4', 'Gamma 2.4']].map(([v, l]) => `<option value="${v}" ${S.cfg.gamma === v ? 'selected' : ''}>${l}</option>`).join('')}
    </select></label></div>`;

  h += `<div class="metrics">
    ${a.method === 'camera'
      ? metric('White point', `${fmt(w.cct)} K`, 'Rough: needs shot 1 to trust', 'warn')
      : metric('White point', `${fmt(w.cct)} K`, `${fmt(w.dE, 1)} ΔE from D65`, cls(w.dE, 1.5, 3))}
    ${metric('Gamma', fmt(r.gamma, 2), `Target ${fmt(tg, 1)}`, r.gamma == null ? '' : cls(Math.abs(r.gamma - tg), 0.08, 0.15))}
    ${metric('Gray tint', fmt(r.maxTint, 1), 'Worst gray step, ΔE', cls(r.maxTint, 2, 4))}
    ${r.brightness != null ? metric('Brightness', `${fmt(r.brightness, 2)}×`, `White vs ${esc(refLabel())}`, cls(Math.abs(r.brightness - 1), 0.1, 0.25)) : ''}
    ${nb ? metric('Near-black', lost.length ? `Lost to ${lost[lost.length - 1].level}%` : 'Fine', !nb.selfReferenced && nb.round !== r.round ? `From round ${nb.round}` : 'Darkest steps', lost.length ? 'bad' : 'ok') : ''}
  </div>`;

  if (w.dE > 2 && S.recipe && S.recipe.state === 'found') h += recipeHtml();
  h += whitePanelHtml(a);

  if (a.advice.length) {
    h += `<h2>What to change</h2>`;
    h += a.advice.map((x) => `
      <div class="advice ${x.level}"><span class="ico">${{ ok: '✓', adjust: '→', warn: '!' }[x.level]}</span>
        <div><div class="t">${esc(x.title)}</div><div class="d">${esc(x.detail)}</div>${x.note ? `<div class="n">${esc(x.note)}</div>` : ''}</div></div>`).join('');
  }
  h += `<div class="card" style="margin-top:16px"><h3 style="margin-bottom:6px">Next round</h3>
    <p class="muted" style="margin:0">Change one setting at a time and wait about a minute. Then retake shot 3 without moving the phone, and drop the new photo here. Retake shot 4 too if you changed brightness, gamma or black level.</p></div>`;
  h += settingsCardHtml(a);

  const steps = r.steps;
  const xs = steps.map((s) => s.level);
  const bal = [0, 1, 2].map((i) => steps.map((s) => s.balance[i]));
  const [bLo, bHi] = range(bal.flat().concat([100]), 12, 1);
  const gSteps = steps.filter((s) => s.level < 100);
  const gx = gSteps.map((s) => s.level);
  const gm = gSteps.map((s) => s.gamma);
  const gt = gSteps.map((s) => Math.log(s.Yt) / Math.log(s.level / 100));
  const [gLo, gHi] = range(gm.concat(gt), 0.4, 0.05);

  h += `<h2>Gray scale</h2><div class="charts">
    <div class="card chart-card"><h3>RGB balance</h3><p class="small muted" style="margin:0 0 8px">100% on every channel means a neutral gray.</p>
      ${lineChart({ xs, yMin: bLo, yMax: bHi, ticks: ticksFor(bLo, bHi, bHi - bLo > 16 ? 4 : 2), fmtTick: (t) => `${t}%`, series: [
        { values: xs.map(() => 100), color: '#5a5a5a', dash: true },
        { values: bal[0], color: '#ff6b6b' }, { values: bal[1], color: '#56c48e' }, { values: bal[2], color: '#6f9bff' }] })}
      <div class="legend"><span><i style="background:#ff6b6b"></i>Red</span><span><i style="background:#56c48e"></i>Green</span><span><i style="background:#6f9bff"></i>Blue</span><span class="small">Gray level (%) along the bottom</span></div></div>
    <div class="card chart-card"><h3>Gamma per step</h3><p class="small muted" style="margin:0 0 8px">Higher means darker midtones.</p>
      ${lineChart({ xs: gx, yMin: gLo, yMax: gHi, ticks: ticksFor(gLo, gHi, 0.1), fmtTick: (t) => t.toFixed(1), series: [
        { values: gt, color: '#737373', dash: true }, { values: gm, color: '#ececec' }] })}
      <div class="legend"><span><i style="background:#ececec"></i>Measured</span><span><i style="background:#737373"></i>Target</span></div></div>
  </div>`;

  h += `<div class="table-wrap" style="margin-top:16px"><table>
    <tr><th>Gray</th><th>Luminance</th><th>Target</th><th>Gamma</th><th>Red</th><th>Green</th><th>Blue</th><th>ΔE</th><th>Tint ΔE</th></tr>
    ${steps.map((s) => `<tr>
      <td><span class="swatch" style="background:rgb(${s.code},${s.code},${s.code})"></span>${s.level}%</td>
      <td>${fmt(s.Y * 100, 2)}%</td><td>${fmt(s.Yt * 100, 2)}%</td><td>${fmt(s.gamma, 2)}</td>
      <td>${fmt(s.balance[0], 1)}%</td><td>${fmt(s.balance[1], 1)}%</td><td>${fmt(s.balance[2], 1)}%</td>
      <td class="${cls(s.dE, 2, 4)}">${fmt(s.dE, 1)}</td><td class="${cls(s.dEtint, 2, 4)}">${fmt(s.dEtint, 1)}</td></tr>`).join('')}
  </table></div>`;

  if (nb) {
    h += `<h2>Near-black${!nb.selfReferenced && nb.round !== r.round ? ` <span class="muted small">(measured against round ${nb.round})</span>` : ''}</h2>
      <div class="table-wrap"><table>
      <tr><th>Gray</th><th>Measured</th><th>Target</th><th>Measured ÷ target</th><th>Result</th></tr>
      ${nb.steps.map((s) => {
        const [label, c] = NB_STATUS[s.status];
        return `<tr><td>${s.level}% <span class="muted small">(code ${s.code})</span></td><td>${fmt(Math.max(0, s.Y) * 100, 3)}%</td><td>${fmt(s.Yt * 100, 3)}%</td>
          <td>${s.ratio == null ? '–' : fmt(Math.max(0, s.ratio), 2)}</td><td class="${c}">${label}</td></tr>`;
      }).join('')}
      </table></div>`;
  }

  if (a.rounds.length > 1) {
    h += `<h2>Rounds</h2><div class="table-wrap"><table>
      <tr><th>Round</th><th>Photo</th><th>White</th><th>ΔE</th><th>Gamma</th><th>Worst tint</th>${r.brightness != null ? '<th>Brightness</th>' : ''}</tr>
      ${a.rounds.map((x) => `<tr><td>${x.round}</td><td class="muted">${esc(x.name)}</td><td>${fmt(x.white.cct)} K</td>
        <td class="${cls(x.white.dE, 1.5, 3)}">${fmt(x.white.dE, 1)}</td><td>${fmt(x.gamma, 2)}</td><td class="${cls(x.maxTint, 2, 4)}">${fmt(x.maxTint, 1)}</td>
        ${r.brightness != null ? `<td>${fmt(x.brightness, 2)}×</td>` : ''}</tr>`).join('')}
    </table></div>`;
  }

  const p = r.primaries;
  const ref = a.reference;
  h += `<h2>Details</h2><div class="card"><dl class="details">
    <dt>Measured against</dt><dd>${a.method === 'reference' ? `${esc(refLabel())}, assumed to show exactly ${esc(a.referenceSpec ? a.referenceSpec.name : 'sRGB')}` : a.method === 'profile' ? `The phone’s color data, corrected with ${esc(a.profile.label)}` : 'The phone camera’s built-in color data (rough)'}</dd>
    <dt>White</dt><dd>x ${fmt(w.xy[0], 4)}, y ${fmt(w.xy[1], 4)} · ${fmt(w.cct)} K · Duv ${w.duv >= 0 ? '+' : ''}${fmt(w.duv, 4)} (positive is greenish, negative magenta)</dd>
    ${a.crossCheck ? `<dt>Camera’s own reading</dt><dd>${fmt(a.crossCheck.cct)} K using the phone’s built-in color data, as a cross-check</dd>` : ''}
    <dt>Red, green, blue</dt><dd>R ${fmt(p.R[0], 3)}, ${fmt(p.R[1], 3)} · G ${fmt(p.G[0], 3)}, ${fmt(p.G[1], 3)} · B ${fmt(p.B[0], 3)}, ${fmt(p.B[1], 3)} · ${fmt(r.gamut * 100)}% of the sRGB area (rough)</dd>
    ${ref ? `<dt>Reference check</dt><dd>Gamma ${fmt(ref.gamma, 2)} · worst gray tint ${fmt(ref.maxTint, 1)} ΔE · white vs R+G+B ${fmt(ref.additivity * 100, 1)}%</dd>` : ''}
    ${a.flatField ? `<dt>Lens falloff fit</dt><dd>Within ${fmt(a.flatField.residual * 100, 1)}% across the white chart</dd>` : ''}
    <dt>White patches agree</dt><dd>Within ${fmt(Math.abs(r.whiteConsistency) * 100, 1)}% (center vs corner, after lens correction)</dd>
  </dl></div>`;
  return h;
}

function renderResultsBody() {
  const el = $('#results-body');
  if (!el) return;
  const sess = S.session;
  if (!sess) { el.innerHTML = ''; return; }
  const a = sess.analysis || {};
  const have = a.have || {};
  const chip = (ok, text) => `<span class="chip ${ok ? 'have' : 'need'}">${ok ? '✓' : '○'} ${text}</span>`;
  let h = '';

  if (sess.photos.length) {
    h += '<div class="shots-status">';
    if (S.cfg.hasRef) h += chip(have.reference, 'Shot 1 reference');
    h += chip(have.white, 'Shot 2 white');
    h += chip(have.rounds > 0, have.rounds > 1 ? `Shot 3 bright · ${have.rounds} rounds` : 'Shot 3 bright');
    h += chip(have.dark, 'Shot 4 dark');
    h += '</div>';
    h += `<div class="row"><h2 style="margin:8px 0">Photos</h2><span class="spacer"></span><button class="small-btn quiet" id="reset">Start over</button></div>`;
    h += sess.photos.map(photoRow).join('');
  }
  (a.warnings || []).forEach((w) => (h += banner('warn', esc(w))));
  if (a.rounds && a.rounds.length) {
    h += correctionStatusHtml(a);
    if (shareNote(a)) h += `<div class="status-line">${esc(shareNote(a))}</div>`;
    h += analysisHtml(a);
  }
  el.innerHTML = h;
  wireSettingsCard(el);
  maybeShare(a);
  if (a.rounds && a.rounds.length) wireWhitePanel(el, a);

  $$('[data-del]', el).forEach((b) => b.addEventListener('click', async () => {
    S.session = await api(`/api/photo/${b.dataset.del}/delete?${qs()}`, { method: 'POST' });
    renderResultsBody();
  }));
  $$('[data-assign]', el).forEach((s) => s.addEventListener('change', async () => {
    const [type, role] = s.value === 'auto' ? ['', ''] : s.value.split(':');
    S.session = await api(`/api/photo/${s.dataset.assign}/assign?type=${type}&role=${role}&${qs()}`, { method: 'POST' });
    renderResultsBody();
  }));
  const reset = $('#reset', el);
  if (reset) reset.addEventListener('click', async () => {
    if (!confirm('Start over? The current photos are moved to the sessions folder and the results are cleared.')) return;
    S.session = await api(`/api/reset?${qs()}`, { method: 'POST' });
    renderResultsBody();
  });
  const sel = $('#target-sel', el);
  if (sel) sel.addEventListener('change', () => { S.cfg.gamma = sel.value; saveCfg(); refresh(); });
}

/* ---------- shared database: settings, consent, contributions ---------- */

// Where "What's shared" points.
const DATA_URL = 'https://github.com/navidabasi/monitor-calibration/blob/main/docs/DATA.md';

async function loadSettings() {
  try { S.settings = await api('/api/settings'); } catch { S.settings = { share: null, apiConfigured: false }; }
  return S.settings;
}

async function setSharing(share) {
  S.settings = await api('/api/settings', { method: 'POST', body: JSON.stringify({ share }), headers: { 'Content-Type': 'application/json' } });
  S.shareKey = '';
  if (!$('#results').hidden) refresh();
}

// Asked once, at first use, and only if there is somewhere to send anything. Neither answer is pre-selected.
function askConsent() {
  if (!S.settings || !S.settings.apiConfigured || S.settings.share !== null) return;
  const el = $('#consent');
  el.innerHTML = `<div class="card">
    <h2 id="consent-title">Help everyone calibrate without a reference monitor</h2>
    <p>Share anonymous measurements from your sessions. No photos, no personal data. People with the same phone and kind of screen can then get an accurate white point without a reference monitor, and so can you.</p>
    <p class="small muted">We don’t use your data to identify you, build a profile of you, or track you. You can change this any time in Settings and delete what you shared.</p>
    <div class="row" style="margin-top:16px">
      <a href="${DATA_URL}" target="_blank" rel="noopener noreferrer">What’s shared</a><span class="spacer"></span>
      <button id="consent-no">Don’t share</button><button id="consent-yes">Share</button>
    </div></div>`;
  el.hidden = false;
  const answer = async (share) => { el.hidden = true; try { await setSharing(share); } catch { /* asked again next launch */ } };
  $('#consent-yes').addEventListener('click', () => answer(true));
  $('#consent-no').addEventListener('click', () => answer(false));
}

async function renderSettings() {
  const el = $('#settings');
  const st = S.settings || (await loadSettings());
  let items = [];
  try { items = (await api('/api/contributions')).items; } catch { /* shown as empty */ }
  const choice = (value, label) => `<label><input type="radio" name="share" value="${value}" ${String(st.share) === value ? 'checked' : ''} ${st.apiConfigured ? '' : 'disabled'}> ${label}</label>`;
  el.innerHTML = `
    <h1>Settings</h1>
    <div class="card" style="margin-top:16px">
      <h3 style="margin-bottom:8px">Shared database</h3>
      <p class="muted">Share anonymous measurements from sessions that used a reference monitor, so people with the same phone and kind of screen get an accurate white point without one. No photos and no personal data are sent. <a href="${DATA_URL}" target="_blank" rel="noopener noreferrer">What’s shared</a></p>
      <div class="choice">${choice('true', 'Share anonymous measurements')}${choice('false', 'Don’t share')}</div>
      ${st.apiConfigured ? '' : '<p class="small muted" style="margin:12px 0 0">The shared database isn’t available in this build yet, so nothing is sent whatever you choose.</p>'}
      ${st.share === null && st.apiConfigured ? '<p class="small muted" style="margin:12px 0 0">You haven’t chosen yet. Nothing is sent until you do.</p>' : ''}
    </div>
    <div class="card" style="margin-top:16px">
      <h3 style="margin-bottom:8px">Your contributions</h3>
      ${items.length ? items.map((i) => `<div class="contrib"><div><b>${esc(i.label)}</b> <span class="muted small">· ${esc(i.month)}</span>
            <div class="code">Deletion code ${esc(i.code)}</div></div><button class="small-btn" data-contrib="${esc(i.id)}">Delete</button></div>`).join('')
        : '<p class="muted" style="margin:0">Nothing shared yet.</p>'}
      <p class="small muted" style="margin:12px 0 0">The deletion code stays on this computer; the database stores only a hash of it, so nobody can tell which contributions are yours. Deleting removes the contribution and updates the shared correction.</p>
      <div id="contrib-msg" class="small" style="margin-top:8px"></div>
    </div>`;
  $$('input[name="share"]', el).forEach((r) => r.addEventListener('change', async () => { await setSharing(r.value === 'true'); renderSettings(); }));
  $$('[data-contrib]', el).forEach((b) => b.addEventListener('click', async () => {
    b.disabled = true;
    try {
      await api(`/api/contributions/${b.dataset.contrib}/delete`, { method: 'POST' });
      renderSettings();
    } catch (e) {
      b.disabled = false;
      $('#contrib-msg', el).innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`;
    }
  }));
}

// What happened with the phone's color data on the Results page: a shared or saved correction, or why not.
function correctionStatusHtml(a) {
  if (a.method === 'profile' && a.profile) {
    const p = a.profile;
    const detail = p.source === 'shared' ? ` (${p.contributors} contributors, ±${fmt(p.spreadK)} K)` : '';
    return `<div class="status-line ok"><b>Using ${esc(p.label)}</b>${detail}. No reference shot needed.</div>`;
  }
  const text = {
    none: `No shared correction for ${a.phone ? esc(a.phone.label) : 'your phone'} yet. A reference shot would make you the first contributor.`,
    off: 'Sharing is off. Turn it on in Settings to use corrections shared by other people with your phone.',
    unknown_screen: 'Pick the screen type of your monitor in Setup to use saved or shared corrections for your phone.',
    error: 'Couldn’t reach the shared database, so the phone’s own color data is used.',
  }[a.sharedState];
  return text ? `<div class="status-line">${text}</div>` : '';
}

// What the Results page says about saving and sharing what a reference shot taught the app.
function shareNote(a) {
  const c = a.correction;
  if (!c || a.method !== 'reference') return '';
  if (!c.good) return `This reference shot wasn’t used to teach the app about your phone: ${c.problems[0]}`;
  return (c.saved ? 'Saved what this reference shot taught the app about your phone, on this computer.' : '') + S.shareResult;
}

// After a good reference session, offer it to the shared database once (the server checks consent and skips repeats).
async function maybeShare(a) {
  const c = a.correction;
  if (!c || !c.good || a.method !== 'reference' || !S.settings || S.settings.share !== true) return;
  const key = S.session.photos.map((p) => `${p.id}${p.type}${p.role}`).join(',');
  if (key === S.shareKey) return;
  S.shareKey = key;
  S.shareResult = '';
  try {
    const r = await api(`/api/share?${qs()}`, { method: 'POST' });
    if (r.status === 'shared') S.shareResult = ' Shared anonymously. Thank you!';
    else if (r.status === 'error') S.shareResult = ` Couldn’t share: ${r.message}`;
  } catch { S.shareKey = ''; /* try again the next time the results change */ }
  renderResultsBody();
}

/* ---------- suggested settings, and sharing your own ---------- */

// Asks the local server whether enough people with this monitor model and picture mode agree on settings.
// The server only answers with numbers when they do; otherwise there is nothing to show.
async function loadRecipe() {
  try { S.recipe = await api(`/api/recipe?${qs()}`); } catch { S.recipe = null; }
  return S.recipe;
}

const MODE_NAMES = Object.fromEntries(MODES.filter(([v]) => v).map(([v, l]) => [v, l]));

function recipeHtml() {
  const r = S.recipe && S.recipe.state === 'found' ? S.recipe.recipe : null;
  if (!r) return '';
  const max = Number(S.cfg.rgb.max) || 100;
  const v = r.rgb.map((x) => Math.round(x * max));
  const mode = MODE_NAMES[S.cfg.targetMode] || S.cfg.targetMode;
  const spread = Math.max(1, Math.round(r.spread * max));
  const base = r.baseline ? ` Left as it was, this monitor usually measures about ${fmt(r.baseline.cct)} K.` : '';
  return `<div class="status-line ok"><b>A starting point from other people</b><br>
    ${r.contributors} people with this monitor in ${esc(mode)} mode ended up near
    <b>Red ${v[0]}, Green ${v[1]}, Blue ${v[2]}</b> (on a slider that goes to ${max}; they agree within about ±${spread}).${base}
    Every unit differs a little, so try these, then take shot 3 to check.</div>`;
}

async function showRecipe(sel) {
  const el = $(sel);
  if (!el) return;
  await loadRecipe();
  const again = $(sel);
  if (again) again.innerHTML = recipeHtml();
}

// Measured primaries for the reference monitor model in its picture mode, from other people's ICC profiles
// and report values. Offered, never applied on its own: the person decides whether to use them.
async function showRefProfile() {
  const el = $('#ref-profile-hint');
  if (!el) return;
  let r = null;
  try { r = await api(`/api/reference-profile?${qs()}`); } catch { /* nothing to show */ }
  const again = $('#ref-profile-hint');
  if (!again) return;
  const p = r && r.state === 'found' ? r.profile : null;
  if (!p) { again.innerHTML = ''; return; }
  const mode = MODE_NAMES[S.cfg.refMode] || S.cfg.refMode;
  const using = S.cfg.refSpec.source === 'shared';
  again.innerHTML = `<div class="status-line ok" style="margin:0 0 16px"><b>Measured values for this reference</b><br>
    ${p.contributors} people loaded an ICC profile or calibration report for this monitor in ${esc(mode)} mode, and their primaries agree.
    ${using ? 'You’re using them.' : 'Using them is more accurate than assuming exact sRGB.'}
    ${using ? '' : '<div style="margin-top:8px"><button class="small-btn" id="use-ref-profile">Use these values</button></div>'}</div>`;
  const btn = $('#use-ref-profile', again);
  if (btn) btn.addEventListener('click', () => { S.cfg.refSpec = p.spec; saveCfg(); renderSetup(); });
}

// The "did you calibrate it?" question on the Results page. Only offered once sharing is on and the session
// has a reference shot, because the server measures the result itself from the shared readings.
function settingsCardHtml(a) {
  if (!S.settings || S.settings.share !== true || a.method !== 'reference' || !(a.rounds && a.rounds.length)) return '';
  const c = S.cfg;
  const named = c.targetMon && c.targetMon.manufacturer && c.targetMon.product_code != null && c.targetMode;
  if (!named) {
    return `<div class="card" style="margin-top:16px"><h3 style="margin-bottom:6px">Share this monitor’s settings?</h3>
      <p class="muted" style="margin:0">To help others with the same monitor, pick it from the detected list and choose its picture mode in <a href="#setup">Setup</a>.</p></div>`;
  }
  const g = c.rgb;
  return `<div class="card" id="settings-card" style="margin-top:16px"><h3 style="margin-bottom:6px">Share this monitor’s settings?</h3>
    <p class="muted">Other people with ${esc(devName(c.targetMon))} in ${esc(MODE_NAMES[c.targetMode] || c.targetMode)} mode could start from your numbers. Did you calibrate this monitor, or is it still at its own settings? Sent anonymously: the values below, your answer, and how this photo measured.</p>
    <div class="choice" style="margin-bottom:12px">
      <label><input type="radio" name="calib" value="yes"> I calibrated it. These are the settings I ended with.</label>
      <label><input type="radio" name="calib" value="no"> I haven’t changed anything. These are its own settings.</label>
    </div>
    <p class="small muted" style="margin:0 0 12px">Red ${esc(g.r)}, Green ${esc(g.g)}, Blue ${esc(g.b)}, slider maximum ${esc(g.max)}. These are the values typed in the white point panel above. Check they match what the monitor’s menu showed when you took the latest shot 3.</p>
    <div class="row"><button id="settings-share" disabled>Share</button><span class="small" id="settings-msg">${esc(S.settingsMsg)}</span></div></div>`;
}

function wireSettingsCard(el) {
  const btn = $('#settings-share', el);
  if (!btn) return;
  $$('input[name="calib"]', el).forEach((r) => r.addEventListener('change', () => { btn.disabled = false; }));
  btn.addEventListener('click', async () => {
    const calibrated = $('input[name="calib"]:checked', el).value === 'yes';
    const g = S.cfg.rgb;
    btn.disabled = true;
    const msg = $('#settings-msg', el);
    try {
      const r = await api(`/api/share-settings?${qs()}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ calibrated, rgb: { r: +g.r, g: +g.g, b: +g.b, max: +g.max } }),
      });
      S.settingsMsg = { shared: 'Shared anonymously. Thank you!', already: 'Already shared.', nothing: 'Nothing to share yet.', off: 'Sharing is off.',
        error: `Couldn’t share: ${r.message || ''}` }[r.status] || '';
    } catch (e) {
      S.settingsMsg = e.message;
      btn.disabled = false;
    }
    msg.textContent = S.settingsMsg;
  });
}

/* ---------- routing ---------- */

const PAGES = { setup: renderSetup, prep: renderPrep, photos: renderPhotos, results: renderResults, settings: renderSettings };

function route() {
  const step = location.hash.slice(1);
  const cur = PAGES[step] ? step : 'setup';
  $$('.page').forEach((p) => { p.hidden = p.id !== cur; });
  $$('.steps a').forEach((a) => a.classList.toggle('active', a.dataset.step === cur));
  PAGES[cur]();
  window.scrollTo(0, 0);
}

document.addEventListener('click', (e) => {
  const go = e.target.closest('[data-go]');
  if (go) location.hash = go.dataset.go;
});
// Stop the browser from opening a file dropped outside the drop zone.
window.addEventListener('dragover', (e) => e.preventDefault());
window.addEventListener('drop', (e) => e.preventDefault());
window.addEventListener('hashchange', route);

(async function init() {
  try {
    S.layout = await api('/api/layout');
  } catch (e) {
    document.querySelector('main').innerHTML = banner('bad', 'Couldn’t reach the analyzer. Start the site with <code>python calibrate.py</code>.');
    return;
  }
  route();
  loadDevices();
  loadSettings().then(askConsent);
})();
