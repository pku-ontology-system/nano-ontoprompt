# 工厂数字孪生控制塔：完整演示 Guide

更新日期：2026-11-01

## 1. 你要展示的故事

这是一个“设备健康风险驱动生产与维护协同排程”的工厂数字孪生演示。它参考 Palantir 官方 [Lear JIT Production](https://www.youtube.com/watch?v=-DPdyQR1bG4) 的操作结构，以及 Foundry 的 [Dynamic Scheduling](https://www.palantir.com/docs/foundry/dynamic-scheduling/scheduling-overview) 契约：业务人员先看到工厂态势，进入资源排程，查看依赖和推荐，制造一个约束冲突并看到系统阻断，再提交一条有效改排到 Scenario 预览。

新增的“方案对比与决策”页把故事补到最后一步：用户 Function 按 `decision/v1` 比较多个候选方案并标出依据，推荐方案经决策包提交、由第二个人批准后才合并到本体，并且可以回滚。

它不是静态 UI mock：页面读取真实本体快照；推荐和业务校验调用已发布的用户 Function；预览调用已发布 Action；Scenario 使用实时 ETag。资产页使用本地固定的 NASA C-MAPSS FD001 子集；生产排程和交期是确定性演示数据，不能描述为真实工厂历史。

演示地址：

[打开 Factory Twin Operations](http://localhost:15173/apps/workshop/dea63459-3064-4385-83c7-acb3b5f47daf?ontology=6681e0bf-a289-40e0-b6c5-b823186b3198)

本机重新安装后，链接以 `backend/.factory-twin-demo.json` 里的 `url` 为准（每次 `--force-new` 都会生成新链接）。

登录：`admin` / `admin123`。审核人账号由安装脚本准备，只写在本机私有文件 `backend/.factory-twin-demo.users.json`（600 权限，不进 Git）。

建议使用 1440×900 或更大的浏览器窗口，缩放保持 100%。完整演示约 10–13 分钟，其中决策闭环约 3 分钟。

## 2. 一句话开场

> 这不是针对某家工厂写死的看板。平台把任意客户数据映射成资产、资源和排程对象，再通过配置化 Workshop、可发布 Function、Action 和 Scenario 组装应用；换数据集时替换映射和配置，不改平台代码。

## 3. Step 1 — 工厂控制塔

打开链接后停留在“工厂控制塔”。指出页面同时呈现：

1. 13 个排程对象，其中 7 个已发布工单、1 个未排任务。
2. 设备健康风险分布与健康评分排行。
3. 生产任务、维护任务和资源可用性在同一对象语义下展示。
4. 顶部明确显示每个数据源的对象/聚合数量和刷新方式。

推荐讲法：

> 管理者先看影响和异常，不需要先理解模型节点。这里把设备健康、生产状态和未排任务放在同一运营语境中；下一步从风险进入可执行排程。

![工厂控制塔](screenshots/01-command-center.png)

## 4. Step 2 — 打开生产与维护协同排程

点击顶部“生产与维护协同排程”。指出：

- 灰色是可用产能背景层；蓝色是生产层；紫色是预测性维护层。
- 每一行是配置的资源，不是代码里的固定产线。
- 黑色箭头表示工序前置依赖。
- `WO-100 · Final assembly` 同时出现在 CNC 与 Assembly 两行，证明同一任务可以链接多个资源。
- 下方 `WO-300 · Expedite machining` 没有资源和时间，因此进入未排任务抽屉，没有被静默丢弃。

![动态排程全景](screenshots/02-dynamic-schedule.png)

## 5. Step 3 — 选择任务，展示影响链

点击蓝色任务 `WO-200 · Machine bracket`。

你会看到：

- 任务出现选中描边。
- 与它有关的依赖链变成紫色。
- 下方出现资源、提前、延后、缩短、延长控件。
- 推荐区域显示当前选择对象。

推荐讲法：

> 用户操作的是本体里的一个排程对象。任务标识、资源、开始结束、前置任务和层全部来自字段绑定；这里没有 WO-200 或 CNC 的前端分支。

![选择任务和依赖链](screenshots/03-select-and-reason.png)

## 6. Step 4 — 调用通用推荐能力

点击“为所选任务查找推荐”。等待出现 `Constraint-aware alternatives`。

推荐 Function 会收到：当前所有排程对象、已提议修改、资源数组、层、依赖、可见时间范围和触发任务。它根据任务数据里的 `compatible_resources` 寻找可用空档，并返回 `SLOT`/`PUCK` 高亮，而不是把工厂规则写进 Gantt。

推荐讲法：

> UI 只认识 `schedule-search/v1` 输出契约。今天这个 Function 是空档搜索；换成 MILP、启发式算法或客户自有优化器，只要发布同一契约，页面不需要改。

![推荐结果](screenshots/04-recommendation.png)

## 7. Step 5 — 制造一个不可行方案，证明系统会阻断

保持 `WO-200 · Machine bracket` 选中，点击“提前一步”，再点击“运行业务校验”。

这会把任务提前 15 分钟，与共享 CNC 资源的任务发生容量冲突。页面显示 `capacity collision`，并阻止 Scenario 预览/保存。

推荐讲法：

> 视觉上的拖动或按钮操作只是本地提案。结构校验和业务 Function 都通过后，才允许进入受控 Action。不可行不是空结果，也不是悄悄调整，而是明确错误。

![约束冲突被阻断](screenshots/05-constraint-block.png)

然后点击变更记录里的“撤销”。

## 8. Step 6 — 生成有效方案并预览 Action

点击 `WO-200 · Inspection`，点击“延后一步”，然后依次点击：

1. “运行业务校验”——应显示“业务校验通过”。
2. “检查 Scenario 变更”——应显示“Action 预览：3 项本体修改”。

三个修改分别对应绑定的开始时间、结束时间和资源字段。此时尚未修改主本体。

推荐讲法：

> 这是 Palantir 式 operational loop 的关键：用户建议变更，Function 重新验证，Action 编译成可审查编辑，Scenario 隔离结果。点击“保存到 Scenario”只产生 Scenario 修订；后续 merge 仍要做冲突检查。

演示时可以停在预览。如果希望展示审计链，可点击“保存到 Scenario”；不要把它描述成已经写回 MES。

![Scenario Action 预览](screenshots/06-scenario-preview.png)

## 9. Step 7 — 查看设备数字孪生

刷新页面清除未保存的本地提案，然后点击“设备数字孪生”。

指出：

- 5 台资产来自固定的 NASA C-MAPSS FD001 子集。
- 页面展示资产—资源归属、风险等级、健康评分、维护状态和传感器字段。
- EQ002/EQ005 的风险状态可以成为维护层任务或规则告警的输入。
- 这里展示的是数据和决策数字孪生，不冒充 3D 几何孪生。

![设备数字孪生](screenshots/07-asset-twin.png)

## 10. Step 8 — 方案对比、审批与执行（新增）

点击“方案对比与决策”。这一页全部由通用 `study_run` 组件组成：已发布的 Study 用三个 Object Query 节点读取排程、设备和资源，用户 Function `factory_schedule_decision`（`decision/v1`）在固定的数据视图上比较方案。

先指出问题和依据（顶部指标都标了依据类型）：

- **330 分钟高风险暴露（推断）**：EQ005 健康分 52、风险 High，但排程里没有它的检修任务；CNC 上 WO-100、WO-200 的工序都在它检修之前运行。
- **1 个延误或未排工单（观测）**：加急单 WO-300 尚未排产，交期 16:00。
- **维护覆盖率 50%（推断）**：两台高风险设备中，只有 EQ002 在首个生产任务前完成检修。
- 下方表格逐个工单给出原因，每条都标明“观测”或“推断”；证据里保留 C-MAPSS 单元与周期。

再看候选方案。它们全部是模拟结果，不改正式数据：

| 方案 | 结果 |
| --- | --- |
| 推荐 · 组合：EQ005 07:00–07:45 检修 + WO-300 排入 CNC 14:00–15:00 | 暴露 330→0，交付风险 1→0，覆盖率 50%→100% |
| 可行 · 只检修 EQ005 | 暴露归零，但 WO-300 仍未排产 |
| 可行 · 只把 WO-300 排到 07:00 | 按期完成，但在未检修设备上多跑 60 分钟（暴露 390） |
| 不可行 · WO-300 直接插入 08:30 | 与 WO-100 机加工冲突（capacity_collision），不能生成决策包 |

![方案对比](screenshots/09-plan-comparison.png)

推荐讲法：

> 候选方案由 Function 按数据生成，约束检查和排程页用的是同一套规则。不可行方案不会被悄悄修正，而是明确显示冲突并禁止提交。

然后走审批闭环：

1. 选择推荐方案，查看前后对比，以及将要调用的两个 Action：“安排维护任务”“修改排程项”。
2. 写决策理由，点击“创建决策包”。服务端从固定数据视图编译出 4 项本体修改。
3. 点击“提交审核”。作者自己再点“批准”会被拒绝：作者不能审批自己的决策包。
4. 退出登录，换审核人账号登录，打开同一页、选同一方案，点击“批准”，再点击“执行（合并到本体）”。
5. 点击页面顶部的“刷新数据”。应用读取的是本体快照，执行后要重新拍一份。切到“生产与维护协同排程”：EQ005 的检修出现在 07:00，WO-300 排在 14:00，显示“14 已排 · 0 未排”。
6. 回到决策页，点击“回滚本体变更”，再点“刷新数据”，排程恢复为“12 已排 · 1 未排”。

![决策包](screenshots/10-decision-package.png)

![审核人批准并执行](screenshots/11-approved-and-applied.png)

![执行后的排程](screenshots/12-schedule-after-apply.png)

推荐讲法：

> 推演只发生在 Scenario 和数据视图里。真正写回本体必须经过决策包：运行摘要固定、修改由服务端编译、由他人批准、合并时做冲突检查、全程记录事件，并且可以回滚。

注意：

- 每次安装生成的这条运行只能完整走一次闭环；要重演，用 `--force-new` 重新安装。
- 登录接口每分钟最多 5 次，排练时不要频繁切换账号。

## 11. Step 9 — 证明这不是硬编码应用

点击“编辑”，切换编辑器页面到“生产与维护协同排程”，点击 `Factory Twin · Dynamic Schedule` 的“配置”。

右侧可以直接看到：

- 任务 ID、名称、资源、开始、结束、依赖和排程层字段绑定。
- 任意数量的层定义及 `background / standard / event` 样式。
- 资源列表和单/多资源语义。
- Scenario 变量、修改 Action、创建 Action及其参数名。
- 通过输出契约筛选的推荐和校验 Logic Asset。
- 吸附间隔、运营时段、拖放行为和阻断策略。

推荐讲法：

> 当前工厂只是一个已安装配置。把 `ScheduleItem` 换成另一家工厂的作业对象，重新绑定字段、资源和 Function，即可复用同一组件、Action/Scenario 流程和浏览器测试。

![通用 Builder 配置](screenshots/08-generic-builder.png)

## 12. 可选操作 — Shift+拖拽创建任务

返回运行态排程页，按住 Shift，在任一资源行空白区域横向拖拽。系统会生成黄色本地任务提案，并把开始、结束、资源、名称和任务 ID 编译给配置的“创建排程项”Action。它同样必须经过业务校验和 Scenario 预览。

这一步不要在正式演示开头做，因为它会增加现场变量；建议放在问答环节。

## 13. “没有 hardcode”的证据清单

| 层 | 演示中的工厂内容存放位置 | 平台通用边界 |
| --- | --- | --- |
| 数据 | `backend/examples/factory_twin/data.json` | 任意 Ontology 对象和属性 |
| 对象类型 | Seeder 创建的 `FactoryAsset / FactoryResource / ScheduleItem` | 通用本体类型、实例、快照 |
| UI | Workshop definition 中的字段、图层、资源和页面配置 | 通用组件运行时 |
| 推荐 | 用户 Function `schedule_search.py` | `schedule-search/v1` |
| 校验 | 用户 Function `schedule_validation.py` | `schedule-validation/v1` |
| 修改/创建 | 已发布 Scenario Action 配置 | 通用 Action 编译与权限边界 |
| 隔离推演 | Tracking Scenario | ETag、revision、preview/save |
| 方案比较与推荐 | 用户 Function `decision_function.py`（Study 节点 `decide`） | `decision/v1` |
| 审批与写回 | 决策包：提交、他人批准、执行、回滚 | 通用 Decision Package + Scenario merge |

“不硬编码”不等于没有领域数据或领域配置。任务名、工厂资源、兼容能力和规则必须存在，但它们属于用户可替换的数据、Function 和应用定义，不属于平台 `if manufacturing` 分支。

## 14. 稳定性与生产级别应如何表述

已经通过的真实浏览器验收包括：JWT 登录、真实 PostgreSQL/FalkorDB 数据读取、Workshop 快照、三层排程、多资源显示、依赖箭头、隔离 Function 推荐、冲突校验、有效修改、Action 预览、刷新恢复、资产页和 Builder 配置。该验收没有使用前端 API mock。

决策闭环另有一条真实浏览器验收（2026-11-01，本机隔离栈）：方案对比、不可行方案禁止提交、创建决策包、作者自批被拒、审核人批准、执行合并、刷新快照后排程变化、回滚恢复。

可以说“具备生产导向的控制面”：版本化应用、发布态 Function/Action、资源限制、Scenario 隔离、ETag、明确失败状态和审计边界。

不能说“已完成生产认证”：本演示没有完成 24 小时 soak、真实 PLC/MES 写回、现场权限矩阵、灾备演练或代表性工厂规模性能验收。

## 15. 复现与维护

演示包：

- `backend/scripts/seed_factory_twin_demo.py`
- `backend/examples/factory_twin/data.json`
- `backend/examples/factory_twin/schedule_search.py`
- `backend/examples/factory_twin/schedule_validation.py`
- `backend/examples/factory_twin/decision_function.py`（`decision/v1` 方案比较）
- `backend/unit_tests/test_factory_decision_function.py`（已知答案单测）
- `frontend/src/test/e2e/factory-twin-showcase.spec.ts`
- `frontend/src/test/e2e/factory-twin-decision.spec.ts`（决策闭环）

重新安装时在 backend 容器运行：

```text
FACTORY_TWIN_PASSWORD=<local-admin-password> python scripts/seed_factory_twin_demo.py --api http://127.0.0.1:8000 --force-new
```

浏览器验收：

```text
PLAYWRIGHT_BASE_URL=http://127.0.0.1:15173 npx playwright test src/test/e2e/factory-twin-showcase.spec.ts --project=chromium --workers=1
PLAYWRIGHT_BASE_URL=http://127.0.0.1:15173 npx playwright test src/test/e2e/factory-twin-decision.spec.ts --project=chromium --workers=1 --retries=0
```

决策闭环用例需要 JWT 模式（安装脚本会准备审核人账号），并且会消耗这次安装的运行；重跑前先用 `--force-new` 重新安装。

## 16. 结尾话术

> 这个演示的价值不是一张排程图，而是一套可复用的业务构建方式：把真实世界资产和任务映射成本体对象，把客户约束发布为可版本化 Function，把操作发布为 Action，再通过 Scenario 先验证、后提交。相同制造领域换一套数据时，重做映射和配置，不重写平台。
