/**
 * 六领域全量测试：每个业务域分别跑 Pipeline Mapping + Dataset Construction 两条路径
 * 每次运行结果截图保存到 test-results/all-domains-{timestamp}/
 */

/// <reference types="node" />

import { test, expect, type APIRequestContext, type Page } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { buildPrivateStructuredDataset } from './datasetConstructionFlow'

const BASE = 'http://localhost:5173'
const API  = 'http://localhost:8000'
const __filename = fileURLToPath(import.meta.url)
const __dirname  = path.dirname(__filename)
const TEST_DATA  = path.resolve(__dirname, '../../../../test_data')

interface ApiData {
  id?: string
  status?: string
  task_id?: string
  total?: number
  progress?: { pct?: number }
}

interface BuildStats {
  curated_dataset_ids?: string[]
  meta?: { outputs?: Array<{ curated_dataset_id: string; source_file?: string }> }
}

interface ApiBody {
  id?: string
  status?: string
  task_id?: string
  progress?: { pct?: number }
  data?: ApiData
  stats?: BuildStats
  total_entities?: number
  total_relations?: number
  total_logic?: number
  total_actions?: number
}

type DomainResult = Record<string, string | number | undefined>

function messageFrom(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function requiredString(value: string | undefined, label: string): string {
  if (!value) throw new Error(`${label} missing from API response`)
  return value
}

// 本体创建时 domain 字段需在白名单中（营销→其他）
const ONTOLOGY_DOMAIN: Record<string, string> = {
  '供应链': '供应链',
  '医疗':   '医疗',
  '教育':   '教育',
  '法律':   '法律',
  '营销':   '其他',
  '财务':   '财务',
}

// 文件名 → Entity Class 名（通用规则：驼峰化去后缀）
function toEntityClass(filename: string): string {
  const base = filename.replace(/\.[^.]+$/, '')
  return base
    .split(/[_\-\s]+/)
    .map(w => w.charAt(0).toUpperCase() + w.slice(1))
    .join('')
}

// ── 工具函数 ──────────────────────────────────────────────────────────

async function login(page: Page): Promise<string> {
  const username = process.env.PROJECT4_E2E_USERNAME
  const password = process.env.PROJECT4_E2E_PASSWORD
  if (!username || !password) throw new Error('Set PROJECT4_E2E_USERNAME and PROJECT4_E2E_PASSWORD to run this E2E flow')
  await page.goto(`${BASE}/login`)
  await page.fill('input[placeholder="用户名"]', username)
  await page.fill('input[placeholder="密码"]', password)
  await page.click('button[type="submit"]')
  await page.waitForURL(`${BASE}/overview`, { timeout: 10000 })
  const token = await page.evaluate(() => localStorage.getItem('token') || '')
  expect(token, 'JWT token must be set after login').toBeTruthy()
  return token
}

async function api(
  request: APIRequestContext,
  method: 'GET' | 'POST' | 'PUT' | 'DELETE',
  url: string,
  token: string,
  data?: unknown,
): Promise<ApiBody> {
  const res = await request.fetch(`${API}${url}`, {
    method,
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    data: data ? JSON.stringify(data) : undefined,
  })
  const body = await res.json() as ApiBody
  if (!res.ok()) throw new Error(`${method} ${url} → ${res.status()}: ${JSON.stringify(body).slice(0, 300)}`)
  return body
}

async function shot(page: Page, outDir: string, name: string) {
  await page.screenshot({ path: path.join(outDir, `${name}.jpg`), type: 'jpeg', quality: 75 })
}

// ── Pipeline Mapping 完整流程 ─────────────────────────────────────────

async function runPipelineMapping(
  page: Page,
  request: APIRequestContext,
  token: string,
  domainCn: string,
  ts: number,
  outDir: string,
) {
  const domainDir = path.join(TEST_DATA, domainCn)
  const files = fs.readdirSync(domainDir).filter(f => fs.statSync(path.join(domainDir, f)).isFile()).sort()
  console.log(`  [${domainCn}] pipeline: ${files.length} 个文件`)

  // 1. 上传所有文件到 v2 datasets
  const uploaded: Array<{ name: string; dataset_id: string }> = []
  for (const filename of files) {
    const filePath = path.join(domainDir, filename)
    const res = await request.post(`${API}/api/v2/datasets/upload`, {
      headers: { Authorization: `Bearer ${token}` },
      multipart: { file: { name: filename, mimeType: 'application/octet-stream', buffer: fs.readFileSync(filePath) } },
    })
    const body = await res.json()
    expect(res.ok(), `upload ${filename}: ${JSON.stringify(body)}`).toBeTruthy()
    uploaded.push({ name: filename, dataset_id: body.data.id })
    console.log(`    ✓ 上传 ${filename} → ${body.data.id.slice(0, 8)}`)
  }

  // 2. 创建 Pipeline（含单个连接器，引用所有已上传文件）
  const pipelineName = `E2E_${domainCn}_Pipeline_${ts}`
  const plBody = await api(request, 'POST', '/api/v2/pipelines', token, {
    name: pipelineName,
    domain: ONTOLOGY_DOMAIN[domainCn],
    description: `E2E 自动化测试 - ${domainCn} Pipeline Mapping`,
    route: 'A',
    definition: {
      schema_version: '2.0',
      nodes: [
        {
          id: 'connector_all', type: 'connector', label: `${domainCn}数据源`,
          position: { x: 80, y: 180 },
          config: { source_type: 'file', files: uploaded },
        },
        {
          id: 'storage_all', type: 'storage', label: '分类存储',
          position: { x: 330, y: 180 },
          config: { storage_mode: 'auto' },
        },
        {
          id: 'transform_all', type: 'transform', label: '数据转换',
          position: { x: 580, y: 180 },
          config: { path: 'auto', steps: [] },
        },
        {
          id: 'output_all', type: 'output', label: '结构化输出',
          position: { x: 830, y: 180 },
          config: { dataset_type: 'curated_dataset', primary_key: [] },
        },
      ],
      edges: [
        { id: 'e1', source: 'connector_all', target: 'storage_all' },
        { id: 'e2', source: 'storage_all', target: 'transform_all' },
        { id: 'e3', source: 'transform_all', target: 'output_all' },
      ],
    },
  })
  const pipelineId = requiredString(plBody.id ?? plBody.data?.id, 'pipeline id')
  expect(pipelineId).toBeTruthy()
  console.log(`  Pipeline 创建: ${pipelineId.slice(0, 8)}`)

  // 3. 前端查看 Pipeline Builder
  await page.goto(`${BASE}/data/pipelines/${pipelineId}`)
  await page.waitForTimeout(1500)
  await shot(page, outDir, `${domainCn}_01_pipeline_builder`)

  // 4. 同步运行 Pipeline
  console.log(`  运行 Pipeline...`)
  const runBody = await api(request, 'POST', `/api/v2/pipelines/${pipelineId}/run-sync`, token)
  expect(runBody.status, `Pipeline run failed: ${JSON.stringify(runBody)}`).toBe('success')
  const curatedIds: string[] = runBody.stats?.curated_dataset_ids ?? []
  console.log(`  ✓ 运行完成，产出 ${curatedIds.length} 个 curated dataset`)

  // 5. Publish pipeline
  await api(request, 'POST', `/api/v2/pipelines/${pipelineId}/publish`, token)
  await page.goto(`${BASE}/data/pipelines/${pipelineId}`)
  await page.waitForTimeout(1500)
  await shot(page, outDir, `${domainCn}_02_pipeline_published`)

  // 6. 批准所有 curated datasets
  for (const id of curatedIds) {
    await api(request, 'POST', `/api/v2/curated/${id}/review?action=approve`, token)
  }
  console.log(`  ✓ 批准 ${curatedIds.length} 个 curated dataset`)

  // 7. 前端查看结构数据页
  await page.goto(`${BASE}/data/structured`)
  await page.waitForTimeout(1500)
  await shot(page, outDir, `${domainCn}_03_structured_data`)

  // 8. 创建 Pipeline Mapping 本体
  const ontoName = `E2E_${domainCn}_PipelineMapping_${ts}`
  const ontoBody = await api(request, 'POST', '/api/v1/ontologies', token, {
    name: ontoName,
    domain: ONTOLOGY_DOMAIN[domainCn],
    description: `E2E Pipeline Mapping — ${domainCn}`,
    build_mode: 'pipeline_mapping',
  })
  const ontologyId = requiredString(ontoBody.data?.id ?? ontoBody.id, 'ontology id')
  expect(ontologyId).toBeTruthy()
  console.log(`  本体创建: ${ontologyId.slice(0, 8)}`)

  // 9. 为每个 curated dataset 创建 mapping
  const outputs: Array<{ curated_dataset_id: string; source_file?: string }> =
    runBody.stats?.meta?.outputs ?? curatedIds.map(id => ({ curated_dataset_id: id }))

  for (const output of outputs) {
    const sourceFile = output.source_file
    const entityClass = sourceFile ? toEntityClass(sourceFile) : `Entity_${output.curated_dataset_id.slice(0, 6)}`
    await api(request, 'POST', `/api/v2/ontologies/${ontologyId}/mappings`, token, {
      curated_dataset_id: output.curated_dataset_id,
      entity_class: entityClass,
      field_mapping: { '__primary_key__': '__row_hash__' },
      confidence: 1.0,
    })
  }
  console.log(`  ✓ 创建 ${outputs.length} 个 mapping`)

  // 10. Build all
  console.log(`  构建本体中...`)
  const buildBody = await api(request, 'POST', `/api/v2/ontologies/${ontologyId}/mappings/build-all`, token)
  console.log(`  ✓ 构建完成: entities=${buildBody.total_entities} relations=${buildBody.total_relations} logic=${buildBody.total_logic} actions=${buildBody.total_actions}`)

  // 11. 前端查看本体详情
  await page.goto(`${BASE}/ontologies/${ontologyId}`)
  await page.waitForTimeout(1500)
  await shot(page, outDir, `${domainCn}_04_ontology_info`)

  const entityTab = page.locator('button').filter({ hasText: '实体' })
  if (await entityTab.isVisible()) {
    await entityTab.click()
    await page.waitForTimeout(1500)
    await shot(page, outDir, `${domainCn}_05_entities`)
  }

  await page.goto(`${BASE}/ontologies/${ontologyId}?tab=graph`)
  await page.waitForTimeout(1500)
  await page.waitForTimeout(2000)
  await shot(page, outDir, `${domainCn}_06_graph`)

  return { pipelineId, ontologyId, curatedIds, buildBody }
}

// ── Dataset Construction 完整流程 ─────────────────────────────────────

async function runDatasetConstruction(
  page: Page,
  request: APIRequestContext,
  token: string,
  domainCn: string,
  ts: number,
  outDir: string,
) {
  const domainDir = path.join(TEST_DATA, domainCn)
  const structuredFile = fs.readdirSync(domainDir)
    .filter(file => /\.(csv|xlsx|xls)$/i.test(file) && fs.statSync(path.join(domainDir, file)).isFile())
    .sort()[0]
  if (!structuredFile) throw new Error(`${domainCn} has no CSV or Excel Dataset fixture`)
  console.log(`  [${domainCn}] Dataset Construction: ${structuredFile} (private, deterministic mapping)`)

  const username = process.env.PROJECT4_E2E_USERNAME || ''
  const role = process.env.PROJECT4_E2E_ROLE || 'editor'
  await page.goto(`${BASE}/login`)
  await page.evaluate((session: { tok: string; user: string; roleName: string }) => {
    localStorage.setItem('token', session.tok)
    localStorage.setItem('auth-store', JSON.stringify({ state: { token: session.tok, user: { username: session.user, role: session.roleName } }, version: 0 }))
  }, { tok: token, user: username, roleName: role })

  const built = await buildPrivateStructuredDataset(request, {
    apiBase: API,
    token,
    filePath: path.join(domainDir, structuredFile),
    domain: ONTOLOGY_DOMAIN[domainCn],
    ontologyName: `E2E_${domainCn}_DatasetConstruction_${ts}`,
  })
  console.log(`  ConstructionRun: ${built.runId.slice(0, 8)} · ontology: ${built.ontologyId.slice(0, 8)}`)

  await page.goto(`${BASE}/data/regular/build?run=${encodeURIComponent(built.runId)}`)
  await page.waitForTimeout(1500)
  await shot(page, outDir, `${domainCn}_construction_01_run`)

  const ontologyId = built.ontologyId
  await page.goto(`${BASE}/ontologies/${ontologyId}`)
  await page.waitForTimeout(1500)
  await shot(page, outDir, `${domainCn}_construction_02_ontology_info`)

  const entityTab = page.locator('button').filter({ hasText: '实体' })
  if (await entityTab.isVisible()) {
    await entityTab.click()
    await page.waitForTimeout(1500)
    await shot(page, outDir, `${domainCn}_construction_03_entities`)
  }

  await page.goto(`${BASE}/ontologies/${ontologyId}?tab=graph`)
  await page.waitForTimeout(1500)
  await page.waitForTimeout(2000)
  await shot(page, outDir, `${domainCn}_construction_04_graph`)

  // 统计
  const entities = await api(request, 'GET', `/api/v1/ontologies/${ontologyId}/entities?page=1&page_size=1`, token)
  const totalEntities: number = entities.data?.total ?? 0

  return { ontologyId, taskId: built.mappingTaskId, finalStatus: built.status, totalEntities }
}

// ── 测试入口 ──────────────────────────────────────────────────────────

const DOMAINS = ['供应链', '医疗', '教育', '法律', '营销', '财务']

test.describe.configure({ mode: 'serial' })

test.describe('六领域 Pipeline Mapping + Dataset Construction 全量测试', () => {
  const ts = Date.now()
  const outDir = path.resolve(__dirname, '../../../../test-results/all-domains', String(ts))
  let token = ''
  const results: Record<string, DomainResult> = {}

  test.beforeAll(async ({ browser }) => {
    fs.mkdirSync(outDir, { recursive: true })
    const loginPage = await browser.newPage()
    token = await login(loginPage)
    await loginPage.close()
    console.log(`\n输出目录: ${outDir}`)
    console.log('Authenticated test session prepared.')
  })

  test.afterAll(() => {
    fs.mkdirSync(outDir, { recursive: true })
    const summary = path.join(outDir, 'summary.json')
    fs.writeFileSync(summary, JSON.stringify(results, null, 2), 'utf-8')
    console.log(`\n=== 汇总 ===`)
    for (const [key, val] of Object.entries(results)) {
      const icon = val.error ? '❌' : '✅'
      console.log(`${icon} ${key}: ${val.error ?? JSON.stringify({ ...val, error: undefined })}`)
    }
    console.log(`结果保存至: ${summary}`)
  })

  // ── Pipeline Mapping 测试（6个域）──────────────────────────────────
  for (const domain of DOMAINS) {
    test(`Pipeline Mapping — ${domain}`, async ({ page, request }) => {
      test.setTimeout(600_000)
      const key = `${domain}_pipeline`
      try {
        console.log(`\n${'='.repeat(50)}`)
        console.log(`Pipeline Mapping: ${domain}`)
        console.log('='.repeat(50))
        const result = await runPipelineMapping(page, request, token, domain, ts, outDir)
        results[key] = {
          pipeline_id: result.pipelineId,
          ontology_id: result.ontologyId,
          curated_count: result.curatedIds.length,
          entities: result.buildBody.total_entities,
          relations: result.buildBody.total_relations,
          logic: result.buildBody.total_logic,
          actions: result.buildBody.total_actions,
        }
        expect(result.buildBody.total_entities, '应有至少 1 个实体').toBeGreaterThan(0)
      } catch (error: unknown) {
        results[key] = { error: messageFrom(error) }
        await page.screenshot({ path: path.join(outDir, `${domain}_pipeline_ERROR.jpg`), type: 'jpeg', quality: 75 }).catch(() => {})
        throw error
      }
    })
  }

  // ── Dataset Construction 测试（6个域）─────────────────────────────
  for (const domain of DOMAINS) {
    test(`Dataset Construction — ${domain}`, async ({ page, request }) => {
      test.setTimeout(600_000)
      const key = `${domain}_construction`
      try {
        console.log(`\n${'='.repeat(50)}`)
        console.log(`Dataset Construction: ${domain}`)
        console.log('='.repeat(50))
        const result = await runDatasetConstruction(page, request, token, domain, ts, outDir)
        results[key] = {
          ontology_id: result.ontologyId,
          task_id: result.taskId,
          status: result.finalStatus,
          entities: result.totalEntities,
        }
        expect(result.finalStatus, '提取应成功完成').toBe('completed')
      } catch (error: unknown) {
        results[key] = { error: messageFrom(error) }
        await page.screenshot({ path: path.join(outDir, `${domain}_construction_ERROR.jpg`), type: 'jpeg', quality: 75 }).catch(() => {})
        throw error
      }
    })
  }
})
