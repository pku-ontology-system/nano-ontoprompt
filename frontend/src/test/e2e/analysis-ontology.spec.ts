import { test, expect, type Page } from '@playwright/test';

// The analysis pages on a platform ontology, against a mocked API: the source switch, the object types, what each page asks for, and that
// objects that fail a check or share an entity can be opened in the object explorer.
const OID = 'ont-1';
const ROOT = `/api/v2/analysis/ontologies/${OID}`;
const props = (...names: string[]) => names.map(api_name => ({ api_name, data_type: 'string', nullable: true, is_array: false }));
const TYPES = [
  { api_name: 'Person', properties: props('unique_id', 'first_name', 'surname', 'dob', 'city', 'email', 'cluster'), object_count: 200, object_count_capped: true },
  { api_name: 'Gadget', properties: props('gadget_id'), object_count: 0, object_count_capped: false },
];
const READ = { object_type: 'Person', snapshot: true, truncated: false, row_count: 1000, counts: { ontology: 11, baseline: 0 }, source_table: 'person_master' };
const SUITE = { name: 'Person_ontology', expectations: [
  { expectation_type: 'expect_column_values_to_match_regex', kwargs: { column: 'email', regex: '^[^@]+@[^@]+$' }, meta: { source: '本体', notes: '属性定义里写明的格式' } }] };

const failing = (extra = {}) => ({ expectation_type: 'expect_column_values_to_match_regex', label: '符合正则', kwargs: { column: 'email', regex: '^[^@]+@[^@]+$' }, meta: { source: '本体', notes: '属性定义里写明的格式' }, success: false,
  result: { element_count: 1000, unexpected_count: 34, unexpected_percent: 4.3, partial_unexpected_counts: [{ value: 'x@y.m', count: 1 }], partial_unexpected_object_ids: ['ont:Person:117', 'ont:Person:124'] },
  exception_info: { raised_exception: false }, ...extra });
const passing = { expectation_type: 'expect_column_values_to_not_be_null', label: '不为空', kwargs: { column: 'unique_id' }, meta: { source: '本体' }, success: true, result: { element_count: 1000, unexpected_count: 0 }, exception_info: { raised_exception: false } };
const stats = (ok: number, bad: number) => ({ evaluated_expectations: ok + bad, successful_expectations: ok, unsuccessful_expectations: bad, errored_expectations: 0, success_percent: Math.round((ok / (ok + bad)) * 100) });
const healthRun = (objects: number) => ({ id: 'run-h', kind: 'health', dataset_id: `ontology:${OID}:Person`, title: 'x', summary: {}, params: {}, created_at: null, result: {
  success: false, statistics: stats(1, 1), results: [passing, failing()], meta: { suite_name: 'Person_ontology', dataset_id: 'd', row_count: objects, column_count: 7, run_time_ms: 8, object_count: objects },
  source: { kind: 'ontology', ontology_id: OID, object_type: 'Person', read: READ } } });

const row = (id: string, first: string) => ({ __object_id: id, first_name: first, surname: 'Atkinson' });
const RESOLUTION = { settings: { unique_id: '__object_id', threshold_match_probability: 0.5, ground_truth: 'cluster', blocking_rules: [], comparisons: [] },
  summary: { records: 1000, total_possible_pairs: 499500, candidate_pairs: 4307, pairs_above_threshold: 1422, entities_found: 311, records_in_clusters: 890, singletons: 110, largest_cluster: 7, run_time_ms: 2000 },
  evaluation: { ground_truth_column: 'cluster', true_entities: 251, true_pairs: 2031, predicted_pairs: 1800, true_positive_pairs: 1800, precision: 1, recall: 0.9, f1: 0.947, blocking_recall: 0.9, threshold: 0.5 },
  model: { lambda: 0.0008, prior_weight: -10.2, random_pairs_for_u: 499500, sessions: [], comparisons: [] }, histogram: { bin_start: [0, 2], bin_width: 2, all: [3, 4] },
  clusters: [{ cluster_id: 1, size: 2, records: [row('ont:Person:109', 'Oliver'), row('ont:Person:110', 'Oievr')], true_entities_inside: 1 }],
  examples: { borderline: [], false_positives: [], false_negatives: [], confident: [{ id_l: 'ont:Person:109', id_r: 'ont:Person:110', left: row('ont:Person:109', 'Oliver'), right: row('ont:Person:110', 'Oievr'), weight: 9, probability: 0.99, same_entity: true, contributions: [] }] },
  source: { kind: 'ontology', ontology_id: OID, object_type: 'Person', read: READ, notes: [] } };
const PRESET = { settings: { unique_id: '__object_id', threshold_match_probability: 0.5, ground_truth: 'cluster' }, notes: ['first_name：名字类，按 Jaro-Winkler 相似度分级比较', '粗筛规则 dob：约 873 个记录对'],
  skipped: [{ column: 'unique_id', reason: '对象标识' }], truth_candidates: ['cluster'], estimated_pairs: 6381, read: READ };

const DUPLICATES = { ontology_id: OID, types_compared: 6, minimum: 0.5, weights: { name: 0.35, properties: 0.35, table: 0.3 }, pairs: [{
  a: 'ScheduleItem', b: 'ScheduledItem', score: 0.912, verdict: 'likely', name_similarity: 1, property_overlap: { score: 0.75, jaccard: 0.59, containment: 0.91, shared: [{ a: 'schedule_id', b: 'item_id' }, { a: 'layer', b: 'layer' }], only_a: ['due'], only_b: ['primary_resource_id'] },
  shared_tables: ['schedule_item'], reasons: ['名字相近（ScheduleItem / ScheduledItem）', '来自同一张表 schedule_item'], objects: { a: 13, b: 0 }, origin: { a: 'type', b: 'type' },
  suggestion: '建议保留 ScheduleItem（已有 13 个对象）；ScheduledItem 还没有对象，核对后可以并入或删除' }] };

const ns = 'ontology:人员主数据';
const lnode = (id: string, kind: 'job' | 'dataset', name: string, layer: string, extra = {}) => ({ id: `${kind}:${ns}:${name}`, kind, namespace: ns, name, label: name, status: kind === 'job' ? 'success' : null, run_count: 1, layer, ref: null, ...extra });
const GRAPH = { nodes: [lnode('', 'dataset', 'person_master', 'source'), lnode('', 'job', 'autoontology.1', 'build'),
  lnode('', 'dataset', 'Person', 'object', { fields: [{ name: 'email', type: 'string', description: null }], ref: { kind: 'object_type', ontology_id: OID, api_name: 'Person' } }), lnode('', 'job', 'resolve.Person', 'analysis'), lnode('', 'dataset', 'entities.Person', 'analysis')],
  edges: [['dataset', 'person_master', 'job', 'autoontology.1'], ['job', 'autoontology.1', 'dataset', 'Person'], ['dataset', 'Person', 'job', 'resolve.Person'], ['job', 'resolve.Person', 'dataset', 'entities.Person']]
    .map(([sk, s, tk, t]) => ({ source: `${sk}:${ns}:${s}`, target: `${tk}:${ns}:${t}` })), namespaces: [ns], event_count: 6, layers: { source: '数据源表', build: 'Auto Ontology 生成', object: '对象类型', analysis: '数据分析' } };
const PERSON = `dataset:${ns}:Person`;
const IMPACT = { node: PERSON, downstream: [], downstream_jobs: 1, downstream_datasets: 1, upstream_count: 2, column: { node: PERSON, field: 'email', has_column_lineage: true, layers: ['analysis'], jobs: [
  { id: `job:${ns}:resolve.Person`, label: 'resolve.Person', layer: 'analysis', ref: null, distance: 1, description: '概率式记录匹配' }],
  columns: [{ dataset: `dataset:${ns}:entities.Person`, label: 'entities.Person', namespace: ns, layer: 'analysis', ref: null, field: 'entity_id', distance: 1, derived_from: { dataset: PERSON, field: 'email' }, transformation: '匹配概率高于阈值' }] } };
const PIPELINE = { id: 'run-p', kind: 'pipeline', dataset_id: `ontology:${OID}:Person`, title: 'x', summary: {}, params: {}, created_at: null, result: {
  dataset_id: `ontology:${OID}:Person`, title: 'Person（本体）', sha256: null, source: { kind: 'ontology', ontology_id: OID, object_type: 'Person', read: READ }, runs: { health: 'run-h', resolution: 'run-r' },
  steps: [{ key: 'ingest', title: '读取对象', node: PERSON, summary: { rows: 1000, columns: 7 } }, { key: 'validate', title: '数据健康检查', node: `job:${ns}:validate.Person`, summary: {} }, { key: 'resolve', title: '实体消歧', node: `job:${ns}:resolve.Person`, summary: {} }],
  health: healthRun(1000).result, resolution: RESOLUTION, lineage: { namespace: ns, focus: PERSON, events: 6, ontology_id: OID } } };

async function mount(page: Page, path: string) {
  const calls: string[] = [];
  const bodies: Record<string, unknown> = {};
  let generated = false;
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.addInitScript(() => {
    localStorage.setItem('token', 'analysis-test');
    localStorage.setItem('auth-store', JSON.stringify({ state: { token: 'analysis-test', user: { id: 'u', username: 'admin', role: 'admin' } }, version: 0 }));
  });
  await page.route(url => url.pathname.startsWith('/api/'), route => {
    const url = new URL(route.request().url());
    const p = url.pathname;
    const json = (body: unknown) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    calls.push(`${route.request().method()} ${p}${url.search}`);
    if (route.request().method() === 'POST') bodies[p] = route.request().postDataJSON();
    if (p === '/api/v1/ontologies') return json({ data: { items: [{ id: OID, name: '人员主数据', domain: '科技', version: '1', status: 'published', entity_count: 1, relation_count: 0 }], total: 1 } });
    if (p === '/api/v2/analysis/demos') return json({ datasets: [] });
    if (p === `${ROOT}/object-types`) return json({ ontology_id: OID, types: TYPES });
    if (p === `${ROOT}/health/suites/Person`) return json({ suite: SUITE, read: READ, expectation_types: [] });
    if (p === `${ROOT}/health/suites/Gadget`) return json({ suite: { name: 'Gadget_ontology', expectations: [] }, read: { ...READ, row_count: 0, counts: { ontology: 1, baseline: 0 } }, expectation_types: [] });
    if (p === `${ROOT}/health/run`) return json(healthRun(bodies[p] && (bodies[p] as { object_type: string }).object_type === 'Gadget' ? 0 : 1000));
    if (p === `${ROOT}/resolution/presets/Person`) return json(PRESET);
    if (p === `${ROOT}/resolution/run`) return json({ id: 'run-r', kind: 'resolution', dataset_id: 'x', title: 'x', summary: {}, params: {}, created_at: null, result: RESOLUTION });
    if (p === `${ROOT}/duplicate-types`) return json(DUPLICATES);
    if (p === `${ROOT}/lineage/generate`) { generated = true; return json({ namespace: ns, jobs: { build: 1, analysis: 1 }, types: 1, events: 6, replaced: 0 }); }
    if (p === `${ROOT}/lineage/graph`) return json(generated || path.includes('pipeline') ? GRAPH : { nodes: [], edges: [], namespaces: [], event_count: 0 });
    if (p === `${ROOT}/lineage/impact`) return json(IMPACT);
    if (p === `${ROOT}/pipeline/run`) { generated = true; return json(PIPELINE); }
    return json({});
  });
  await page.goto(path);
  return { calls, bodies };
}

test('the source switch: the demo data by default, the objects of an ontology on request, and back', async ({ page }) => {
  await mount(page, '/analysis/health');
  await expect(page.getByTestId('source-demo')).toHaveAttribute('aria-selected', 'true');
  await page.getByTestId('source-ontology').click();
  await expect(page).toHaveURL(/source=ontology/);
  await expect(page.getByTestId('type-Person')).toHaveAttribute('aria-checked', 'true'); // the first type that has objects
  await expect(page.getByTestId('type-Gadget')).toContainText('没有对象');
  await page.getByTestId('source-demo').click();
  await expect(page).not.toHaveURL(/ontology=|source=/);
});

test('data health on an ontology: the suite comes from the type and a failure opens the objects that broke it', async ({ page }) => {
  const { bodies } = await mount(page, `/analysis/health?source=ontology&ontology=${OID}&type=Person`);
  await expect(page.getByTestId('suite-origin')).toContainText('11 条由本体推出');
  await expect(page.getByTestId('suite-origin')).toContainText('person_master');
  await expect(page.getByLabel('期望套件')).toHaveValue(/Person_ontology/);
  await page.getByRole('button', { name: '运行检查' }).click();
  const report = page.getByTestId('health-report');
  await expect(report).toContainText('1,000 个对象');
  const failed = report.locator('[data-testid=expectation-result][data-success=false]');
  await expect(failed).toContainText('本体定义');
  await expect(failed).toContainText('34 行不符合');
  const broken = failed.getByTestId('broken-objects');
  await expect(broken.getByRole('link')).toHaveCount(2);
  await expect(broken.getByRole('link').first()).toHaveAttribute('href', `/ontologies/${OID}/object?objectType=Person&objectId=ont%3APerson%3A117`);
  await expect(broken).toContainText('共 34 个，这里只列前 2 个');
  expect(bodies[`${ROOT}/health/run`]).toMatchObject({ object_type: 'Person', baseline: false });
  await page.getByLabel('再加上按现有数据生成的基线').check();
  await page.getByRole('button', { name: '运行检查' }).click();
  await expect.poll(() => bodies[`${ROOT}/health/run`]).toMatchObject({ baseline: true });
});

test('data health on an ontology: a type without objects says so instead of passing quietly', async ({ page }) => {
  await mount(page, `/analysis/health?source=ontology&ontology=${OID}&type=Gadget`);
  await page.getByRole('button', { name: '运行检查' }).click();
  await expect(page.getByRole('note')).toContainText('这个类型下没有对象');
});

test('entity resolution on an ontology: duplicate types first, then the objects, each member opens in the explorer', async ({ page }) => {
  const { bodies } = await mount(page, `/analysis/resolution?source=ontology&ontology=${OID}&type=Person`);
  const duplicates = page.getByTestId('duplicate-types');
  await expect(duplicates.getByTestId('duplicate-pair')).toHaveCount(1);
  await expect(duplicates).toContainText('ScheduleItem ≈ ScheduledItem');
  await expect(duplicates).toContainText('很可能重复');
  await expect(duplicates).toContainText('建议保留 ScheduleItem（已有 13 个对象）');
  await page.getByTestId('resolution-draft').locator('summary').click();
  await expect(page.getByTestId('resolution-draft')).toContainText('first_name：名字类');
  await expect(page.getByTestId('resolution-draft')).toContainText('unique_id（对象标识）');
  await expect(page.getByLabel('真实分组列')).toHaveValue('cluster');
  await page.getByRole('button', { name: '运行实体消歧' }).click();
  await expect(page.getByTestId('resolution-result')).toContainText('1,000 个对象');
  await page.getByRole('button', { name: /2 个对象/ }).click();
  await expect(page.getByRole('link', { name: '109' }).first()).toHaveAttribute('href', `/ontologies/${OID}/object?objectType=Person&objectId=ont%3APerson%3A109`);
  expect(bodies[`${ROOT}/resolution/run`]).toMatchObject({ object_type: 'Person', settings: { threshold_match_probability: 0.5 } });
});

test('lineage of an ontology: generate it, read the layers, and ask what a column reaches', async ({ page }) => {
  const { bodies } = await mount(page, `/analysis/lineage?source=ontology&ontology=${OID}`);
  await expect(page.getByText('这个本体还没有血缘')).toBeVisible();
  await page.getByRole('button', { name: '从本体生成血缘' }).click();
  await expect(page.getByTestId('lineage-generated')).toContainText('已写入 6 条事件');
  expect(bodies[`${ROOT}/lineage/generate`]).toEqual({});
  await expect(page.getByTestId('lineage-graph')).toBeVisible();
  await expect(page.getByRole('list', { name: '图例' })).toContainText('Auto Ontology 生成');
  await page.getByTestId('lineage-node-Person').click();
  await expect(page.getByTestId('lineage-detail').getByRole('link', { name: '检查这个类型的数据健康' })).toHaveAttribute('href', `/analysis/health?source=ontology&ontology=${OID}&type=Person`);
  await page.getByTestId('field-impact-email').click();
  const impact = page.getByTestId('column-impact');
  await expect(impact).toContainText('改 Person.email 会影响 1 个下游列、1 个作业');
  await expect(impact).toContainText('entities.Person.entity_id');
  await expect(impact).toContainText('第 1 跳 · 由 email 得来（匹配概率高于阈值）');
  await page.getByTestId('lineage-node-resolve.Person').click();
  await expect(page.getByTestId('column-impact')).toHaveCount(0); // another node: the answer about the first one is not shown against it
});

test('pipeline on an ontology: one type through the three, linked to its objects and to the lineage it left', async ({ page }) => {
  const { bodies } = await mount(page, `/analysis/pipeline?source=ontology&ontology=${OID}&type=Person`);
  await expect(page.getByTestId('pipeline-lineage')).toBeVisible(); // the ontology already has a lineage
  await page.getByRole('button', { name: '运行流水线' }).click();
  expect(bodies[`${ROOT}/pipeline/run`]).toEqual({ object_type: 'Person' });
  await expect(page.getByTestId('pipeline-step-1')).toContainText('1,000 个对象 × 7 个属性');
  await expect(page.getByTestId('pipeline-step-2')).toContainText('1 / 2 条期望通过');
  await expect(page.getByTestId('pipeline-step-3')).toContainText('311');
  await expect(page.getByTestId('pipeline-step-2').getByRole('link', { name: /打开不合格的对象/ })).toHaveAttribute('href', `/analysis/health?source=ontology&ontology=${OID}&type=Person`);
  await expect(page.getByTestId('pipeline-story')).toContainText('按本体推出的 2 条期望检查');
  await expect(page.getByTestId('lineage-detail')).toContainText('Person');
});
