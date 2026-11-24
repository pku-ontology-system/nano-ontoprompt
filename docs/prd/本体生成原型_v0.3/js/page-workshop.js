/* 页面三：应用搭建（参考 Foundry Workshop 的做法，只用在本体展示上）
   专员从模块库挑模块，放到页面的六个位置（顶部、左上、右上、左下、右下、底部），模块之间联动，按企业保存。
   只放「本体生成 / 本体展示」相关的模块（中游）；数据接入归数据中台（上游），数据分析和业务应用归下游。
   模块定义在 ws-modules.js；本体总览原有的五种视图和面板仍由 makeOntoView 渲染。 */
const PageWorkshop = (() => {
  const MOD = WS_MODULES.by;
  const HEIGHT = { xs: 'auto', s: 220, m: 380, l: 540 };
  const H_CN = { xs: '自适应', s: '矮', m: '中', l: '高' };
  const ZONES = [['top', '顶部整行'], ['tl', '左上'], ['tr', '右上'], ['bl', '左下'], ['br', '右下'], ['bottom', '底部整行']];
  const Z_CN = Object.fromEntries(ZONES);
  const RATIOS = ['1:1', '2:1', '1:2', '3:1', '1:3'];
  const ENT = [['hd', '华东动力工厂'], ['bf', '北方能源公司'], ['lab', '实验室演示']];
  const LAYOUT_V = 2;   // 布局格式版本：旧版（12 栅格）存的数据打开时按模板重建

  // ---------- 模板 ----------
  const W = (kind, zone, extra = {}) => Object.assign({ kind, zone, h: MOD[kind].h }, extra);
  const TPL = {
    review: { name: '本体审阅', desc: '看生成结果对不对：类型、关系、待确认的推断、健康检查', ratios: { up: '2:1', low: '1:1' }, ws: () => [
      W('title', 'top', { text: '本体审阅', sub: '核对生成的类型、关系和字段来源；推断出来的关系逐条确认' }), W('bar', 'top'), W('banner', 'top'),
      W('type', 'tl'), W('detail', 'tr'), W('pending', 'bl'), W('health', 'br'), W('attrs', 'bottom', { h: 'm' })] },
    dynamic: { name: '动态本体监控', desc: '看数据变化有没有及时进到本体，结构变化是否已处理', ratios: { up: '2:1', low: '1:1' }, ws: () => [
      W('title', 'top', { text: '动态本体监控', sub: '数据中台每次更新后，本体是否跟着变；结构变化是否已处理' }), W('bar', 'top'), W('banner', 'top'),
      W('daily', 'tl'), W('feed', 'tr'), W('ts', 'bl'), W('versions', 'br'), W('upstream', 'bottom')] },
    show: { name: '演示展示', desc: '给企业领导看的展示页，以图为主', ratios: { up: '2:1', low: '1:1' }, ws: () => [
      W('title', 'top', { text: '工厂设备本体一览', sub: '由数据中台「工厂主题库」自动生成，持续更新' }), W('stats', 'top'),
      W('star', 'tl'), W('detail', 'tr'), W('share', 'bl'), W('chord', 'br')] },
    quality: { name: '质量与来源核对', desc: '每个类型从哪来、怎么识别的、还有哪些要人工处理', ratios: { up: '1:1', low: '1:1' }, ws: () => [
      W('title', 'top', { text: '质量与来源核对', sub: '给数据中台和业务部门一起核对：来源、映射、识别方式' }), W('health', 'top', { h: 'xs' }),
      W('lineage', 'tl'), W('methods', 'tr'), W('mapping', 'bl'), W('pending', 'br')] },
    handoff: { name: '上下游对接', desc: '本体从哪来（中台）、给谁用（下游接口），附本体说明书', ratios: { up: '1:1', low: '1:1' }, ws: () => [
      W('title', 'top', { text: '上下游对接', sub: '上游：数据中台资产；下游：查询、订阅、导出接口' }), W('stats', 'top'),
      W('upstream', 'tl'), W('downstream', 'tr'), W('lineage', 'bl'), W('doc', 'br')] },
    blank: { name: '空白应用', desc: '从零开始挑模块', ratios: { up: '1:1', low: '1:1' }, ws: () => [W('title', 'top', { text: '新应用', sub: '从左侧模块库添加模块' })] },
  };
  const ENT_TPL = { hd: 'review', bf: 'dynamic', lab: 'show' };

  let root = null, insts = [], live = null, editing = false, hist = [], pick = null, target = 'auto', q = '';
  const store = () => { S.ws = S.ws || {}; return S.ws; };
  let seq = 1; const nid = () => 'w' + Date.now().toString(36) + (seq++);
  const newApp = tpl => ({ lv: LAYOUT_V, tpl, ver: 1, saved: '—', ratios: Object.assign({}, TPL[tpl].ratios), widgets: TPL[tpl].ws().map(x => Object.assign({ id: nid() }, x)) });
  const cur = () => {
    const s = store(); s.ent = s.ent || 'hd'; if (!s.apps) s.apps = {};
    const a = s.apps[s.ent]; if (!a || a.lv !== LAYOUT_V || !(a.widgets || []).every(w => MOD[w.kind])) s.apps[s.ent] = newApp(a && TPL[a.tpl] ? a.tpl : ENT_TPL[s.ent]);
    return s.apps[s.ent];
  };
  const snap = () => { const a = cur(); hist.push(JSON.stringify({ widgets: a.widgets, ratios: a.ratios })); if (hist.length > 30) hist.shift(); };
  const count = k => cur().widgets.filter(w => w.kind === k).length;
  const canAdd = k => MOD[k].multi || !count(k);

  function render(v) { root = v; editing = false; hist = []; pick = null; paintAll(); return teardown; }
  function teardown() { insts.forEach(i => { try { i.off && i.off(); } catch (e) { /* 忽略 */ } }); insts = []; if (live) { clearInterval(live); live = null; } }

  // ---------- 整页 ----------
  function paintAll() {
    teardown();
    const s = store(), app = cur(), entName = ENT.find(e => e[0] === s.ent)[1];
    root.innerHTML = `<div class="page ws ${editing ? 'ws-editing' : ''}">
      <div class="ws-top card">
        <div class="ws-top-l">
          <div class="eyebrow">应用搭建 · 本体展示与定制</div>
          <div class="ws-title"><b>${esc(entName)}</b><span class="sub">· ${esc(TPL[app.tpl] ? TPL[app.tpl].name : '自定义')} · ${app.widgets.length} 个模块 · v${app.ver} · 保存于 ${esc(app.saved)}</span></div>
        </div>
        <label class="ws-ctl"><span>企业</span><select id="wsEnt">${ENT.map(([k, n]) => `<option value="${k}" ${k === s.ent ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
        <div class="seg" role="tablist"><button class="${editing ? '' : 'on'}" data-mode="view">查看</button><button class="${editing ? 'on' : ''}" data-mode="edit">编辑</button></div>
        ${editing ? `<button class="btn" id="wsTpl">换模板</button><button class="btn ghost" id="wsUndo" ${hist.length ? '' : 'disabled'}>撤销</button><button class="btn primary" id="wsSave">保存</button>` : '<button class="btn primary" id="wsEdit">编辑这个应用</button>'}
      </div>
      <div class="ws-body">
        ${editing ? libHtml() : ''}
        <div class="ws-canvas" id="wsCanvas">${canvasHtml(app)}</div>
      </div>
    </div>`;
    $('#wsEnt', root).onchange = e => { store().ent = e.target.value; save(); hist = []; pick = null; paintAll(); toast('已切换到「' + ENT.find(x => x[0] === e.target.value)[1] + '」的应用'); };
    $$('[data-mode]', root).forEach(b => b.onclick = () => { editing = b.dataset.mode === 'edit'; paintAll(); });
    if ($('#wsEdit', root)) $('#wsEdit', root).onclick = () => { editing = true; paintAll(); };
    if (editing) wireEdit();
    mountAll();
  }

  // 模块库：按分组列出；默认只能加一次，已加的显示「已添加」，点它跳到那个模块
  function libHtml() {
    const app = cur(); const hit = m => !q || (m.name + m.desc + (m.ref || '')).toLowerCase().includes(q.toLowerCase());
    return `<aside class="ws-lib card"><div class="ws-lib-h"><b>模块库</b><small>${WS_MODULES.list.length} 个模块 · 默认每个只能加一次，标「可多次」的除外</small></div>
      <input class="ws-q" id="wsQ" placeholder="搜索模块" value="${esc(q)}">
      <label class="ws-target"><span>添加到</span><select id="wsTarget"><option value="auto" ${target === 'auto' ? 'selected' : ''}>自动选位置</option>${ZONES.map(([k, n]) => `<option value="${k}" ${target === k ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
      ${WS_MODULES.groups.map(g => { const ms = WS_MODULES.list.filter(m => m.g === g && hit(m)); if (!ms.length) return '';
        return `<div class="ws-lib-g"><h4>${g}</h4>${ms.map(m => { const n = count(m.k), ok = canAdd(m.k);
          return `<button class="ws-lib-it ${ok ? '' : 'used'}" data-add="${m.k}" title="${esc(ok ? '添加' : '已在页面上，点击定位')}"><b>${esc(m.name)}${m.multi ? '<em class="tag">可多次</em>' : ''}${n ? `<em class="tag ok">已添加${m.multi && n > 1 ? ' ' + n : ''}</em>` : ''}${m.fromV2 ? '<em class="tag v2">原有功能</em>' : ''}</b><span>${esc(m.desc)}</span>${m.ref && m.ref !== '—' ? `<small>参考：${esc(m.ref)}</small>` : ''}</button>`; }).join('')}</div>`; }).join('')}
      <p class="ws-lib-note">联动：任何模块里点中一个类型、关系或实例，详情面板、类型卡、实例卡、邻居网络和各视图会一起定位。</p></aside>`;
  }

  // 画布：顶部整行 → 上排（左上 | 右上）→ 下排（左下 | 右下）→ 底部整行；查看时空的位置不占地方
  function canvasHtml(app) {
    const by = z => app.widgets.filter(w => w.zone === z);
    if (!app.widgets.length) return '<div class="empty-box center"><b>页面是空的</b><p>点「编辑这个应用」，从模块库添加模块。</p></div>';
    const zone = z => { const ws = by(z); if (!ws.length && !editing) return '';
      return `<div class="ws-zone ws-z-${z}" data-zone="${z}">${editing ? `<div class="ws-zone-h"><b>${Z_CN[z]}</b><small>${ws.length} 个模块</small><button class="btn ghost sm" data-target="${z}">添加到这里</button></div>` : ''}${ws.map(cell).join('') || (editing ? '<div class="ws-zone-empty">空位置。在左侧选「添加到：' + Z_CN[z] + '」，或点上面的「添加到这里」。</div>' : '')}</div>`; };
    const row = (key, a, b) => { const A = zone(a), B = zone(b); if (!A && !B) return '';
      const [x, y] = (app.ratios[key] || '1:1').split(':'); const cols = A && B ? `${x}fr ${y}fr` : '1fr';
      return `<div class="ws-row" style="grid-template-columns:${cols}">${editing ? `<label class="ws-ratio"><span>${key === 'up' ? '上排' : '下排'}左右宽度</span><select data-ratio="${key}">${RATIOS.map(r => `<option ${app.ratios[key] === r ? 'selected' : ''}>${r}</option>`).join('')}</select></label>` : ''}${A}${B}</div>`; };
    return zone('top') + row('up', 'tl', 'tr') + row('low', 'bl', 'br') + zone('bottom');
  }

  function cell(w) {
    const m = MOD[w.kind]; const h = HEIGHT[w.h] || HEIGHT[m.h];
    const hs = m.h === 'xs' ? Object.keys(HEIGHT) : ['s', 'm', 'l'];
    const optSel = (m.opts || []).map(o => `<select data-opt="${w.id}" data-key="${o.key}" title="${esc(o.label)}">${o.choices(ctxBase()).map(([v, l]) => `<option value="${esc(v)}" ${String(w[o.key] || '') === String(v) ? 'selected' : ''}>${esc(o.label)}：${esc(l)}</option>`).join('')}</select>`).join('');
    const tools = editing ? `<div class="ws-tools">${optSel}
        <select data-zone-of="${w.id}" title="位置">${ZONES.map(([k, n]) => `<option value="${k}" ${w.zone === k ? 'selected' : ''}>${n}</option>`).join('')}</select>
        <select data-h="${w.id}" title="高度">${hs.map(k => `<option value="${k}" ${w.h === k ? 'selected' : ''}>高 ${H_CN[k]}</option>`).join('')}</select>
        <button class="ic-btn" data-up="${w.id}" title="在这个位置里上移">↑</button><button class="ic-btn" data-down="${w.id}" title="在这个位置里下移">↓</button>${m.multi ? `<button class="ic-btn" data-dup="${w.id}" title="复制一个">⧉</button>` : ''}<button class="ic-btn ws-del" data-del="${w.id}" title="删除">×</button></div>` : '';
    const bare = ['title', 'bar', 'banner', 'stats'].includes(w.kind);
    const head = (editing || !bare) ? `<div class="ws-cell-h"><b>${esc(m.name)}</b>${!editing && m.follows && w.follow !== 'fixed' && (w.kind !== 'attrs' || w.follow === 'pick') ? '<small class="ws-follow">跟随选中</small>' : ''}${editing ? `<small>${esc(m.ref && m.ref !== '—' ? '参考：' + m.ref : m.desc)}</small>` : ''}${tools}</div>` : '';
    return `<section class="ws-cell ws-k-${w.kind}" data-id="${w.id}">${head}<div class="ws-cell-b" style="${h === 'auto' ? '' : 'height:' + h + 'px'}"></div></section>`;
  }

  // ---------- 挂载与联动 ----------
  const ctxBase = () => ({ o: curOnto(), pick });
  function setPick(p, from) {
    pick = p;
    insts.forEach(i => {
      if (i === from) return;
      if (i.view) i.view.showPick(p, from && from.view ? from.html : null);
      else if (i.mod && (i.mod.follows || i.w.kind === 'insttable')) i.draw();
    });
  }
  function mountAll() {
    const app = cur();
    app.widgets.forEach(w => {
      const body = $(`.ws-cell[data-id="${w.id}"] .ws-cell-b`, root); if (!body) return;
      const m = MOD[w.kind]; const inst = { w, mod: m };
      if (m.view) {
        const hooks = { widget: true, onOnto: () => { pick = null; paintAll(); } };
        inst.view = makeOntoView({ view: w.kind, pick: null }, hooks);
        hooks.onPick = (p, html) => { inst.html = html; setPick(p, inst); };
        try { inst.off = inst.view.mountWidget(w.kind, body); } catch (e) { console.error(e); body.innerHTML = '<div class="wg-quiet">这个模块加载失败</div>'; }
      } else if (m.layout) {
        drawLayout(w, body);
      } else {
        let charts = [], cys = [];
        const clean = () => { charts.forEach(c => { try { c.dispose(); } catch (e) { /* 忽略 */ } }); cys.forEach(c => { try { c.destroy(); } catch (e) { /* 忽略 */ } }); charts = []; cys = []; };
        const ctx = { get o() { return curOnto(); }, get pick() { return pick; }, setPick: p => setPick(p, inst),
          chart: el => { const c = echarts.init(el); charts.push(c); return c; }, cy: opts => { const c = cytoscape(opts); cys.push(c); return c; } };
        inst.draw = () => { clean(); body.innerHTML = ''; try { m.render(body, w, ctx); } catch (e) { console.error(e); body.innerHTML = '<div class="wg-quiet">这个模块加载失败</div>'; } };
        const onResize = () => charts.forEach(c => c.resize());
        window.addEventListener('resize', onResize);
        inst.off = () => { clean(); window.removeEventListener('resize', onResize); };
        inst.draw();
      }
      insts.push(inst);
    });
    // 持续更新：整页只跑一个计时器；有视图模块时由它推进数据，其余模块只刷新显示
    if (curOnto().dynamic) live = setInterval(() => {
      const vs = insts.filter(i => i.view).map(i => i.view);
      if (vs.length) { vs[0].tick(); vs.slice(1).forEach(x => x.paint()); }
      insts.filter(i => i.draw && ['stats'].includes(i.w.kind)).forEach(i => i.draw());
    }, 4500);
  }
  function drawLayout(w, body) {
    if (w.kind === 'title') {
      body.innerHTML = editing ? `<div class="ws-titlebox"><input class="ws-in-t" data-t="${w.id}" value="${esc(w.text || '')}" placeholder="标题"><input class="ws-in-s" data-s="${w.id}" value="${esc(w.sub || '')}" placeholder="一句说明"></div>`
        : `<div class="ws-titlebox"><h1>${esc(w.text || '')}</h1><p>${esc(w.sub || '')}</p></div>`;
    } else {
      const t = w.text || '这个页面给设备部看：\n• 点任何图里的一个类型，详情面板显示它的属性和来源字段\n• 虚线和「推断」标记的关系是平台推断出来的，需要人工确认';
      body.innerHTML = editing ? `<textarea class="ws-ta" data-x="${w.id}">${esc(t)}</textarea>` : `<div class="ws-text">${esc(t).replace(/\n/g, '<br>')}</div>`;
    }
  }

  // ---------- 编辑 ----------
  function autoZone(k) {
    const m = MOD[k]; if (m.h === 'xs' || k === 'title') return 'top';
    const app = cur(); return ['tl', 'tr', 'bl', 'br'].find(z => !app.widgets.some(w => w.zone === z)) || 'bottom';
  }
  function wireEdit() {
    const app = cur();
    const find = id => app.widgets.findIndex(w => w.id === id);
    const change = fn => { snap(); fn(); save(); paintAll(); };
    const locate = id => setTimeout(() => { const c = $(`.ws-cell[data-id="${id}"]`, root); if (c) { c.scrollIntoView({ behavior: 'smooth', block: 'center' }); c.classList.add('ws-flash'); setTimeout(() => c.classList.remove('ws-flash'), 1200); } }, 60);
    $('#wsQ', root).oninput = e => { q = e.target.value; const pos = e.target.selectionStart; const lib = $('.ws-lib', root); const st = lib.scrollTop; lib.outerHTML = libHtml(); wireEdit(); const nq = $('#wsQ', root); nq.focus(); nq.setSelectionRange(pos, pos); $('.ws-lib', root).scrollTop = st; };
    $('#wsTarget', root).onchange = e => { target = e.target.value; };
    $$('[data-target]', root).forEach(b => b.onclick = () => { target = b.dataset.target; $('#wsTarget', root).value = target; toast('接下来添加的模块会放到「' + Z_CN[target] + '」'); });
    $$('[data-add]', root).forEach(b => b.onclick = () => {
      const k = b.dataset.add;
      if (!canAdd(k)) { const w = app.widgets.find(x => x.kind === k); toast(`「${MOD[k].name}」已经在「${Z_CN[w.zone]}」了，这个模块只能加一次`, 'warn'); locate(w.id); return; }
      const id = nid(); const zone = target === 'auto' ? autoZone(k) : target;
      change(() => { app.widgets.push(Object.assign({ id }, W(k, zone))); });
      toast(`已添加「${MOD[k].name}」到${Z_CN[zone]}`); locate(id);
    });
    $$('[data-ratio]', root).forEach(s => s.onchange = () => change(() => { app.ratios[s.dataset.ratio] = s.value; }));
    $$('[data-zone-of]', root).forEach(s => s.onchange = () => { const id = s.dataset.zoneOf; change(() => { const w = app.widgets.splice(find(id), 1)[0]; w.zone = s.value; app.widgets.push(w); }); locate(id); });
    $$('[data-h]', root).forEach(s => s.onchange = () => change(() => { app.widgets[find(s.dataset.h)].h = s.value; }));
    $$('[data-opt]', root).forEach(s => s.onchange = () => change(() => { app.widgets[find(s.dataset.opt)][s.dataset.key] = s.value; }));
    // 上移 / 下移：只在同一个位置里换顺序
    const move = (id, dir) => { const w = app.widgets[find(id)]; const same = app.widgets.filter(x => x.zone === w.zone); const j = same.indexOf(w) + dir; if (j < 0 || j >= same.length) return toast(dir < 0 ? '已经在这个位置的最上面' : '已经在这个位置的最下面', 'warn');
      change(() => { const a = find(id), b = find(same[j].id); [app.widgets[a], app.widgets[b]] = [app.widgets[b], app.widgets[a]]; }); };
    $$('[data-up]', root).forEach(b => b.onclick = () => move(b.dataset.up, -1));
    $$('[data-down]', root).forEach(b => b.onclick = () => move(b.dataset.down, 1));
    $$('[data-dup]', root).forEach(b => b.onclick = () => { const i = find(b.dataset.dup); const id = nid(); change(() => app.widgets.splice(i + 1, 0, Object.assign({}, app.widgets[i], { id }))); toast('已复制一个「' + MOD[app.widgets[i].kind].name + '」'); locate(id); });
    $$('[data-del]', root).forEach(b => b.onclick = () => { const i = find(b.dataset.del); const n = MOD[app.widgets[i].kind].name; change(() => app.widgets.splice(i, 1)); toast('已删除「' + n + '」，可以撤销'); });
    $$('[data-t]', root).forEach(i => i.onchange = () => { snap(); app.widgets[find(i.dataset.t)].text = i.value; save(); });
    $$('[data-s]', root).forEach(i => i.onchange = () => { snap(); app.widgets[find(i.dataset.s)].sub = i.value; save(); });
    $$('[data-x]', root).forEach(i => i.onchange = () => { snap(); app.widgets[find(i.dataset.x)].text = i.value; save(); });
    $('#wsUndo', root).onclick = () => { if (!hist.length) return; const h = JSON.parse(hist.pop()); app.widgets = h.widgets; app.ratios = h.ratios; save(); paintAll(); toast('已撤销'); };
    $('#wsSave', root).onclick = () => { app.ver++; app.saved = now(); save(); editing = false; paintAll(); toast(`已保存为 v${app.ver}，「${ENT.find(e => e[0] === store().ent)[1]}」的人员打开就是这个样子`); };
    $('#wsTpl', root).onclick = () => {
      const m = modal(`<div class="dlg-h"><h2>换模板</h2><p>模板是一组预先摆好位置的模块。换模板会替换当前页面上的模块（可以撤销）。</p></div>
        <div class="dlg-b"><div class="ws-tpls">${Object.entries(TPL).map(([k, t]) => { const ws = t.ws();
          return `<button class="ws-tpl ${app.tpl === k ? 'on' : ''}" data-tpl="${k}"><b>${t.name}</b><span>${t.desc}</span>${miniMap(ws)}<small>${ws.filter(x => !MOD[x.kind].layout).map(x => MOD[x.kind].name).join(' · ')}</small></button>`; }).join('')}</div></div>
        <div class="dlg-f"><button class="btn" data-close>取消</button></div>`, { wide: true });
      $$('[data-tpl]', m.el).forEach(b => b.onclick = () => { m.close(); const k = b.dataset.tpl; change(() => { const n = newApp(k); app.tpl = k; app.widgets = n.widgets; app.ratios = n.ratios; }); toast('已套用模板「' + TPL[k].name + '」'); });
    };
  }
  // 模板缩略图：六个位置各放了什么
  function miniMap(ws) {
    const z = k => ws.filter(w => w.zone === k && !MOD[w.kind].layout).map(w => MOD[w.kind].name).join('、');
    const box = k => z(k) ? `<i>${esc(z(k))}</i>` : '';
    return `<div class="ws-mini">${box('top') ? `<div class="mm-full">${box('top')}</div>` : ''}<div class="mm-row">${box('tl')}${box('tr')}</div><div class="mm-row">${box('bl')}${box('br')}</div>${box('bottom') ? `<div class="mm-full">${box('bottom')}</div>` : ''}</div>`;
  }

  return { title: '应用搭建', render };
})();
