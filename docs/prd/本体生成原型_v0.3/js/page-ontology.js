/* 页面二：本体总览（五种视图 + 详情 + 持续更新 + 结构变化提示）
   makeOntoView(st, hooks) 建一个独立实例：页面用一个，应用搭建里每个部件各用一个（st 是该实例的视图与选中状态）。 */
function makeOntoView(st, hooks = {}) {
  const VIEWS = [['type', '类型图'], ['star', '实例星图'], ['struct', '数据结构'], ['matrix', '关系矩阵'], ['time', '时间视图']];
  const FONT = '-apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif';
  const METHOD_CLS = { '声明主键': 'm-decl', '声明外键': 'm-decl', '码表': 'm-decl', '画像规则': 'm-prof', '时序识别': 'm-ts', '码表 + 时序识别': 'm-ts', '大模型补全': 'm-llm', '推断': 'm-inf' };
  const mchip = m => `<span class="mchip ${METHOD_CLS[m] || 'm-prof'}">${esc(m)}</span>`;
  const DAYS = (() => { const a = []; for (let d = 20; d <= 31; d++) a.push('10-' + d); for (let d = 1; d <= 17; d++) a.push('11-' + String(d).padStart(2, '0')); return a; })();
  let v = null, cy = null, charts = [], timers = [], onResize = null;

  function ensure(o) {
    o.delta = o.delta || {}; o.extra = o.extra || {}; o.ver = o.ver || 12;
    if (!o.feed) o.feed = o.id === 'o1' ? [['02:05', '工单 +3、维保记录 +2（主题库 2026-11-17 分区）'], ['02:05', '工厂主题库：dwd_maintenance 新增字段 remark，等你确认'],
      ['11-16', '测点窗口统计 +20,736（10 分钟窗口）'], ['11-05', 'EQ003 状态改为「高风险」，按 11-02 生效（迟到的 T50 数据）'], ['10-20', '首次生成：7 个实体类型、7 条关系']] : [];
  }
  const total = o => o.types.reduce((a, k) => a + typeCount(o, k), 0);
  const srcTables = (o, k) => o.src.map(srcById).filter(Boolean).flatMap(s => s.tables.filter(t => t.to.includes(k)).map(t => t.name));
  const relKey = r => r[0] + '>' + r[1] + ':' + r[2];
  const conf = r => (r[5].match(/(\d+)%/) || [])[1];

  function render(view) {
    v = view; const o = curOnto(); ensure(o); if (!VIEWS.find(x => x[0] === st.view)) st.view = 'type'; st.pick = null;
    const rels = ontoRels(o);
    v.innerHTML = `<div class="page onto">
      ${barHtml(o)}
      <div id="banner"></div>
      <div class="vtabs"><div class="tabs" role="tablist">${VIEWS.map(([k, n]) => `<button role="tab" data-v="${k}" class="${st.view === k ? 'on' : ''}">${n}</button>`).join('')}</div><div class="vtools" id="vtools"></div></div>
      <div class="obody"><div class="card vcard" id="vbox"></div><aside class="card dpanel" id="dpanel"></aside></div>
    </div>`;
    $('#ontoSel', v).onchange = e => { S.cur = e.target.value; save(); st.pick = null; route(); };
    $$('.tabs button', v).forEach(b => b.onclick = () => { st.view = b.dataset.v; st.pick = null; mountView(); });
    banner(o); mountView();
    if (o.dynamic) timers.push(setInterval(() => live(o), 4500));
    onResize = () => charts.forEach(c => c.resize()); window.addEventListener('resize', onResize);
    return () => { cleanup(); timers.forEach(clearInterval); timers = []; window.removeEventListener('resize', onResize); };
  }
  function barHtml(o) {
    const rels = ontoRels(o);
    return `<div class="onto-bar card">
        <div class="ob-l"><label class="sr" for="ontoSel">选择本体</label>
          <div class="osel"><select id="ontoSel">${S.ontos.map(x => `<option value="${x.id}" ${x.id === o.id ? 'selected' : ''}>${esc(x.name)}</option>`).join('')}</select><svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg></div>
          <div class="badges">${o.modes.map(m => `<span class="badge ${m}">${m === 'ts' ? '时序' : '普通'}</span>`).join('')}${o.dynamic ? '<span class="badge dyn">动态</span>' : ''}</div>
          <div class="ob-src">来自 ${o.src.map(id => { const s = srcById(id); return s ? `<b>${esc(s.name)}</b>` : ''; }).join('、')}</div>
        </div>
        <div class="ob-stats"><div><b>${o.types.length}</b><small>实体类型</small></div><div><b>${rels.length}</b><small>关系</small></div><div><b id="stI">${fmt(total(o))}</b><small>实例</small></div><div><b id="stV">v${o.ver}</b><small>版本</small></div></div>
        <div class="ob-live">${o.dynamic ? `<span class="pill live"><i></i>持续更新中</span><small>最近更新 <b id="stU">${esc(o.updated)}</b></small>` : '<span class="pill gray">未开启持续更新</span>'}</div>
      </div>`;
  }
  function cleanup() { if (cy) { cy.destroy(); cy = null; } charts.forEach(c => c.dispose()); charts = []; }
  function mountView() {
    cleanup(); const o = curOnto();
    $$('.tabs button', v).forEach(b => b.classList.toggle('on', b.dataset.v === st.view));
    const box = $('#vbox', v), tools = $('#vtools', v); box.className = 'card vcard v-' + st.view; tools.innerHTML = ''; box.innerHTML = '';
    ({ type: vType, star: vStar, struct: vStruct, matrix: vMatrix, time: vTime })[st.view](o, box, tools);
    if (!st.pick) detail(o, dOnto(o));
  }

  // ---------- 结构变化提示 ----------
  function banner(o) {
    const el = $('#banner', v);
    if (o.pendingChange !== true || !o.src.includes('dp')) { el.innerHTML = ''; return; }
    el.innerHTML = `<div class="banner"><div class="bn-ic"><svg viewBox="0 0 24 24"><path d="M12 8v5M12 16.5v.5"/><path d="M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg></div>
      <div class="grow"><b>数据源结构有变化</b><p>工厂主题库 · <code>dwd_maintenance</code> 新增字段 <code>remark</code>（备注，2,311 行非空）。更新本体后，「维保记录」会增加属性「备注」。记录变化一直在自动写入，不受影响。</p></div>
      <button class="btn ghost" id="bnIgn">忽略</button><button class="btn primary" id="bnUpd">更新本体</button></div>`;
    $('#bnIgn', el).onclick = () => { o.pendingChange = 'ignored'; o.feed.unshift([now(), '忽略结构变化：dwd_maintenance.remark']); save(); banner(o); if (!st.pick) detail(o, dOnto(o)); toast('已忽略。以后需要时可以在数据源里重新生成', 'warn'); };
    $('#bnUpd', el).onclick = () => {
      const steps = ['读取新字段', '识别属性', '写入本体']; let i = 0;
      const paint = () => { $('.banner', el).classList.add('busy'); $('.banner', el).innerHTML = `<div class="bn-ic spin"><svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-3-6.7"/></svg></div><div class="grow"><b>正在更新本体</b>
        <div class="bn-steps">${steps.map((s, j) => `<span class="${j < i ? 'done' : j === i ? 'run' : ''}">${j < i ? '✓ ' : ''}${s}</span>`).join('<i>→</i>')}</div></div>`; };
      paint(); const t = setInterval(() => { i++; if (i < steps.length) return paint(); clearInterval(t);
        o.extra.mt = [['备注', '文本', 'remark', '更换后试运行 2 小时', '画像规则']]; o.pendingChange = false; o.ver++; o.updated = now();
        o.feed.unshift([now(), '结构更新：「维保记录」新增属性「备注」（dwd_maintenance.remark）']); save();
        if (hooks.widget) { toast('「维保记录」新增属性「备注」'); if (hooks.onOnto) hooks.onOnto(); return; }
        el.innerHTML = ''; $('#stV', v).textContent = 'v' + o.ver; if ($('#stU', v)) $('#stU', v).textContent = o.updated; toast('「维保记录」新增属性「备注」'); mountView();
        if (st.view !== 'struct') { st.pick = { k: 'mt' }; detail(o, dType(o, 'mt')); } }, 600);
      timers.push(t);
    };
  }

  // ---------- 持续更新（演示：每 4.5 秒来一批增量） ----------
  function live(o) {
    const r = Math.random(); let msg;
    if (r < .55 && o.types.includes('wo')) { const k = 1 + Math.floor(Math.random() * 3); o.delta.wo = (o.delta.wo || 0) + k; msg = `工单 +${k}（dwd_work_order 增量）`; if (o.types.includes('mt') && Math.random() < .5) { o.delta.mt = (o.delta.mt || 0) + 1; msg = `工单 +${k}、维保记录 +1`; } }
    else if (r < .8 && o.types.includes('eq')) { const e = 'EQ0' + (10 + Math.floor(Math.random() * 26)); const st = ['关注', '正常'][Math.floor(Math.random() * 2)]; msg = `${e} 状态 → ${st}（dwd_equip_status，旧记录已关闭）`; }
    else if (o.types.includes('point')) msg = '测点窗口统计 +144（新的 10 分钟窗口）';
    else { const k = o.types[0]; o.delta[k] = (o.delta[k] || 0) + 1; msg = `${typeDef(o, k).name} +1`; }
    o.updated = now(); o.feed.unshift([now(), msg]); o.feed = o.feed.slice(0, 30); save();
    if ($('#stI', v)) $('#stI', v).textContent = fmt(total(o)); if ($('#stU', v)) { $('#stU', v).textContent = o.updated; const p = $('.ob-live .pill', v); p.classList.remove('flash'); void p.offsetWidth; p.classList.add('flash'); }
    if (!st.pick) { const f = $('#feed', v); if (f) f.innerHTML = feedHtml(o); }
    if (cy && st.view === 'type') o.types.forEach(k => { const n = cy.getElementById(k); if (n.nonempty() && n.data('n') !== typeCount(o, k)) { n.data({ n: typeCount(o, k), img: countImg(typeCount(o, k)) }); } });
  }

  // ---------- 详情面板 ----------
  function detail(o, html) { const el = $('#dpanel', v); if (el) { el.innerHTML = html; el.scrollTop = 0; wireDetail(o, el); } if (hooks.onPick && !muting) hooks.onPick(st.pick, html); }
  const feedHtml = o => o.feed.slice(0, 12).map(([t, s]) => `<li><time>${esc(t)}</time><span>${esc(s)}</span></li>`).join('') || '<li class="sub">还没有变化</li>';
  function dOnto(o) {
    return `<div class="dp-h"><small>本体</small><h3>${esc(o.name)}</h3></div>
      <dl class="kv"><dt>类型</dt><dd>${o.modes.map(m => m === 'ts' ? '时序' : '普通').join(' + ')}${o.dynamic ? ' + 动态' : ''}</dd>
        <dt>来源</dt><dd>${o.src.map(id => { const s = srcById(id); return s ? `${esc(s.name)}<small class="sub">（${esc(s.access)}）</small>` : ''; }).join('<br>')}</dd>
        <dt>生成时间</dt><dd>${esc(o.created)}</dd><dt>最近更新</dt><dd>${esc(o.updated)}</dd></dl>
      ${o.dynamic ? `<h4>最近变化 <span class="pill live sm"><i></i>实时</span></h4><ul class="feed" id="feed">${feedHtml(o)}</ul>` : ''}
      <p class="hint">点击图里的类型、关系或实例，可以在这里查看详情。</p>`;
  }
  function dType(o, k) {
    const d = typeDef(o, k); const rels = ontoRels(o).filter(r => r[0] === k || r[1] === k); const ext = (o.extra[k] || []).map(a => a[0]);
    return `<div class="dp-h"><small>实体类型</small><h3><i class="dot" style="background:${d.color}"></i>${esc(d.name)}</h3><p class="sub">${fmt(typeCount(o, k))} 个实例 · 来自 ${srcTables(o, k).map(t => `<code>${esc(t)}</code>`).join(' ') || '—'}</p></div>
      <h4>属性 <small>${typeAttrs(o, k).length}</small></h4>
      <table class="tbl mini attrs"><thead><tr><th>属性</th><th>来源字段</th><th>识别方式</th></tr></thead><tbody>
        ${typeAttrs(o, k).map(a => `<tr class="${ext.includes(a[0]) ? 'new' : ''}"><td><b>${esc(a[0])}</b>${ext.includes(a[0]) ? ' <span class="flag new">新</span>' : ''}<small class="sub" title="${esc(a[1] + ' · ' + a[3])}">${esc(a[1])} · ${esc(a[3])}</small></td><td><code>${esc(a[2])}</code></td><td>${mchip(a[4])}</td></tr>`).join('')}
      </tbody></table>
      <h4>关系 <small>${rels.length}</small></h4>
      <div class="rel-list">${rels.map(r => `<button class="rel-it" data-rel="${esc(relKey(r))}"><span>${esc(typeDef(o, r[0]).name)}</span><b>${esc(r[2])}</b><span>${esc(typeDef(o, r[1]).name)}</span>${mchip(r[3])}</button>`).join('') || '<p class="sub">没有关系</p>'}</div>
      <div class="dp-actions">${st.view !== 'star' ? `<button class="btn sm" data-goview="star" data-type="${k}">在实例星图中查看</button>` : ''}${st.view !== 'struct' ? `<button class="btn sm" data-goview="struct" data-type="${k}">在数据结构中查看</button>` : ''}</div>`;
  }
  function dRel(o, r) {
    const a = typeDef(o, r[0]), b = typeDef(o, r[1]); const c = conf(r);
    return `<div class="dp-h"><small>关系</small><h3 class="rel-title"><span><i class="dot" style="background:${a.color}"></i>${esc(a.name)}</span><b>${esc(r[2])}</b><span><i class="dot" style="background:${b.color}"></i>${esc(b.name)}</span></h3></div>
      <dl class="kv"><dt>识别方式</dt><dd>${mchip(r[3])}</dd><dt>实例数</dt><dd class="num">${fmt(r[4])}</dd>${c ? `<dt>置信度</dt><dd><div class="conf"><i style="width:${c}%"></i></div>${c}%</dd>` : ''}<dt>依据</dt><dd>${esc(r[5])}</dd></dl>
      ${r[3] === '推断' ? '<p class="note">源表没有声明这条外键，是平台按字段名和值域重合推断出来的，所以单独标出来。</p>' : ''}
      ${r[3] === '时序识别' ? '<p class="note">从时序数据里识别出来：测点来自读数表的数值列，事件按窗口统计和规则生成。</p>' : ''}`;
  }
  function wireDetail(o, el) {
    $$('[data-rel]', el).forEach(b => b.onclick = () => { const r = ontoRels(o).find(r => relKey(r) === b.dataset.rel); st.pick = { rel: relKey(r) }; detail(o, dRel(o, r)); });
    $$('[data-goview]', el).forEach(b => b.onclick = () => { st.view = b.dataset.goview; st.pick = { k: b.dataset.type }; mountView(); detail(o, dType(o, b.dataset.type)); focusType(b.dataset.type); });
    $$('[data-time]', el).forEach(b => b.onclick = () => { st.view = 'time'; st.pick = null; mountView(); });
    if (hooks.widget) $$('[data-time]', el).forEach(b => b.onclick = () => toast('时间视图是单独的部件，可以在应用里添加「时间视图」'));
    if (hooks.widget) $$('[data-goview]', el).forEach(b => b.onclick = () => { const k = b.dataset.type; st.pick = { k }; if (hooks.onPick) hooks.onPick(st.pick, null); toast('已在应用里的其他部件中定位「' + typeDef(o, k).name + '」'); });
  }
  function focusType(k) {
    if (st.view === 'star' && cy) { const ns = cy.nodes(`[t = "${k}"]`); if (ns.nonempty()) { cy.elements().addClass('dim'); ns.removeClass('dim').addClass('hl'); cy.animate({ fit: { eles: ns, padding: 60 } }, { duration: 500 }); } }
    if (st.view === 'struct') { const c = $(`.scard[data-k="${k}"]`, v); if (c) { c.classList.add('pulse'); c.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' }); } }
  }

  // ========== 视图 1：类型图（Playground 风格） ==========
  function countImg(n) {
    const s = fmt(n).replace(' ', ''); const fs = s.length > 5 ? 22 : s.length > 3 ? 26 : 32;
    return 'data:image/svg+xml;utf8,' + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100"><text x="50" y="51" dominant-baseline="middle" text-anchor="middle" font-family="-apple-system,PingFang SC,Helvetica,Arial,sans-serif" font-size="${fs}" font-weight="700" fill="#ffffff">${s}</text></svg>`);
  }
  function vType(o, box, tools) {
    tools.innerHTML = `<span class="legend"><i class="lg-decl"></i>声明外键<i class="lg-inf"></i>推断<i class="lg-ts"></i>时序识别</span><button class="btn sm" id="fit">适应画布</button>`;
    box.innerHTML = '<div id="cy" class="cy grid-bg"></div><div class="canvas-tip">滚轮缩放 · 拖动节点调整位置 · 点击查看详情</div>';
    const els = o.types.map(k => { const d = typeDef(o, k); const n = typeCount(o, k); return { data: { id: k, name: d.name, color: d.color, n, img: countImg(n), size: Math.round(56 + 12 * Math.log10(n + 1)) }, position: Object.assign({}, posOf(k)) }; })
      .concat(ontoRels(o).map(r => ({ data: { id: relKey(r), source: r[0], target: r[1], label: r[2] }, classes: r[3] === '推断' ? 'inf' : r[3] === '时序识别' ? 'tsr' : 'decl' })));
    cy = window.__cy = cytoscape({ container: $('#cy', box), elements: els, layout: { name: 'preset', fit: true, padding: 50 }, minZoom: .3, maxZoom: 2.5, boxSelectionEnabled: false, wheelSensitivity: .25,
      style: [
        { selector: 'node', style: { width: 'data(size)', height: 'data(size)', 'background-color': 'data(color)', 'background-image': 'data(img)', 'background-width': '100%', 'background-height': '100%', 'background-clip': 'none',
          'border-width': 4, 'border-color': '#FFFFFF', 'border-opacity': .95, label: 'data(name)', 'text-valign': 'bottom', 'text-margin-y': 8, 'font-family': FONT, 'font-size': 14, 'font-weight': 600, color: '#17212B',
          'underlay-color': 'data(color)', 'underlay-opacity': .12, 'underlay-padding': 7, 'underlay-shape': 'ellipse', 'transition-property': 'opacity,underlay-opacity', 'transition-duration': 200 } },
        { selector: 'edge', style: { width: 2, 'curve-style': 'bezier', 'line-color': '#A8B4C0', 'target-arrow-color': '#A8B4C0', 'target-arrow-shape': 'triangle', 'arrow-scale': .9, label: 'data(label)', 'font-family': FONT, 'font-size': 12, color: '#3D4B58',
          'text-background-color': '#FFFFFF', 'text-background-opacity': 1, 'text-background-padding': 3, 'text-background-shape': 'roundrectangle', 'text-border-width': 1, 'text-border-color': '#E3E8EE', 'text-border-opacity': 1, 'transition-property': 'opacity', 'transition-duration': 200 } },
        { selector: 'edge.inf', style: { 'line-style': 'dashed', 'line-dash-pattern': [6, 4], 'line-color': '#8C7AE6', 'target-arrow-color': '#8C7AE6', color: '#5B45C9' } },
        { selector: 'edge.tsr', style: { 'line-color': '#22A88E', 'target-arrow-color': '#22A88E', color: '#137A64' } },
        { selector: '.dim', style: { opacity: .18 } },
        { selector: 'node.sel', style: { 'underlay-opacity': .35, 'underlay-padding': 10, 'border-color': '#17212B', 'border-width': 3 } },
        { selector: 'edge.sel', style: { width: 3.5 } },
      ] });
    const clear = () => cy.elements().removeClass('dim sel');
    cy.on('tap', 'node', e => { const n = e.target; clear(); cy.elements().not(n.closedNeighborhood()).addClass('dim'); n.addClass('sel'); st.pick = { k: n.id() }; detail(o, dType(o, n.id())); });
    cy.on('tap', 'edge', e => { const ed = e.target; clear(); cy.elements().not(ed.union(ed.connectedNodes())).addClass('dim'); ed.addClass('sel'); const r = ontoRels(o).find(r => relKey(r) === ed.id()); st.pick = { rel: ed.id() }; detail(o, dRel(o, r)); });
    cy.on('tap', e => { if (e.target === cy) { clear(); st.pick = null; detail(o, dOnto(o)); } });
    $('#fit', tools).onclick = () => cy.animate({ fit: { padding: 50 } }, { duration: 400 });
    if (st.pick && st.pick.k) { const n = cy.getElementById(st.pick.k); if (n.nonempty()) n.emit('tap'); }
  }

  // ========== 视图 2：实例星图（Semantica 风格） ==========
  function vStar(o, box, tools) {
    const { nodes, edges } = D.instances(o.types);
    if (!nodes.length) { box.innerHTML = '<div class="empty-box center"><b>这个本体没有实例样本</b><p>原型只为工厂设备相关的类型准备了实例样本。</p></div>'; return; }
    // 位置：4 条产线像星团分布在四周，设备围着产线，测点、工单等围着设备；人员在中间
    const pos = {}; const byId = {}; nodes.forEach(n => byId[n.id] = n);
    const LC = { 1: [-1, -1], 2: [1, -1], 3: [1, 1], 4: [-1, 1] };
    const kids = {}; nodes.forEach(n => { if (n.par) (kids[n.par] = kids[n.par] || []).push(n); });
    for (let l = 1; l <= 4; l++) { const cx = LC[l][0] * 470, cyy = LC[l][1] * 330; pos['L' + l] = { x: cx, y: cyy };
      const eqs = nodes.filter(n => n.t === 'eq' && n.line === l); eqs.forEach((e, i) => { const a = i / eqs.length * Math.PI * 2 + l; const R = 175 + (i % 2) * 40; pos[e.id] = { x: cx + R * Math.cos(a), y: cyy + R * Math.sin(a) };
        const ks = kids[e.id] || []; ks.forEach((c, j) => { const b = a + (j - (ks.length - 1) / 2) * .42; const rr = 44 + (j % 3) * 13; pos[c.id] = { x: pos[e.id].x + rr * Math.cos(b), y: pos[e.id].y + rr * Math.sin(b) }; }); }); }
    const st = nodes.filter(n => n.t === 'staff'); st.forEach((n, i) => { const a = i / st.length * Math.PI * 2; const R = 120 + (i % 3) * 26; pos[n.id] = { x: R * Math.cos(a) * 1.25, y: R * Math.sin(a) }; });
    const deg = {}; edges.forEach(([a, b]) => { deg[a] = (deg[a] || 0) + 1; deg[b] = (deg[b] || 0) + 1; }); const maxDeg = Math.max(...Object.values(deg));
    const COMM = { 1: '#4FC3F7', 2: '#FFB74D', 3: '#BA68C8', 4: '#81C784', '-1': '#B0BEC5' };
    const heat = d => { const x = Math.sqrt(d / maxDeg); const c = [[38, 70, 120], [30, 160, 200], [250, 190, 70], [240, 80, 60]]; const p = x * 3, i = Math.min(2, Math.floor(p)), f = p - i; return `rgb(${c[i].map((v, j) => Math.round(v + (c[i + 1][j] - v) * f)).join(',')})`; };
    const SIZE = { line: 30, eq: 15, staff: 8, point: 6, wo: 6, mt: 6, event: 7, ins: 6, meter: 6 };
    const els = nodes.map(n => ({ data: { id: n.id, label: n.t === 'line' || n.t === 'eq' ? n.label : '', full: n.label, t: n.t, d: n.d, line: n.line, risk: !!n.risk, deg: deg[n.id] || 0,
      cT: n.risk ? '#FF5A4E' : typeDef(o, n.t).color, cC: COMM[n.line] || COMM['-1'], cH: heat(deg[n.id] || 0), c: n.risk ? '#FF5A4E' : typeDef(o, n.t).color, s: SIZE[n.t] || 6 }, position: pos[n.id] || { x: 0, y: 0 }, classes: n.t }))
      .concat(edges.filter(([a, b]) => pos[a] && pos[b]).map(([a, b, r], i) => ({ data: { id: 'e' + i, source: a, target: b, r } })));
    box.innerHTML = `<div class="star-ctl">
        <div class="sc-row"><span>着色</span><div class="seg dark" id="cMode"><button data-c="cT" class="on">类型</button><button data-c="cC">社区</button><button data-c="cH">热度</button></div></div>
        <div class="sc-row"><span>点击</span><div class="seg dark" id="pMode"><button data-p="ego" class="on">看邻居</button><button data-p="path">找路径</button></div></div>
        <div class="sc-row"><span>查找</span><input id="find" placeholder="如 EQ003、U-1107" class="dark-in"></div>
        <div class="sc-row"><span>时间</span><input type="range" id="tl" min="0" max="${DAYS.length - 1}" value="${DAYS.length - 1}"><b id="tlL">截至 11-17</b></div>
      </div>
      <div id="cy" class="cy star-bg"></div>
      <div class="star-legend">${o.types.filter(k => nodes.some(n => n.t === k)).map(k => `<span><i style="background:${typeDef(o, k).color}"></i>${esc(typeDef(o, k).name)} ${nodes.filter(n => n.t === k).length}</span>`).join('')}<span><i style="background:#FF5A4E"></i>高风险设备</span>
        <em>样本 ${nodes.length} 个实例 · 本体共 ${fmt(total(o))} 个</em></div>
      <div class="star-tip" id="stip"></div>`;
    tools.innerHTML = '<button class="btn sm" id="rst">重置视图</button>';
    cy = window.__cy = cytoscape({ container: $('#cy', box), elements: els, layout: { name: 'preset', fit: true, padding: 30 }, minZoom: .2, maxZoom: 4, wheelSensitivity: .25, boxSelectionEnabled: false, textureOnViewport: true, motionBlur: false, autoungrabify: false,
      style: [
        { selector: 'node', style: { width: 'data(s)', height: 'data(s)', 'background-color': 'data(c)', 'border-width': 0, 'underlay-color': 'data(c)', 'underlay-opacity': .28, 'underlay-padding': 3, 'underlay-shape': 'ellipse',
          label: 'data(label)', color: '#D5E6F0', 'font-family': FONT, 'font-size': 7, 'text-valign': 'bottom', 'text-margin-y': 3, 'min-zoomed-font-size': 7, 'text-outline-color': '#08121A', 'text-outline-width': 1.5 } },
        { selector: 'node.line', style: { 'font-size': 26, 'font-weight': 600, 'text-margin-y': 6, 'underlay-opacity': .35, 'underlay-padding': 12, color: '#FFFFFF', 'min-zoomed-font-size': 0 } },
        { selector: 'node.eq', style: { 'underlay-padding': 5, 'underlay-opacity': .32 } },
        { selector: 'node[?risk]', style: { 'underlay-padding': 9, 'underlay-opacity': .55 } },
        { selector: 'edge', style: { width: .6, 'curve-style': 'haystack', 'haystack-radius': 0, 'line-color': '#6FA8C8', opacity: .22 } },
        { selector: '.dim', style: { opacity: .07 } },
        { selector: 'node.hl', style: { 'underlay-opacity': .6, 'underlay-padding': 6, label: 'data(full)', 'font-size': 8, 'min-zoomed-font-size': 0 } },
        { selector: 'edge.hl', style: { opacity: .85, width: 1.2, 'line-color': '#9FD3F0' } },
        { selector: 'node.path', style: { 'background-color': '#FFD54F', 'underlay-color': '#FFD54F', 'underlay-opacity': .7, 'underlay-padding': 7, label: 'data(full)', 'font-size': 9, color: '#FFF3C4', 'min-zoomed-font-size': 0 } },
        { selector: 'edge.path', style: { opacity: 1, width: 2.4, 'line-color': '#FFD54F' } },
        { selector: 'node.pa', style: { 'border-width': 2, 'border-color': '#FFD54F' } },
      ] });
    let pmode = 'ego', pa = null;
    const clear = () => { cy.elements().removeClass('dim hl path pa'); pa = null; $('#stip', box).textContent = ''; };
    const ego = n => { clear(); const nb = n.closedNeighborhood(); cy.elements().not(nb).addClass('dim'); nb.addClass('hl'); st.pick = { inst: n.id() }; detail(o, dInst(o, n)); };
    cy.on('tap', 'node', e => { const n = e.target;
      if (pmode === 'ego') return ego(n);
      if (!pa) { clear(); pa = n; n.addClass('pa'); $('#stip', box).textContent = `起点 ${n.data('full')}，再点一个终点`; return; }
      const r = cy.elements(':visible').aStar({ root: pa, goal: n, directed: false }); const a = pa; clear();
      if (!r.found) { $('#stip', box).textContent = '这两个实例之间没有路径'; return; }
      cy.elements().addClass('dim'); r.path.removeClass('dim').addClass('path'); st.pick = { path: 1 }; detail(o, dPath(o, r.path.nodes().map(x => x.data())));
      $('#stip', box).textContent = `${a.data('full')} → ${n.data('full')}：${r.path.nodes().length - 1} 步`; });
    cy.on('tap', e => { if (e.target === cy) { clear(); st.pick = null; detail(o, dOnto(o)); } });
    $$('#cMode button', box).forEach(b => b.onclick = () => { $$('#cMode button', box).forEach(x => x.classList.toggle('on', x === b)); cy.batch(() => cy.nodes().forEach(n => n.data('c', n.data(b.dataset.c)))); });
    $$('#pMode button', box).forEach(b => b.onclick = () => { $$('#pMode button', box).forEach(x => x.classList.toggle('on', x === b)); pmode = b.dataset.p; clear(); if (pmode === 'path') $('#stip', box).textContent = '找路径：先点起点，再点终点'; });
    $('#find', box).onkeydown = e => { if (e.key !== 'Enter') return; const q = e.target.value.trim().toUpperCase(); const n = cy.nodes().filter(n => n.id().toUpperCase() === q || n.data('full').toUpperCase() === q)[0];
      if (!n) return toast('没找到 ' + e.target.value, 'warn'); ego(n); cy.animate({ center: { eles: n }, zoom: 2 }, { duration: 500 }); };
    $('#tl', box).oninput = e => { const d = DAYS[+e.target.value]; $('#tlL', box).textContent = '截至 ' + d; cy.batch(() => cy.nodes().forEach(n => n.style('display', n.data('d') <= d ? 'element' : 'none'))); };
    $('#rst', tools).onclick = () => { clear(); $('#tl', box).value = DAYS.length - 1; $('#tl', box).oninput({ target: $('#tl', box) }); cy.animate({ fit: { padding: 30 } }, { duration: 400 }); st.pick = null; detail(o, dOnto(o)); };
    if (st.pick && st.pick.k) setTimeout(() => focusType(st.pick.k), 50);
  }
  function dInst(o, n) {
    const d = n.data(); const td = typeDef(o, d.t); const groups = {};
    n.connectedEdges().forEach(e => { const other = e.source().id() === d.id ? e.target() : e.source(); const k = e.data('r') + ' · ' + typeDef(o, other.data('t')).name; (groups[k] = groups[k] || []).push(other.data('full')); });
    const eq3 = d.id === 'EQ003';
    return `<div class="dp-h"><small>实例 · ${esc(td.name)}</small><h3><i class="dot" style="background:${d.risk ? '#FF5A4E' : td.color}"></i>${esc(d.full)}</h3>${d.full !== d.id ? `<p class="sub"><code>${esc(d.id)}</code></p>` : ''}</div>
      <dl class="kv"><dt>类型</dt><dd>${esc(td.name)}</dd>${d.line > 0 ? `<dt>产线</dt><dd>L${d.line}</dd>` : ''}<dt>出现时间</dt><dd>${esc(d.d)}</dd><dt>连接数</dt><dd class="num">${d.deg}</dd>
        ${d.t === 'eq' ? `<dt>状态</dt><dd>${d.risk ? '<span class="pill bad">高风险</span>' : '<span class="pill ok">正常</span>'}${eq3 ? '<small class="sub"> 11-02 起生效，11-05 记录</small>' : ''}</dd>` : ''}</dl>
      <h4>关联</h4>${Object.entries(groups).map(([k, a]) => `<div class="grp"><b>${esc(k)}</b><span>${a.slice(0, 8).map(x => `<code>${esc(x)}</code>`).join(' ')}${a.length > 8 ? ` 等 ${a.length} 个` : ''}</span></div>`).join('')}
      ${eq3 ? '<div class="dp-actions"><button class="btn sm" data-time="1">在时间视图中看 EQ003 的变化</button></div>' : ''}`;
  }
  function dPath(o, ns) {
    return `<div class="dp-h"><small>路径</small><h3>${ns.length - 1} 步</h3><p class="sub">无向最短路径（A*）</p></div>
      <ol class="path-list">${ns.map(d => `<li><i class="dot" style="background:${typeDef(o, d.t).color}"></i><b>${esc(d.full)}</b><span class="sub">${esc(typeDef(o, d.t).name)}</span></li>`).join('')}</ol>`;
  }

  // ========== 视图 3：数据结构 ==========
  const SPOS = { line: [30, 30], wo: [30, 300], staff: [30, 560], eq: [420, 190], mt: [420, 520], point: [810, 20], event: [810, 270], ins: [810, 540], meter: [1200, 20], tariff: [1200, 280] };
  function vStruct(o, box, tools) {
    tools.innerHTML = '<span class="legend"><i class="lg-decl"></i>声明外键<i class="lg-inf"></i>推断<i class="lg-ts"></i>时序识别</span>';
    let extraI = 0; const P = {}; o.types.forEach(k => { P[k] = SPOS[k] || [1200, 540 + 260 * extraI++]; });
    const used = Object.values(P); const minX = Math.min(...used.map(p => p[0])); Object.keys(P).forEach(k => P[k] = [P[k][0] - minX + 30, P[k][1]]);
    const W = Math.max(...Object.values(P).map(p => p[0])) + 290, H = Math.max(...Object.values(P).map(p => p[1])) + 240;
    const ext = k => (o.extra[k] || []).map(a => a[0]);
    box.innerHTML = `<div class="struct-wrap"><div class="struct" style="width:${W}px;height:${H}px"><svg class="wires" width="${W}" height="${H}"></svg>
      ${o.types.map(k => { const d = typeDef(o, k); return `<div class="scard" data-k="${k}" style="left:${P[k][0]}px;top:${P[k][1]}px;--c:${d.color}">
        <div class="sc-h"><i></i><b>${esc(d.name)}</b><span class="num">${fmt(typeCount(o, k))}</span></div>
        <div class="sc-src">${srcTables(o, k).map(t => `<code>${esc(t)}</code>`).join(' ') || '<code>—</code>'}</div>
        <table>${typeAttrs(o, k).map(a => `<tr class="${ext(k).includes(a[0]) ? 'new' : ''}"><td>${a[4] === '声明主键' ? '<span class="pk" title="主键">PK</span>' : a[1].includes('时间序列') || a[4] === '时序识别' ? '<span class="tsk" title="时序">∿</span>' : '<span class="nk"></span>'}${esc(a[0])}</td><td class="sub">${esc(a[1])}</td><td><code>${esc(a[2])}</code></td></tr>`).join('')}</table>
      </div>`; }).join('')}</div></div>`;
    const svg = $('.wires', box); const rects = {}; $$('.scard', box).forEach(c => rects[c.dataset.k] = { l: c.offsetLeft, t: c.offsetTop, r: c.offsetLeft + c.offsetWidth, b: c.offsetTop + c.offsetHeight });
    const rels = ontoRels(o); const ends = [];
    rels.forEach((r, i) => { const A = rects[r[0]], B = rects[r[1]]; let sa, sb;
      if (B.l > A.r + 20) { sa = 'r'; sb = 'l'; } else if (B.r < A.l - 20) { sa = 'l'; sb = 'r'; } else if (B.t > A.b) { sa = 'b'; sb = 't'; } else { sa = 't'; sb = 'b'; }
      ends.push({ i, k: r[0], s: sa, o: r[1] }, { i, k: r[1], s: sb, o: r[0] }); });
    const pt = {}; const grp = {}; ends.forEach(e => (grp[e.k + e.s] = grp[e.k + e.s] || []).push(e));
    Object.values(grp).forEach(g => { const R = rects[g[0].k]; const cen = k => { const q = rects[k]; return { x: (q.l + q.r) / 2, y: (q.t + q.b) / 2 }; };
      const vert = g[0].s === 'l' || g[0].s === 'r'; g.sort((a, b) => vert ? cen(a.o).y - cen(b.o).y : cen(a.o).x - cen(b.o).x);
      g.forEach((e, j) => { const f = (j + 1) / (g.length + 1);
        pt[e.i + ':' + e.k] = e.s === 'l' ? { x: R.l, y: R.t + 30 + (R.b - R.t - 40) * f, s: 'l' } : e.s === 'r' ? { x: R.r, y: R.t + 30 + (R.b - R.t - 40) * f, s: 'r' } : e.s === 't' ? { x: R.l + (R.r - R.l) * f, y: R.t, s: 't' } : { x: R.l + (R.r - R.l) * f, y: R.b, s: 'b' }; }); });
    const dir = { l: [-1, 0], r: [1, 0], t: [0, -1], b: [0, 1] }; const COL = { decl: '#8E9BA8', inf: '#8C7AE6', tsr: '#22A88E' };
    let html = `<defs>${Object.entries(COL).map(([k, c]) => `<marker id="ar-${k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0 10 5 0 10z" fill="${c}"/></marker>`).join('')}</defs>`;
    rels.forEach((r, i) => { const a = pt[i + ':' + r[0]], b = pt[i + ':' + r[1]]; const cls = r[3] === '推断' ? 'inf' : r[3] === '时序识别' ? 'tsr' : 'decl';
      const L = Math.max(50, Math.hypot(b.x - a.x, b.y - a.y) / 2.4); const c1 = { x: a.x + dir[a.s][0] * L, y: a.y + dir[a.s][1] * L }, c2 = { x: b.x + dir[b.s][0] * L, y: b.y + dir[b.s][1] * L };
      const m = { x: (a.x + 3 * c1.x + 3 * c2.x + b.x) / 8, y: (a.y + 3 * c1.y + 3 * c2.y + b.y) / 8 }; const lw = r[2].length * 13 + 14;
      html += `<g class="wire ${cls}" data-rel="${esc(relKey(r))}"><path d="M${a.x} ${a.y}C${c1.x} ${c1.y} ${c2.x} ${c2.y} ${b.x} ${b.y}" stroke="${COL[cls]}" marker-end="url(#ar-${cls})"/><path class="hit" d="M${a.x} ${a.y}C${c1.x} ${c1.y} ${c2.x} ${c2.y} ${b.x} ${b.y}"/>
        <rect x="${m.x - lw / 2}" y="${m.y - 10}" width="${lw}" height="20" rx="10"/><text x="${m.x}" y="${m.y + 4.5}" fill="${cls === 'decl' ? '#3D4B58' : COL[cls]}">${esc(r[2])}</text></g>`; });
    svg.innerHTML = html;
    $$('.scard', box).forEach(c => c.onclick = () => { $$('.scard', box).forEach(x => x.classList.toggle('on', x === c)); const k = c.dataset.k;
      $$('.wire', box).forEach(w => { const r = rels.find(r => relKey(r) === w.dataset.rel); w.classList.toggle('dim', r[0] !== k && r[1] !== k); }); st.pick = { k }; detail(o, dType(o, k)); });
    $$('.wire', box).forEach(w => w.onclick = e => { e.stopPropagation(); const r = rels.find(r => relKey(r) === w.dataset.rel); st.pick = { rel: w.dataset.rel }; detail(o, dRel(o, r)); });
    $('.struct', box).addEventListener('click', e => { if (e.target.classList.contains('struct') || e.target.tagName === 'svg') { $$('.scard', box).forEach(x => x.classList.remove('on')); $$('.wire', box).forEach(w => w.classList.remove('dim')); st.pick = null; detail(o, dOnto(o)); } });
    if (st.pick && st.pick.k) setTimeout(() => focusType(st.pick.k), 50);
  }

  // ========== 视图 4：关系矩阵 ==========
  function vMatrix(o, box) {
    box.innerHTML = '<div class="mx"><div class="mx-main"><h3>类型 × 类型 <small>格子里是关系名称和实例数，颜色越深实例越多</small></h3><div id="hm" class="chart"></div></div><div class="mx-side"><h3>各类型实例数</h3><div id="bars" class="chart"></div></div></div>';
    const ks = o.types; const names = ks.map(k => typeDef(o, k).name); const rels = ontoRels(o);
    const data = rels.map(r => [ks.indexOf(r[1]), ks.indexOf(r[0]), Math.log10(r[4] + 1), r]);
    const hm = echarts.init($('#hm', box)); charts.push(hm);
    hm.setOption({ textStyle: { fontFamily: FONT }, grid: { left: 86, right: 16, top: 34, bottom: 70 },
      tooltip: { formatter: p => { const r = p.data[3]; return `${typeDef(o, r[0]).name} <b>${r[2]}</b> ${typeDef(o, r[1]).name}<br>${r[3]} · ${fmt(r[4])} 条`; } },
      xAxis: { type: 'category', data: names, position: 'top', axisTick: { show: false }, axisLine: { show: false }, axisLabel: { color: '#3D4B58', fontSize: 12, interval: 0 }, splitArea: { show: true, areaStyle: { color: ['#FAFBFC', '#FFFFFF'] } }, name: '终点', nameLocation: 'start', nameTextStyle: { color: '#8A96A3' } },
      yAxis: { type: 'category', data: names, inverse: true, axisTick: { show: false }, axisLine: { show: false }, axisLabel: { color: '#3D4B58', fontSize: 12, interval: 0 }, splitArea: { show: true, areaStyle: { color: ['#FAFBFC', '#FFFFFF'] } }, name: '起点', nameTextStyle: { color: '#8A96A3' } },
      visualMap: { show: true, min: 0, max: 4, calculable: false, orient: 'horizontal', left: 'center', bottom: 8, itemWidth: 12, itemHeight: 160, text: ['多', '少'], textStyle: { color: '#8A96A3' }, inRange: { color: ['#D7ECF3', '#5BAFC9', '#0F6E8C', '#0A3F52'] }, dimension: 2 },
      series: [{ type: 'heatmap', data, itemStyle: { borderColor: '#fff', borderWidth: 3, borderRadius: 6 },
        label: { show: true, formatter: p => `{n|${p.data[3][2]}}\n{c|${fmt(p.data[3][4])}}`, rich: { n: { fontSize: 13, fontWeight: 600, color: '#fff', lineHeight: 18 }, c: { fontSize: 11, color: 'rgba(255,255,255,.8)' } } },
        emphasis: { itemStyle: { borderColor: '#17212B', borderWidth: 2 } } }] });
    hm.on('click', p => { const r = p.data[3]; st.pick = { rel: relKey(r) }; detail(o, dRel(o, r)); });
    const bars = echarts.init($('#bars', box)); charts.push(bars);
    const sorted = ks.slice().sort((a, b) => typeCount(o, a) - typeCount(o, b));
    bars.setOption({ textStyle: { fontFamily: FONT }, grid: { left: 70, right: 56, top: 10, bottom: 30 }, tooltip: { trigger: 'axis', axisPointer: { type: 'none' }, formatter: p => `${p[0].name}：${fmt(p[0].value)}` },
      xAxis: { type: 'log', logBase: 10, min: 1, axisLabel: { color: '#8A96A3', formatter: v => fmt(v) }, splitLine: { lineStyle: { color: '#EEF1F4' } } },
      yAxis: { type: 'category', data: sorted.map(k => typeDef(o, k).name), axisTick: { show: false }, axisLine: { lineStyle: { color: '#E3E8EE' } }, axisLabel: { color: '#3D4B58' } },
      series: [{ type: 'bar', barWidth: 14, data: sorted.map(k => ({ value: typeCount(o, k), itemStyle: { color: typeDef(o, k).color, borderRadius: [0, 4, 4, 0] } })), label: { show: true, position: 'right', color: '#5E6B78', formatter: p => fmt(p.value) } }] });
    bars.on('click', p => { const k = sorted[p.dataIndex]; st.pick = { k }; detail(o, dType(o, k)); });
  }

  // ========== 视图 5：时间视图 ==========
  function vTime(o, box) {
    const isDemo = o.id === 'o1'; const hasEq = o.types.includes('eq'), hasPt = o.types.includes('point');
    box.innerHTML = `<div class="tv">
      <div class="tv-card wide"><div class="tv-h"><h3>每天的变化量</h3><small>新增和关闭的事实条数（对数刻度）。10-20 是首次生成，之后每天随数据源增量更新。</small></div><div id="daily" class="chart"></div></div>
      <div class="tv-card"><div class="tv-h"><h3>站在哪天回看 · EQ003 的状态</h3><small>每条事实有两套时间：<b>有效时间</b>是事情在现实里从哪天起成立，<b>记录时间</b>是系统从哪天起知道。改错不覆盖旧记录，而是关掉旧的、写一条新的。</small></div><div id="bt"></div></div>
      <div class="tv-card"><div class="tv-h"><h3>EQ003 · T50 测点</h3><small>低压涡轮出口温度，10 分钟窗口均值（图中每 6 小时取一个点）。红色区域是识别出的高温事件。</small></div><div id="t50" class="chart"></div></div>
    </div>`;
    const daily = echarts.init($('#daily', box)); charts.push(daily);
    const days = isDemo ? Array.from({ length: 48 }, (_, i) => { const d = new Date(2026, 9, 1 + i); return String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }) : ['11-17'];
    const vals = isDemo ? D.daily.map((x, i) => i === 47 ? x + o.feed.length : x) : [total(o)];
    daily.setOption({ textStyle: { fontFamily: FONT }, grid: { left: 50, right: 20, top: 16, bottom: 26 }, tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: p => `${p[0].name}：${p[0].value ? fmt(p[0].value) + ' 条' : '尚未生成'}` },
      xAxis: { type: 'category', data: days, axisTick: { show: false }, axisLine: { lineStyle: { color: '#E3E8EE' } }, axisLabel: { color: '#8A96A3', interval: isDemo ? 6 : 0 } },
      yAxis: { type: 'log', min: 1, logBase: 10, splitLine: { lineStyle: { color: '#EEF1F4' } }, axisLabel: { color: '#8A96A3', formatter: v => fmt(v) } },
      series: [{ type: 'bar', data: vals.map((x, i) => ({ value: x || null, itemStyle: { color: isDemo && i === 19 ? '#0F6E8C' : '#7FBFD3', borderRadius: [3, 3, 0, 0] } })), barMaxWidth: 16,
        markPoint: isDemo ? { symbol: 'pin', symbolSize: 0, label: { show: true, color: '#0F6E8C', fontWeight: 600, formatter: '首次生成', offset: [0, -6] }, data: [{ coord: ['10-20', 6600] }] } : undefined }] });
    // 双时间
    const bt = $('#bt', box);
    if (!hasEq || !isDemo) bt.innerHTML = '<div class="empty-box"><b>这个本体还没有改过的事实</b><p>原型只给「工厂设备本体」里的 EQ003 准备了修改记录。</p></div>';
    else {
      let V = DAYS.indexOf('11-02'), R = DAYS.indexOf('11-04');
      const span = (a, b) => { const i = DAYS.indexOf(a), j = b ? DAYS.indexOf(b) : DAYS.length - 1; return `left:${i / (DAYS.length - 1) * 100}%;width:${Math.max(1.5, (j - i) / (DAYS.length - 1) * 100)}%`; };
      const paint = () => { const vd = DAYS[V], rd = DAYS[R];
        const cands = D.facts.filter(f => f.r0 <= rd && (!f.r1 || rd < f.r1) && f.v0 <= vd && (!f.v1 || vd < f.v1)); const hit = cands.sort((a, b) => b.v0 > a.v0 ? 1 : -1)[0];
        bt.innerHTML = `<div class="bt-ctl">
            <label><span>业务日期<small>事情发生在哪天</small></span><input type="range" id="vd" min="0" max="${DAYS.length - 1}" value="${V}"><b>${vd}</b></label>
            <label><span>系统所知日期<small>站在哪天回看</small></span><input type="range" id="rd" min="0" max="${DAYS.length - 1}" value="${R}"><b>${rd}</b></label></div>
          <div class="bt-quick"><button data-q="11-02,11-04">11-04 那天看 11-02</button><button data-q="11-02,11-17">今天看 11-02</button><button data-q="10-28,11-17">今天看 10-28</button></div>
          <div class="bt-ans ${hit ? (hit.st === '高风险' ? 'bad' : 'ok') : ''}"><small>按 ${rd} 时系统所知，EQ003 在 ${vd} 的状态</small><b>${hit ? hit.st : '还不知道'}</b><span>${hit ? `依据记录 #${hit.n}：${esc(hit.note)}` : '那天系统里还没有这台设备的记录'}</span></div>
          <div class="facts"><div class="f-axis"><span>10-20</span><span>11-02</span><span>11-17</span></div>
            ${D.facts.map(f => `<div class="fact ${hit && hit.n === f.n ? 'on' : ''}"><div class="f-l"><b>#${f.n}</b><span class="pill ${f.st === '高风险' ? 'bad' : 'ok'} sm">${f.st}</span></div>
              <div class="f-bars"><div class="f-row"><em>有效</em><div class="f-track"><i class="vb" style="${span(f.v0, f.v1)}"></i></div></div><div class="f-row"><em>记录</em><div class="f-track"><i class="rb ${f.r1 ? 'closed' : ''}" style="${span(f.r0, f.r1)}"></i></div></div></div>
              <div class="f-n">${esc(f.note)}${f.r1 ? `<small>记录于 ${f.r1} 关闭</small>` : ''}</div></div>`).join('')}
            <div class="f-cursor" style="left:calc(118px + (100% - 118px) * ${R / (DAYS.length - 1)})" title="系统所知日期"></div></div>`;
        $('#vd', bt).oninput = e => { V = +e.target.value; paint(); }; $('#rd', bt).oninput = e => { R = +e.target.value; paint(); };
        $$('[data-q]', bt).forEach(b => b.onclick = () => { const [a, c] = b.dataset.q.split(','); V = DAYS.indexOf(a); R = DAYS.indexOf(c); paint(); });
      };
      paint();
    }
    // T50
    const tb = $('#t50', box);
    if (!hasPt) { tb.innerHTML = '<div class="empty-box"><b>这个本体没有时序数据</b><p>生成时选上带时间列的读数表，就会有测点和窗口统计。</p></div>'; return; }
    const ch = echarts.init(tb); charts.push(ch);
    const xs = D.t50.map((_, i) => { const d = new Date(2026, 9, 20, i * 6); return String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0') + ' ' + String(d.getHours()).padStart(2, '0') + ':00'; });
    ch.setOption({ textStyle: { fontFamily: FONT }, grid: { left: 50, right: 70, top: 20, bottom: 28 }, tooltip: { trigger: 'axis', valueFormatter: v => v + ' °R' },
      xAxis: { type: 'category', data: xs, boundaryGap: false, axisTick: { show: false }, axisLine: { lineStyle: { color: '#E3E8EE' } }, axisLabel: { color: '#8A96A3', interval: 19, formatter: v => v.slice(0, 5) } },
      yAxis: { type: 'value', min: 1540, max: 1630, splitLine: { lineStyle: { color: '#EEF1F4' } }, axisLabel: { color: '#8A96A3' } },
      series: [{ type: 'line', data: D.t50, showSymbol: false, smooth: .3, lineStyle: { color: '#22A88E', width: 2 }, areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: 'rgba(34,168,142,.22)' }, { offset: 1, color: 'rgba(34,168,142,0)' }]) },
        markLine: { symbol: 'none', lineStyle: { color: '#D9443A', type: 'dashed' }, label: { formatter: '阈值 1,598', color: '#D9443A', position: 'end' }, data: [{ yAxis: D.THRESH }] },
        markArea: { itemStyle: { color: 'rgba(217,68,58,.12)' }, label: { color: '#D9443A', fontSize: 11 }, data: [[{ name: '高温事件', xAxis: xs[53] }, { xAxis: xs[59] }]] } }] });
  }

  // ---------- 作为「应用搭建」部件使用 ----------
  const WIDGET_VIEWS = { type: vType, star: vStar, struct: vStruct, matrix: vMatrix, time: vTime };
  let muting = false;
  function mountWidget(kind, root) {
    v = root; const o = curOnto(); ensure(o); cleanup(); st.view = kind;
    if (WIDGET_VIEWS[kind]) {
      root.innerHTML = `<div class="wg-view"><div class="vtools wg-vtools" id="vtools"></div><div class="vcard wg-vbox v-${kind}" id="vbox"></div></div><div id="dpanel" hidden></div>`;
      WIDGET_VIEWS[kind](o, $('#vbox', root), $('#vtools', root));
    } else if (kind === 'detail') { root.innerHTML = '<div class="dpanel wg-dpanel" id="dpanel"></div>'; muting = true; detail(o, dOnto(o)); muting = false; }
    else if (kind === 'bar') { root.innerHTML = barHtml(o); $('#ontoSel', root).onchange = e => { S.cur = e.target.value; save(); if (hooks.onOnto) hooks.onOnto(); }; }
    else if (kind === 'banner') { root.innerHTML = '<div id="banner"></div>'; banner(o); if (!$('.banner', root)) root.innerHTML = '<div class="wg-quiet">数据源结构没有变化。出现变化时，这里会提示是否更新本体。</div>'; }
    else if (kind === 'feed') { root.innerHTML = o.dynamic ? `<ul class="feed wg-feed" id="feed">${feedHtml(o)}</ul>` : '<div class="wg-quiet">这个本体没有开启持续更新。</div>'; }
    onResize = () => charts.forEach(c => c.resize()); window.addEventListener('resize', onResize);
    return () => { cleanup(); timers.forEach(clearInterval); timers = []; window.removeEventListener('resize', onResize); };
  }
  // 别的部件选中了类型 / 关系 / 实例：详情部件显示详情，视图部件高亮
  function showPick(pick, html) {
    st.pick = pick; const o = curOnto();
    if (st.view === 'detail') { muting = true; detail(o, html || pickHtml(o, pick)); muting = false; return; }
    const k = pick && pick.k; if (!k) return;
    if (st.view === 'type' && cy) { const n = cy.getElementById(k); if (n.nonempty()) { cy.elements().removeClass('dim sel'); cy.elements().not(n.closedNeighborhood()).addClass('dim'); n.addClass('sel'); } }
    else focusType(k);
  }
  // 应用里的其他模块（表格、树、桑基图等）只传来选中对象，没有现成的详情 HTML，这里补上
  function pickHtml(o, p) {
    if (!p) return dOnto(o);
    if (p.k && o.types.includes(p.k)) return dType(o, p.k);
    if (p.rel) { const r = ontoRels(o).find(r => relKey(r) === p.rel); if (r) return dRel(o, r); }
    if (p.inst) {
      const { nodes, edges } = D.instances(o.types); const n = nodes.find(x => x.id === p.inst); if (!n) return dOnto(o);
      const td = typeDef(o, n.t); const groups = {};
      edges.forEach(e => { if (e[0] !== n.id && e[1] !== n.id) return; const oid = e[0] === n.id ? e[1] : e[0]; const x = nodes.find(m => m.id === oid); const key = e[2] + ' · ' + (x ? typeDef(o, x.t).name : ''); (groups[key] = groups[key] || []).push(x ? (x.par && x.t === 'point' ? x.par + '.' + x.label : x.label) : oid); });
      return `<div class="dp-h"><small>实例 · ${esc(td.name)}</small><h3><i class="dot" style="background:${n.risk ? '#FF5A4E' : td.color}"></i>${esc(n.label)}</h3><p class="sub"><code>${esc(n.id)}</code> · 出现于 ${esc(n.d)}${n.risk ? ' · <b style="color:#D9443A">高风险</b>' : ''}</p></div>`
        + Object.entries(groups).map(([k, l]) => `<h4>${esc(k)} <small>${l.length}</small></h4><p class="sub">${l.slice(0, 12).map(esc).join('、')}${l.length > 12 ? ' 等' : ''}</p>`).join('');
    }
    return dOnto(o);
  }
  function tick() { live(curOnto()); }
  function paint() { const o = curOnto(); if ($('#stI', v)) $('#stI', v).textContent = fmt(total(o)); if ($('#stU', v)) $('#stU', v).textContent = o.updated; const f = $('#feed', v); if (f && (st.view === 'feed' || !st.pick)) f.innerHTML = feedHtml(o); }

  return { title: '本体总览', render, mountWidget, showPick, tick, paint };
}
const PageOntology = makeOntoView(UI);
