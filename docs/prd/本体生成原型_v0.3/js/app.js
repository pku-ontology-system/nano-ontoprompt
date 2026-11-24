/* 应用外壳：状态、路由、弹窗、提示 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = n => n >= 1e8 ? (n / 1e8).toFixed(1) + ' 亿' : n >= 1e4 ? (n / 1e4).toFixed(n >= 1e6 ? 0 : 1) + ' 万' : Number(n).toLocaleString('en-US');
const now = () => { const d = new Date(); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); };
const KIND_CN = { basic: '普通', ts: '时序', state: '状态变更' };

// ---------- 状态（只把演示数据存在本浏览器里，读写失败时照常运行） ----------
const KEY = 'onto-gen-proto-v3';
const fresh = () => ({ ontos: [D.initialOnto()], cur: 'o1', extra: [], seq: 2 });
let S = (() => { try { const s = JSON.parse(localStorage.getItem(KEY)); if (s && s.ontos && s.ontos.length) return s; } catch (e) { /* 忽略 */ } return fresh(); })();
const save = () => { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) { /* 忽略 */ } };
const UI = { gen: null, sel: {}, open: 'dp', view: 'type', pick: null };

// 数据源 = 内置 + 连接数据中台后新增的 + 手动添加的
function allSources() {
  const extra = S.extra.map(x => {
    if (x.cat) { const c = D.catalog.find(c => c.name === x.cat); return { id: x.id, name: c.name, note: '数据中台 · ' + c.path, kind: '数据中台', access: '中台物化完成后回调', updated: '11-17 02:00', live: true,
      meta: { owner: '能源管理组', job: 'energy_theme_daily（每日 02:30）', lineage: '电表采集 → ods → dwd → 主题库', quality: '91 / 100' }, tables: c.tablesDef }; }
    return x;
  });
  return D.sources.concat(extra);
}
const srcById = id => allSources().find(s => s.id === id);
const curOnto = () => S.ontos.find(o => o.id === S.cur) || S.ontos[0];
const ontoOfSource = id => S.ontos.find(o => o.src.includes(id));

// 类型定义：内置目录，或生成时临时建的类型
const typeDef = (o, k) => D.TYPES[k] || (o.custom && o.custom[k]);
const typeCount = (o, k) => (typeDef(o, k).n || 0) + ((o.delta && o.delta[k]) || 0);
const typeAttrs = (o, k) => typeDef(o, k).attrs.concat((o.extra && o.extra[k]) || []);
const ontoRels = o => D.RELS.filter(r => o.types.includes(r[0]) && o.types.includes(r[1])).concat(o.customRels || []);

// ---------- 提示与弹窗 ----------
function toast(msg, kind = 'ok') {
  const t = document.createElement('div'); t.className = 'toast ' + kind; t.textContent = msg; $('#toasts').appendChild(t);
  setTimeout(() => t.classList.add('out'), 2600); setTimeout(() => t.remove(), 3000);
}
function modal(html, opt = {}) {
  const m = $('#modal'); m.innerHTML = `<div class="mask"></div><div class="dlg ${opt.wide ? 'wide' : ''}" role="dialog" aria-modal="true">${html}</div>`; m.hidden = false;
  const close = () => { m.hidden = true; m.innerHTML = ''; document.removeEventListener('keydown', onKey); };
  const onKey = e => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  m.querySelector('.mask').onclick = close; $$('[data-close]', m).forEach(b => b.onclick = close);
  return { el: m.querySelector('.dlg'), close };
}

// ---------- 路由（#sources / #ontology） ----------
const PAGES = { sources: () => PageSources, ontology: () => PageOntology, workshop: () => PageWorkshop };
let leave = null;
function route() {
  let k = location.hash.replace('#', ''); if (!PAGES[k]) k = 'sources';
  if (leave) { try { leave(); } catch (e) { console.error(e); } leave = null; }
  $$('.nav a').forEach(a => a.classList.toggle('on', a.dataset.k === k));
  const P = PAGES[k](); $('#crumb').innerHTML = `<span>本体生成</span><i>/</i><b>${esc(P.title)}</b>`;
  const v = $('#view'); v.innerHTML = ''; v.scrollTop = 0; leave = P.render(v) || null;
}
function go(k) { if (location.hash === '#' + k) route(); else location.hash = k; }
function rerender() { route(); }

document.addEventListener('DOMContentLoaded', () => {
  $('#reset').onclick = () => {
    const m = modal(`<div class="dlg-h"><h2>重置演示数据</h2></div><div class="dlg-b"><p>会清除本浏览器里这个原型的演示数据：新生成的本体、新增的数据源、结构变化的处理结果。不影响其他内容。</p></div>
      <div class="dlg-f"><button class="btn" data-close>取消</button><button class="btn danger" id="doReset">重置</button></div>`);
    $('#doReset', m.el).onclick = () => { S = fresh(); save(); UI.gen = null; UI.sel = {}; UI.open = 'dp'; m.close(); toast('已恢复初始演示数据'); go('sources'); };
  };
  window.addEventListener('hashchange', route); route();
});
