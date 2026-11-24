/* 应用搭建的模块库：只放「本体生成 / 本体展示」相关的模块（中游），不放数据接入和业务分析。
   每个模块：k 键、g 分组、name、desc、ref 参考的开源项目、multi 能否添加多次、h 默认高度、opts 配置项、follows 是否跟随选中、render(el, w, ctx)
   ctx = { o 当前本体, pick 当前选中, setPick(pick), chart(el) 建 echarts 并登记, cy(opts) 建 cytoscape 并登记 } */
const WS_MODULES = (() => {
  const FONT = '-apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif';
  const AX = { axisLine: { lineStyle: { color: '#E3E8EE' } }, axisTick: { show: false }, axisLabel: { color: '#8A96A3', fontSize: 11 }, splitLine: { lineStyle: { color: '#EEF1F4' } } };
  const total = o => o.types.reduce((a, k) => a + typeCount(o, k), 0);
  const tName = (o, k) => typeDef(o, k).name;
  const relKey = r => r[0] + '>' + r[1] + ':' + r[2];
  const srcTables = (o, k) => o.src.map(srcById).filter(Boolean).flatMap(s => s.tables.filter(t => t.to.includes(k)).map(t => t.name));
  const METHOD_CLS = { '声明主键': 'm-decl', '声明外键': 'm-decl', '码表': 'm-decl', '画像规则': 'm-prof', '时序识别': 'm-ts', '码表 + 时序识别': 'm-ts', '大模型补全': 'm-llm', '推断': 'm-inf' };
  const mchip = m => `<span class="mchip ${METHOD_CLS[m] || 'm-prof'}">${esc(m)}</span>`;
  const GROUP = { line: '生产', eq: '生产', point: '时序', event: '时序', wo: '运维', mt: '运维', ins: '运维', staff: '组织', meter: '能耗', tariff: '能耗' };
  const typeOpts = ctx => ctx.o.types.map(k => [k, tName(ctx.o, k)]);
  const dayLabel = i => { const d = new Date(2026, 9, 1 + i); return String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); };
  const confirmState = () => { S.relConfirm = S.relConfirm || {}; return S.relConfirm; };
  const pickType = (ctx, w) => (w.follow !== 'fixed' && ctx.pick && ctx.pick.k && ctx.o.types.includes(ctx.pick.k)) ? ctx.pick.k : (w.type && ctx.o.types.includes(w.type) ? w.type : ctx.o.types.includes('eq') ? 'eq' : ctx.o.types[0]);
  const instOf = (ctx, id) => D.instances(ctx.o.types).nodes.find(n => n.id === id);
  const pickInst = (ctx, w) => { const id = (w.follow !== 'fixed' && ctx.pick && ctx.pick.inst) || w.inst || 'EQ003'; return instOf(ctx, id) || D.instances(ctx.o.types).nodes.find(n => n.t === 'eq'); };
  const empty = (el, t) => { el.innerHTML = `<div class="wg-quiet">${t}</div>`; };

  const M = [];
  const add = (g, k, name, desc, ref, opt, render) => M.push(Object.assign({ g, k, name, desc, ref, multi: false, h: 'm', render }, opt));

  // ================= 概览 =================
  add('概览', 'stats', '本体指标卡', '类型、属性、关系、实例、数据源、版本六个数字', 'Semantica 本体摘要、Grafana Stat、WebProtégé 指标', { h: 'xs' }, (el, w, ctx) => {
    const o = ctx.o; const attrs = o.types.reduce((a, k) => a + typeAttrs(o, k).length, 0);
    el.innerHTML = `<div class="wm-stats">${[['实体类型', o.types.length], ['属性', attrs], ['关系', ontoRels(o).length], ['实例', fmt(total(o))], ['数据源', o.src.length], ['版本', 'v' + o.ver]].map(([l, n]) => `<div><b>${n}</b><small>${l}</small></div>`).join('')}</div>`;
  });
  add('概览', 'share', '类型实例占比', '各类型实例数占比的矩形树图，点一块选中该类型', 'Superset 矩形树图、Metabase', {}, (el, w, ctx) => {
    const o = ctx.o; const ch = ctx.chart(el);
    ch.setOption({ textStyle: { fontFamily: FONT }, tooltip: { formatter: p => `${p.name}：${fmt(p.value)} 个` },
      series: [{ type: 'treemap', roam: false, nodeClick: false, breadcrumb: { show: false }, top: 6, left: 6, right: 6, bottom: 6, label: { formatter: p => `${p.name}\n${fmt(p.value)}`, fontSize: 13 },
        data: o.types.map(k => ({ name: tName(o, k), value: typeCount(o, k), k, itemStyle: { color: typeDef(o, k).color, borderColor: '#fff', borderWidth: 2 } })) }] });
    ch.on('click', p => p.data && p.data.k && ctx.setPick({ k: p.data.k }));
  });
  add('概览', 'gen', '生成入口', '跳到数据源，勾选数据后一键生成本体', '—', { h: 'xs' }, (el, w, ctx) => {
    const o = ctx.o;
    el.innerHTML = `<div class="ws-gen"><div><b>${S.ontos.length} 个本体</b><small>当前：${esc(o.name)}，来自 ${o.src.map(id => (srcById(id) || {}).name).filter(Boolean).join('、')}</small></div><button class="btn primary sm">去生成本体</button></div>`;
    $('button', el).onclick = () => go('sources');
  });

  // ================= 结构 =================
  add('结构', 'tree', '类型层级树', '按分组 → 类型 → 属性展开的树，点类型选中', 'WebProtégé 类层级、GraphDB 类层级、Superset Tree', { h: 'l' }, (el, w, ctx) => {
    const o = ctx.o; const groups = {};
    o.types.forEach(k => { const g = GROUP[k] || '其他'; (groups[g] = groups[g] || []).push(k); });
    const data = { name: o.name, children: Object.entries(groups).map(([g, ks]) => ({ name: g, children: ks.map(k => ({ name: `${tName(o, k)}（${fmt(typeCount(o, k))}）`, k, collapsed: true, itemStyle: { color: typeDef(o, k).color }, children: typeAttrs(o, k).map(a => ({ name: a[0] + ' · ' + a[1] })) })) })) };
    const ch = ctx.chart(el);
    ch.setOption({ textStyle: { fontFamily: FONT }, tooltip: { show: false }, series: [{ type: 'tree', data: [data], top: 12, left: 70, bottom: 12, right: 150, symbolSize: 9, initialTreeDepth: 2, expandAndCollapse: true,
      label: { position: 'left', verticalAlign: 'middle', align: 'right', fontSize: 12 }, leaves: { label: { position: 'right', align: 'left' } }, lineStyle: { color: '#C3CCD5' } }] });
    ch.on('click', p => p.data && p.data.k && ctx.setPick({ k: p.data.k }));
  });
  add('结构', 'attrs', '属性清单表', '全部属性：所属类型、数据类型、来源字段、识别方式；可跟随选中类型', 'OpenMetadata 字段结构、WebProtégé 属性面板', { follows: true, h: 'l', opts: [{ key: 'follow', label: '范围', choices: () => [['all', '全部类型'], ['pick', '跟随选中类型']] }] }, (el, w, ctx) => {
    const o = ctx.o; const ks = w.follow === 'pick' && ctx.pick && ctx.pick.k ? [ctx.pick.k] : o.types;
    el.innerHTML = `<div class="wm-scroll"><table class="tbl mini"><thead><tr><th>类型</th><th>属性</th><th>数据类型</th><th>来源字段</th><th>识别方式</th></tr></thead><tbody>
      ${ks.flatMap(k => typeAttrs(o, k).map(a => `<tr><td><i class="dot" style="background:${typeDef(o, k).color}"></i>${esc(tName(o, k))}</td><td><b>${esc(a[0])}</b></td><td>${esc(a[1])}</td><td><code>${esc(a[2])}</code></td><td>${mchip(a[4])}</td></tr>`)).join('')}</tbody></table></div>`;
  });
  add('结构', 'typecard', '类型卡片', '一个类型的实例数、属性、关系、来源表；可放多张，每张看一个类型', 'Semantica 类型面板、DataHub 实体页', { multi: true, follows: true, h: 'm', opts: [{ key: 'type', label: '类型', choices: typeOpts }, { key: 'follow', label: '选中时', choices: () => [['fixed', '固定这个类型'], ['pick', '跟随选中类型']] }] }, (el, w, ctx) => {
    const o = ctx.o; const k = pickType(ctx, w); const d = typeDef(o, k); const rels = ontoRels(o).filter(r => r[0] === k || r[1] === k);
    el.innerHTML = `<div class="wm-tc"><div class="wm-tc-h"><i style="background:${d.color}"></i><div><b>${esc(d.name)}</b><small>${fmt(typeCount(o, k))} 个实例 · 来自 ${srcTables(o, k).map(t => `<code>${esc(t)}</code>`).join(' ') || '—'}</small></div></div>
      <div class="wm-tc-sec">属性 ${typeAttrs(o, k).length}</div><div class="wm-chips">${typeAttrs(o, k).map(a => `<span title="${esc(a[2] + ' · ' + a[4])}">${esc(a[0])}</span>`).join('')}</div>
      <div class="wm-tc-sec">关系 ${rels.length}</div>${rels.map(r => `<div class="wm-rel">${esc(tName(o, r[0]))} <b>${esc(r[2])}</b> ${esc(tName(o, r[1]))} ${mchip(r[3])}</div>`).join('') || '<p class="sub">没有关系</p>'}</div>`;
  });
  add('结构', 'rels', '关系清单', '全部关系：起点、名称、终点、识别方式、实例数、依据', 'GraphDB 类关系图、OpenMetadata 外键', { h: 'm' }, (el, w, ctx) => {
    const o = ctx.o;
    el.innerHTML = `<div class="wm-scroll"><table class="tbl mini"><thead><tr><th>起点</th><th>关系</th><th>终点</th><th>识别方式</th><th class="r">实例数</th><th>依据</th></tr></thead><tbody>
      ${ontoRels(o).map(r => `<tr data-rk="${esc(relKey(r))}"><td>${esc(tName(o, r[0]))}</td><td><b>${esc(r[2])}</b></td><td>${esc(tName(o, r[1]))}</td><td>${mchip(r[3])}</td><td class="r num">${fmt(r[4])}</td><td class="sub wm-ev">${esc(r[5])}</td></tr>`).join('')}</tbody></table></div>`;
    $$('tr[data-rk]', el).forEach(tr => tr.onclick = () => ctx.setPick({ rel: tr.dataset.rk }));
  });
  add('结构', 'chord', '关系弦图', '类型围成一圈，连线粗细表示关系实例数', 'Superset 弦图、WebVOWL 力导布局', {}, (el, w, ctx) => {
    const o = ctx.o; const ch = ctx.chart(el); const max = Math.max(...ontoRels(o).map(r => r[4]), 1);
    ch.setOption({ textStyle: { fontFamily: FONT }, tooltip: { formatter: p => p.dataType === 'edge' ? `${p.data.n}：${fmt(p.data.v)} 条` : `${p.name}：${fmt(p.value)} 个` },
      series: [{ type: 'graph', layout: 'circular', circular: { rotateLabel: true }, label: { show: true, position: 'right', fontSize: 12 }, lineStyle: { curveness: .3, opacity: .55 },
        data: o.types.map(k => ({ name: tName(o, k), k, value: typeCount(o, k), symbolSize: 14 + 6 * Math.log10(typeCount(o, k) + 1), itemStyle: { color: typeDef(o, k).color } })),
        links: ontoRels(o).map(r => ({ source: tName(o, r[0]), target: tName(o, r[1]), n: r[2], v: r[4], lineStyle: { width: 1 + 7 * r[4] / max, color: typeDef(o, r[0]).color } })) }] });
    ch.on('click', p => p.data && p.data.k && ctx.setPick({ k: p.data.k }));
  });
  add('结构', 'doc', '本体说明书', '按类型自动生成的文字说明：来源、属性、关系，可直接发给企业', 'pyLODE、WIDOCO、LODE 本体文档生成', { h: 'l' }, (el, w, ctx) => {
    const o = ctx.o;
    el.innerHTML = `<div class="wm-doc"><p class="sub">由本体自动生成，共 ${o.types.length} 个类型。</p>${o.types.map(k => { const rels = ontoRels(o).filter(r => r[0] === k);
      return `<h4><i class="dot" style="background:${typeDef(o, k).color}"></i>${esc(tName(o, k))} <small>${fmt(typeCount(o, k))} 个</small></h4><p>来自 ${srcTables(o, k).map(t => `<code>${esc(t)}</code>`).join('、') || '—'}。属性：${typeAttrs(o, k).map(a => esc(a[0])).join('、')}。${rels.length ? '关系：' + rels.map(r => `${esc(r[2])}「${esc(tName(o, r[1]))}」`).join('、') + '。' : ''}</p>`; }).join('')}</div>`;
  });

  // ================= 实例 =================
  add('实例', 'insttable', '实例表格', '某个类型的实例列表，点一行选中该实例；可放多张', 'NocoBase 表格区块、Superset 表格', { multi: true, h: 'm', opts: [{ key: 'type', label: '类型', choices: typeOpts }] }, (el, w, ctx) => {
    const o = ctx.o; const k = w.type && o.types.includes(w.type) ? w.type : (o.types.includes('eq') ? 'eq' : o.types[0]);
    const { nodes, edges } = D.instances(o.types); const rows = nodes.filter(n => n.t === k);
    if (!rows.length) return empty(el, '这个类型没有实例样本');
    const deg = {}; edges.forEach(([a, b]) => { deg[a] = (deg[a] || 0) + 1; deg[b] = (deg[b] || 0) + 1; });
    el.innerHTML = `<div class="wm-scroll"><table class="tbl mini"><thead><tr><th>${esc(tName(o, k))}</th><th>产线</th><th>日期</th><th class="r">关联数</th></tr></thead><tbody>
      ${rows.slice(0, 80).map(n => `<tr data-i="${esc(n.id)}" class="${ctx.pick && ctx.pick.inst === n.id ? 'on' : ''}"><td><b>${esc(n.label)}</b>${n.risk ? ' <span class="pill bad sm">高风险</span>' : ''}</td><td>${n.line > 0 ? 'L' + n.line : '—'}</td><td>${esc(n.d)}</td><td class="r num">${deg[n.id] || 0}</td></tr>`).join('')}</tbody></table>
      <p class="sub wm-foot">样本 ${rows.length} 个 · 本体共 ${fmt(typeCount(o, k))} 个</p></div>`;
    $$('tr[data-i]', el).forEach(tr => tr.onclick = () => ctx.setPick({ inst: tr.dataset.i }));
  });
  add('实例', 'instcard', '实例详情卡', '一个实例的类型、所属、邻居数量；默认 EQ003，可跟随选中；可放多张', 'Metabase 对象详情、Twenty 记录页', { multi: true, follows: true, h: 'm', opts: [{ key: 'follow', label: '选中时', choices: () => [['pick', '跟随选中实例'], ['fixed', '固定 EQ003']] }] }, (el, w, ctx) => {
    const o = ctx.o; const n = pickInst(ctx, w); if (!n) return empty(el, '没有实例');
    const { nodes, edges } = D.instances(o.types); const nb = edges.filter(e => e[0] === n.id || e[1] === n.id).map(e => e[0] === n.id ? e[1] : e[0]);
    const byT = {}; nb.forEach(id => { const x = nodes.find(m => m.id === id); if (x) byT[x.t] = (byT[x.t] || 0) + 1; });
    el.innerHTML = `<div class="wm-tc"><div class="wm-tc-h"><i style="background:${typeDef(o, n.t).color}"></i><div><b>${esc(n.label)}</b><small>${esc(tName(o, n.t))} · ${esc(n.id)}${n.line > 0 ? ' · L' + n.line : ''} · 出现于 ${esc(n.d)}</small></div>${n.risk ? '<span class="pill bad sm">高风险</span>' : ''}</div>
      <div class="wm-tc-sec">关联对象 ${nb.length}</div>${Object.entries(byT).map(([t, c]) => `<div class="wm-rel"><i class="dot" style="background:${typeDef(o, t).color}"></i>${esc(tName(o, t))} <b>${c}</b></div>`).join('') || '<p class="sub">没有关联</p>'}
      ${n.id === 'EQ003' ? '<div class="note">11-02 起 T50 连续 3 个窗口超阈值，11-05 按 11-02 生效改为「高风险」（迟到数据，双时间记录）。</div>' : ''}</div>`;
  });
  add('实例', 'nbr', '邻居网络', '选中实例一跳以内的邻居；默认 EQ003', 'Semantica 邻域面板、Neo4j Browser、AntV G6', { follows: true, h: 'm' }, (el, w, ctx) => {
    const o = ctx.o; const n = pickInst(ctx, w); if (!n) return empty(el, '没有实例');
    const { nodes, edges } = D.instances(o.types); const es = edges.filter(e => e[0] === n.id || e[1] === n.id).slice(0, 40); const ids = new Set([n.id]); es.forEach(e => { ids.add(e[0]); ids.add(e[1]); });
    el.innerHTML = '<div class="wm-cy"></div>';
    const els = [...ids].map(id => { const x = nodes.find(m => m.id === id); return { data: { id, label: x ? x.label : id, color: x ? typeDef(o, x.t).color : '#999', c: id === n.id ? 1 : 0 } }; })
      .concat(es.map((e, i) => ({ data: { id: 'e' + i, source: e[0], target: e[1], label: e[2] } })));
    const cy = ctx.cy({ container: $('.wm-cy', el), elements: els, layout: { name: 'concentric', concentric: x => x.data('c') ? 2 : 1, levelWidth: () => 1, padding: 20, minNodeSpacing: 18 },
      style: [{ selector: 'node', style: { 'background-color': 'data(color)', label: 'data(label)', 'font-size': 10, 'font-family': FONT, width: 14, height: 14, color: '#3D4B58', 'text-valign': 'bottom', 'text-margin-y': 3 } },
        { selector: 'node[c = 1]', style: { width: 28, height: 28, 'font-size': 13, 'font-weight': 700, 'border-width': 3, 'border-color': '#17212B' } },
        { selector: 'edge', style: { width: 1, 'line-color': '#C3CCD5', 'curve-style': 'straight' } }], wheelSensitivity: .25 });
    cy.on('tap', 'node', e => { if (e.target.id() !== n.id) ctx.setPick({ inst: e.target.id() }); });
  });

  // ================= 时间与变化 =================
  add('时间与变化', 'daily', '每日变化量', '本体每天新增、修改的数据量（10-01 起）', '本项目动态本体、Grafana 柱状图', {}, (el, w, ctx) => {
    const ch = ctx.chart(el);
    ch.setOption({ textStyle: { fontFamily: FONT }, grid: { left: 44, right: 12, top: 16, bottom: 26 }, tooltip: { trigger: 'axis' },
      xAxis: Object.assign({ type: 'category', data: D.daily.map((_, i) => dayLabel(i)) }, AX), yAxis: Object.assign({ type: 'value' }, AX),
      series: [{ type: 'bar', data: D.daily, itemStyle: { color: '#1F8FB3', borderRadius: [3, 3, 0, 0] }, markPoint: { symbolSize: 40, label: { fontSize: 10 }, data: [{ coord: [19, D.daily[19]], value: '首次装载' }] } }] });
  });
  add('时间与变化', 'cal', '变化日历', '每天一格，颜色越深当天变化越多', 'Superset 日历热力图、GitHub 贡献图', { h: 's' }, (el, w, ctx) => {
    const ch = ctx.chart(el); const data = D.daily.map((v, i) => [new Date(2026, 9, 1 + i).toISOString().slice(0, 10), v]);
    ch.setOption({ textStyle: { fontFamily: FONT }, tooltip: { formatter: p => `${p.value[0]}：${fmt(p.value[1])} 条` }, visualMap: { show: false, min: 0, max: 1200, inRange: { color: ['#EEF6F9', '#7FC3D8', '#0F6E8C'] } },
      calendar: { range: ['2026-10-01', '2026-11-17'], cellSize: ['auto', 16], top: 24, left: 30, right: 12, itemStyle: { borderColor: '#fff', borderWidth: 2 }, splitLine: { show: false }, dayLabel: { nameMap: 'ZH', fontSize: 10, color: '#8A96A3' }, monthLabel: { nameMap: 'ZH', color: '#5E6B78' }, yearLabel: { show: false } },
      series: [{ type: 'heatmap', coordinateSystem: 'calendar', data }] });
  });
  add('时间与变化', 'ts', '测点时序', 'EQ003.T50 窗口均值，带阈值线和高温事件', 'Grafana 时间序列、ThingsBoard 图表', {}, (el, w, ctx) => {
    if (!ctx.o.types.includes('point')) return empty(el, '这个本体没有时序数据');
    const ch = ctx.chart(el); const xs = D.t50.map((_, i) => { const d = new Date(2026, 9, 20, i * 6); return String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); });
    ch.setOption({ textStyle: { fontFamily: FONT }, grid: { left: 46, right: 60, top: 16, bottom: 26 }, tooltip: { trigger: 'axis', valueFormatter: v => v + ' °R' },
      xAxis: Object.assign({ type: 'category', data: xs, boundaryGap: false }, AX), yAxis: Object.assign({ type: 'value', min: 1540, max: 1630 }, AX),
      series: [{ type: 'line', data: D.t50, showSymbol: false, lineStyle: { color: '#22A88E', width: 2 }, markLine: { symbol: 'none', lineStyle: { color: '#D9443A', type: 'dashed' }, label: { formatter: '阈值 1,598', color: '#D9443A' }, data: [{ yAxis: D.THRESH }] },
        markArea: { itemStyle: { color: 'rgba(217,68,58,.12)' }, data: [[{ xAxis: xs[53] }, { xAxis: xs[59] }]] } }] });
  });
  add('时间与变化', 'versions', '版本记录', '本体每个版本改了什么：新增类型、属性、关系，结构更新', 'Semantica 版本页、WebProtégé 修订历史', { h: 'm' }, (el, w, ctx) => {
    const o = ctx.o; const list = [['v1', '10-20', '首次生成：7 个类型、7 条关系'], ['v4', '10-26', '新增推断关系：维保记录 负责人 人员（置信 98%）'], ['v8', '11-02', '测点窗口统计改为 10 分钟窗口'], ['v11', '11-10', '合并设备清单.xlsx：设备新增属性「供应商」'], ['v12', '11-16', '高温事件规则：连续 3 个窗口超 1,598 °R']];
    if (o.ver > 12) list.push(['v' + o.ver, o.updated, '结构更新：「维保记录」新增属性「备注」']);
    el.innerHTML = `<ol class="wm-ver">${list.reverse().map(([v, t, s], i) => `<li class="${i === 0 ? 'on' : ''}"><b>${v}</b><time>${esc(t)}</time><span>${esc(s)}</span></li>`).join('')}</ol>`;
  });

  // ================= 质量与来源 =================
  add('质量与来源', 'methods', '识别方式分布', '属性和关系分别是怎么识别出来的：声明、规则、时序、大模型、推断', '本项目生成过程、OpenMetadata 数据画像', {}, (el, w, ctx) => {
    const o = ctx.o; const cnt = {}; o.types.forEach(k => typeAttrs(o, k).forEach(a => { cnt[a[4]] = (cnt[a[4]] || 0) + 1; })); ontoRels(o).forEach(r => { cnt[r[3]] = (cnt[r[3]] || 0) + 1; });
    const COL = { '声明主键': '#4B5966', '声明外键': '#6B7A88', '码表': '#9AA5B1', '画像规则': '#2F6F86', '时序识别': '#168069', '码表 + 时序识别': '#22A88E', '大模型补全': '#C77C0E', '推断': '#6B55D6' };
    const ch = ctx.chart(el);
    ch.setOption({ textStyle: { fontFamily: FONT }, tooltip: { formatter: p => `${p.name}：${p.value} 项（${p.percent}%）` }, legend: { orient: 'vertical', right: 8, top: 'middle', textStyle: { fontSize: 12, color: '#3D4B58' } },
      series: [{ type: 'pie', radius: ['45%', '72%'], center: ['36%', '50%'], label: { show: false }, data: Object.entries(cnt).map(([n, v]) => ({ name: n, value: v, itemStyle: { color: COL[n] || '#8796AA' } })) }] });
  });
  add('质量与来源', 'pending', '待确认关系', '平台推断出来的关系，专员逐条确认或驳回', 'OpenMetadata 待审任务、Semantica 对齐审核', { h: 'm' }, (el, w, ctx) => {
    const o = ctx.o; const cs = confirmState(); const inf = ontoRels(o).filter(r => r[3] === '推断');
    const paint = () => {
      el.innerHTML = inf.length ? `<div class="wm-pend">${inf.map(r => { const s = cs[relKey(r)]; const c = (r[5].match(/(\d+)%/) || [])[1];
        return `<div class="wm-pi"><div class="grow"><b>${esc(tName(o, r[0]))} ${esc(r[2])} ${esc(tName(o, r[1]))}</b><small>${esc(r[5])}</small>${c ? `<div class="conf"><i style="width:${c}%"></i></div>` : ''}</div>
          ${s ? `<span class="pill ${s === '已确认' ? 'ok' : 'bad'}">${s}</span><button class="btn ghost sm" data-undo="${esc(relKey(r))}">撤回</button>` : `<button class="btn sm" data-no="${esc(relKey(r))}">驳回</button><button class="btn primary sm" data-ok="${esc(relKey(r))}">确认</button>`}</div>`; }).join('')}</div>`
        : '<div class="wg-quiet">没有待确认的推断关系。</div>';
      const set = (k, v) => { if (v) cs[k] = v; else delete cs[k]; o.feed.unshift([now(), `${v || '撤回'}推断关系：${k.replace('>', ' → ')}`]); save(); paint(); toast(v ? `已${v.slice(1)}` : '已撤回'); };
      $$('[data-ok]', el).forEach(b => b.onclick = () => set(b.dataset.ok, '已确认')); $$('[data-no]', el).forEach(b => b.onclick = () => set(b.dataset.no, '已驳回')); $$('[data-undo]', el).forEach(b => b.onclick = () => set(b.dataset.undo, null));
    };
    paint();
  });
  add('质量与来源', 'health', '本体健康检查', '自动检查：缺主键、孤立类型、待确认关系、未处理的结构变化、大模型补全待抽检', 'Semantica 健康检查、SHACL 校验、WebProtégé 问题列表', { h: 'm' }, (el, w, ctx) => {
    const o = ctx.o; const rels = ontoRels(o); const cs = confirmState();
    const noPk = o.types.filter(k => !typeAttrs(o, k).some(a => /主键|时序识别/.test(a[4])));
    const lonely = o.types.filter(k => !rels.some(r => r[0] === k || r[1] === k));
    const pend = rels.filter(r => r[3] === '推断' && !cs[relKey(r)]);
    const llm = o.types.reduce((a, k) => a + typeAttrs(o, k).filter(a2 => a2[4] === '大模型补全').length, 0);
    const items = [[noPk.length ? 'warn' : 'ok', '每个类型都有主键', noPk.length ? '缺主键：' + noPk.map(k => tName(o, k)).join('、') : `${o.types.length} 个类型都有`],
      [lonely.length ? 'warn' : 'ok', '没有孤立类型', lonely.length ? '没有任何关系：' + lonely.map(k => tName(o, k)).join('、') : '每个类型至少有一条关系'],
      [pend.length ? 'warn' : 'ok', '推断关系已确认', pend.length ? `${pend.length} 条待确认（见「待确认关系」）` : '全部已处理'],
      [o.pendingChange === true ? 'warn' : 'ok', '结构变化已处理', o.pendingChange === true ? 'dwd_maintenance 新增字段 remark 未处理' : '没有未处理的结构变化'],
      [llm ? 'info' : 'ok', '大模型补全已抽检', llm ? `${llm} 个属性的中文名或含义由大模型补全，建议抽检` : '没有大模型补全的属性']];
    const bad = items.filter(x => x[0] === 'warn').length;
    el.innerHTML = `<div class="wm-health"><div class="wm-hs ${bad ? 'warn' : 'ok'}"><b>${items.length - bad} / ${items.length}</b><small>项通过</small></div><ul>${items.map(([s, t, d]) => `<li class="${s}"><i></i><div><b>${t}</b><small>${esc(d)}</small></div></li>`).join('')}</ul></div>`;
  });
  add('质量与来源', 'lineage', '来源血缘', '数据中台资产 → 表 → 本体类型，看每个类型从哪来', 'OpenMetadata 血缘、DataHub 血缘、Semantica 溯源', {}, (el, w, ctx) => {
    const o = ctx.o; const nodes = [], links = [], seen = new Set(); const node = (name, color) => { if (!seen.has(name)) { seen.add(name); nodes.push({ name, itemStyle: { color } }); } };
    o.src.map(srcById).filter(Boolean).forEach(s => s.tables.forEach(t => t.to.filter(k => o.types.includes(k)).forEach(k => {
      node(s.name, '#0F6E8C'); node(t.name, '#8A96A3'); node(tName(o, k), typeDef(o, k).color);
      if (!links.some(l => l.source === s.name && l.target === t.name)) links.push({ source: s.name, target: t.name, value: Math.log10(t.rows + 10) });
      links.push({ source: t.name, target: tName(o, k), value: Math.log10(t.rows + 10) }); })));
    if (!links.length) return empty(el, '没有来源信息');
    const ch = ctx.chart(el);
    ch.setOption({ textStyle: { fontFamily: FONT }, tooltip: { formatter: p => p.dataType === 'edge' ? `${p.data.source} → ${p.data.target}` : p.name },
      series: [{ type: 'sankey', data: nodes, links, left: 8, right: 90, top: 10, bottom: 10, nodeGap: 8, label: { fontSize: 11 }, lineStyle: { color: 'source', opacity: .25 } }] });
    ch.on('click', p => { if (p.dataType === 'node') { const k = o.types.find(x => tName(o, x) === p.name); if (k) ctx.setPick({ k }); } });
  });
  add('质量与来源', 'mapping', '字段映射表', '源表字段 → 本体属性，一一对应，便于企业核对', 'OpenMetadata 列级血缘、本项目生成过程', { follows: true, h: 'm' }, (el, w, ctx) => {
    const o = ctx.o; const ks = ctx.pick && ctx.pick.k ? [ctx.pick.k] : o.types;
    el.innerHTML = `<div class="wm-scroll"><table class="tbl mini"><thead><tr><th>源表</th><th>字段</th><th></th><th>类型.属性</th><th>识别方式</th></tr></thead><tbody>
      ${ks.flatMap(k => typeAttrs(o, k).map(a => `<tr><td><code>${esc(srcTables(o, k)[0] || '—')}</code></td><td><code>${esc(a[2])}</code></td><td class="sub">→</td><td>${esc(tName(o, k))}.<b>${esc(a[0])}</b></td><td>${mchip(a[4])}</td></tr>`)).join('')}</tbody></table>
      ${ctx.pick && ctx.pick.k ? `<p class="sub wm-foot">只显示「${esc(tName(o, ctx.pick.k))}」，点空白处或换选中类型可切换</p>` : ''}</div>`;
  });

  // ================= 上下游 =================
  add('上下游', 'upstream', '上游 · 中台同步', '本体用到的数据中台资产：接入方式、最近同步、质量分、负责人', '数据中台（上游）', { h: 'm' }, (el, w, ctx) => {
    const o = ctx.o; const ss = o.src.map(srcById).filter(Boolean);
    el.innerHTML = `<div class="wm-up">${ss.map(s => `<div class="wm-upi"><div class="wm-up-h"><b>${esc(s.name)}</b><span class="tag">${esc(s.kind)}</span>${s.live ? '<span class="pill ok sm">同步正常</span>' : '<span class="pill gray sm">一次性</span>'}</div>
      <dl class="kv"><dt>接入方式</dt><dd>${esc(s.access)}</dd><dt>最近同步</dt><dd>${esc(s.updated)}</dd><dt>调度</dt><dd>${esc(s.meta.job)}</dd><dt>血缘</dt><dd>${esc(s.meta.lineage)}</dd><dt>质量分</dt><dd>${esc(s.meta.quality)}</dd><dt>负责人</dt><dd>${esc(s.meta.owner)}</dd></dl></div>`).join('')}
      <button class="btn sm" data-go>去数据源管理</button></div>`;
    $('[data-go]', el).onclick = () => go('sources');
  });
  add('上下游', 'downstream', '下游 · 对外接口', '给下游应用用的接口：查询、变更订阅、导出、嵌入；以及已登记的下游', '本项目约定', { h: 'l' }, (el, w, ctx) => {
    const o = ctx.o; const id = o.id;
    const api = [['查询对象', `GET /api/v1/ontologies/${id}/objects?type=设备&filter=状态:高风险`], ['查询关系', `GET /api/v1/ontologies/${id}/objects/EQ003/links?rel=针对`], ['本体结构', `GET /api/v1/ontologies/${id}/schema?version=${o.ver}`], ['时点查询', `GET /api/v1/ontologies/${id}/objects/EQ003?valid_at=2026-11-02&known_at=2026-11-04`],
      ['变更订阅', `Webhook / 消息主题 ontology.${id}.changes`], ['导出', 'JSON-LD · OWL · CSV · 图数据库（Neo4j）'], ['嵌入本应用', '只读链接 / iframe，带企业与版本参数']];
    el.innerHTML = `<div class="wm-down"><table class="tbl mini"><tbody>${api.map(([n, v]) => `<tr><td><b>${n}</b></td><td><code>${esc(v)}</code></td><td><button class="ic-btn" data-cp="${esc(v)}" title="复制">⧉</button></td></tr>`).join('')}</tbody></table>
      <div class="wm-tc-sec">已登记的下游（规划中）</div><div class="wm-chips"><span>设备运维应用</span><span>本体问答助手</span><span>告警与派单</span><span>团队的 Workshop 应用</span></div>
      <p class="sub wm-foot">接口按本体版本发布；结构变化发版前先通知订阅方。原型只展示接口约定。</p></div>`;
    $$('[data-cp]', el).forEach(b => b.onclick = () => { try { navigator.clipboard.writeText(b.dataset.cp).catch(() => {}); } catch (e) { /* 忽略 */ } toast('已复制接口地址（原型示意）'); });
  });

  // ================= 布局与说明 =================
  add('布局与说明', 'title', '页面标题', '应用的大标题和一句说明', '—', { h: 'xs', layout: true }, null);
  add('布局与说明', 'text', '文字说明', '写给企业人员的说明：用途、口径、注意事项；可放多段', 'Grafana 文本面板、NocoBase Markdown', { h: 's', multi: true, layout: true }, null);

  // 本体总览里原有的五种视图和面板（由 makeOntoView 渲染）
  const VIEW = [['概览', 'bar', '本体信息栏', '选择本体；类型数、关系数、实例数、版本、持续更新状态', '本体总览 · 顶栏', 'xs'], ['时间与变化', 'banner', '结构变化提示', '数据源结构变化时提示，一键更新本体', '本体总览 · 结构变化提示', 'xs'],
    ['结构', 'type', '类型图', '实体类型和关系，圆里是实例数', '本体总览 · 视图一（参考微软 Playground）', 'l'], ['实例', 'star', '实例星图', '全部实例的关系网络：着色、看邻居、找路径、时间回放', '本体总览 · 视图二（参考 Semantica）', 'l'],
    ['结构', 'struct', '数据结构', '每个类型一张卡：属性、来源字段、识别方式', '本体总览 · 视图三', 'm'], ['结构', 'matrix', '关系矩阵', '类型 × 类型的关系数量，以及各类型实例数', '本体总览 · 视图四', 'm'],
    ['时间与变化', 'time', '时间视图', '每日变化量、双时间回看、测点时序', '本体总览 · 视图五', 'l'], ['结构', 'detail', '详情面板', '显示选中的类型、关系或实例', '本体总览 · 右侧详情', 'l'], ['时间与变化', 'feed', '变化流', '动态本体最近的数据变化和结构变化', '本体总览 · 最近变化', 'm']];
  VIEW.forEach(([g, k, name, desc, ref, h]) => M.push({ g, k, name, desc, ref, multi: false, h, view: true, fromV2: true }));

  const ORDER = ['概览', '结构', '实例', '时间与变化', '质量与来源', '上下游', '布局与说明'];
  const BY = Object.fromEntries(M.map(m => [m.k, m]));
  return { list: M, by: BY, groups: ORDER };
})();
