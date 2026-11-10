import { test, expect, type Page } from '@playwright/test';
import { BUNDLES, jobAt } from './importFixtures';

// The data sources page against a mocked API: the prepared data sets, watching an import step by step, stepping an event stream forward,
// and importing your own files after seeing what was recognised.
const UPLOAD = {
  upload_id: 'up-1', title: 'orders',
  preview: {
    title: 'orders', kind: 'relational', assets: { image: 2 },
    tables: [
      { name: 'orders', type_name: 'Order', label: 'orders', kind: 'entity', rows: 6, primary_key: ['order_id'], columns: [{ name: 'order_id', type: 'integer', label: '' }, { name: 'placed_at', type: 'timestamp', label: '' }, { name: 'customer_id', type: 'string', label: '' }] },
      { name: 'media', type_name: 'MediaAsset', label: 'media', kind: 'asset', rows: 2, primary_key: ['asset_id'], columns: [{ name: 'asset_id', type: 'string', label: '' }, { name: 'media_kind', type: 'string', label: '' }] },
    ],
    links: [{ name: 'orders_customer_id', from: 'orders.customer_id', to: 'customers.customer_id', basis: 'semantic_fk', coverage: 1 }],
    notes: ['orders：推断主键 order_id'],
  },
};

async function mount(page: Page, path: string, { jobs = 3 } = {}) {
  const posts: { path: string; body: unknown }[] = [];
  let polled = 0;
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.addInitScript(() => {
    localStorage.setItem('token', 'sources-test');
    localStorage.setItem('auth-store', JSON.stringify({ state: { token: 'sources-test', user: { id: 'u', username: 'admin', role: 'admin' } }, version: 0 }));
  });
  let cursor = 400;
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    const url = new URL(route.request().url());
    const p = url.pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (route.request().method() === 'POST') posts.push({ path: p, body: p.endsWith('/files') ? '(files)' : route.request().postDataJSON() });
    if (p === '/api/v1/ontologies') return json({ data: { items: [], total: 0 } });
    if (p === '/api/v2/analysis/overview') return json({ ontologies: [], runs: [], runs_by_kind: {}, health: { average: null, runs: 0 }, lineage_jobs: 0, totals: { ontologies: 0, types: 0, objects: 0, link_types: 0, imports: 0, runs: 0, lineage_jobs: 0 } });
    if (p === '/api/v2/ontology-imports/bundles') return json({ bundles: BUNDLES });
    if (p.endsWith(':import') && p.includes('/bundles/')) return json(jobAt(0), 202);
    if (p.endsWith(':import') && p.includes('/files/')) return json({ ...jobAt(0), title: 'orders' }, 202);
    if (p === '/api/v2/ontology-imports/files') return json(UPLOAD);
    if (p === '/api/v2/ontology-imports/jobs/job-1') { polled += 1; return json(polled >= jobs ? jobAt(8, 'done') : jobAt(Math.min(polled * 3, 7))); }
    if (p === '/api/v2/ontology-imports/ont-taxi/replay') return json({ replay: { table: 'trips', order_by: 'pickup_time', initial: 400, batch: 400, total: 10000, cursor } });
    if (p === '/api/v2/ontology-imports/ont-taxi/advance') { cursor += 400; return json({ cursor, total: 10000, installed: 400, done: false }); }
    return json({});
  });
  await page.goto(path);
  return posts;
}

test('the prepared data sets: five different kinds, none of them about one industry, and what is already imported is marked', async ({ page }) => {
  await mount(page, '/sources');
  const cards = page.getByTestId('bundles').getByRole('listitem').filter({ has: page.getByRole('heading', { level: 3 }) });
  await expect(cards).toHaveCount(5);
  for (const kind of ['关系型多表', '动态事件流', '时序数据', '多来源合并', '多模态']) await expect(page.getByTestId('bundles')).toContainText(kind);
  await expect(page.getByText('Splink')).toHaveCount(0);
  await expect(page.getByText('适合当前本体')).toHaveCount(0);
  const taxi = page.getByTestId('bundle-demo-nyc-taxi');
  await expect(taxi).toHaveAttribute('data-imported', 'true');
  await expect(taxi.getByRole('link', { name: '本体总览' })).toHaveAttribute('href', '/ontologies/ont-taxi?tab=overview');
  await expect(taxi.getByRole('link', { name: '数据模型' })).toHaveAttribute('href', '/ontologies/ont-taxi?tab=data_model');
  await expect(taxi.getByRole('link', { name: '数据血缘' })).toHaveAttribute('href', '/analysis/lineage?source=ontology&ontology=ont-taxi');
  await expect(page.getByTestId('bundle-demo-tpch')).toHaveAttribute('data-imported', 'false');
  await page.screenshot({ path: 'test-results/sources-catalogue.png', fullPage: true });
});

test('an import is watched step by step and ends with where to look at the result', async ({ page }) => {
  const posts = await mount(page, '/sources');
  await page.getByTestId('import-demo-tpch').click();
  const progress = page.getByTestId('import-progress');
  await expect(progress).toBeVisible();
  expect(posts.find(p => p.path.includes('demo-tpch:import'))?.body).toEqual({ replace: false });
  await expect(progress).toHaveAttribute('data-status', 'done');
  await expect(progress.getByTestId('import-step-s7')).toContainText('生成数据血缘');
  await expect(progress.getByTestId('import-step-s7')).toHaveAttribute('data-status', 'done');
  for (let i = 0; i < 8; i += 1) await expect(progress.getByTestId(`import-step-s${i}`)).toHaveAttribute('data-status', 'done');
  await expect(progress).toContainText('7,498');
  await page.screenshot({ path: 'test-results/sources-progress.png', fullPage: true });
});

test('a failed import says which step and why, and offers to try again', async ({ page }) => {
  await mount(page, '/sources', { jobs: 99 });
  await page.route('**/api/v2/ontology-imports/jobs/job-1', route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(jobAt(4, 'failed')) }));
  await page.getByTestId('import-demo-tpch').click();
  const progress = page.getByTestId('import-progress');
  await expect(progress).toHaveAttribute('data-status', 'failed');
  await expect(progress).toContainText('图数据库不可用');
});

test('an event stream that was imported is loaded further one step at a time', async ({ page }) => {
  const posts = await mount(page, '/sources');
  const panel = page.getByTestId('bundle-demo-nyc-taxi').getByTestId('replay-panel');
  await expect(panel.getByTestId('replay-cursor')).toContainText('已载入 400 / 10,000');
  await panel.getByRole('button', { name: /推进 400 条/ }).click();
  await expect(panel.getByTestId('replay-cursor')).toContainText('已载入 800 / 10,000');
  expect(posts.some(p => p.path === '/api/v2/ontology-imports/ont-taxi/advance')).toBe(true);
});

test('your own files: see what was recognised, name the ontology, then import', async ({ page }) => {
  const posts = await mount(page, '/sources');
  await page.getByTestId('file-input').setInputFiles([{ name: 'orders.csv', mimeType: 'text/csv', buffer: Buffer.from('order_id,placed_at,customer_id\n1,2024-01-01,c1\n') }, { name: 'leaf.png', mimeType: 'image/png', buffer: Buffer.from('png') }]);
  const preview = page.getByTestId('upload-preview');
  await expect(preview).toContainText('对象类型 Order');
  await expect(preview).toContainText('文件资产');
  await expect(preview).toContainText('图片 2 个');
  await expect(preview).toContainText('orders.customer_id');
  await expect(page.getByLabel('本体名称')).toHaveValue('orders');
  await page.getByLabel('本体名称').fill('我的订单与照片');
  await page.getByLabel('把一张表当事件流，按顺序逐步载入').check();
  await expect(page.getByLabel('排序列')).toHaveValue('placed_at');
  await page.getByTestId('start-upload-import').click();
  await expect(page.getByTestId('import-progress')).toHaveAttribute('data-status', 'done');
  const sent = posts.find(p => p.path === '/api/v2/ontology-imports/files/up-1:import');
  expect(sent?.body).toMatchObject({ ontology_name: '我的订单与照片', replace: false, replay: { table: 'orders', order_by: 'placed_at' } });
});

test('the dedicated importers stay available but out of the way', async ({ page }) => {
  await mount(page, '/sources');
  await page.getByText('专用格式的导入器').click();
  await expect(page.getByRole('link', { name: '时序数据构建' })).toHaveAttribute('href', '/data/temporal');
});
