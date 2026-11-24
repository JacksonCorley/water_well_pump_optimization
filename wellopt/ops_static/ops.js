/* Operations support page. Loads the newest plan per section from api/views, lists each pump's action for this
   hour and shows the picked pump on its pump curve; marks pumps out of service through api/marks. Plain
   JavaScript with no libraries; wellopt/ops_page.py serves it and works out each chart's frame. */
(() => {
  'use strict';
  const GROUPS = { do: 'Do this hour', keep: 'Keep running', leave: 'Leave off', oos: 'Out of service' };
  const WARN_ICON = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1 11.2 10.6H.8Z" fill="currentColor"/>'
    + '<path d="M6 4.4v3" stroke="var(--card)" stroke-width="1.4" stroke-linecap="round"/><circle cx="6" cy="9" r=".8" fill="var(--card)"/></svg>';
  const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  const state = {
    data: null,        // the last api/views answer
    section: null,     // the section shown
    pump: {},          // section -> picked pump
    notePump: null,    // the pump whose reason is in the note box
    msgPump: null,     // the pump the message under the form is about
    busy: false,       // a mark is being saved
    lastTry: 0, timer: 0, loading: null,
  };
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const withCode = (s) => esc(s).replace(/`([^`]+)`/g, '<code>$1</code>');
  const NF = {};
  const fmt = (v, d = 0, blank = '') => (v == null || !isFinite(v)) ? blank
    : (NF[d] ||= new Intl.NumberFormat('en-US', { minimumFractionDigits: d, maximumFractionDigits: d })).format(v);
  const p2 = (n) => String(n).padStart(2, '0');
  const parse = (s) => {  // a server time ("2019-07-08 00:00:00"), read as the server's local time
    const [d, t = '00:00:00'] = String(s).split(/[ T]/);
    const [y, mo, da] = d.split('-').map(Number), [h, mi, se] = t.split(':').map(Number);
    return new Date(y, mo - 1, da, h || 0, mi || 0, Math.floor(se || 0));
  };
  const hhmm = (d) => `${p2(d.getHours())}:${p2(d.getMinutes())}`;
  const dayLong = (d) => `${DAYS[d.getDay()]} ${p2(d.getDate())} ${MONTHS[d.getMonth()]} ${hhmm(d)}`;
  const dayShort = (d) => `${p2(d.getDate())} ${MONTHS[d.getMonth()]} ${hhmm(d)}`;
  const settings = () => (state.data && state.data.settings) || { por_low_pct: 70, por_high_pct: 120, stale_after_minutes: 120, refresh_minutes: 5, note_max: 200 };
  const view = () => state.data && state.data.sections[state.section];
  const groupOf = (r) => (r.action === 'on' || r.action === 'off') ? 'do' : r.action;
  const pumpOf = (v) => {
    if (!v) return null;
    const ids = v.rows.map((r) => r.pump);
    if (!ids.includes(state.pump[v.section])) state.pump[v.section] = ids[0] || null;
    return v.rows.find((r) => r.pump === state.pump[v.section]) || null;
  };
  const chip = (r) => `<span class="act act-${esc(r.action)}"><span class="g" aria-hidden="true">${esc(r.glyph)}</span>${esc(r.label)}</span>`;

  // ------------------------------------------------------------------ chart helpers
  const interp = (x, xs, ys) => {
    const n = xs.length;
    if (x <= xs[0]) return ys[0];
    if (x >= xs[n - 1]) return ys[n - 1];
    for (let i = 1; i < n; i++) if (x <= xs[i]) return ys[i - 1] + (x - xs[i - 1]) / (xs[i] - xs[i - 1]) * (ys[i] - ys[i - 1]);
    return ys[n - 1];
  };
  const interpIn = (x, xs, ys) => (x < xs[0] || x > xs[xs.length - 1]) ? null : interp(x, xs, ys);
  const ticks = (lo, hi, n) => {  // round tick values across [lo, hi], and the decimals they need
    const raw = (hi - lo) / Math.max(2, n), e = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / e;
    const step = (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * e, vals = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) vals.push(Math.round(v * 1e6) / 1e6);
    return { vals, dec: Math.max(0, -Math.floor(Math.log10(step) + 1e-9)) };
  };
  const star = (cx, cy, R) => {
    const pts = [];
    for (let i = 0; i < 10; i++) {
      const a = -Math.PI / 2 + i * Math.PI / 5, rr = i % 2 ? R * 0.45 : R;
      pts.push(`${(cx + rr * Math.cos(a)).toFixed(1)},${(cy + rr * Math.sin(a)).toFixed(1)}`);
    }
    return `M${pts.join('L')}Z`;
  };
  const SW = {
    band: '<svg class="sw" viewBox="0 0 22 14" aria-hidden="true"><rect class="c-band sw-edge" x="3" y="1.5" width="16" height="11" rx="1.5"/></svg>',
    usual: '<svg class="sw" viewBox="0 0 22 14" aria-hidden="true"><line class="c-usual" x1="1" y1="7" x2="21" y2="7"/></svg>',
    bep: `<svg class="sw" viewBox="0 0 22 14" aria-hidden="true"><path class="c-bep" d="${star(11, 7.6, 6.6)}"/></svg>`,
    now: '<svg class="sw" viewBox="0 0 22 14" aria-hidden="true"><circle class="c-now" cx="11" cy="7" r="5"/></svg>',
    exp: '<svg class="sw" viewBox="0 0 22 14" aria-hidden="true"><circle class="c-exp" cx="11" cy="7" r="5"/></svg>',
    cloud: '<svg class="sw" viewBox="0 0 22 14" aria-hidden="true"><g class="c-cloud"><circle cx="5" cy="9" r="2.4"/>'
      + '<circle cx="11" cy="5" r="2.4"/><circle cx="17" cy="8.5" r="2.4"/></g></svg>',
    maker: '<svg class="sw" viewBox="0 0 22 14" aria-hidden="true"><line class="c-maker" x1="1" y1="7" x2="21" y2="7"/></svg>',
    fit: '<svg class="sw" viewBox="0 0 22 14" aria-hidden="true"><line class="c-fit" x1="1" y1="7" x2="21" y2="7"/></svg>',
  };

  // ------------------------------------------------------------------ the pump curve
  let chart = null, lastW = 0;
  function drawChart(v, pv) {
    const host = $('#plot');
    const W = Math.max(280, Math.round(host.clientWidth || 560)), H = Math.round(Math.min(460, Math.max(280, W * 0.62)));
    const M = { l: 62, r: 16, t: 12, b: 48 }, iw = W - M.l - M.r, ih = H - M.t - M.b;
    lastW = host.clientWidth;
    hideTip();
    chart = null;
    const fr = pv && pv.chart;
    const svgOpen = (label) => `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">`;
    if (!pv || !fr || !fr.x || !fr.y) {
      const msg = pv ? 'No running hours in the history and no maker curve on file.' : 'Pick a pump in the list.';
      host.innerHTML = `${svgOpen(msg)}<rect class="c-bg" x="${M.l}" y="${M.t}" width="${iw}" height="${ih}"/>`
        + `<text class="c-empty" x="${M.l + iw / 2}" y="${M.t + ih / 2}" text-anchor="middle">${esc(msg)}</text></svg>`;
      return;
    }
    const { env, maker, fit } = pv;
    const [x0, x1] = fr.x, [y0, y1] = fr.y, band = fr.band, bep = fr.bep;
    const now = pv.flow != null && pv.head != null ? [pv.flow, pv.head] : null;
    const exp = pv.expected && pv.expected[0] != null && pv.expected[1] != null ? pv.expected : null;
    const sx = (q) => M.l + (q - x0) / (x1 - x0) * iw, sy = (h) => M.t + ih - (h - y0) / (y1 - y0) * ih;
    const r1 = (n) => Math.round(n * 10) / 10;
    const xt = ticks(x0, x1, Math.max(3, Math.round(iw / 95))), yt = ticks(y0, y1, Math.max(3, Math.round(ih / 55)));
    const out = [`<defs><clipPath id="plot-clip"><rect x="${M.l}" y="${M.t}" width="${iw}" height="${ih}"/></clipPath></defs>`,
      `<rect class="c-bg" x="${M.l}" y="${M.t}" width="${iw}" height="${ih}"/>`];
    if (band) {
      out.push(`<rect class="c-band" clip-path="url(#plot-clip)" x="${r1(sx(band[0]))}" y="${M.t}" `
        + `width="${r1(sx(band[1]) - sx(band[0]))}" height="${ih}"/>`);
    }
    out.push('<g class="c-grid">' + xt.vals.map((t) => `<line x1="${r1(sx(t))}" x2="${r1(sx(t))}" y1="${M.t}" y2="${M.t + ih}"/>`).join('')
      + yt.vals.map((t) => `<line x1="${M.l}" x2="${M.l + iw}" y1="${r1(sy(t))}" y2="${r1(sy(t))}"/>`).join('') + '</g>');
    if (env) {
      out.push('<g class="c-cloud" clip-path="url(#plot-clip)">'
        + env.cloud.map(([q, h]) => `<circle cx="${r1(sx(q))}" cy="${r1(sy(h))}" r="2.6"/>`).join('') + '</g>');
      const f = env.flow, h = env.head;
      if (f.p5 != null && f.p95 != null && h.p5 != null && h.p95 != null) {
        out.push(`<rect class="c-usual" clip-path="url(#plot-clip)" x="${r1(sx(f.p5))}" y="${r1(sy(h.p95))}" `
          + `width="${r1(sx(f.p95) - sx(f.p5))}" height="${r1(sy(h.p5) - sy(h.p95))}"/>`);
      }
    }
    if (maker) {
      out.push(`<polyline class="c-maker" clip-path="url(#plot-clip)" points="${maker.flow.map((q, i) => `${r1(sx(q))},${r1(sy(maker.head[i]))}`).join(' ')}"/>`);
    }
    if (fit && fit.credible) {
      const pts = [];
      for (let i = 0; i <= 40; i++) {
        const q = fit.flow_lo + (fit.flow_hi - fit.flow_lo) * i / 40;
        pts.push(`${r1(sx(q))},${r1(sy(fit.h0 + fit.k * q * q))}`);
      }
      out.push(`<polyline class="c-fit" clip-path="url(#plot-clip)" points="${pts.join(' ')}"/>`);
    }
    if (bep) out.push(`<path class="c-bep" d="${star(sx(bep[0]), sy(bep[1]), 8.5)}"/>`);
    if (exp) out.push(`<circle class="c-exp" cx="${r1(sx(exp[0]))}" cy="${r1(sy(exp[1]))}" r="7"/>`);
    if (now) out.push(`<circle class="c-now" cx="${r1(sx(now[0]))}" cy="${r1(sy(now[1]))}" r="7.5"/>`);
    out.push('<g class="c-axis">'
      + xt.vals.map((t) => `<text x="${r1(sx(t))}" y="${M.t + ih + 18}" text-anchor="middle">${fmt(t, xt.dec)}</text>`).join('')
      + yt.vals.map((t) => `<text x="${M.l - 8}" y="${r1(sy(t)) + 4}" text-anchor="end">${fmt(t, yt.dec)}</text>`).join('') + '</g>');
    out.push(`<text class="c-title" x="${r1(M.l + iw / 2)}" y="${H - 8}" text-anchor="middle">Flow (gpm)</text>`);
    out.push(`<text class="c-title" transform="translate(16 ${r1(M.t + ih / 2)}) rotate(-90)" text-anchor="middle">Total dynamic head (ft, estimated)</text>`);
    out.push(`<g class="c-hover" visibility="hidden"><line class="c-xhair" x1="0" x2="0" y1="${M.t}" y2="${M.t + ih}"/>`
      + '<circle class="c-probe" r="4" cx="0" cy="0"/><circle class="c-probe" r="4" cx="0" cy="0"/></g>');
    const label = `${pv.pump} on its pump curve` + (now ? `: now ${fmt(now[0])} gpm at ${fmt(now[1])} ft` : '')
      + (bep ? `; best efficiency ${fmt(bep[0])} gpm at ${fmt(bep[1])} ft` : '');
    host.innerHTML = svgOpen(label) + out.join('') + '</svg>';
    chart = { v, pv, M, iw, ih, x0, x1, sx, sy, now, bep, exp, maker, fit, env };
  }

  function hideTip() {
    $('#tip').hidden = true;
    const g = document.querySelector('#plot .c-hover');
    if (g) g.setAttribute('visibility', 'hidden');
  }

  function onPointer(e) {  // readouts: a marked point when the pointer is near one, else the curves at that flow
    const c = chart;
    if (!c) return;
    const box = $('#plot').getBoundingClientRect(), px = e.clientX - box.left, py = e.clientY - box.top;
    const { M, iw, ih } = c;
    if (px < M.l || px > M.l + iw || py < M.t || py > M.t + ih) { hideTip(); return; }
    const q = c.x0 + (px - M.l) / iw * (c.x1 - c.x0);
    let near = null, best = 16;
    for (const [k, p] of [['now', c.now], ['bep', c.bep], ['exp', c.exp]]) {
      if (!p) continue;
      const d = Math.hypot(c.sx(p[0]) - px, c.sy(p[1]) - py);
      if (d < best) { best = d; near = [k, p]; }
    }
    const g = document.querySelector('#plot .c-hover'), [ln, pm, pf] = g.children, lines = [];
    pm.setAttribute('visibility', 'hidden');
    pf.setAttribute('visibility', 'hidden');
    let ax = px;
    if (near) {
      const [k, [nq, nh]] = near;
      ax = c.sx(nq);
      if (k === 'now') {
        lines.push(`<b>Now (${hhmm(parse(c.v.readings_at))} reading)</b>`, `${fmt(nq)} gpm · ${fmt(nh)} ft`);
        const extra = [c.pv.psi != null ? `${fmt(c.pv.psi, 1)} psi` : '', c.pv.kw != null ? `${fmt(c.pv.kw)} kW` : ''].filter(Boolean).join(' · ');
        if (extra) lines.push(extra);
        if (c.pv.check && c.pv.check.pct_bep != null) lines.push(`${fmt(c.pv.check.pct_bep)}% of best-efficiency flow`);
      } else if (k === 'bep') {
        lines.push('<b>Best efficiency point</b>', `${fmt(nq)} gpm · ${fmt(nh)} ft`);
      } else {
        lines.push('<b>Expected if started</b>', `${fmt(nq)} gpm · ${fmt(nh)} ft`, c.env ? 'its median running point' : 'its average flow and pressure');
      }
    } else {
      lines.push(`<b>${fmt(q)} gpm</b>`);
      const hm = c.maker ? interpIn(q, c.maker.flow, c.maker.head) : null;
      if (hm != null) {
        lines.push(`Maker curve: ${fmt(hm)} ft`);
        pm.setAttribute('cx', px); pm.setAttribute('cy', c.sy(hm)); pm.setAttribute('visibility', 'visible');
      }
      const hf = c.fit && c.fit.credible && q >= c.fit.flow_lo && q <= c.fit.flow_hi ? c.fit.h0 + c.fit.k * q * q : null;
      if (hf != null) {
        lines.push(`Fitted to its history: ${fmt(hf)} ft`);
        pf.setAttribute('cx', px); pf.setAttribute('cy', c.sy(hf)); pf.setAttribute('visibility', 'visible');
      }
      if (c.maker && c.maker.bep) lines.push(`${fmt(q / c.maker.bep * 100)}% of best-efficiency flow`);
      if (lines.length === 1 && c.env && q >= c.env.flow.p5 && q <= c.env.flow.p95) lines.push('Inside its usual flow range');
    }
    ln.setAttribute('x1', ax); ln.setAttribute('x2', ax);
    g.setAttribute('visibility', 'visible');
    const tip = $('#tip');
    tip.innerHTML = lines.join('<br>');
    tip.hidden = false;
    const tw = tip.offsetWidth, th = tip.offsetHeight, wrapW = $('#plot').clientWidth;
    let left = px + 14;
    if (left + tw > wrapW - 4) left = px - 14 - tw;
    let top = py - th - 10;
    if (top < 4) top = py + 14;
    tip.style.left = `${Math.max(4, left)}px`;
    tip.style.top = `${top}px`;
  }

  // ------------------------------------------------------------------ rendering
  function metricCard(title, value, lines, extra = '') {
    const val = value.map(([t, isNum]) => `<span class="${isNum ? 'n' : 'u'}">${esc(t)}</span>`).join(' ');
    const ps = lines.filter(Boolean).map((l) => `<p${l.startsWith('▲') ? ' class="alert"' : ''}>${esc(l)}</p>`).join('');
    return `<section class="card metric"><h2>${esc(title)}</h2><p class="value">${val}</p>${extra}${ps}</section>`;
  }

  function renderMetrics(v) {
    const m = v.metrics, s = settings(), start = parse(v.plan_start), tr = parse(v.readings_at);
    const [lo, hi] = String(m.band || 'p10-p90').toUpperCase().split('-');
    const diff = (m.planned_kw || 0) - (m.running_kw || 0);
    const made = v.created_at ? dayShort(parse(v.created_at)) : 'at an unknown time';
    let strip = '';
    if (m.demand_lo != null && m.demand_hi != null && m.demand_p50 != null) {
      const vals = [m.demand_lo, m.demand_hi, m.demand_p50, m.produced_gpm].filter((x) => x != null);
      const a = Math.min(...vals) * 0.9, b = Math.max(...vals) * 1.04 || 1, x = (n) => ((n - a) / ((b - a) || 1) * 100).toFixed(2);
      strip = `<div class="strip" role="img" aria-label="${lo}–${hi} band ${fmt(m.demand_lo)} to ${fmt(m.demand_hi)} gpm; median ${fmt(m.demand_p50)}`
        + (m.produced_gpm != null ? `; produced ${fmt(m.produced_gpm)}` : '') + ' gpm">'
        + `<span class="sb" style="left:${x(m.demand_lo)}%;width:${(x(m.demand_hi) - x(m.demand_lo)).toFixed(2)}%"></span>`
        + `<span class="sm" style="left:${x(m.demand_p50)}%"></span>`
        + (m.produced_gpm != null ? `<span class="sp" style="left:${x(m.produced_gpm)}%"></span>` : '') + '</div>'
        + `<p class="key" aria-hidden="true"><span><i class="kb"></i>${lo}–${hi}</span><span><i class="km"></i>median</span>`
        + (m.produced_gpm != null ? `<span><i class="kp"></i>produced at ${hhmm(tr)}</span>` : '') + '</p>';
    }
    const pct = m.storage_mg != null && m.usable_mg ? Math.max(0, Math.min(100, m.storage_mg / m.usable_mg * 100)) : null;
    const meter = pct == null ? '' : `<div class="meter" role="img" aria-label="${fmt(pct)}% of usable storage"><i style="width:${pct.toFixed(1)}%"></i></div>`;
    $('#metrics').innerHTML = [
      metricCard('Demand', [[fmt(m.demand_p50, 0, '—'), 1], ['gpm', 0]], [
        `Forecast for ${dayLong(start)} (median)`,
        `${lo}–${hi}: ${fmt(m.demand_lo, 0, '?')}–${fmt(m.demand_hi, 0, '?')} gpm`,
        `Produced at ${hhmm(tr)}: ${m.produced_gpm == null ? 'no reading' : `${fmt(m.produced_gpm)} gpm`}`], strip),
      metricCard('Running Now', [[String(m.running_n), 1], ['pumps ·', 0], [fmt(m.running_gpm, 0, '—'), 1], ['gpm', 0]], [
        `Readings at ${dayLong(tr)}`,
        `${fmt(m.running_kw, 0, '—')} kW (pump table averages)`,
        m.running_no_reading.length ? `No reading for ${m.running_no_reading.join(', ')}` : '']),
      metricCard('Planner Recommends', [[String(m.planned_n), 1], ['pumps ·', 0], [fmt(m.planned_gpm, 0, '—'), 1], ['gpm', 0]], [
        `${fmt(m.planned_kw, 0, '—')} kW (${diff >= 0 ? '+' : '−'}${fmt(Math.abs(diff))} kW vs now)`,
        `Turn on: ${m.turn_on.join(', ') || 'none'}`,
        `Turn off: ${m.turn_off.join(', ') || 'none'}`]),
      metricCard('Storage and Plan', [[fmt(m.storage_mg, 2, '—'), 1], [`of ${fmt(m.usable_mg, 1, '—')} MG`, 0]], [
        (m.storage_measured ? 'Measured' : 'Assumed') + (m.storage_source ? `: ${m.storage_source}` : ''),
        `Plan ${v.status_label}, made ${made} (${v.age})`,
        v.stale ? `▲ Older than ${fmt(s.stale_after_minutes)} min: check the planner is running.` : ''], meter),
    ].join('');
  }

  function rowHtml(r, on, isDo) {
    const chk = !r.check ? '' : `<span class="chk chk-${esc(r.check.level)}">`
      + (r.check.level === 'warning' ? `<span class="ic">${WARN_ICON}</span><span class="sr">Warning: </span>`
        : '<span class="ic" aria-hidden="true">✓</span>')
      + `${esc(r.check.label)}</span>`;
    return `<tr class="row${isDo ? ' do' : ''}${on ? ' sel' : ''}" data-pump="${esc(r.pump)}" tabindex="${on ? 0 : -1}"${on ? ' aria-current="true"' : ''}>`
      + `<th scope="row" class="pump">${esc(r.pump)}</th><td>${chip(r)}</td>`
      + `<td class="num">${fmt(r.flow)}</td><td class="num">${fmt(r.head)}</td><td class="num">${fmt(r.kwh_per_kgal, 3)}</td><td>${chk}</td></tr>`;
  }

  function renderRows(v) {
    const sel = state.pump[v.section], groups = [];
    for (const r of v.rows) {
      const g = groupOf(r);
      if (!groups.length || groups[groups.length - 1].key !== g) groups.push({ key: g, rows: [] });
      groups[groups.length - 1].rows.push(r);
    }
    const html = groups.map((g) => `<tbody><tr class="grp"><th colspan="6" scope="rowgroup">${esc(GROUPS[g.key] || g.key)}`
      + `<span>${g.rows.length}</span></th></tr>${g.rows.map((r) => rowHtml(r, r.pump === sel, g.key === 'do')).join('')}</tbody>`).join('');
    const table = $('#pumps');
    table.querySelectorAll('tbody').forEach((b) => b.remove());
    table.insertAdjacentHTML('beforeend', html || '<tbody><tr><td colspan="6" class="muted">This plan lists no pumps.</td></tr></tbody>');
  }

  function renderPump(v) {
    const pv = pumpOf(v), s = settings();
    $('#curve-title').textContent = pv ? `${pv.pump} Pump Curve` : 'Pump Curve';
    $('#curve-action').innerHTML = pv ? `${chip(pv)}${esc(pv.why)}${pv.next ? ` Planned: ${esc(pv.next)}.` : ''}` : '';
    const legend = [];
    if (pv) {
      const { env, maker, fit } = pv;
      if (pv.chart && pv.chart.band) legend.push(['band', `Preferred range (${fmt(s.por_low_pct)}–${fmt(s.por_high_pct)}% of BEP)`]);
      if (env) legend.push(['usual', 'Usual range (middle 90%)']);
      if (pv.chart && pv.chart.bep) legend.push(['bep', 'Best efficiency point']);
      if (pv.flow != null && pv.head != null) legend.push(['now', `Now (${hhmm(parse(v.readings_at))} reading)`]);
      if (pv.expected) legend.push(['exp', 'Expected if started']);
      if (env) {
        const since = parse(env.since);
        legend.push(['cloud', `Running hours since ${MONTHS[since.getMonth()]} ${since.getFullYear()}`]);
      }
      if (maker) legend.push(['maker', 'Maker curve']);
      if (fit && fit.credible) legend.push(['fit', `Fitted to its history (R² ${fmt(fit.r2, 2)})`]);
    }
    $('#legend').innerHTML = legend.map(([k, t]) => `<li>${SW[k]}${esc(t)}</li>`).join('');
    $('#legend').hidden = !legend.length;
    drawChart(v, pv);
    $('#curve-notes').innerHTML = pv ? pv.notes.map((n) => `<li>${esc(n)}</li>`).join('') : '';
    const note = $('#oos-note');
    if ((pv ? pv.pump : null) !== state.notePump) {  // a different pump: show its saved reason, if any
      note.value = pv && pv.mark ? (pv.mark.note || '') : '';
      state.notePump = pv ? pv.pump : null;
    }
    if (state.msgPump && state.msgPump !== state.notePump && !state.busy) setMsg('');
    syncForm(pv);
  }

  function syncForm(pv) {
    const note = $('#oos-note'), btn = $('#oos-btn'), label = $('#oos-label');
    note.maxLength = settings().note_max;
    if (state.busy) {
      btn.textContent = 'Saving…';
      btn.disabled = note.disabled = true;
      return;
    }
    note.disabled = false;
    note.readOnly = false;
    if (!pv) {
      btn.textContent = 'Mark out of service';
      btn.disabled = note.disabled = true;
    } else if (pv.mark) {
      btn.textContent = 'Return to service';
      btn.disabled = false;
      note.readOnly = true;
      note.placeholder = 'No reason was given';
      label.textContent = 'Reason it was marked out of service';
    } else if (pv.oos) {
      btn.textContent = 'Not available to plan';
      btn.disabled = note.disabled = true;
      note.value = '';
      note.placeholder = 'This pump cannot be planned';
    } else {
      btn.textContent = 'Mark out of service';
      btn.disabled = false;
      note.placeholder = 'Reason for marking it out of service (optional)';
      label.textContent = 'Reason for marking it out of service';
    }
  }

  function renderTabs() {
    const names = state.data.section_names.length ? state.data.section_names : Object.keys(state.data.sections);
    $('#tabs').innerHTML = names.map((s, i) => {
      const on = s === state.section, has = !!state.data.sections[s];
      return `<button type="button" role="tab" id="tab-${i}" data-section="${esc(s)}" aria-selected="${on}" aria-controls="panel" tabindex="${on ? 0 : -1}">`
        + `<span class="dot dot-${esc(s)}" aria-hidden="true"></span>${esc(s)}${has ? '' : '<span class="none">no plan</span>'}</button>`;
    }).join('');
    const sel = document.querySelector('#tabs button[aria-selected="true"]');
    if (sel) $('#panel').setAttribute('aria-labelledby', sel.id);
  }

  function showStatus(title, text) {
    $('#status-title').textContent = title;
    $('#status-text').innerHTML = withCode(text);
    $('#status').hidden = false;
    $('#content').hidden = true;
    chart = null;
  }

  function render() {
    const data = state.data;
    renderTabs();
    $('#loaded').classList.remove('err');
    const v = view();
    if (!v) {
      const any = Object.keys(data.sections).length > 0;
      showStatus(any ? `No ${state.section} Plan Yet` : 'No Plans Yet',
        any ? `The plan store has no ${state.section} plan yet.` : (data.message || 'No plans yet.'));
      $('#loaded').textContent = `Updated ${hhmm(parse(data.loaded_at))}`;
      return;
    }
    $('#status').hidden = true;
    $('#content').hidden = false;
    $('#app').dataset.section = v.section;
    $('#loaded').textContent = `Updated ${hhmm(parse(data.loaded_at))} · plan for ${dayShort(parse(v.plan_start))}`;
    renderMetrics(v);
    $('#actions-title').textContent = `${v.section} Pump Actions`;
    $('#warnings').innerHTML = v.warnings.map((w) => `<p class="warn-line">▲ ${esc(w)}</p>`).join('');
    pumpOf(v);
    renderRows(v);
    renderPump(v);
    $('#plan-notes').innerHTML = (v.notes.length ? v.notes : ['No notes.']).map((n) => `<li>${esc(n)}</li>`).join('');
  }

  function setSection(sec, focusTab) {
    if (!state.data || sec === state.section) return;
    state.section = sec;
    history.replaceState(null, '', `#${encodeURIComponent(sec)}`);
    render();
    if (focusTab) {
      const b = [...document.querySelectorAll('#tabs button')].find((x) => x.dataset.section === sec);
      if (b) b.focus();
    }
  }

  function select(pump, focus) {
    const v = view();
    if (!v) return;
    state.pump[v.section] = pump;
    for (const tr of document.querySelectorAll('#pumps tr.row')) {
      const on = tr.dataset.pump === pump;
      tr.classList.toggle('sel', on);
      if (on) tr.setAttribute('aria-current', 'true'); else tr.removeAttribute('aria-current');
      tr.tabIndex = on ? 0 : -1;
      if (on && focus) { tr.focus({ preventScroll: true }); tr.scrollIntoView({ block: 'nearest' }); }
    }
    renderPump(v);
  }

  function setMsg(text, kind = '') {
    const el = $('#oos-msg');
    el.textContent = text;
    el.className = `form-msg${kind ? ` ${kind}` : ''}`;
    state.msgPump = text ? state.notePump : null;
  }

  let toastTimer = 0;
  function toast(msg) {
    const t = $('#toast');
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 6000);
  }

  // ------------------------------------------------------------------ talking to the server
  async function getJSON(url, init) {
    const res = await fetch(url, { cache: 'no-store', credentials: 'same-origin', ...init,
      headers: { Accept: 'application/json', ...((init && init.headers) || {}) } });
    const body = await res.json().catch(() => null);
    if (!res.ok) throw new Error((body && body.error) || `The server answered ${res.status} ${res.statusText}.`.replace(' .', '.'));
    if (!body) throw new Error('The server sent an answer the page could not read.');
    return body;
  }

  function apply(data) {
    state.data = data;
    const names = data.section_names.length ? data.section_names : Object.keys(data.sections);
    if (!names.includes(state.section)) {
      const fromHash = decodeURIComponent(location.hash.slice(1));
      state.section = names.includes(fromHash) ? fromHash : (names.find((s) => data.sections[s]) || names[0] || null);
    }
    render();
  }

  function load() {
    if (state.loading) return state.loading;
    state.lastTry = Date.now();
    $('#refresh').disabled = true;
    state.loading = getJSON('api/views')
      .then(apply)
      .catch((err) => {
        console.error(err);
        const msg = err instanceof TypeError ? 'The page could not reach its server.' : err.message;
        if (state.data) {
          const at = hhmm(parse(state.data.loaded_at));
          $('#loaded').textContent = `Refresh failed at ${hhmm(new Date())}; showing the plan loaded at ${at}`;
          $('#loaded').classList.add('err');
          toast(`Could not refresh: ${msg}`);
        } else {
          showStatus('The Plan Could Not Be Loaded', `${msg} Press Refresh to try again.`);
          $('#loaded').textContent = `Failed at ${hhmm(new Date())}`;
          $('#loaded').classList.add('err');
        }
      })
      .finally(() => {
        $('#refresh').disabled = false;
        state.loading = null;
        clearTimeout(state.timer);
        state.timer = setTimeout(load, settings().refresh_minutes * 60000);
      });
    return state.loading;
  }

  async function submitMark(e) {
    e.preventDefault();
    const v = view(), pv = pumpOf(v);
    if (!pv || state.busy || (pv.oos && !pv.mark)) return;
    const out = !pv.mark, note = out ? $('#oos-note').value.trim() : '';
    state.busy = true;
    syncForm(pv);
    setMsg(`${out ? 'Marking' : 'Returning'} ${pv.pump} ${out ? 'out of service' : 'to service'} and re-planning ${v.section}. `
      + 'This can take up to a minute.');
    try {
      const body = await getJSON('api/marks', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pump: pv.pump, out_of_service: out, note }) });
      state.busy = false;
      state.notePump = null;  // show the saved reason (or an empty box) once the new plan is in
      apply(body.views);
      setMsg(body.message, 'ok');
    } catch (err) {
      state.busy = false;
      syncForm(pumpOf(view()));
      setMsg(`▲ ${err instanceof TypeError ? 'The mark was not sent: the page could not reach its server. Try again.' : err.message}`, 'err');
    }
  }

  // ------------------------------------------------------------------ events
  $('#refresh').addEventListener('click', () => { load(); });
  $('#tabs').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-section]');
    if (b) setSection(b.dataset.section, false);
  });
  $('#tabs').addEventListener('keydown', (e) => {
    const names = [...document.querySelectorAll('#tabs button')].map((b) => b.dataset.section);
    const i = names.indexOf(state.section), n = names.length;
    const j = { ArrowRight: (i + 1) % n, ArrowLeft: (i - 1 + n) % n, Home: 0, End: n - 1 }[e.key];
    if (j == null || !n) return;
    e.preventDefault();
    setSection(names[j], true);
  });
  window.addEventListener('hashchange', () => {
    const sec = decodeURIComponent(location.hash.slice(1));
    if (state.data && state.data.section_names.includes(sec)) setSection(sec, false);
  });
  $('#pumps').addEventListener('click', (e) => { const tr = e.target.closest('tr.row'); if (tr) select(tr.dataset.pump, true); });
  $('#pumps').addEventListener('keydown', (e) => {
    const tr = e.target.closest('tr.row');
    if (!tr) return;
    const rows = [...document.querySelectorAll('#pumps tr.row')], i = rows.indexOf(tr);
    const j = { ArrowDown: Math.min(rows.length - 1, i + 1), ArrowUp: Math.max(0, i - 1), Home: 0, End: rows.length - 1 }[e.key];
    if (j == null) return;
    e.preventDefault();
    select(rows[j].dataset.pump, true);
  });
  const plot = $('#plot');
  plot.addEventListener('pointermove', onPointer);
  plot.addEventListener('pointerdown', onPointer);
  plot.addEventListener('pointerleave', hideTip);
  $('#oos-form').addEventListener('submit', submitMark);
  document.addEventListener('visibilitychange', () => {  // back on the page after a while: fetch the newest plan
    if (document.visibilityState === 'visible' && Date.now() - state.lastTry > settings().refresh_minutes * 60000) load();
  });
  let raf = 0;
  new ResizeObserver(() => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => {
      const v = view();
      if (v && !$('#content').hidden && plot.clientWidth !== lastW) drawChart(v, pumpOf(v));
    });
  }).observe(plot);

  load();
})();
