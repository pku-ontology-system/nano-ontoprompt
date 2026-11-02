/**
 * 三领域对比测试：供应链 / 医疗 / 财务
 * 每个业务域跑 Pipeline Mapping + Dataset Construction 两条路径，
 * 最终输出实体数、边数、逻辑数、动作数汇总表。
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
  progress?: { pct?: number }
  edges?: unknown[]
  length: number
}

interface ApiBody {
  id?: string
  status?: string
  task_id?: string
  data?: ApiData
  stats?: {
    curated_dataset_ids?: string[]
    meta?: { outputs?: Array<{ curated_dataset_id: string; source_file?: string }> }
  }
  total_concepts?: number
  total_instances?: number
  total_relations?: number
  total_logic?: number
  total_actions?: number
}

function messageFrom(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function requiredString(value: string | undefined, label: string): string {
  if (!value) throw new Error(`${label} missing from API response`)
  return value
}

const DOMAINS = ['供应链', '医疗', '财务'] as const
type Domain = typeof DOMAINS[number]

// ── 工具 ─────────────────────────────────────────────────────────────────

async function login(page: Page): Promise<string> {
  const username = process.env.PROJECT4_E2E_USERNAME
  const password = process.env.PROJECT4_E2E_PASSWORD
  if (!username || !password) throw new Error('Set PROJECT4_E2E_USERNAME and PROJECT4_E2E_PASSWORD to run this E2E flow')
  await page.goto(`${BASE}/login`)
  await page.fill('input[placeholder="用户名"]', username)
  await page.fill('input[placeholder="密码"]', password)
  await page.click('button[type="submit"]')
  await page.waitForURL(`${BASE}/overview`, { timeout: 10000 })
  const token = await page.evaluate(() => localStorage.getItem('token') ?? '')
  expect(token, 'JWT token must be set').toBeTruthy()
  return token
}

async function apiCall(
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

// 统一统计函数：从 SQLite 查实体/边/逻辑/动作
async function collectStats(request: APIRequestContext, token: string, ontologyId: string) {
  // 实体 — v1 endpoint 返回 {data: [...]}, 无分页
  const entBody = await apiCall(request, 'GET', `/api/v1/ontologies/${ontologyId}/entities`, token)
  const entities: number = Array.isArray(entBody.data) ? entBody.data.length : 0

  // 边 (从 v1 graph 接口，SQLite fallback)
  const graphBody = await apiCall(request, 'GET', `/api/v1/ontologies/${ontologyId}/graph?limit=5000`, token)
  const edges: number = (graphBody.data?.edges ?? []).length

  // 逻辑规则
  const logicBody = await apiCall(request, 'GET', `/api/v1/ontologies/${ontologyId}/logic`, token)
  const logic: number = (logicBody.data ?? []).length

  // 动作
  const actionBody = await apiCall(request, 'GET', `/api/v1/ontologies/${ontologyId}/actions`, token)
  const actions: number = (actionBody.data ?? []).length

  return { entities, edges, logic, actions }
}

// ── Pipeline Mapping 路径 ─────────────────────────────────────────────────

async function runPipelineMapping(
  page: Page,
  request: APIRequestContext,
  token: string,
  domain: Domain,
  ts: number,
  outDir: string,
) {
  const domainDir = path.join(TEST_DATA, domain)
  const files = fs.readdirSync(domainDir).filter(f => fs.statSync(path.join(domainDir, f)).isFile()).sort()
  console.log(`\n  [${domain}][Pipeline] 文件数: ${files.length}`)

  // 1. 上传文件到 v2 datasets
  const uploaded: Array<{ name: string; dataset_id: string }> = []
  for (const filename of files) {
    const res = await request.post(`${API}/api/v2/datasets/upload`, {
      headers: { Authorization: `Bearer ${token}` },
      multipart: { file: { name: filename, mimeType: 'application/octet-stream', buffer: fs.readFileSync(path.join(domainDir, filename)) } },
    })
    const body = await res.json()
    expect(res.ok(), `upload ${filename}: ${JSON.stringify(body)}`).toBeTruthy()
    uploaded.push({ name: filename, dataset_id: body.data.id })
  }
  console.log(`    上传完成: ${uploaded.length} 个文件`)

  // 2. 创建 Pipeline
  const plBody = await apiCall(request, 'POST', '/api/v2/pipelines', token, {
    name: `E2E_${domain}_Pipeline_${ts}`,
    domain,
    description: `三领域对比 E2E — ${domain} Pipeline`,
    route: 'A',
    definition: {
      schema_version: '2.0',
      nodes: [
        { id: 'connector', type: 'connector', label: `${domain}数据源`, position: { x: 80, y: 180 },
          config: { source_type: 'file', files: uploaded } },
        { id: 'storage',   type: 'storage',   label: '分类存储',       position: { x: 330, y: 180 },
          config: { storage_mode: 'auto' } },
        { id: 'transform', type: 'transform', label: '数据转换',       position: { x: 580, y: 180 },
          config: { path: 'auto', steps: [] } },
        { id: 'output',    type: 'output',    label: '结构化输出',     position: { x: 830, y: 180 },
          config: { dataset_type: 'curated_dataset', primary_key: [] } },
      ],
      edges: [
        { id: 'e1', source: 'connector', target: 'storage' },
        { id: 'e2', source: 'storage',   target: 'transform' },
        { id: 'e3', source: 'transform', target: 'output' },
      ],
    },
  })
  const pipelineId = requiredString(plBody.id ?? plBody.data?.id, 'pipeline id')
  expect(pipelineId).toBeTruthy()
  console.log(`    Pipeline: ${pipelineId.slice(0, 8)}`)

  // 3. 前端截图 Pipeline Builder
  await page.goto(`${BASE}/data/pipelines/${pipelineId}`)
  await page.waitForTimeout(1500)
  await shot(page, outDir, `${domain}_pm_01_pipeline_builder`)

  // 4. 同步运行 Pipeline
  console.log(`    运行 Pipeline...`)
  const runBody = await apiCall(request, 'POST', `/api/v2/pipelines/${pipelineId}/run-sync`, token)
  expect(runBody.status, `Pipeline 运行失败: ${JSON.stringify(runBody).slice(0, 200)}`).toBe('success')
  const curatedIds: string[] = runBody.stats?.curated_dataset_ids ?? []
  console.log(`    Pipeline 完成: ${curatedIds.length} 个 curated dataset`)

  // 5. Publish
  await apiCall(request, 'POST', `/api/v2/pipelines/${pipelineId}/publish`, token)

  // 6. 批准全部 curated datasets
  for (const id of curatedIds) {
    await apiCall(request, 'POST', `/api/v2/curated/${id}/review?action=approve`, token)
  }

  // 7. 创建 Pipeline Mapping 本体
  const ontoBody = await apiCall(request, 'POST', '/api/v1/ontologies', token, {
    name:        `E2E_${domain}_PipelineMapping_${ts}`,
    domain,
    description: `三领域对比 — ${domain} Pipeline Mapping`,
    build_mode:  'pipeline_mapping',
  })
  const ontologyId = requiredString(ontoBody.data?.id ?? ontoBody.id, 'ontology id')
  expect(ontologyId).toBeTruthy()
  console.log(`    本体: ${ontologyId.slice(0, 8)}`)

  // 8. 为每个 curated dataset 创建 mapping
  const outputs: Array<{ curated_dataset_id: string; source_file?: string }> =
    runBody.stats?.meta?.outputs ?? curatedIds.map((id: string) => ({ curated_dataset_id: id }))

  for (const output of outputs) {
    const sf = output.source_file ?? ''
    const entityClass = sf
      ? sf.replace(/\.[^.]+$/, '').split(/[_\-\s]+/).map((w: string) => w.charAt(0).toUpperCase() + w.slice(1)).join('')
      : `Dataset_${output.curated_dataset_id.slice(0, 6)}`
    await apiCall(request, 'POST', `/api/v2/ontologies/${ontologyId}/mappings`, token, {
      curated_dataset_id: output.curated_dataset_id,
      entity_class: entityClass,
      field_mapping: { '__primary_key__': '__row_hash__' },
      confidence: 1.0,
    })
  }

  // 9. Build all
  console.log(`    构建本体...`)
  const buildBody = await apiCall(request, 'POST', `/api/v2/ontologies/${ontologyId}/mappings/build-all`, token)
  console.log(`    构建完成: 概念=${buildBody.total_concepts} 实例=${buildBody.total_instances} 关系=${buildBody.total_relations} 逻辑=${buildBody.total_logic} 动作=${buildBody.total_actions}`)

  // 10. 前端截图本体详情 + 图谱
  await page.goto(`${BASE}/ontologies/${ontologyId}`)
  await page.waitForTimeout(1500)
  await shot(page, outDir, `${domain}_pm_02_ontology`)

  await page.goto(`${BASE}/ontologies/${ontologyId}?tab=graph`)
  await page.waitForTimeout(3000)
  await shot(page, outDir, `${domain}_pm_03_graph`)

  // 11. 统一统计（从 SQLite 直接数 — 与 pipeline build 结果相同，但统一口径）
  const stats = await collectStats(request, token, ontologyId)
  return { pipelineId, ontologyId, stats }
}

// ── Dataset Construction 路径 ───────────────────────────────────────────

async function runDatasetConstruction(
  page: Page,
  request: APIRequestContext,
  token: string,
  domain: Domain,
  ts: number,
  outDir: string,
) {
  const domainDir = path.join(TEST_DATA, domain)
  const structuredFile = fs.readdirSync(domainDir)
    .filter(file => /\.(csv|xlsx|xls)$/i.test(file) && fs.statSync(path.join(domainDir, file)).isFile())
    .sort()[0]
  if (!structuredFile) throw new Error(`${domain} has no CSV or Excel Dataset fixture`)
  console.log(`\n  [${domain}][Dataset Construction] ${structuredFile} (private, deterministic mapping)`)

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
    domain,
    ontologyName: `E2E_${domain}_DatasetConstruction_${ts}`,
  })
  console.log(`    ConstructionRun: ${built.runId.slice(0, 8)} · ontology: ${built.ontologyId.slice(0, 8)}`)

  await page.goto(`${BASE}/data/regular/build?run=${encodeURIComponent(built.runId)}`)
  await page.waitForTimeout(1500)
  await shot(page, outDir, `${domain}_construction_01_run`)

  const ontologyId = built.ontologyId
  await page.goto(`${BASE}/ontologies/${ontologyId}`)
  await page.waitForTimeout(1500)
  await shot(page, outDir, `${domain}_construction_02_ontology`)

  await page.goto(`${BASE}/ontologies/${ontologyId}?tab=graph`)
  await page.waitForTimeout(3000)
  await shot(page, outDir, `${domain}_construction_03_graph`)

  // 7. 统计
  const stats = await collectStats(request, token, ontologyId)
  return { ontologyId, taskId: built.mappingTaskId, finalStatus: built.status, stats }
}

// ── 测试入口 ──────────────────────────────────────────────────────────────

test.describe.configure({ mode: 'serial' })

test.describe('三领域对比：Pipeline Mapping vs Dataset Construction', () => {
  const ts = Date.now()
  const outDir = path.resolve(__dirname, '../../../../test-results/three-domains-comparison', String(ts))
  let token = ''

  interface Row { domain: string; path: string; entities: number; edges: number; logic: number; actions: number; ontologyId: string; error?: string }
  const rows: Row[] = []

  test.beforeAll(async ({ browser }) => {
    fs.mkdirSync(outDir, { recursive: true })
    const pg = await browser.newPage()
    token = await login(pg)
    await pg.close()
    console.log(`\n输出目录: ${outDir}`)
  })

  test.afterAll(() => {
    // 保存 JSON
    fs.writeFileSync(path.join(outDir, 'summary.json'), JSON.stringify(rows, null, 2), 'utf-8')

    // Markdown 表格
    const header = '| 业务域 | 路径 | 实体数 | 边数 | 逻辑规则数 | 动作数 | 本体ID |'
    const sep    = '|--------|------|--------|------|-----------|--------|--------|'
    const body   = rows.map(r =>
      r.error
        ? `| ${r.domain} | ${r.path} | ❌ | ❌ | ❌ | ❌ | ${r.error?.slice(0, 60)} |`
        : `| ${r.domain} | ${r.path} | ${r.entities} | ${r.edges} | ${r.logic} | ${r.actions} | \`${r.ontologyId.slice(0, 8)}\` |`
    )

    const table = [header, sep, ...body].join('\n')
    fs.writeFileSync(path.join(outDir, 'summary.md'), table + '\n', 'utf-8')

    console.log('\n\n======================================')
    console.log('         三领域 Ontology 汇总表')
    console.log('======================================')
    console.log(table)
    console.log('======================================\n')
    console.log(`完整结果: ${path.join(outDir, 'summary.md')}`)
  })

  // ── Pipeline Mapping 路径（3个域）──────────────────────────────────────
  for (const domain of DOMAINS) {
    test(`Pipeline Mapping — ${domain}`, async ({ page, request }) => {
      test.setTimeout(600_000)
      try {
        const result = await runPipelineMapping(page, request, token, domain, ts, outDir)
        rows.push({ domain, path: 'Pipeline Mapping', ...result.stats, ontologyId: result.ontologyId })
        expect(result.stats.entities, '应有至少 1 个实体').toBeGreaterThan(0)
      } catch (error: unknown) {
        rows.push({ domain, path: 'Pipeline Mapping', entities: 0, edges: 0, logic: 0, actions: 0, ontologyId: '', error: messageFrom(error) })
        await page.screenshot({ path: path.join(outDir, `${domain}_pipeline_ERROR.jpg`), type: 'jpeg', quality: 75 }).catch(() => {})
        throw error
      }
    })
  }

  // ── Dataset Construction 路径（3个域）────────────────────────────────
  for (const domain of DOMAINS) {
    test(`Dataset Construction — ${domain}`, async ({ page, request }) => {
      test.setTimeout(1_800_000)
      try {
        const result = await runDatasetConstruction(page, request, token, domain, ts, outDir)
        rows.push({ domain, path: 'Dataset Construction', ...result.stats, ontologyId: result.ontologyId })
        expect(result.finalStatus, 'Dataset Construction should complete').toBe('completed')
        expect(result.stats.entities, '应有至少 1 个实体').toBeGreaterThan(0)
      } catch (error: unknown) {
        rows.push({ domain, path: 'Dataset Construction', entities: 0, edges: 0, logic: 0, actions: 0, ontologyId: '', error: messageFrom(error) })
        await page.screenshot({ path: path.join(outDir, `${domain}_construction_ERROR.jpg`), type: 'jpeg', quality: 75 }).catch(() => {})
        throw error
      }
    })
  }
})
