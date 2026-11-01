import { expect, test, type Page } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'

// Runs against a real JWT stack seeded by backend/scripts/seed_factory_twin_demo.py.
// The admin account authors the package; the seed's private users file supplies the
// second-person reviewer. Each seeded run can be driven through the loop once; seed again
// with --force-new for another pass. E2E_USERNAME / E2E_PASSWORD override the author.
type DemoManifest = { url: string; decision_run_id?: string; reviewer?: string | null }
type DemoUsers = { reviewer: { username: string; password: string } }

const read = <T,>(file: string): T | null => (fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) as T : null)
const manifest = read<DemoManifest>(path.resolve('../backend/.factory-twin-demo.json'))
const users = read<DemoUsers>(path.resolve('../backend/.factory-twin-demo.users.json'))
const shots = path.resolve('../docs/showcase/factory-twin/screenshots')

test.skip(!manifest?.decision_run_id || !users?.reviewer, 'needs a JWT stack seeded by seed_factory_twin_demo.py')

async function signIn(page: Page, username: string, password: string) {
  const response = await page.request.post('/api/v1/auth/login', { data: { username, password } })
  expect(response.ok(), `sign in as ${username}`).toBeTruthy()
  const body = await response.json() as { data?: { access_token?: string }; access_token?: string }
  const token = body.data?.access_token ?? body.access_token
  expect(token).toBeTruthy()
  await page.addInitScript(([value, name]) => {
    localStorage.setItem('token', value)
    localStorage.setItem('auth-store', JSON.stringify({ state: { token: value, user: { username: name, role: 'admin' } }, version: 0 }))
  }, [token as string, username] as const)
}

async function openPage(page: Page, title: string) {
  await page.setViewportSize({ width: 1600, height: 1200 })
  await page.goto(manifest!.url)
  await expect(page.getByText('Factory Twin Operations', { exact: true })).toBeVisible({ timeout: 30_000 })
  await page.getByRole('button', { name: title }).click()
}

test('factory plans -> compare -> blocked plan -> package -> second-person approval -> apply -> revert', async ({ browser }) => {
  test.setTimeout(240_000)
  const author = await browser.newPage()
  await signIn(author, process.env.E2E_USERNAME ?? 'admin', process.env.E2E_PASSWORD ?? 'admin123')
  await openPage(author, '方案对比与决策')

  // Problem and evidence: exposure on the uninspected high-risk asset, coverage, delivery.
  const kpis = author.getByTestId('kpi-strip')
  await expect(kpis).toContainText('330', { timeout: 30_000 })
  await expect(kpis).toContainText('50%')
  await expect(kpis).toContainText('14:45')
  await expect(author.getByRole('cell', { name: '未排产', exact: true })).toBeVisible()
  await expect(author.getByText(/高风险设备 EQ005（健康分 52）尚未检修/).first()).toBeVisible()

  // Simulated candidates: the recommended plan first, the naive one blocked by the same rules.
  const list = author.getByTestId('action-list')
  await expect(list.getByRole('button')).toHaveCount(4)
  await expect(list.getByRole('button').first()).toContainText('推荐')
  await expect(list).toContainText('不可行：capacity_collision')
  await top(author)
  await author.screenshot({ path: path.join(shots, '09-plan-comparison.png'), fullPage: true })

  const detail = author.getByTestId('action-detail')
  await list.getByRole('button', { name: /直接插入/ }).click()
  await expect(detail).toContainText('capacity_collision')
  await expect(detail.getByRole('button', { name: '创建决策包' })).toBeDisabled()

  await list.getByRole('button', { name: /组合/ }).click()
  await expect(detail).toContainText('330 → 0')
  await expect(detail).toContainText('安排维护任务')
  await expect(detail).toContainText('wo-300-machine')
  await detail.getByPlaceholder('为什么选择这个方案，以及接受哪些代价').fill('先检修高风险 EQ005，再把 WO-300 排到 14:00；暴露 330→0，交付风险 1→0。')
  await detail.getByRole('button', { name: '创建决策包' }).click()
  const panel = detail.getByTestId('decision-package')
  await expect(panel).toContainText('草稿', { timeout: 20_000 })
  await expect(detail.getByTestId('package-edits')).toContainText('新建 · ScheduleItem')
  await expect(detail.getByTestId('package-edits')).toContainText('wo-300-machine')
  await top(author)
  await author.screenshot({ path: path.join(shots, '10-decision-package.png'), fullPage: true })

  await panel.getByRole('button', { name: '提交审核' }).click()
  await expect(panel).toContainText('审核中')
  await panel.getByRole('button', { name: '批准' }).click()
  await expect(detail.getByRole('alert')).toContainText(/cannot review|self_review/)

  // A second person reviews the same server-side package and executes it.
  const reviewer = await browser.newPage()
  await signIn(reviewer, users!.reviewer.username, users!.reviewer.password)
  await openPage(reviewer, '方案对比与决策')
  await reviewer.getByTestId('action-list').getByRole('button', { name: /组合/ }).click()
  const reviewPanel = reviewer.getByTestId('decision-package')
  await expect(reviewPanel).toContainText('审核中', { timeout: 20_000 })
  await reviewPanel.getByRole('button', { name: '批准' }).click()
  await expect(reviewPanel).toContainText('已批准', { timeout: 20_000 })
  await reviewPanel.getByRole('button', { name: '执行（合并到本体）' }).click()
  await expect(reviewPanel).toContainText('已执行', { timeout: 30_000 })
  await top(reviewer)
  await reviewer.screenshot({ path: path.join(shots, '11-approved-and-applied.png'), fullPage: true })

  // The app reads an Ontology snapshot; a new snapshot shows the merged live schedule.
  await refreshSnapshot(reviewer)
  await reviewer.getByRole('button', { name: '生产与维护协同排程' }).click()
  const gantt = reviewer.getByTestId('schedule-gantt')
  await expect(gantt).toContainText('14 已排 · 0 未排', { timeout: 30_000 })
  await expect(gantt.locator('[data-task="maint-EQ005"]')).toHaveCount(1)
  await top(reviewer)
  await reviewer.screenshot({ path: path.join(shots, '12-schedule-after-apply.png'), fullPage: true })

  // Revert restores the live schedule exactly.
  await reviewer.getByRole('button', { name: '方案对比与决策' }).click()
  await reviewer.getByTestId('action-list').getByRole('button', { name: /组合/ }).click()
  await expect(reviewPanel).toContainText('已执行', { timeout: 20_000 })
  await reviewPanel.getByRole('button', { name: '回滚本体变更' }).click()
  await expect(reviewPanel).toContainText('已回滚', { timeout: 30_000 })
  await refreshSnapshot(reviewer)
  await reviewer.getByRole('button', { name: '生产与维护协同排程' }).click()
  await expect(reviewer.getByTestId('schedule-gantt')).toContainText('12 已排 · 1 未排', { timeout: 30_000 })
})

async function top(page: Page) {
  await page.evaluate(() => window.scrollTo(0, 0))
}

async function refreshSnapshot(page: Page) {
  const button = page.getByRole('button', { name: '刷新数据' })
  await button.click()
  await expect(button).toBeEnabled({ timeout: 30_000 })
}
