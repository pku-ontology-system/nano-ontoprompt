/* 页面一：数据源（选择数据 → 生成本体 → 生成过程） */
// 类型图里每种类型的位置（类型图、生成画布共用）
const TYPE_POS = { eq: { x: 0, y: 0 }, line: { x: -250, y: -150 }, point: { x: 250, y: -150 }, event: { x: 290, y: 110 }, wo: { x: -270, y: 120 },
  mt: { x: -20, y: 230 }, staff: { x: -300, y: 330 }, ins: { x: 250, y: 290 }, meter: { x: 20, y: -270 }, tariff: { x: 260, y: -330 } };
function posOf(k) {
  if (TYPE_POS[k]) return TYPE_POS[k];
  let h = 0; for (const c of k) h = (h * 31 + c.charCodeAt(0)) % 360; const a = h / 180 * Math.PI;
  return { x: Math.round(430 * Math.cos(a)), y: Math.round(380 * Math.sin(a)) };
}
const KIND_IC = {
  '数据中台': '<svg viewBox="0 0 24 24"><path d="M12 3 3 7.5 12 12l9-4.5L12 3Z"/><path d="m3 12 9 4.5 9-4.5M3 16.5 12 21l9-4.5"/></svg>',
  '业务数据库': '<svg viewBox="0 0 24 24"><ellipse cx="12" cy="6" rx="7" ry="2.8"/><path d="M5 6v12c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8V6M5 12c0 1.5 3.1 2.8 7 2.8s7-1.3 7-2.8"/></svg>',
  '时序数据': '<svg viewBox="0 0 24 24"><path d="M3 17l4-6 4 3 4-8 3 5h3"/></svg>',
  '文件': '<svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 12h6M9 16h6"/></svg>',
};
const KIND_CLS = { '数据中台': 'k-dp', '业务数据库': 'k-db', '时序数据': 'k-ts', '文件': 'k-file' };

const PageSources = (() => {
  const F = { q: '', kind: '全部', x: null };
  const selCount = () => Object.values(UI.sel).reduce((a, s) => a + s.length, 0);
  const statusPill = id => { const o = ontoOfSource(id); if (!o) return '<span class="pill gray">未生成</span>';
    return o.dynamic ? '<span class="pill live"><i></i>持续更新中</span>' : '<span class="pill ok">已生成</span>'; };
  const flagChip = f => !f ? '' : f === 'pk' ? '<span class="flag pk">主键</span>' : f === 'code' ? '<span class="flag code">码表</span>' : f.startsWith('fk:') ? `<span class="flag fk">外键 → ${esc(f.slice(3))}</span>` : '';

  function render(v) {
    if (UI.gen) return Gen.render(v);
    const srcs = allSources();
    const kinds = ['全部', '数据中台', '业务数据库', '时序数据', '文件'];
    const q = F.q.trim().toLowerCase();
    const shown = srcs.filter(s => (F.kind === '全部' || s.kind === F.kind) && (!q || (s.name + s.note + s.tables.map(t => t.name + t.cn).join(' ')).toLowerCase().includes(q)));
    if (!srcs.find(s => s.id === UI.open)) UI.open = srcs[0].id;
    const nTables = srcs.reduce((a, s) => a + s.tables.length, 0);
    v.innerHTML = `<div class="page has-selbar">
      <div class="page-head">
        <div><h1>数据源</h1><p class="lead">勾选要生成本体的表，再点「生成本体」。平台只读取元数据和样本，不改动源数据。</p></div>
        <div class="actions"><button class="btn" id="addSrc"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>添加数据源</button>
          <button class="btn primary" id="connect">${KIND_IC['数据中台']}连接数据中台</button></div>
      </div>
      <div class="kpis">
        <div class="kpi"><small>数据源</small><b>${srcs.length}</b><span>${srcs.filter(s => s.kind === '数据中台').length} 个来自数据中台</span></div>
        <div class="kpi"><small>表 · 数据集</small><b>${nTables}</b><span>其中时序 ${srcs.reduce((a, s) => a + s.tables.filter(t => t.kind === 'ts').length, 0)} 个</span></div>
        <div class="kpi"><small>已生成本体</small><b>${S.ontos.length}</b><span><a href="#ontology">去本体总览 →</a></span></div>
        <div class="kpi"><small>持续更新中</small><b class="c-live">${S.ontos.filter(o => o.dynamic).length}</b><span>数据源更新后本体跟着更新</span></div>
      </div>
      <div class="toolbar">
        <div class="search"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/></svg><input id="q" placeholder="搜索数据源或表名" value="${esc(F.q)}"></div>
        <div class="seg">${kinds.map(k => `<button data-kind="${k}" class="${F.kind === k ? 'on' : ''}">${k}</button>`).join('')}</div>
      </div>
      <div class="split">
        <div class="card list">
          <table class="tbl src-tbl"><thead><tr><th>名称</th><th>种类</th><th class="r">表 · 数据集</th><th class="r">数据量</th><th>最近更新</th><th>本体状态</th></tr></thead>
          <tbody>${shown.map(s => { const n = (UI.sel[s.id] || []).length; return `<tr data-id="${s.id}" class="${UI.open === s.id ? 'on' : ''}">
            <td><div class="nm"><span class="src-ic ${KIND_CLS[s.kind]}">${KIND_IC[s.kind]}</span><div><b>${esc(s.name)}${n ? `<em class="selno">已选 ${n}</em>` : ''}</b><small>${esc(s.note)}</small></div></div></td>
            <td><span class="tag ${KIND_CLS[s.kind]}">${s.kind}</span></td><td class="r num">${s.tables.length}</td>
            <td class="r num">${s.tables.length ? fmt(s.tables.reduce((a, t) => a + t.rows, 0)) + ' 行' : '—'}</td><td class="sub">${esc(s.updated)}</td><td>${statusPill(s.id)}</td></tr>`; }).join('')
            || '<tr><td colspan="6" class="empty">没有符合条件的数据源</td></tr>'}</tbody></table>
        </div>
        <div class="card detail" id="srcDetail"></div>
      </div>
      <div class="selbar" id="selbar"></div>
    </div>`;
    $('#q', v).oninput = e => { F.q = e.target.value; const pos = e.target.selectionStart; render(v); const i = $('#q', v); i.focus(); i.setSelectionRange(pos, pos); };
    $$('.seg button', v).forEach(b => b.onclick = () => { F.kind = b.dataset.kind; render(v); });
    $$('.src-tbl tbody tr[data-id]', v).forEach(tr => tr.onclick = () => { UI.open = tr.dataset.id; F.x = null; render(v); });
    $('#connect', v).onclick = () => connectDlg(v);
    $('#addSrc', v).onclick = () => addDlg(v);
    renderDetail(v); renderSelbar(v);
  }

  function renderDetail(v) {
    const s = srcById(UI.open); const el = $('#srcDetail', v); if (!s) { el.innerHTML = ''; return; }
    const sel = UI.sel[s.id] || []; const o = ontoOfSource(s.id);
    el.innerHTML = `<div class="d-head"><span class="src-ic big ${KIND_CLS[s.kind]}">${KIND_IC[s.kind]}</span><div class="grow"><h2>${esc(s.name)}</h2><p>${esc(s.note)}</p></div>${statusPill(s.id)}</div>
      <div class="meta-grid">
        <div><small>接入方式</small><b>${esc(s.access)}</b></div><div><small>最近更新</small><b>${esc(s.updated)}</b></div>
        <div><small>负责人</small><b>${esc(s.meta.owner)}</b></div><div><small>同步任务</small><b>${esc(s.meta.job)}</b></div>
        <div><small>血缘</small><b>${esc(s.meta.lineage)}</b></div><div><small>质量分</small><b>${esc(s.meta.quality)}</b></div>
      </div>
      ${o ? `<div class="onto-link"><span>已生成「<b>${esc(o.name)}</b>」${o.dynamic ? ' · 持续更新中' : ''}</span><a href="#ontology" data-onto="${o.id}">查看本体 →</a></div>` : ''}
      <div class="sec-h"><h3>表 · 数据集 <small>${s.tables.length}</small></h3>${s.tables.length ? `<label class="chk"><input type="checkbox" id="all" ${sel.length === s.tables.length ? 'checked' : ''}>全选</label>` : ''}</div>
      <div class="tbls">${s.tables.length ? s.tables.map(t => `
        <div class="trow ${sel.includes(t.name) ? 'checked' : ''} ${F.x === t.name ? 'open' : ''}">
          <label class="chk"><input type="checkbox" data-t="${esc(t.name)}" ${sel.includes(t.name) ? 'checked' : ''} aria-label="选择 ${esc(t.name)}"></label>
          <button class="tn" data-x="${esc(t.name)}"><code>${esc(t.name)}</code><span>${esc(t.cn)}</span></button>
          <span class="tag t-${t.kind}">${KIND_CN[t.kind]}</span><span class="rows num">${fmt(t.rows)} 行</span>
          <button class="ic-btn" data-x="${esc(t.name)}" aria-label="查看字段"><svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg></button>
        </div>
        ${F.x === t.name ? `<div class="fields"><table class="tbl mini"><thead><tr><th>字段</th><th>类型</th><th>说明</th><th>样本</th><th></th></tr></thead><tbody>
          ${t.cols.map(c => `<tr><td><code>${esc(c[0])}</code></td><td class="sub">${esc(c[1])}</td><td>${esc(c[2] || '—')}</td><td class="sub">${esc(c[3])}</td><td>${flagChip(c[4])}</td></tr>`).join('')}
          </tbody></table></div>` : ''}`).join('')
        : `<div class="empty-box"><b>还没有读取到表</b><p>原型没有连接真实数据库，所以这里是空的。演示请用「工厂主题库」或「MES 业务库」。</p></div>`}</div>`;
    const set = (name, on) => { const a = new Set(UI.sel[s.id] || []); on ? a.add(name) : a.delete(name); UI.sel[s.id] = [...a]; if (!UI.sel[s.id].length) delete UI.sel[s.id]; };
    $$('input[data-t]', el).forEach(c => c.onchange = () => { set(c.dataset.t, c.checked); render(v); });
    const all = $('#all', el); if (all) all.onchange = () => { s.tables.forEach(t => set(t.name, all.checked)); render(v); };
    $$('[data-x]', el).forEach(b => b.onclick = () => { F.x = F.x === b.dataset.x ? null : b.dataset.x; renderDetail(v); });
    const ol = $('[data-onto]', el); if (ol) ol.onclick = () => { S.cur = ol.dataset.onto; save(); };
  }

  function renderSelbar(v) {
    const n = selCount(); const el = $('#selbar', v);
    const ids = Object.keys(UI.sel); const rows = ids.reduce((a, id) => a + srcById(id).tables.filter(t => UI.sel[id].includes(t.name)).reduce((b, t) => b + t.rows, 0), 0);
    el.innerHTML = n ? `<div class="sel-info"><b>已选 ${n} 张表</b><span>来自 ${ids.map(id => esc(srcById(id).name)).join('、')} · 约 ${fmt(rows)} 行</span></div>
        <button class="btn ghost" id="clr">清空</button><button class="btn primary lg" id="genBtn">生成本体<svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button>`
      : `<div class="sel-info"><span>在右侧勾选表以后，可以生成本体</span></div><button class="btn primary lg" disabled>生成本体<svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button>`;
    if (n) { $('#clr', el).onclick = () => { UI.sel = {}; render(v); }; $('#genBtn', el).onclick = () => Gen.panel(v); }
  }

  // ---------- 连接数据中台 ----------
  function connectDlg(v) {
    const reg = new Set(S.extra.filter(x => x.cat).map(x => x.cat));
    const m = modal(`<div class="dlg-h"><h2>连接数据中台</h2><p>从数据中台的资产目录里选主题库。平台用中台分配的只读服务账号访问，不需要填写数据库密码。</p></div>
      <div class="dlg-b">
        <div class="form-grid">
          <label>中台地址</label><div class="inline"><input value="https://dataplatform.factory.local" readonly><span class="pill ok">已连接</span></div>
          <label>服务账号</label><input value="svc-ontology（只读）" readonly>
          <label>更新通知</label><input value="主题库物化完成后回调本平台" readonly>
        </div>
        <h4 class="dlg-sub">资产目录</h4>
        <div class="cat-list">${D.catalog.map((c, i) => { const done = c.reg || reg.has(c.name); const can = !done && c.tablesDef;
          return `<label class="cat ${done ? 'done' : ''} ${!done && !can ? 'locked' : ''}"><input type="checkbox" data-i="${i}" ${done ? 'checked disabled' : can ? '' : 'disabled'}>
            <span class="src-ic k-dp">${KIND_IC['数据中台']}</span><div class="grow"><b>${esc(c.name)}</b><code>${esc(c.path)}</code></div><span class="sub">${c.tables} 张表</span>
            ${done ? '<span class="pill ok">已接入</span>' : can ? '<span class="pill gray">可接入</span>' : '<span class="pill warn">需要在中台申请权限</span>'}</label>`; }).join('')}</div>
      </div>
      <div class="dlg-f"><button class="btn" data-close>取消</button><button class="btn primary" id="doConnect">接入所选主题库</button></div>`);
    $('#doConnect', m.el).onclick = () => {
      const picked = $$('input[data-i]:checked:not(:disabled)', m.el).map(c => D.catalog[+c.dataset.i]);
      if (!picked.length) return toast('请先勾选要接入的主题库', 'warn');
      picked.forEach(c => { const id = 'dp' + (S.seq++); S.extra.push({ id, cat: c.name }); UI.open = id; });
      save(); m.close(); toast(`已接入 ${picked.map(c => c.name).join('、')}，读取到 ${picked.reduce((a, c) => a + c.tables, 0)} 张表`); render(v);
    };
  }

  // ---------- 添加数据源 ----------
  function addDlg(v) {
    let kind = '业务数据库';
    const fields = {
      '业务数据库': `<label>数据库类型</label><select><option>PostgreSQL</option><option>MySQL</option><option>Oracle</option><option>SQL Server</option></select>
        <label>地址</label><div class="inline"><input placeholder="例如 10.2.0.15"><input class="w90" placeholder="端口" value="5432"></div>
        <label>数据库</label><input placeholder="例如 mes"><label>账号</label><input placeholder="只读账号"><label>密码</label><input type="password" placeholder="只用于读取元数据和样本" autocomplete="off">
        <label>更新方式</label><select><option>变更捕获（推荐，持续更新）</option><option>定时全量读取</option></select>`,
      '时序数据': `<label>协议</label><select><option>MQTT</option><option>Kafka</option><option>OPC UA</option></select>
        <label>地址</label><input placeholder="例如 mqtt://10.2.0.40:1883"><label>主题</label><input placeholder="例如 plant/+/metrics"><label>聚合窗口</label><select><option>10 秒</option><option>1 分钟</option><option>10 分钟</option></select>`,
      '文件': `<label>文件</label><div class="drop"><svg viewBox="0 0 24 24"><path d="M12 16V4M7 9l5-5 5 5M4 20h16"/></svg><b>拖入 Excel 或 CSV 文件</b><span>原型不会真的上传</span></div>`,
    };
    const m = modal(`<div class="dlg-h"><h2>添加数据源</h2><p>没有进数据中台的数据，可以直接接入业务库、时序数据或文件。</p></div>
      <div class="dlg-b"><div class="kind-pick">${Object.keys(fields).map(k => `<button data-k="${k}" class="${k === kind ? 'on' : ''}"><span class="src-ic ${KIND_CLS[k]}">${KIND_IC[k]}</span>${k}</button>`).join('')}</div>
        <div class="form-grid"><label>名称</label><input id="nm" placeholder="给数据源起个名字"></div><div class="form-grid" id="ff"></div><div id="testRes"></div></div>
      <div class="dlg-f"><button class="btn" id="test">测试连接</button><span class="grow"></span><button class="btn" data-close>取消</button><button class="btn primary" id="saveSrc">保存</button></div>`);
    const paint = () => { $('#ff', m.el).innerHTML = fields[kind]; $$('.kind-pick button', m.el).forEach(b => b.classList.toggle('on', b.dataset.k === kind)); };
    $$('.kind-pick button', m.el).forEach(b => b.onclick = () => { kind = b.dataset.k; paint(); }); paint();
    $('#test', m.el).onclick = () => { $('#testRes', m.el).innerHTML = '<div class="note">原型没有连接真实系统：这里只演示流程。真实环境会检查网络、账号权限，并读取表清单。</div>'; };
    $('#saveSrc', m.el).onclick = () => {
      const name = $('#nm', m.el).value.trim(); if (!name) return toast('请填写名称', 'warn');
      const id = 'u' + (S.seq++);
      S.extra.push({ id, name, note: kind + ' · 新添加', kind, access: kind === '文件' ? '上传' : kind === '时序数据' ? '订阅' : '变更捕获', updated: now(), live: kind !== '文件',
        meta: { owner: 'Jie Zhang', job: '—', lineage: '—', quality: '—' }, tables: [] });
      save(); UI.open = id; m.close(); toast('已添加数据源「' + name + '」'); render(v);
    };
  }

  return { title: '数据源', render };
})();

// ============ 生成：设置面板 + 生成过程 ============
const Gen = (() => {
  const STEPS = [
    { k: 'meta', name: '读取元数据', sub: '字段、注释、主外键、码表', ms: 1400 },
    { k: 'profile', name: '数据画像', sub: '取样，统计空值率、取值分布、值域重合', ms: 1800 },
    { k: 'entity', name: '识别实体', sub: '有主键的表 → 实体类型；状态变更表 → 带有效期的属性', ms: 1600 },
    { k: 'rel', name: '识别关系', sub: '声明外键直接采用；没有外键的按值域重合推断', ms: 1700 },
    { k: 'ts', name: '时序识别', sub: '时间列 + 数值列 → 测点；窗口统计；按规则生成事件', ms: 2000 },
    { k: 'llm', name: '大模型补全', sub: '中文名称、描述、单位（本机模型）', ms: 2000 },
    { k: 'check', name: '校验', sub: '主键唯一、外键完整、推断关系置信度', ms: 1000 },
    { k: 'write', name: '写入', sub: '写入本体库；开启持续更新', ms: 900 },
  ];
  const SLOTS = 4;
  let G = null, cy = null, mounted = null;

  // ---------- 设置面板 ----------
  function detect(t) {
    const ts = t.cols.find(c => /timestamp|date/.test(c[1]) && /ts|time|day|at$/.test(c[0])); const nums = t.cols.filter(c => c[1] === 'float');
    const key = t.cols.find(c => /equip|meter|point/.test(c[0]) && c !== ts);
    if (t.kind === 'ts') return `时间列 ${ts ? ts[0] : '—'}，${nums.length} 个数值列${key ? '，按 ' + key[0] + ' 分组' : ''}`;
    if (t.kind === 'state') return `每行是一次状态变化（status + effective_at），并入「设备.状态」`;
    const pk = t.cols.find(c => c[4] === 'pk'); const fks = t.cols.filter(c => (c[4] || '').startsWith('fk:'));
    return `主键 ${pk ? pk[0] : '—'}${fks.length ? '，外键 ' + fks.map(c => c[0]).join('、') : ''}`;
  }
  function panel(v) {
    const list = []; Object.keys(UI.sel).forEach(id => { const s = srcById(id); s.tables.filter(t => UI.sel[id].includes(t.name)).forEach(t => list.push({ s, t, kind: t.kind })); });
    const srcNames = [...new Set(list.map(x => x.s.name))];
    const st = { dyn: true, target: 'new', name: srcNames[0].replace(/库$/, '') + (srcNames.length > 1 ? '等' : '') + '本体' };
    const m = modal(`<div class="dlg-h"><h2>生成本体</h2><p>已选 ${list.length} 张表，来自 ${srcNames.map(esc).join('、')}。下面是平台自动识别的结果，可以修改。</p></div>
      <div class="dlg-b">
        <div class="type-cards" id="tcs"></div>
        <h4 class="dlg-sub">每张表的识别结果</h4>
        <table class="tbl mini detect"><thead><tr><th>表</th><th class="r">行数</th><th>识别为</th><th>依据</th></tr></thead><tbody>
          ${list.map((x, i) => `<tr><td><code>${esc(x.t.name)}</code><small class="sub"> ${esc(x.t.cn)}</small></td><td class="r num">${fmt(x.t.rows)}</td>
            <td>${x.t.kind === 'basic' ? '<span class="tag t-basic">普通</span>' : `<select data-i="${i}" class="sel-sm"><option value="${x.t.kind}">${KIND_CN[x.t.kind]}</option><option value="basic">按普通表处理</option></select>`}</td>
            <td class="sub">${esc(detect(x.t))}</td></tr>`).join('')}
        </tbody></table>
        <div class="form-grid two">
          <label>本体名称</label><input id="gName" value="${esc(st.name)}">
          <label>写入到</label><select id="gTarget"><option value="new">新建本体</option>${S.ontos.map(o => `<option value="${o.id}">合并到「${esc(o.name)}」</option>`).join('')}</select>
        </div>
      </div>
      <div class="dlg-f"><span class="sub">分层并行：每一步最多 ${SLOTS} 张表同时处理</span><span class="grow"></span><button class="btn" data-close>取消</button><button class="btn primary" id="go">开始生成</button></div>`, { wide: true });
    const paint = () => {
      const nb = list.filter(x => x.kind !== 'ts').length, nt = list.filter(x => x.kind === 'ts').length, canTs = list.some(x => x.t.kind === 'ts');
      const modes = [...new Set(list.map(x => x.s.access))];
      $('#tcs', m.el).innerHTML = `
        <div class="tcard on"><div class="tc-h"><span class="tc-ic b">◆</span><b>普通本体</b><span class="auto">自动识别</span></div><p>实体、属性和关系。来自台账、工单这类业务表。</p><div class="tc-f">${nb} 张表</div></div>
        <button class="tcard ${nt ? 'on' : ''} ${canTs ? '' : 'off'}" id="tcTs" ${canTs ? '' : 'disabled'}><div class="tc-h"><span class="tc-ic t">∿</span><b>时序本体</b><span class="auto">${canTs ? '自动识别' : '未发现时序表'}</span></div><p>在普通本体上增加测点、窗口统计和事件。来自带时间列的读数表。</p><div class="tc-f">${nt} 张时序表${canTs ? (nt ? ' · 点击改为只生成普通本体' : ' · 点击恢复') : ''}</div></button>
        <button class="tcard dyn ${st.dyn ? 'on' : ''}" id="tcDyn"><div class="tc-h"><span class="tc-ic d">↻</span><b>动态本体</b><span class="switch ${st.dyn ? 'on' : ''}"><i></i></span></div><p>数据源更新后本体跟着更新。记录变化自动写入，结构变化先提示你确认。</p><div class="tc-f">更新方式：${modes.map(esc).join('、')}</div></button>`;
      $('#tcTs', m.el).onclick = () => { const to = nt ? 'basic' : 'ts'; list.forEach((x, i) => { if (x.t.kind === 'ts') { x.kind = to; const s = $(`select[data-i="${i}"]`, m.el); if (s) s.value = to; } }); paint(); };
      $('#tcDyn', m.el).onclick = () => { st.dyn = !st.dyn; paint(); };
    };
    $$('select[data-i]', m.el).forEach(s => s.onchange = () => { list[+s.dataset.i].kind = s.value; paint(); });
    $('#gTarget', m.el).onchange = e => { const o = S.ontos.find(o => o.id === e.target.value); $('#gName', m.el).value = o ? o.name : st.name; $('#gName', m.el).disabled = !!o; };
    paint();
    $('#go', m.el).onclick = () => {
      const name = $('#gName', m.el).value.trim() || st.name; const target = $('#gTarget', m.el).value;
      m.close(); start(list, { name, target, dyn: st.dyn }); UI.sel = {}; route();
    };
  }

  // ---------- 生成计划 ----------
  function plan(list, opt) {
    const tables = list.map(x => ({ src: x.s, t: x.t, kind: x.kind }));
    const custom = {}, customRels = [];
    tables.forEach(x => {
      if (x.kind === 'basic' && x.t.kind !== 'basic') { // 改成按普通表处理的时序表 / 状态表
        const k = 'x_' + x.t.name.replace(/\W/g, '_');
        custom[k] = { name: x.t.cn, color: '#8C9BAB', n: x.t.rows, attrs: x.t.cols.map(c => [c[2] || c[0], c[1] === 'float' ? '数值' : /time|date/.test(c[1]) ? '日期时间' : '文本', c[0], c[3], '画像规则']) };
        x.types = [k]; if (x.t.cols.some(c => c[0] === 'equip_id')) customRels.push([k, 'eq', '关联', '推断', x.t.rows, `${x.t.name}.equip_id 与设备编号值域重合 100%`]);
      } else x.types = x.kind === 'state' ? ['eq'] : x.t.to.slice();
    });
    const types = [...new Set(tables.flatMap(x => x.types))];
    const rels = D.RELS.filter(r => types.includes(r[0]) && types.includes(r[1])).concat(customRels.filter(r => types.includes(r[1])));
    const tsTables = tables.filter(x => x.kind === 'ts');
    const steps = STEPS.filter(s => s.k !== 'ts' || tsTables.length);
    // 每一层按最多 4 个并行排程，再缩放到这一步的时长
    const sched = ms => { const w = tables.map(x => 1 + 0.3 * Math.log10(x.t.rows + 10)); const slot = Array(SLOTS).fill(0); const out = w.map(wi => { const i = slot.indexOf(Math.min(...slot)); const s0 = slot[i]; slot[i] += wi; return [s0, slot[i]]; });
      const T = Math.max(...slot); return out.map(([a, b]) => [a / T * ms * .94, b / T * ms * .94]); };
    let t = 0; const ev = []; const at = (dt, fn) => ev.push({ t: dt, fn });
    const def = k => D.TYPES[k] || custom[k];
    const lanes = {};
    steps.forEach((s, si) => {
      const t0 = t; s.t0 = t0; s.t1 = t0 + s.ms; t += s.ms;
      if (si === 0) at(t0, g => g.log(`开始：${tables.length} 张表，来自 ${[...new Set(tables.map(x => x.src.name))].join('、')}`));
      if (['meta', 'profile', 'entity'].includes(s.k)) {
        const sc = sched(s.ms); lanes[s.k] = sc.map(([a, b]) => [t0 + a, t0 + b]);
        tables.forEach((x, i) => at(t0 + sc[i][1], g => {
          if (s.k === 'meta') { g.addTable(i); g.log(`读取 ${x.t.name}：${x.t.cols.length} 个字段${x.t.cols.some(c => c[4] === 'pk') ? '，有主键' : ''}`); }
          if (s.k === 'profile') { g.profile(i); g.log(`画像 ${x.t.name}：${fmt(x.t.rows)} 行，取样 ${fmt(Math.min(x.t.rows, 5000))} 行`); }
          if (s.k === 'entity') g.entity(i);
        }));
      }
      if (s.k === 'rel') { const rs = rels.filter(r => r[3] !== '时序识别'); rs.forEach((r, i) => at(t0 + (i + .6) * s.ms / (rs.length + .5), g => g.rel(r)));
        if (!rs.length) at(t0 + 200, g => g.log('这些表之间没有发现关系')); }
      if (s.k === 'ts') tsTables.forEach((x, j) => { const base = t0 + j * s.ms / tsTables.length; const span = s.ms / tsTables.length;
        at(base + span * .15, g => g.log(`${x.t.name}：时间列 + ${x.t.cols.filter(c => c[1] === 'float').length} 个数值列`));
        at(base + span * .45, g => g.tsSplit(tables.indexOf(x)));
        rels.filter(r => r[3] === '时序识别' && x.types.includes(r[0])).forEach((r, k) => at(base + span * (.65 + k * .12), g => g.rel(r))); });
      if (s.k === 'llm') types.forEach((k, i) => at(t0 + (i + .5) * s.ms / types.length, g => g.name(k)));
      if (s.k === 'check') {
        const nPk = tables.filter(x => x.t.cols.some(c => c[4] === 'pk')).length, decl = rels.filter(r => r[3] === '声明外键').length, inf = rels.filter(r => r[3] === '推断');
        at(t0 + 150, g => g.log(`主键唯一：${nPk} / ${nPk} 张表通过`)); at(t0 + 400, g => g.log(`外键完整：${decl} 条声明外键，完整率 100%`));
        if (inf.length) at(t0 + 650, g => g.log(`推断关系 ${inf.length} 条，值域重合都在 97% 以上，标为「推断」`));
        at(t0 + 800, g => g.check());
      }
      if (s.k === 'write') { at(t0 + 200, g => g.log(`写入 ${types.length} 个实体类型、${rels.length} 条关系、${fmt(types.reduce((a, k) => a + def(k).n, 0))} 个实例`));
        if (opt.dyn) at(t0 + 500, g => g.log(`开启持续更新：${[...new Set(tables.map(x => x.src.name + '（' + x.src.access + '）'))].join('、')}`));
        at(t0 + s.ms, g => g.finish()); }
    });
    ev.sort((a, b) => a.t - b.t);
    return { tables, types, rels, custom, customRels, steps, ev, lanes, total: t, def };
  }

  // ---------- 运行 ----------
  function start(list, opt) {
    const P = plan(list, opt);
    G = { P, opt, t0: performance.now(), ei: 0, logs: [], gn: new Map(), ge: new Map(), gone: {}, dirty: true, done: false, startedAt: now() };
    UI.gen = G;
    const tpos = (i) => { const x = P.tables[i]; const k = x.types[0]; const p = posOf(k); const same = P.tables.filter(y => y.types[0] === k); const j = same.indexOf(x);
      return { x: p.x + (j - (same.length - 1) / 2) * 165 + (x.kind === 'ts' ? 0 : 0), y: p.y + (x.kind === 'ts' ? -10 : 0) + (same.length > 1 ? 0 : 0) }; };
    const typeNode = (k, from) => { const d = P.def(k); G.gn.set('y:' + k, { label: D.TABLE_LABEL[k] || P.tables.find(x => x.types.includes(k)).t.name, color: d.color, size: Math.round(42 + 11 * Math.log10(d.n + 1)), pos: posOf(k), from, cls: 'typ raw' }); };
    Object.assign(G, {
      log: s => { const e = (performance.now() - G.t0) / 1000; G.logs.push([e.toFixed(1).padStart(4, '0') + 's', s]); },
      addTable: i => { G.gn.set('t:' + i, { label: P.tables[i].t.name, pos: tpos(i), cls: 'tbl' + (P.tables[i].kind === 'ts' ? ' tsx' : '') }); G.dirty = true; },
      profile: i => { const n = G.gn.get('t:' + i); n.label = P.tables[i].t.name + '\n' + fmt(P.tables[i].t.rows) + ' 行'; n.cls += ' prof'; G.dirty = true; },
      entity: i => { const x = P.tables[i];
        if (x.kind === 'ts') { G.gn.get('t:' + i).cls += ' wait'; G.log(`${x.t.name} 是时序表，留到「时序识别」处理`); G.dirty = true; return; }
        const from = G.gn.get('t:' + i).pos; const k = x.types[0];
        if (!G.gn.has('y:' + k)) { typeNode(k, from); G.log(`${x.t.name} → 实体类型（主键 ${(x.t.cols.find(c => c[4] === 'pk') || ['—'])[0]}）`); }
        else G.log(x.kind === 'state' ? `${x.t.name} → 并入「设备.状态」，按生效时间记录有效期` : `${x.t.name} → 合并到已有的实体类型`);
        G.gn.delete('t:' + i); G.gone['t:' + i] = posOf(k); G.dirty = true; },
      tsSplit: i => { const x = P.tables[i]; const from = G.gn.get('t:' + i).pos;
        x.types.forEach(k => { if (!G.gn.has('y:' + k)) typeNode(k, from); });
        if (x.types.includes('point')) { G.log('按 equip_id 分组：36 台设备 × 4 个数值列 → 144 个测点'); G.log('10 分钟窗口统计：均值、峰值、越线次数'); }
        if (x.types.includes('event')) G.log('规则：T50 连续 3 个窗口 > 1,598 °R → 87 个高温事件');
        if (x.types.includes('meter')) G.log(`${x.t.name} → 电表.日用电量（时间序列属性）`);
        G.gn.delete('t:' + i); G.gone['t:' + i] = posOf(x.types[0]); G.dirty = true; },
      rel: r => { G.ge.set(r[0] + '>' + r[1] + ':' + r[2], { source: 'y:' + r[0], target: 'y:' + r[1], label: r[2] + (r[3] === '推断' ? ' · 推断' : ''), cls: r[3] === '推断' ? 'inf' : r[3] === '时序识别' ? 'tsr' : 'decl' });
        G.log((r[3] === '推断' ? '推断：' : r[3] === '时序识别' ? '时序：' : '外键：') + r[5]); G.dirty = true; },
      name: k => { const n = G.gn.get('y:' + k); if (!n) return; const d = P.def(k); n.label = d.name; n.cls = 'typ'; G.log(`${D.TABLE_LABEL[k] || k} → ${d.name}（${fmt(d.n)} 个实例）`); G.dirty = true; },
      check: () => { G.gn.forEach(n => { if (n.cls.startsWith('typ')) n.cls = 'typ ok'; }); G.dirty = true; },
      finish: () => { if (G.done) return; G.done = true; G.dur = ((performance.now() - G.t0) / 1000).toFixed(1); commit(); }
    });
    clearInterval(Gen.timer); Gen.timer = setInterval(tick, 80);
  }
  function commit() {
    const P = G.P, o = G.opt; const srcIds = [...new Set(P.tables.map(x => x.src.id))];
    const modes = P.tables.some(x => x.kind === 'ts') ? ['basic', 'ts'] : ['basic'];
    let onto = S.ontos.find(x => x.id === o.target);
    if (onto) {
      const added = P.types.filter(k => !onto.types.includes(k));
      onto.types = [...new Set(onto.types.concat(P.types))]; onto.src = [...new Set(onto.src.concat(srcIds))]; onto.modes = [...new Set(onto.modes.concat(modes))];
      onto.custom = Object.assign(onto.custom || {}, P.custom); onto.customRels = (onto.customRels || []).concat(P.customRels); onto.dynamic = onto.dynamic || o.dyn;
      onto.updated = now(); onto.ver = (onto.ver || 12) + 1;
      (onto.feed = onto.feed || []).unshift([now(), `合并 ${P.tables.length} 张表${added.length ? '，新增「' + added.map(k => P.def(k).name).join('」「') + '」' : ''}`]);
    } else {
      onto = { id: 'o' + (S.seq++), name: o.name, src: srcIds, types: P.types, modes, dynamic: o.dyn, created: '11-17 ' + now(), updated: now(), extra: {}, custom: P.custom, customRels: P.customRels,
        pendingChange: false, delta: {}, ver: 1, feed: [[now(), `首次生成：${P.types.length} 个实体类型、${P.rels.length} 条关系`]] };
      S.ontos.push(onto);
    }
    G.ontoId = onto.id; S.cur = onto.id; save();
    clearInterval(Gen.timer);
    if (mounted) paintSide(); else toast(`「${onto.name}」生成完成`);
  }
  function tick() {
    if (!G) return; const e = performance.now() - G.t0;
    while (G.ei < G.P.ev.length && G.P.ev[G.ei].t <= e) { G.P.ev[G.ei].fn(G); G.ei++; }
    if (mounted) { paintProgress(e); if (G.dirty) { sync(true); G.dirty = false; } }
  }

  // ---------- 画布 ----------
  const CY_STYLE = [
    { selector: 'node', style: { label: 'data(label)', 'font-family': '-apple-system,"PingFang SC","Microsoft YaHei",sans-serif', 'text-wrap': 'wrap', 'transition-property': 'background-color,border-color,border-width', 'transition-duration': 300 } },
    { selector: '.tbl', style: { shape: 'round-rectangle', width: 150, height: 40, 'background-color': '#FFFFFF', 'border-width': 1, 'border-color': '#C9D2DC', 'font-family': '"SF Mono",Menlo,Consolas,monospace', 'font-size': 13, color: '#5E6B78', 'text-valign': 'center', 'text-halign': 'center' } },
    { selector: '.tbl.prof', style: { 'border-color': '#0F6E8C', 'background-color': '#F2F8FA', height: 50 } },
    { selector: '.tbl.wait', style: { 'border-style': 'dashed', 'border-color': '#22A88E', 'border-width': 1.5, 'background-color': '#EFFAF7' } },
    { selector: '.typ', style: { width: 'data(size)', height: 'data(size)', 'background-color': 'data(color)', 'border-width': 3, 'border-color': '#FFFFFF', 'font-size': 17, 'font-weight': 600, color: '#17212B', 'text-valign': 'bottom', 'text-margin-y': 7, 'text-background-color': '#FFFFFF', 'text-background-opacity': .85, 'text-background-padding': 2 } },
    { selector: '.typ.raw', style: { 'font-family': '"SF Mono",Menlo,Consolas,monospace', 'font-weight': 400, 'font-size': 13, color: '#5E6B78', 'background-opacity': .55 } },
    { selector: '.typ.ok', style: { 'border-color': '#1C9A6C', 'border-width': 3 } },
    { selector: 'edge', style: { width: 2, 'curve-style': 'bezier', 'target-arrow-shape': 'triangle', 'arrow-scale': .8, 'line-color': '#9AA8B6', 'target-arrow-color': '#9AA8B6', label: 'data(label)', 'font-size': 13, color: '#5E6B78', 'text-background-color': '#FFFFFF', 'text-background-opacity': 1, 'text-background-padding': 2, 'text-rotation': 'autorotate' } },
    { selector: '.inf', style: { 'line-style': 'dashed', 'line-color': '#8C7AE6', 'target-arrow-color': '#8C7AE6', color: '#6B55D6' } },
    { selector: '.tsr', style: { 'line-color': '#22A88E', 'target-arrow-color': '#22A88E', color: '#168069' } },
  ];
  function sync(anim) {
    if (!cy) return;
    cy.edges().forEach(e => { if (!G.ge.has(e.id())) e.remove(); });
    cy.nodes().forEach(n => { if (!G.gn.has(n.id())) { const to = G.gone[n.id()];
      if (anim && to) n.animate({ position: to, style: { opacity: 0 } }, { duration: 520, easing: 'ease-in-cubic', complete: () => n.remove() }); else n.remove(); } });
    G.gn.forEach((d, id) => { let n = cy.getElementById(id);
      if (n.empty()) { n = cy.add({ group: 'nodes', data: { id, label: d.label, color: d.color, size: d.size }, position: Object.assign({}, anim && d.from ? d.from : d.pos), classes: d.cls });
        if (anim) { n.style('opacity', 0); n.animate({ position: d.pos, style: { opacity: 1 } }, { duration: 620, easing: 'ease-out-cubic' }); } }
      else { if (n.data('label') !== d.label) n.data('label', d.label); n.classes(d.cls); } });
    G.ge.forEach((d, id) => { if (cy.getElementById(id).empty()) { const e = cy.add({ group: 'edges', data: { id, source: d.source, target: d.target, label: d.label }, classes: d.cls });
      if (anim) { e.style('opacity', 0); e.animate({ style: { opacity: 1 } }, { duration: 500 }); } } });
  }
  function camera() {
    const pts = G.P.types.map(posOf).concat(G.P.tables.map((x, i) => (G.gn.get('t:' + i) || {}).pos || posOf(x.types[0])));
    const xs = pts.map(p => p.x), ys = pts.map(p => p.y); const x1 = Math.min(...xs) - 90, x2 = Math.max(...xs) + 90, y1 = Math.min(...ys) - 50, y2 = Math.max(...ys) + 70;
    const w = cy.width(), h = cy.height(); const z = Math.min(1.25, w / (x2 - x1), h / (y2 - y1));
    cy.zoom(z); cy.pan({ x: w / 2 - (x1 + x2) / 2 * z, y: h / 2 - (y1 + y2) / 2 * z });
  }

  // ---------- 生成过程页面 ----------
  function render(v) {
    const P = G.P;
    v.innerHTML = `<div class="page gen">
      <div class="page-head">
        <div><div class="eyebrow">${G.done ? '生成完成' : '正在生成'}</div><h1>${esc(G.opt.name)}</h1>
          <p class="lead">${P.tables.length} 张表 · ${P.tables.some(x => x.kind === 'ts') ? '普通 + 时序' : '普通'}${G.opt.dyn ? ' · 动态（持续更新）' : ''} · 开始于 ${G.startedAt}</p></div>
        <div class="actions" id="genAct"></div>
      </div>
      <div class="gen-grid">
        <div class="card steps"><ol id="steps"></ol><div id="genSide"></div></div>
        <div class="card canvas"><div class="canvas-h"><b>本体草图</b><span class="legend"><i class="lg-decl"></i>声明外键<i class="lg-inf"></i>推断<i class="lg-ts"></i>时序识别</span><span class="grow"></span><span class="sub" id="cnt"></span></div><div id="genCy" class="cy grid-bg"></div></div>
        <div class="card lanes"><div class="lanes-h"><h3>并行处理</h3><span class="sub" id="par"></span></div><div id="lanes"></div><div class="lanes-h"><h3>日志</h3></div><div class="logs" id="logs"></div></div>
      </div></div>`;
    cy = cytoscape({ container: $('#genCy', v), style: CY_STYLE, elements: [], layout: { name: 'preset' }, minZoom: .3, maxZoom: 2, boxSelectionEnabled: false, autoungrabify: true });
    mounted = v; camera(); sync(false); G.dirty = false; G.logN = 0; paintProgress(performance.now() - G.t0); paintSide();
    return () => { mounted = null; if (cy) { cy.destroy(); cy = null; } };
  }
  function paintSide() {
    if (!mounted) return; const v = mounted, P = G.P;
    $('#genAct', v).innerHTML = G.done ? `<button class="btn" id="back">返回数据源</button><button class="btn primary" id="see">查看本体</button>` : `<button class="btn danger-ghost" id="stop">停止</button>`;
    if (G.done) {
      $('.eyebrow', v).textContent = '生成完成';
      $('#genSide', v).innerHTML = `<div class="done-card"><div class="ok-ic"><svg viewBox="0 0 24 24"><path d="m5 12 5 5 9-10"/></svg></div><div><h3>生成完成</h3><p class="sub">用时 ${G.dur} 秒（演示加速）</p></div>
        <div class="done-stats"><div><b>${P.types.length}</b><small>实体类型</small></div><div><b>${P.rels.length}</b><small>关系</small></div><div><b>${fmt(P.types.reduce((a, k) => a + P.def(k).n, 0))}</b><small>实例</small></div></div>
        ${G.opt.dyn ? '<p class="done-note"><span class="pill live"><i></i>持续更新中</span>数据源更新后本体跟着更新</p>' : ''}</div>`;
      $('#see', v).onclick = () => { UI.gen = null; G = null; go('ontology'); };
      $('#back', v).onclick = () => { UI.gen = null; G = null; route(); };
    } else $('#stop', v).onclick = () => { clearInterval(Gen.timer); UI.gen = null; G = null; toast('已停止，没有写入任何内容', 'warn'); route(); };
  }
  function paintProgress(e) {
    const v = mounted, P = G.P; if (!v) return;
    const cur = G.done ? P.steps.length : P.steps.findIndex(s => e < s.t1);
    $('#steps', v).innerHTML = STEPS.map(s => { const i = P.steps.indexOf(s);
      if (i < 0) return `<li class="skip"><span class="dot"></span><div><b>${s.name}</b><small>跳过：没有时序表</small></div></li>`;
      const st = i < cur ? 'done' : i === cur ? 'run' : 'todo'; const p = st === 'run' ? Math.min(1, (e - s.t0) / s.ms) : 0;
      return `<li class="${st}"><span class="dot">${st === 'done' ? '<svg viewBox="0 0 24 24"><path d="m5 12 5 5 9-10"/></svg>' : i + 1}</span><div><b>${s.name}</b><small>${s.sub}</small>${st === 'run' ? `<div class="bar"><i style="width:${p * 100}%"></i></div>` : ''}</div><em>${st === 'done' ? (s.ms / 1000).toFixed(1) + 's' : ''}</em></li>`; }).join('');
    // 并行处理
    const layers = ['meta', 'profile', 'entity']; const LN = { meta: '读取', profile: '画像', entity: '识别' };
    let running = 0;
    $('#lanes', v).innerHTML = P.tables.map((x, i) => { let p = 0, label = '排队', st = 'q';
      layers.forEach((L, li) => { const [a, b] = P.lanes[L][i]; if (e >= b) p = li + 1; else if (e >= a) { p = li + (e - a) / (b - a); label = LN[L] + '中'; st = 'run'; running++; } });
      if (st !== 'run') {
        if (p >= 3) { st = 'done'; label = x.kind === 'ts' && G.gn.has('t:' + i) ? '待时序识别' : '→ ' + x.types.map(k => P.def(k).name).join('、'); }
        else if (p > 0) label = '等下一步';
      }
      return `<div class="lane ${st}"><code>${esc(x.t.name)}</code><div class="bar"><i style="width:${p / 3 * 100}%"></i></div><span>${esc(label)}</span></div>`; }).join('');
    $('#par', v).textContent = cur >= 0 && cur < 3 ? `正在并行 ${running} / ${SLOTS}` : `每一步最多 ${SLOTS} 张表同时处理`;
    const nt = cy ? cy.nodes('.typ').length : 0, ne = cy ? cy.edges().length : 0; $('#cnt', v).textContent = `实体类型 ${nt} · 关系 ${ne}`;
    const lg = $('#logs', v); if (G.logN !== G.logs.length) { lg.insertAdjacentHTML('beforeend', G.logs.slice(G.logN).map(([t, s]) => `<div><time>${t}</time>${esc(s)}</div>`).join('')); lg.scrollTop = lg.scrollHeight; G.logN = G.logs.length; }
  }

  return { panel, render, timer: null };
})();
