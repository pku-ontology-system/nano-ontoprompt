import { test, expect, type Page } from '@playwright/test';

// The home screen: the platform's name, what is in it right now (read from the API, not written into the page), and the way into the rest.
const ONTOLOGIES = [
  { id: 'ont-tpch', name: '供应链订单履约（TPC-H）', domain: '供应链', status: 'created', build_mode: 'data_import', created_at: '2026-11-10T00:00:00+00:00', types: 12, objects: 7498, link_types: 13, imported: { tables: 12, sources: 1, kind: 'relational', replay: null } },
  { id: 'ont-abt', name: '商品主数据合并（Abt-Buy）', domain: '供应链', status: 'created', build_mode: 'data_import', created_at: '2026-11-10T00:00:00+00:00', types: 3, objects: 2332, link_types: 4, imported: { tables: 3, sources: 1, kind: 'multisource', replay: null } },
  { id: 'ont-empty', name: '空本体', domain: '通用', status: 'draft', build_mode: 'manual', created_at: null, types: 0, objects: 0, link_types: 0, imported: null },
];
const OVERVIEW = {
  ontologies: ONTOLOGIES,
  runs: [{ id: 'r1', kind: 'resolution', dataset_id: 'x', title: 'Product · 实体消歧（本体）', summary: { precision: 0.83, recall: 0.48, f1: 0.6 }, params: {}, created_at: new Date(Date.now() - 120_000).toISOString() },
    { id: 'r2', kind: 'health', dataset_id: 'y', title: 'LineItem · 数据健康（本体）', summary: { success_percent: 96.4 }, params: {}, created_at: new Date(Date.now() - 3_600_000).toISOString() }],
  runs_by_kind: { health: 7, resolution: 3 }, health: { average: 96.4, runs: 7 }, lineage_jobs: 18,
  totals: { ontologies: 3, types: 15, objects: 9830, link_types: 17, imports: 2, runs: 10, lineage_jobs: 18 },
};

async function mount(page: Page, overview: unknown = OVERVIEW) {
  const visited: string[] = [];
  await page.setViewportSize({ width: 1600, height: 960 });
  await page.addInitScript(() => {
    localStorage.setItem('token', 'home-test');
    localStorage.setItem('auth-store', JSON.stringify({ state: { token: 'home-test', user: { id: 'u', username: 'admin', role: 'admin' } }, version: 0 }));
  });
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    const p = new URL(route.request().url()).pathname;
    visited.push(p);
    const json = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    if (p === '/api/v2/analysis/overview') return json(overview);
    if (p === '/api/v2/ontology-imports/bundles') return json({ bundles: [] });
    if (p === '/api/v1/ontologies') return json({ data: { items: [], total: 0 } });
    return json({});
  });
  await page.goto('/overview');
  return visited;
}

test('the home screen carries the platform name and numbers read from the API', async ({ page }) => {
  await mount(page);
  const screen = page.getByTestId('home-screen');
  await expect(screen.getByRole('heading', { level: 1 })).toHaveText('本体图谱平台');
  await expect(page.getByTestId('kpi-本体')).toContainText('3');
  await expect(page.getByTestId('kpi-对象')).toContainText('9,830');
  await expect(page.getByTestId('kpi-对象类型')).toContainText('15');
  await expect(page.getByTestId('kpi-关系类型')).toContainText('17');
  for (const [stage, value] of [['数据源', '2'], ['对象类型', '15'], ['对象', '9,830'], ['血缘作业', '18'], ['分析运行', '10']]) await expect(page.getByTestId(`stage-${stage}`)).toContainText(value);
  const runs = page.getByTestId('recent-runs');
  await expect(runs).toContainText('Product · 实体消歧（本体）');
  await expect(runs).toContainText('LineItem · 数据健康（本体）');
  await expect(screen).not.toContainText('本体构筑工作台');
  await page.waitForTimeout(1800);  // the entrance animations have run
  await page.screenshot({ path: 'test-results/home-screen.png' });
});

test('the ontology graph shows the ontologies that have objects and lets one be picked', async ({ page }) => {
  await mount(page);
  const tabs = page.getByRole('tablist', { name: '切换本体' });
  await expect(tabs.getByRole('tab')).toHaveCount(2);  // the empty ontology has nothing to draw
  await expect(tabs.getByRole('tab').first()).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('home-screen')).toContainText('12 个类型');
  await tabs.getByRole('tab', { name: /商品主数据/ }).click();
  await expect(tabs.getByRole('tab', { name: /商品主数据/ })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('home-screen')).toContainText('3 个类型 · 2,332 个对象');
});

test('the quick entries lead to the data sources, ontology modelling and analysis', async ({ page }) => {
  await mount(page);
  const entries = page.getByRole('navigation', { name: '快速入口' });
  await entries.getByRole('button', { name: '数据源' }).click();
  await expect(page).toHaveURL(/\/sources$/);
  await page.goBack();
  await page.getByRole('navigation', { name: '快速入口' }).getByRole('button', { name: '数据分析' }).click();
  await expect(page).toHaveURL(/\/analysis\/pipeline/);
});

test('with nothing imported the screen says where to start instead of drawing an empty graph', async ({ page }) => {
  await mount(page, { ...OVERVIEW, ontologies: [], runs: [], health: { average: null, runs: 0 }, totals: { ontologies: 0, types: 0, objects: 0, link_types: 0, imports: 0, runs: 0, lineage_jobs: 0 } });
  await expect(page.getByTestId('home-screen')).toContainText('还没有带对象的本体');
  await expect(page.getByTestId('recent-runs')).toContainText('还没有分析记录');
  await expect(page.getByTestId('kpi-对象')).toContainText('0');
});
