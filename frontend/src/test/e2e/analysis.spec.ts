import { test, expect, type Page } from '@playwright/test';

// The four analysis pages against a mocked API: what they ask for, and that what comes back reaches the screen.
const dataset = (id: string, tool: string, extra = {}) => ({ id, tool, title: id, description: `${id} 的演示数据`, default: true, file: `${id}.csv`, format: 'csv', available: true, bytes: 55983, rows: 1000,
  columns: ['unique_id', 'first_name'], ground_truth: null, has_health_suite: true, has_resolution_preset: tool === 'splink', source: { project: 'X', url: 'https://example.org', license: 'MIT' }, ...extra });
const DEMOS = [dataset('gx-taxi-2019-01', 'great_expectations'), dataset('splink-fake-1000', 'splink', { ground_truth: 'cluster' }), dataset('marquez-food-delivery', 'marquez', { format: 'openlineage-events', rows: undefined })];

const expectationResult = (success: boolean, column: string, extra = {}) => ({ expectation_type: 'expect_column_values_to_be_between', label: '取值在范围内', kwargs: { column, min_value: 0, max_value: 500 }, meta: { notes: '车费不应为负' },
  success, result: success ? { element_count: 10000, unexpected_count: 0, partial_unexpected_counts: [] } : { element_count: 10000, unexpected_count: 9, unexpected_percent: 0.09, partial_unexpected_counts: [{ value: -52, count: 2 }, { value: 3004, count: 1 }] },
  exception_info: { raised_exception: false }, ...extra });
const HEALTH_RUN = { id: 'run-h', kind: 'health', dataset_id: 'gx-taxi-2019-01', title: 'x', summary: {}, params: {}, created_at: null, result: {
  success: false, statistics: { evaluated_expectations: 2, successful_expectations: 1, unsuccessful_expectations: 1, errored_expectations: 0, success_percent: 50 },
  results: [expectationResult(true, 'tip_amount'), expectationResult(false, 'fare_amount')], meta: { suite_name: 'taxi', dataset_id: 'gx-taxi-2019-01', row_count: 10000, column_count: 18, run_time_ms: 40 } } };

const pair = (same: boolean | null) => ({ id_l: 1, id_r: 2, left: { first_name: 'Robert' }, right: { first_name: 'Rob' }, weight: 5.2, probability: 0.97, same_entity: same,
  contributions: [{ column: '先验', label: 'λ', weight: -10 }, { column: 'first_name', label: '相似度 ≥ 0.7', weight: 15.2 }] });
const RESOLUTION_RUN = { id: 'run-r', kind: 'resolution', dataset_id: 'splink-fake-1000', title: 'x', summary: {}, params: {}, created_at: null, result: {
  settings: { unique_id: 'unique_id', threshold_match_probability: 0.5, ground_truth: 'cluster', blocking_rules: [], comparisons: [] },
  summary: { records: 1000, total_possible_pairs: 499500, candidate_pairs: 4307, pairs_above_threshold: 800, entities_found: 311, records_in_clusters: 890, singletons: 110, largest_cluster: 7, run_time_ms: 1500 },
  evaluation: { ground_truth_column: 'cluster', true_entities: 251, true_pairs: 2031, predicted_pairs: 1800, true_positive_pairs: 1800, precision: 0.99, recall: 0.9, f1: 0.943, blocking_recall: 0.84, threshold: 0.5 },
  model: { lambda: 0.0008, prior_weight: -10.2, random_pairs_for_u: 499500, sessions: [{ rule: ['dob'], pairs: 873, iterations: 12, lambda: 0.1 }],
    comparisons: [{ column: 'first_name', type: 'name', term_frequency_adjusted: true, levels: [{ level: -1, label: '缺失', m: null, u: null, weight: null }, { level: 0, label: '其他', m: 0.19, u: 0.96, weight: -2.3 }, { level: 1, label: '完全一致', m: 0.5, u: 0.006, weight: 6.4 }] }] },
  histogram: { bin_start: [-4, -2, 0, 2], bin_width: 2, match: [0, 1, 5, 40], non_match: [900, 300, 20, 1] },
  clusters: [{ cluster_id: 1, size: 3, records: [{ first_name: 'Robert' }, { first_name: 'Rob' }, { first_name: 'Robbie' }], true_entities_inside: 1 }],
  examples: { borderline: [pair(true)], false_positives: [], false_negatives: [pair(true)], confident: [pair(true)] } } };

const node = (id: string, kind: 'job' | 'dataset', extra = {}) => ({ id, kind, namespace: 'workbench', name: id.split(':')[2], label: id.split(':')[2], status: kind === 'job' ? 'success' : null, run_count: 1, ...extra });
const GRAPH = { nodes: [node('job:workbench:ingest', 'job'), node('dataset:workbench:raw', 'dataset', { fields: [{ name: 'unique_id', type: 'BIGINT', description: null }], quality: { passed: 1, failed: 1, assertions: [{ assertion: 'expect_column_values_to_not_be_null', success: false, column: 'city' }, { assertion: 'expect_column_values_to_be_unique', success: true, column: 'unique_id' }] } }),
  node('job:workbench:resolve', 'job')], edges: [{ source: 'job:workbench:ingest', target: 'dataset:workbench:raw' }, { source: 'dataset:workbench:raw', target: 'job:workbench:resolve' }], namespaces: ['workbench'], event_count: 6 };
const PIPELINE_RUN = { id: 'run-p', kind: 'pipeline', dataset_id: 'splink-fake-1000', title: 'x', summary: {}, params: {}, created_at: null, result: {
  dataset_id: 'splink-fake-1000', title: 'x', sha256: 'a'.repeat(64),
  steps: [{ key: 'ingest', title: '读入数据', node: 'job:workbench:ingest', summary: { rows: 1000, columns: 7 } }, { key: 'validate', title: '健康检查', node: 'job:workbench:validate', summary: {} }, { key: 'resolve', title: '实体消歧', node: 'job:workbench:resolve', summary: {} }],
  health: { success: false, statistics: { evaluated_expectations: 10, successful_expectations: 6, unsuccessful_expectations: 4, errored_expectations: 0, success_percent: 60 }, meta: { suite_name: 's', dataset_id: 'd', row_count: 1000, column_count: 7, run_time_ms: 5 },
    results: [expectationResult(false, 'city', { expectation_type: 'expect_column_values_to_not_be_null', label: '不为空', result: { unexpected_count: 187 } }),
      expectationResult(false, 'x', { expectation_type: 'expect_compound_columns_to_be_unique', label: '组合列唯一', kwargs: { column_list: ['a', 'b'] }, result: { unexpected_count: 158 } })] },
  resolution: RESOLUTION_RUN.result, lineage: { namespace: 'workbench', focus: 'dataset:workbench:raw', events: 6, datasets: {} } } };

async function mount(page: Page, path: string) {
  const calls: string[] = [];
  let ran = false;
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.addInitScript(() => {
    localStorage.setItem('token', 'analysis-test');
    localStorage.setItem('auth-store', JSON.stringify({ state: { token: 'analysis-test', user: { id: 'u', username: 'admin', role: 'admin' } }, version: 0 }));
  });
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    const url = new URL(route.request().url());
    const p = url.pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    calls.push(`${route.request().method()} ${p}${url.search}`);
    if (p === '/api/v1/ontologies') return json({ data: { items: [], total: 0 } });
    if (p === '/api/v2/analysis/demos') return json({ datasets: DEMOS });
    if (p.endsWith('/preview')) return json({ columns: ['unique_id', 'first_name'], rows: [[1, 'Robert']], total: 1000 });
    if (p.startsWith('/api/v2/analysis/health/suites/')) return json({ suite: { name: 'taxi', expectations: [{ expectation_type: 'expect_column_values_to_be_between', kwargs: { column: 'fare_amount', min_value: 0, max_value: 500 } }] }, expectation_types: [] });
    if (p === '/api/v2/analysis/health/run') return json(HEALTH_RUN);
    if (p.startsWith('/api/v2/analysis/resolution/presets/')) return json({ settings: { unique_id: 'unique_id', threshold_match_probability: 0.5 } });
    if (p === '/api/v2/analysis/resolution/run') return json(RESOLUTION_RUN);
    if (p === '/api/v2/analysis/lineage/seed') { ran = true; return json({ events: 26, namespace: 'workbench' }); }
    if (p === '/api/v2/analysis/lineage/graph') return json(ran || path.includes('lineage') && calls.some(c => c.includes('seed')) ? GRAPH : { nodes: [], edges: [], namespaces: [], event_count: 0 });
    if (p === '/api/v2/analysis/lineage/impact') return json({ node: url.searchParams.get('node'), downstream: [{ id: 'job:workbench:resolve', kind: 'job', label: 'resolve', namespace: 'workbench', distance: 1, status: 'success', quality_failed: 0 }], downstream_jobs: 1, downstream_datasets: 0, upstream_count: 1 });
    if (p === '/api/v2/analysis/pipeline/run') { ran = true; return json(PIPELINE_RUN); }
    return json({});
  });
  await page.goto(path);
  return calls;
}

test('data health: run the default suite, see which expectation fails and which values broke it', async ({ page }) => {
  const calls = await mount(page, '/analysis/health');
  await expect(page.getByTestId('dataset-gx-taxi-2019-01')).toHaveAttribute('aria-checked', 'true');
  await expect(page.getByLabel('期望套件')).toHaveValue(/fare_amount/);
  await page.getByRole('button', { name: '运行检查' }).click();
  const report = page.getByTestId('health-report');
  await expect(report.getByText('50%')).toBeVisible();
  await expect(report.getByTestId('expectation-result')).toHaveCount(2);
  const failed = report.locator('[data-testid=expectation-result][data-success=false]');
  await expect(failed).toContainText('9 行不符合');
  await expect(failed).toContainText('-52 ×2');
  await expect(failed).toContainText('车费不应为负');
  await report.getByLabel('只看未通过的').check();
  await expect(report.getByTestId('expectation-result')).toHaveCount(1);
  expect(calls.some(c => c === 'POST /api/v2/analysis/health/run')).toBe(true);
});

test('data health: a suite that is not JSON is refused before anything is sent', async ({ page }) => {
  const calls = await mount(page, '/analysis/health');
  await page.getByLabel('期望套件').fill('{ not json');
  await page.getByRole('button', { name: '运行检查' }).click();
  await expect(page.getByText('套件不是合法的 JSON')).toBeVisible();
  expect(calls.some(c => c === 'POST /api/v2/analysis/health/run')).toBe(false);
});

test('entity resolution: scores, learned weights and the pair-by-pair explanation', async ({ page }) => {
  await mount(page, '/analysis/resolution');
  await expect(page.getByLabel('匹配概率阈值')).toHaveValue('0.5');
  await page.getByRole('button', { name: '运行实体消歧' }).click();
  const result = page.getByTestId('resolution-result');
  await expect(result.getByText('99.0%')).toBeVisible();
  await expect(result.getByText('90.0%').first()).toBeVisible();
  await expect(result.getByText('311')).toBeVisible();
  await expect(page.getByText('词频校正').first()).toBeVisible();
  await expect(page.getByTestId('pair-example')).toHaveCount(1);
  await page.getByRole('tab', { name: /错并/ }).click();
  await expect(page.getByText('这一类一个也没有')).toBeVisible();
  await page.getByRole('tab', { name: /漏并/ }).click();
  await expect(page.getByTestId('pair-example')).toContainText('确实是同一实体');
  await page.getByRole('button', { name: /3 条记录/ }).click();
  await expect(page.getByText('Robbie')).toBeVisible();
});

test('lineage: load the demo, pick a dataset, read its quality and ask what depends on it', async ({ page }) => {
  await mount(page, '/analysis/lineage');
  await expect(page.getByText('还没有血缘事件')).toBeVisible();
  await page.getByRole('button', { name: '载入 Marquez 外卖演示' }).click();
  await expect(page.getByTestId('lineage-graph')).toBeVisible();
  await page.getByTestId('lineage-node-raw').click();
  const detail = page.getByTestId('lineage-detail');
  await expect(detail).toContainText('unique_id');
  await expect(detail).toContainText('1 通过，1 未通过');
  await expect(detail.getByText('column_values_to_not_be_null · city')).toBeVisible();
  await detail.getByRole('button', { name: /影响分析/ }).click();
  await expect(page.getByTestId('impact-result')).toContainText('下游共 1 个作业');
  await page.getByTestId('lineage-node-resolve').click();
  await expect(page.getByTestId('impact-result')).toHaveCount(0); // another node, the earlier answer is not shown against it
});

test('pipeline: one run shows the three steps, how they connect and the lineage they left', async ({ page }) => {
  await mount(page, '/analysis/pipeline');
  await expect(page.getByTestId('pipeline-lineage')).toHaveCount(0);
  await page.getByRole('button', { name: '运行流水线' }).click();
  await expect(page.getByTestId('pipeline-step-2')).toContainText('6 / 10');
  await expect(page.getByTestId('pipeline-step-3')).toContainText('311');
  const story = page.getByTestId('pipeline-story');
  await expect(story).toContainText('158 条记录和别的记录');
  await expect(story).toContainText('归并成 311 个实体（真实 251 个）');
  await expect(page.getByTestId('pipeline-lineage').getByTestId('lineage-graph')).toBeVisible();
  await expect(page.getByTestId('lineage-detail')).toContainText('raw');
});
