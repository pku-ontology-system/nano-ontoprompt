# Project 4 本体决策平台 PRD：三类数据闭环与平台化路线

| 项目 | 内容 |
|---|---|
| 版本 | v1.0（2026-10-22） |
| 负责人 | nihou |
| 状态 | 产品需求基线；P0/P1/P2 为计划，除“当前证据”明确写出的项目外，均不表示已经实现 |
| 对应分支与源码基线 | palantir，7ab1cd1 feat: deliver plan a 2.0 |
| 参考范围 | Palantir Foundry/AIP 公开文档、当前 Project 4 源码与本地运行状态、其他开源项目的公开文档 |
| 读者 | 产品负责人、前后端与数据工程师、演示负责人、评审者 |

> 本文把参考产品的能力转成 Project 4 自己的需求，不复刻商标、页面或未公开的内部实现。任何开源项目都只是候选设计参考；本文没有作出安装、引入依赖或替换现有系统的决定。

## 0. 一页决策摘要

Project 4 要成为面向制造与供应链的本体决策平台。用户从数据或业务事件进入，沿同一套 Ontology 对象、关系、证据、规则、情景和权限完成“发现 → 解释 → 比较 → 人工决定 → 受控写回 → 审计复盘”。三类已有入口——I-BADAS 多模态、FactoryNet 动态数据、FactoryNet 时序数据——都必须在 P0 形成可演示、可追溯的本地业务闭环，而不只完成构筑或回放。木材供应韧性演示是现成的横向基准，先恢复原结果并保持独立，不用它替代三条数据线的 P0 验收。

| 决策 | 已确定的边界 |
|---|---|
| P0 闭环 | 三条数据线均有人工审核后的本地 Ontology Action 写回；外部 ERP/MES 执行留到后期 |
| 数据事实 | PostgreSQL 保存权威事实和运行记录；FalkorDB 为可重建关系/情景投影；MinIO 保存原始媒体；向量检索不成为事实来源 |
| 决策事实 | 建议、审核和执行分开；模型输出只能是待核验候选；Action 执行以服务端权限和修订检查为准 |
| Agent | P0 可用确定性规则与人工工作流；P1 建持久 Session、Context、Event、Checkpoint、Tool Registry；付费模型调用须单独取得用户授权 |
| 交付节奏 | P0a 统一语义与 Action 基础；P0b 三条数据线同时完成闭环；P1 做可复用工作台与 Agent；P2 做平台级治理、生态和规模化 |
| 当前声明 | 源码存在不等于生产就绪；历史验收记录不等于当前版本已验证 |

### 0.1 状态标签

- **已实现**：当前源码存在相应模块或接口；并不自动代表跨入口一致、可运营或已验收。
- **本次已验证**：在本工作树和当前本地环境观察到可重现的行为，记录具体证据。
- **部分实现**：能力有基础，但仍存在语义、界面、覆盖率或运行保障缺口。
- **规划**：PRD 要求，尚未按本文验收。
- **研究候选**：有潜在价值，需要验证价值、成本、兼容性和数据适用性后才能排期。

### 0.2 来源与事实边界

Palantir 的 [Ontology](https://www.palantir.com/docs/foundry/ontology)、[Action 类型与日志](https://www.palantir.com/docs/foundry/action-types/action-log)、[Agent Stack](https://www.palantir.com/docs/foundry/aip/agent-stack)、[Workflow Lineage](https://www.palantir.com/docs/foundry/workflow-lineage/overview)、[应用参考](https://www.palantir.com/docs/foundry/getting-started/application-reference) 提供能力参照。仓库内 [能力补集与目标架构](PALANTIR_PROJECT4_COMPLEMENT_AND_TARGET_ARCHITECTURE.md) 和 [Ontology 差距审计](PALANTIR_ONTOLOGY_GAP_AUDIT_2026_09_23.md) 提供 Project 4 的历史设计与源码线索；本文用当前工作树和本地 API 校核会变化的状态。所有“可借鉴”均为产品推论，不表示 Palantir 与 Project 4 的实现方式相同。

## 1. 问题、用户与产品结果

### 1.1 用户问题

1. 业务人员能看图、看数据，却难以从一个问题直接找到对象、来源、影响和可执行下一步。
2. 三类数据入口已有构建与展示，但同一对象的状态、证据、决定和行动没有稳定的共同工作流。
3. Action、Scenario Action、Agent 建议与各类审计记录分布在不同入口，用户难判断“谁批准、改了什么、能否撤回”。
4. 来源、权限、运行状态和失败恢复在各页面呈现不一致；评审难从演示推断长期可运营性。
5. 平台潜力大，但缺少明确的优先级、接口合同和阶段门槛，容易把“加页面”误当成“产品闭环”。

### 1.2 目标用户与职责

| 角色 | 核心任务 | 需要看到的证明 |
|---|---|---|
| 质量复核员 | 复核多模态样例，确认、驳回或标记待补证据 | 媒体同步关系、来源版本、审核记录 |
| 设备工程师 | 按 episode 与时间窗口查看时序证据，决定是否发起维护复核 | 原始行、规则/阈值版本、时间范围、处理结果 |
| 运行值班员 | 监视逐事件状态与异常，确认或处置事件 | 事件顺序、水位、状态变更、幂等结果 |
| 供应链分析员 | 比较木材供应商方案与影响 | Baseline/B/C、需求影响、指标计算来源 |
| 审核人 | 对候选 Action 作批准、驳回或退回修改 | 预览差异、权限、冲突、责任人 |
| 平台管理员 | 管资源、规则、权限、健康、版本和恢复 | 可查询的血缘、运行指标、审计与故障状态 |
| 应用开发者 | 复用对象、查询、Action、事件与组件 | 稳定的版本化合同、SDK、兼容说明 |

### 1.3 成功标准

- P0 的三条入口各能用当前真实样例完成一次“证据 → 决定 → 本地写回 → 审计/回看”；成功与失败路径均可演示。
- 相同对象与证据引用在查询、图、详情、Scenario、Action 和 Agent 中保持身份与版本一致；不允许仅凭前端显示断言一致。
- 无权限用户不能执行写回；相同请求编号重放不会造成第二次业务效果；过期修订必须返回可解释冲突。
- 审核拒绝不会改变业务对象；执行后能查看前后差异、操作者、来源及结果；只能在明确支持的操作上提供自动撤销。
- 前端要让非开发者从业务线索完成任务，不要求知道 ontology ID、对象表名或 JSON 查询语法。

### 1.4 P0 的非目标

不接入真实 ERP/MES、不自动控制物理设备、不把合成数据当生产观测、不在没有独立验证时声称缺陷检测或故障预测准确率、不默认调用付费模型、不引入没有授权边界的自主写回、不一次性重建全部前后端。

## 2. 当前系统基线与木材演示恢复

### 2.1 当前可确认的实现

| 能力 | 当前依据 | 判断 |
|---|---|---|
| 数据集/版本/管道/构建 | backend/app/models/v2/dataset.py、pipeline.py、construction.py；v2 datasets/pipelines 路由 | 已实现基础 |
| Ontology 语义与对象查询 | semantic_core 合同、object_query/object_sets/object_views 路由 | 部分实现，入口与契约需统一 |
| Action 与 Scenario | OntologyActionType/Run；ScenarioStudy/Case/Run/Stage/Artifact；logic_actions 与 supplier_studies 路由 | 两套路径并存 |
| Agent | v2_agent_runs 保存单次 Run 与 JSON payload；agent 路由支持 context draft/decision | 已有单次运行持久记录；缺多轮持久 Session |
| 多模态 | I-BADAS 样例、RGB/深度/掩码/点云资产与证据工作区 | 已有构建和查看；质量决定写回未贯通 |
| 动态数据 | FactoryNet simulated-live、逐事件写入、独立图、水位、暂停/单步/发布 | 已有运行底座；业务处置未贯通 |
| 时序数据 | FactoryNet episode/time_s、replay/batches/graph、时间查询 | 已有回放与构建；维护决定未贯通 |
| 权限、来源、数据平面 | authorization_service、ExecutionContext、ResultManifest、EvidenceRef 等 | 基础存在；跨入口传播仍需验收 |

此表是源码级判断，不把历史 README 中的“三类构筑闭环”理解为本文要求的“决策与 Action 闭环”。2026-09-21 的 [供应商工程验收记录](VERIFY_SUPPLIER_SCENARIO_WORKBENCH.md) 是历史结果，不能直接证明本工作树的完整回归。

### 2.2 木材供应韧性演示：本次恢复的基准

调查时，当前 Study 的三个 Case 配置仍与固定 manifest 相符，但各 Case 的 definition_revision 和 scenario_revision 都为 0、runs 为空，Compare 的选择原因是 no_compatible_result。Demo bootstrap 只负责建立 Study/Case 和基础视图，不自动提交候选 Action 或运行案例。因此启动页面后看不到原来的结果；这与上一轮前端页面改动是两个不同问题。上一轮新增前端页面已恢复到此分支原有版本。

当前 Study ID 为 9a4048fb-e1a9-4ed7-adb6-072401d272d6，本体 ID 为 frepple-d7eb98078882b234c395fd05。复用了既有 Study、基础视图与 data/frepple_demo/whatif_synthetic_manifest.json 的固定 profile；没有重导数据或直接改数据库。2026-10-22 通过现有 Action/Run API 提交 B、C，B 自动触发兼容 Baseline。三个 Run 均完成七个持久阶段，并有独立 result view、demand_impacts artifact 和输入/输出 digest。浏览器刷新后，模型表与 Compare 仍显示结果。

| Case | Run ID | 准时率 | 采购成本 | 缺货数量 |
|---|---|---:|---:|---:|
| Baseline | f2b048f3-08a9-451e-8897-2ba26ee56c76 | 62.5% | 22500 cost units | 0 |
| Supplier B | f000a347-b1f1-4a55-8bc2-a5268a8b61aa | 43.75% | 10260 cost units | 220 |
| Supplier C | a55a8a39-8939-4b82-b676-00c021ad7cb3 | 68.75% | 14100 cost units | 0 |

这些是 frePPLe 制造样例和项目合成供应商 profile 的结果；cost units 不是美元。2026-10-22 逐个重启本地 backend、Celery worker 和 FalkorDB 后，health 恢复为 ok；三个原 Run ID、result view、输入/输出 digest 和上述指标保持不变，B/C 图仍分别返回 308 个对象、329 条关系。浏览器再次刷新后 Compare 仍显示 B/C 相对基线的差异。这证明当前本地栈的这次恢复验收，不代表所有环境的长期可用性或第 15 节全部 Gate 已通过。演示按 [逐步操作稿](DEMO_SUPPLIER_SCENARIO_WORKBENCH.md) 保持独立，不把本次恢复误写成三类数据的业务闭环已完成。

### 2.3 2026-10-22 新增：小样例决策分析切片

在 `palantir` 分支新增 `/decision-lab`：AI4I 维护优先级、FactoryNet CNC 速度偏差、I-BADAS 证据预算三个独立本体和九个预置计算 Run。每个用例可更改阈值或预算、保存新 Run、比较指标、打开对象与来源证据；木材 Study 仍从原 `/what-if` 进入。三条示例的共同能力是固定来源版本、规则公式、方案对比、输入/输出摘要和来源行追溯。它们只做分析，不执行生产动作。

| 演示 | 来源与取样 | 三方案权衡 | 当前已验证的预置结果 |
|---|---|---|---|
| AI4I 维护优先级 | [UCI AI4I 2020](https://archive.ics.uci.edu/dataset/601/ai4i%2B2020%2B) 的 29 条有意分层记录；下载归档后仅保留切片 | 规则阈值 25/40/60；复核工作量与切片内官方故障标签覆盖 | 复核 18/8/4 条，覆盖 11/7/4 个故障标签 |
| FactoryNet CNC | 已安装、哈希固定的 [FactoryNet CNC](https://huggingface.co/datasets/Forgis/FactoryNet) `cnc_000.parquet`；4 个 episode 各等距取 64 点 | 速度跟踪偏差阈值 0.25/0.5/1.0；告警与人工复核量 | 告警 22/14/5 点，需复核 4/2/1 批次 |
| I-BADAS | 已安装的 [I-BADAS](https://huggingface.co/datasets/moiaraya/i-badas) 12 组 RGB、深度、掩码、点云样本；不复制大媒体 | 同一 12 MB 预算下，RGB/RGB+深度/四模态覆盖 | 可复核 12/6/4 组，传输 10.503/10.971/11.095 MB |

三个演示在 PostgreSQL 留有 DatasetVersion、Ontology、ConstructionRun、EvidenceRef 与计算 Run；FactoryNet 和 I-BADAS 的媒体/来源由现有对象存储保存。来源页面、选样规则、SHA-256 和限制见 `backend/data/decision_lab/manifest.json`。AI4I 是合成数据，故障切片刻意分层；FactoryNet 是离散下采样，不是连续故障检测；I-BADAS 的异常标签仅用于回看统计。此演示不构成预测模型准确率、Action 审批写回、Agent 持久会话或平台级治理的验收。FactoryNet CNC 子集依原始 UMich CNC 来源条款，当前产品页面明确标记条款待核验，不能复用其他子集的许可证标注。

本地单用户 Docker Compose 的 25 次顺序预热 GET 记录在 `backend/evidence/decision_lab_local_perf_2026-10-22.json`：目录接口 p95 为 26.41 ms，三组 Run 详情接口 p95 为 6.13–9.13 ms。该记录只验证当前小样例的本机响应，不代表并发吞吐或生产环境性能。自定义 Run `71812f00-4373-464c-8026-41b8befd8d13` 在 backend 重启后仍可回读，用于证明这次本地持久化路径；并不替代跨环境恢复验收。

## 3. Palantir 对照能力清单与产品优先级

“现状”以当前代码审计为基础。P0/P1/P2 是本项目优先级，而不是 Palantir 官方路线。每项的完成条件由后文需求与阶段门槛进一步定义。

| ID | 能力 | 现状 | 本项目目标与完成信号 | 优先级 |
|---|---|---|---|---|
| G01 | 资源/项目/用例目录 | 主要是本地资源和固定工作台 | 统一 Resource ID、owner、revision、依赖、发布状态；可按业务用例导航 | P1 |
| G02 | 连接器生命周期 | 文件/DB/REST 接入有基础 | 连接测试、凭据引用、schema 漂移、checkpoint、失败重试、回填 | P2 |
| G03 | CDC 与持续同步 | 动态演示不等于通用 CDC | 可接来源变更流，处理重复、断点、重放和一致性 | P2 |
| G04 | 数据质量/新鲜度 | 构建与审计局部存在 | 每个 DatasetVersion 有可查询规则、结果、freshness、owner、阻断策略 | P1 |
| G05 | 跨资源血缘 | source manifest/Evidence 有基础 | 从源字段追到对象、指标、Scenario、Action、应用及下游影响 | P1 |
| G06 | Canonical Ontology 类型 | v2 semantic core 与旧 Entity/Relation 并存 | Object/Link/Interface/Value/Struct/Action/Function 稳定 ID、版本和依赖 | P0a |
| G07 | 对象后端与索引 | Object Query、Object Set、PG/Falkor 适配存在 | 权威事实、投影、查询、修订和重建边界统一；有规模证据 | P1 |
| G08 | Object Explorer | 后端丰富，前端入口不足 | 搜索、过滤、pivot、compare、save、Action、导出可被业务用户发现 | P1 |
| G09 | Object View | 对象面板与详情存在 | full/panel 一致复用，属性、链接、证据、时间线、Action、历史合一 | P1 |
| G10 | 图工作台 | 关系图、动态图、时序图存在 | Search Around、时间擦洗、图层、保存、跨页上下文同步 | P1 |
| G11 | 可复用分析 | 图表、聚合、供应商指标存在 | 保存的指标定义、公式、时间分析、钻取和来源可复用 | P1 |
| G12 | 应用编排 | 目前多为手写 React 工作台 | 配置化模块、输入输出绑定、版本、预览和发布 | P2 |
| G13 | 统一 Action Runtime | live 与 Scenario 分路径 | 同一 preview/权限/审核/执行/日志/失败语义覆盖 UI、API、Agent、Scenario | P0a |
| G14 | 版本化 Function | Logic Asset/确定性执行存在 | 类型签名、依赖、发布、运行资源限制、评估与监控 | P1 |
| G15 | Scenario 协作 | pinned/revision/compare 强，tracking 弱 | tracking、rebase、conflict、merge、清晰写回与恢复 | P1 |
| G16 | Automation | 局部任务调度和阶段运行存在 | 可管理的触发器、条件、工作队列、通知与受控 Action | P2 |
| G17 | Agent 持久会话 | AgentRun 单次记录存在 | Session/Context/Event/Checkpoint/等待审核/恢复/工具注册 | P1 |
| G18 | AI/Agent 评估 | 局部 trace/benchmark 存在 | 固定评估集、权限越权、证据充分性、工具回归与成本记录 | P1 |
| G19 | 平台观测 | 健康端点、任务和基准存在 | 统一 trace/log/metric、P95、告警和跨服务关联 ID | P1 |
| G20 | 细粒度治理 | 权限投影与 digest 存在 | 查询、图、证据、Agent、Action 贯穿对象/字段策略和审计 | P0a→P2 |
| G21 | SDK/外部接口 | 内部 v2 API/client 存在 | 稳定 API、TS/Python SDK、Webhook/MCP、版本兼容说明 | P2 |
| G22 | 交付与协作 | 固定页面与导出存在 | 用例包、资源依赖、环境提升、回滚、共享/注释 | P2 |
| G23 | 质量复核/标注 | 多模态资产展示存在 | 人工审核、争议处理、样例证据与标签来源分离 | P0b→P1 |
| G24 | 事件时间语义 | 事件/水位/有效区间有基础 | 迟到与乱序策略、快照/增量一致性、可回放处置 | P0b→P2 |
| G25 | 时序维护分析 | episode/time_s 与 replay 有基础 | 可解释规则、时间窗口、维护复核 Action；预测模型可选 | P0b→P2 |
| G26 | 约束优化与推荐 | 供应商固定模型存在 | 明确目标、约束、可行性、权衡和人工采纳；不自动称“最优” | 研究候选 |
| G27 | 数字孪生/现场连接 | Ontology 能表达设备，未连接物理控制 | 区分 reported/desired/approved state，只有在真实设备需求成立时立项 | 研究候选 |
| G28 | 地图/空间分析 | 当前非核心 | 供应链地点、物流风险、空间检索的明确用例后立项 | 研究候选 |

官方对照入口：[Object Explorer](https://www.palantir.com/docs/foundry/object-explorer/overview/)、[Object View](https://www.palantir.com/docs/foundry/object-views/config-overview)、[Workshop](https://www.palantir.com/docs/foundry/workshop/overview)、[Ontology query compute](https://www.palantir.com/docs/foundry/ontologies/query-compute-usage)、[Observability](https://www.palantir.com/docs/foundry/observability/overview)。以上表格是基于公开能力和本项目代码的差距判断，不是对 Palantir 产品完整覆盖范围的承诺。

## 4. 产品结构与共用语义

### 4.1 用户可见的主路径

1. 从业务问题、数据集、设备 episode、事件或多模态样例进入。
2. 定位一个稳定对象或对象集，查看关系、时间和证据；每个数据面板标来源版本和权限范围。
3. 查看可解释规则、指标或情景结果；候选建议明确假设和不确定性。
4. 发起 Decision Case，预览拟修改对象、字段、理由和影响。
5. 审核人批准、驳回或退回；只有批准且权限/修订复核成功才执行 Action。
6. 结果返回对象视图；用户能追溯原始证据、审核者、执行者、前后状态和失败/补偿。

### 4.2 资源与存储责任

| 层 | 权威内容 | 不承担的责任 |
|---|---|---|
| PostgreSQL | 资源元数据、修订、事件/事实、Decision Case、Action Run、审核/审计与引用 | 大体量原始媒体和每个图布局 |
| FalkorDB | 对象关系、动态/Scenario 图投影和探索查询 | 唯一权威事实或不可重建审计 |
| MinIO | 媒体原件、可验证派生文件、对象校验值 | 业务状态机 |
| Chroma/可选检索层 | 检索索引与候选召回 | 原始来源、权限决定或写回事实 |
| Redis/Celery | 异步任务派发和进度 | 唯一持久决策记录 |

所有跨存储读写至少要有稳定资源 ID、来源版本、可重试请求 ID 和状态；投影失败时显示“待同步/失败”而非成功幻象。不能因为图暂时不可用而丢掉 PostgreSQL 中的业务审计。

### 4.3 P0 共用数据合同

| 合同 | 必含字段 | 目的 |
|---|---|---|
| ObjectRef | ontology_id、object_type、object_id、revision/context | 跨页面、运行和 Action 指向同一对象 |
| EvidenceRef | source_id、dataset_version_id、locator/asset_id、digest、采集或事件时间、可见性 | 使结论可回到原始证据 |
| DecisionCase | id、kind、target_refs、evidence_refs、建议、假设、状态、发起人、审核人、创建/更新时间 | 表示“待决定的业务事项”，不与 Action Run 混淆 |
| ActionRequest | action_type/version、target、typed parameters、ExecutionContext、expected_etag、client_request_id、reason | 固定执行意图和幂等身份 |
| ActionPreview | permission_decision、validation、before/after diff、受影响对象、side-effect 分类、可撤销性 | 让审核人批准具体效果 |
| ActionResult | run_id、status、result_manifest、before/after digest、audit/event refs、error/recovery_hint | 可验证执行与失败 |
| ResultManifest | 输入版本、规则/模型版本、输出引用、digest、权限摘要、运行 trace ID | 统一数据/分析/Agent/Action 的来源链 |

DecisionCase 是拟新增的业务记录；尽量扩展现有 OntologyActionRun 和 v2 ExecutionContext/ResultManifest，而不是建立第二套 Action 真相。对外 API 的已有路径保留兼容入口，内部归并到共同服务。正式字段命名以 P0a 类型合同、迁移和兼容评审为准；本文定义不可缺失的语义。

## 5. P0：三类数据的完整闭环

### 5.1 多模态 I-BADAS：证据驱动的质量复核

**入口**：I-BADAS 样例列表与样例 Evidence Workspace。现有 RGB、深度、掩码、点云和元数据同步显示是输入，不等于系统已经识别了缺陷。

**用户故事**：质量复核员看到某个样例的媒体与标签，检查来源和模态完整性，记录“待确认问题/无问题/证据不足”；审核人批准或驳回后，系统把复核状态及理由写回样例关联的业务对象，保留原标签和资产不变。

| ID | 要求 | 可验收表现 |
|---|---|---|
| MM-01 | 同一 sample_id 绑定各模态资产、来源文件、digest 和数据集版本 | 切换 RGB/深度/掩码/点云不改变样例身份；缺失资产明确标记 |
| MM-02 | 把原始标签、规则建议、模型候选、人工结论分层显示 | 页面和 API 不能把候选描述写成已核验事实 |
| MM-03 | 复核员可框选或引用媒体区域、点云视角或整个样例作为证据 | DecisionCase 保存 asset_id 与定位参数；无法显示的媒体仍保留来源链接和错误原因 |
| MM-04 | 创建质量复核事项，可填写结论、理由、严重度和待补证据 | 审核前不更改样例的正式复核状态 |
| MM-05 | 审核人查看前后差异并批准、驳回、退回 | 驳回只记录审核结果；批准后经 Action 写回状态和 evidence_refs |
| MM-06 | 处理重复提交、资产版本变化、权限不足 | 不重复写回；过期版本阻止执行并提示重新预览 |
| MM-07 | 对象视图显示复核历史与原件/派生件链 | 可从业务结论回到资产、版本和责任人 |

P0 使用规则或人工输入生成待复核事项，不以付费视觉模型作为完成前提。P1 可研究多人复核、争议仲裁、抽样质控和主动学习。CVAT 的[人工复核与质量控制](https://docs.cvat.ai/docs/qa-analytics/quality-control/)提供可借鉴模式，但其某些质量分析功能有版本/版本线差异；本文只借鉴“任务—标注—复核—质量结果”的模式，不承诺集成 CVAT。

### 5.2 动态 FactoryNet：逐事件处置闭环

**入口**：动态数据工作台或本体的动态演化页。当前模拟实时数据仍来自文件回放，不能称作已接真实设备。

**用户故事**：值班员逐事件观察设备/关系状态与有效区间，系统按已发布确定性规则产生待处理事件；值班员核对来源事件、水位、当前对象状态，创建处置事项；审核后把确认/忽略/处置状态写回业务对象，原始事件流不可修改。

| ID | 要求 | 可验收表现 |
|---|---|---|
| DY-01 | 事件记录 event_key、source_cursor、event_time、ingest_time、ordinal、source_ref 与状态 | 重复 push/step 不生成第二个事实或第二次 Action |
| DY-02 | 图中对象/关系可按时间点查看有效区间、来源事件和 EvidenceRef | 选中边可回到事件与原始行；状态不混用其他 run |
| DY-03 | 规则触发必须记录规则版本和触发输入 | 用户能看见为何触发，也能将误报标为驳回 |
| DY-04 | DecisionCase 提供待确认、确认、忽略、处置中、已处置语义 | Action 写回的是处置状态，不是改写原事件 |
| DY-05 | 暂停、单步、恢复、断线重连后保留水位和事项 | 重启后不重复触发已完成业务效果 |
| DY-06 | 明确迟到、乱序和缺失事件的显示与策略 | P0 固定样例可拒绝或标记乱序；P2 再扩展通用事件时间窗口 |
| DY-07 | 动态事件可跳到对象视图、时序证据和 Action 日志 | 同一 run_id/episode_id 在页面之间传递 |

Apache Flink 将[事件时间、状态和快照](https://nightlies.apache.org/flink/flink-docs-stable/docs/learn-flink/overview/)作为持续流处理的核心，并用[水位处理乱序](https://nightlies.apache.org/flink/flink-docs-stable/docs/dev/datastream/execution_mode/)；这是 P2 通用事件平台的设计参照。P0 不为固定 FactoryNet 演示引入 Flink 集群。

### 5.3 时序 FactoryNet：设备维护复核闭环

**入口**：时序构建与 replay 页。时间轴和图显示既有观测；设备维护判断要明确规则、阈值和人工责任。

**用户故事**：工程师选 episode 和时间窗口，查看传感器序列、工序/设备关系、异常变化与原始行；在证据充分时发起“维护检查建议”；审核人批准后，系统通过 Action 写回维护复核事项与状态。此动作不直接派发外部工单或停机指令。

| ID | 要求 | 可验收表现 |
|---|---|---|
| TM-01 | 统一 episode_id、time_s/ordinal、数据集版本与对象身份 | 相同窗口回放结果可复现，图与曲线选择同一对象 |
| TM-02 | 时间窗口分析显示输入范围、统计/规则版本和缺失区间 | 不把阈值越界称作真实故障 |
| TM-03 | 能从时间点/片段回到原始 source_row_id 和关系证据 | 多次重放与批次追加不丢定位 |
| TM-04 | 维护复核事项包含设备、窗口、风险描述、证据、建议动作、假设 | 审核前不改变正式维护状态 |
| TM-05 | 审核后只写回本地维护决策/检查状态 | 不自动控制设备；审核和执行记录可查询 |
| TM-06 | 对重叠窗口、重复建议和版本变化去重或标冲突 | 相同 episode/窗口/规则版本不会悄悄产生多个执行效果 |
| TM-07 | 失败批次、断点续跑与图投影缺失可恢复 | PG 事实保留，投影修复后对象视图可回读 |

P1 可增加时序特征/模式库与维护策略比较；P2 才在有标签与评估集时研究预测性维护。不能用固定回放结果证明真实生产预测能力。

### 5.4 木材供应商情景作为横向对照

木材 Study 已能展示 Baseline/B/C、ChangeSet、独立 revision、七阶段运行、需求影响和证据。它在 P0 承担共同 Context、Action、权限与审计的回归夹具；三类数据入口的验收仍需各自完成。供应商 B 的低成本伴随缺货和准时率下降，C 的准时率更高而成本高于 B，因此推荐卡必须显示权衡与约束，不把一个指标的最优当整体最优。

### 5.5 跨三线统一的 DecisionCase 生命周期

| 状态 | 谁触发 | 允许的动作与后果 |
|---|---|---|
| draft | 发起人/规则 | 编辑理由、证据、目标；无业务写回 |
| ready_for_review | 发起人 | 冻结候选参数与预览摘要，等待审核 |
| approved / rejected / changes_requested | 审核人 | 记录审核者、时间、意见和被审核的 preview digest |
| executing | Runtime | 重检权限、目标版本、条件及幂等键；持久化执行状态 |
| completed / failed | Runtime | 保存结果或可操作错误；失败不宣称已完成 |
| reverted / compensated | 授权操作人 | 只在该 Action 的效果支持时出现；保留原执行与逆向记录 |

审核通过后若对象修订、规则版本、权限摘要或证据版本变化，既有批准失效，必须重新预览并复核。审核人和执行人分开记录。正式多用户部署默认由 editor 提交、admin 或被明确授权的审核人批准；本地单用户演示可以由 admin 同时完成两步，但日志必须标明同一身份。这是 P0 的产品策略，不能通过前端按钮隐藏服务端检查。

## 6. P0a：统一 Action Runtime 与安全写回

### 6.1 单一执行路径

UI、API、Scenario 与 Agent 都创建同一 ActionRequest，经过同一执行核心。Scenario 通过 ExecutionContext 指定隔离目标，不能调用一条权限更宽的旁路。现有 logic_actions.py 路由、scenario_actions.py 编译器及 supplier_studies.py 的固定案例接口暂保留作为兼容适配器；底层编译结果统一为带类型的 EditBatch。迁移期间同一请求只允许由一个执行核心提交。

**服务端顺序**：读取 ActionType 的发布版本 → 解析 Context 和对象 → 检查可见性/提交权限/审核策略 → 校验参数和 submission criteria → 编译 EditBatch → 生成预览与受影响对象 → 提交审核 → 执行时重新校验修订、权限与 preview digest → 原子保存权威事实和运行日志 → 派发图/搜索投影 → 返回 ResultManifest。外部 side effect 接口在 P0 只定义契约并关闭。

| ID | 要求 | 完成条件 |
|---|---|---|
| AR-01 | ActionType 统一类型化参数、目标、条件、权限、效果、版本/发布状态 | 无效/未知 effect 必须拒绝；不能静默 skipped |
| AR-02 | Preview 不改变业务状态 | 返回 before/after、被修改对象、风险、权限判定、可撤销性、预计副作用 |
| AR-03 | 审核绑定 preview digest、参数、目标修订和权限摘要 | 审核后内容变化必须重审 |
| AR-04 | 客户端请求编号在 ActionType + Context + 发起人范围内唯一 | 同 key 同输入返回同 Run；同 key 不同输入为冲突 |
| AR-05 | 对象级 expected_etag/修订比较 | 并发修改返回冲突和当前版本；不覆盖他人变更 |
| AR-06 | 单次本地权威事实写入与 ActionRun/audit 同事务 | 部分失败不能出现“对象已改但无执行记录” |
| AR-07 | 图/索引投影异步一致且可 reconcile | 投影失败显示待同步；修复后与 PG 事实一致 |
| AR-08 | Revert/compensation 能力按 effect 类型声明 | 只对已支持的可逆效果开放；其余显示人工补偿步骤 |
| AR-09 | 运行日志包含来源、审核、前后摘要、调用者、版本、trace ID | 从对象、DecisionCase、Scenario、Agent 均可追到同一 Run |
| AR-10 | 查询和执行权限必须服务端检查，审计输出按同一策略脱敏 | 无权字段不通过 diff、日志、Agent 工具结果泄露 |

Palantir 的 [Action 权限](https://www.palantir.com/docs/foundry/action-types/permissions/)和[Action 日志](https://www.palantir.com/docs/foundry/action-types/action-log/)表明这是一等业务写回边界；本文规定 Project 4 自己可实现的 P0 合同，不承诺复制其全部一致性或外部系统能力。

### 6.2 API 形状与兼容边界

| 阶段 | 建议的 v2 资源接口 | 响应核心 |
|---|---|---|
| 创建/读取事项 | /api/v2/ontologies/{ontology_id}/decision-cases | Case ID、状态、对象/证据引用、版本 |
| 预览 | /decision-cases/{case_id}:preview | diff、权限决定、冲突、preview digest |
| 提交审核 | /decision-cases/{case_id}:submit | 审核快照、状态、幂等结果 |
| 审核 | /decision-cases/{case_id}:review | 批准/驳回/退回、审核人、意见 |
| 执行 | /decision-cases/{case_id}:execute | ActionRun ID、运行状态、结果引用 |
| 查询历史 | /decision-cases/{case_id}/events | 只追加状态/审核/执行事件 |
| 现有 Action 入口 | /actions/{action_id}/preview 与 /run | 兼容旧调用，映射到同一内部核心 |

接口仅在 P0a 合同评审后固化 OpenAPI 与生成的前端类型。HTTP 冲突语义：403 权限不足；404 不可见/不存在；409 版本或幂等冲突；422 参数/规则不满足；503 依赖暂不可用。请求都携带 client_request_id 和 expected_etag，写操作返回可查询资源 ID。不能只靠 200/202 推断业务已完成。

### 6.3 迁移与回退

1. 先做新数据合同与旧路由适配，不原地重写历史 ActionRun 和 ScenarioRun。
2. 为现有 Action/Scenario Run 补只读统一展示映射；旧记录缺失的审核/来源字段显示“历史记录未提供”，不填伪值。
3. 新执行核心按用例开关逐步接入木材、质量、动态、时序入口；每次切换先验证同一输入的预览/结果摘要。
4. 保留数据库迁移的前进/回退路径和旧路由；回退不能删除已生成审计事实。跨存储投影通过 reconcile 修复。

## 7. P1：对象工作台、分析与 Scenario

### 7.1 对象探索到业务动作

Object Explorer 应成为跨三线的统一起点：关键词搜对象 → 可视化过滤 → 沿链接 pivot/Search Around → 保存动态 Exploration 或静态 List → 比较对象集 → 打开统一 Object View → 发起 DecisionCase 或 Scenario。Object View 的 full/panel 版本共享对象身份、权限、证据、Action 和历史组件；图选择、曲线选择和媒体选择都能深链到该对象。

| ID | 要求 | 验收 |
|---|---|---|
| OE-01 | 全局搜索与类型、属性、时间、来源过滤 | 非开发者不写 JSON，也能找到供应商、设备或多模态样例 |
| OE-02 | 链接 pivot/Search Around 有路径、方向和结果数量 | 从设备到 episode、从供应商到订单可解释，不无限制全图展开 |
| OE-03 | 动态 Exploration 保存查询定义，静态 List 保存明确成员 | 来源修订变化时两种保存语义不同且清晰 |
| OE-04 | Compare 保留单位、指标定义和权限差异 | 不把缺失值或无权值当 0 |
| OV-01 | Full/Panel 共用字段、关系、证据、时间线、Action、审计 | 从三条入口打开同一对象，数据与身份一致 |
| OV-02 | 内容按权限投影 | 隐藏字段在列表、导出、图提示和日志中同样不可见 |
| AN-01 | 可保存指标与分析资源，绑定输入 Object Set、公式、单位和版本 | 指标值可回到来源、计算逻辑和运行 |
| AN-02 | 时间序列、变化点、需求影响与权衡图支持钻取 | 图表不是孤立图片，可继续进入对象和 Action |

Object Explorer/View 与 Workshop 的[公开应用模式](https://www.palantir.com/docs/foundry/ontology/applications)用于定义产品目标；P1 先做可复用工作台模块，P2 再做可配置应用编排。借鉴 [Apache Superset 的交互探索与保存图表](https://superset.apache.org/docs/using-superset/exploring-data/)时，应保留 Project 4 的对象、权限与来源语义，而不把普通 SQL 图表直接冒充 Ontology 分析。

### 7.2 Scenario 协作与结果语义

当前 pinned/reproducible 研究保留为强项。P1 新增 tracking 模式时必须明确数据更新是否影响 Scenario、如何 rebase、冲突如何由人处理。Scenario 的 Action 只写隔离视图；从 Scenario 合并到 live 要独立预览、审批和审计，不能把“模拟成功”理解为已执行。

| ID | 要求 | 验收 |
|---|---|---|
| SC-01 | pinned 与 tracking 模式显式区分 | 页面、API、导出均标模式和基线修订 |
| SC-02 | ChangeSet、Revision、Run、Artifact 稳定链接 | 从指标回到方案参数、模型/规则版本和对象差异 |
| SC-03 | rebase 产生可读冲突清单与建议处理 | 不自动覆盖其他人的 live 修改 |
| SC-04 | merge 前重新权限/条件/修订检查 | 审批记录绑定当前 preview digest |
| SC-05 | 多 Case 对比保留单位、计算版本和数据时间 | 不兼容结果不能并排伪比较 |
| SC-06 | 失败、取消、重跑和恢复保留历史 | 不能用新 Run 覆盖旧审计事实 |

P1 可探索多目标供应链优化，但先让“可行性、成本、交付、库存、影响客户”分别可解释。若引入约束求解器，须展示输入约束、不可行原因和人类最终选择；[OR-Tools 的约束求解](https://developers.google.com/optimization/cp/)仅是潜在技术参考。

## 8. P1：Agent 持久会话与 AI 评估

### 8.1 产品定位

Agent 首先是受限的对象/情景助手：帮助检索、解释、比较和起草候选 Action。任何写回都必须进入同一 DecisionCase 与 Action Runtime。它不能通过直接数据库工具绕过审核。用户可暂停、修改上下文、拒绝建议、继续任务，并查看完整工具链。现有 AgentRun 应成为 Session 下的一次运行，而不是 Session 的替身。

| 资源 | 关键字段/职责 |
|---|---|
| AgentSession | id、owner、ontology、状态、权限上下文、创建/最后活动时间、会话版本 |
| AgentContextItem | typed object/evidence/scenario/query refs、来源版本、可见性、加入/移除事件 |
| AgentEvent | user_input、tool_call、tool_result、answer、proposal、approval_wait、error、resume 等顺序事件 |
| AgentCheckpoint | 可恢复状态、事件游标、工具幂等键、模型/提示/规则版本 |
| ToolRegistry | 工具签名、所需权限、输入输出 schema、副作用级别、超时、版本 |
| AgentRun | 一次任务执行状态、输入/输出摘要、trace、成本；关联 Session |

推荐状态：idle → running → waiting_for_review → resumed → completed；另有 failed、cancelled。服务重启后从持久检查点恢复，已产生副作用的工具必须用幂等键，不重放写回。并发会话操作要有明确冲突策略；P1 默认同一 Session 同时只允许一个活动 Run，后续请求排队或显式拒绝。

| ID | 要求 | 验收 |
|---|---|---|
| AG-01 | 多轮会话与 typed context 持久化 | 刷新、重启后可继续，引用不会变成无来源自由文本 |
| AG-02 | 工具白名单与细粒度权限 | Agent 只得到用户可访问对象/字段；工具运行前再次授权 |
| AG-03 | Context budget 与摘要保留来源 | 压缩上下文后仍能定位依据，不能用摘要解除权限 |
| AG-04 | 等待人工审核与 resume | Action 候选等待审批；驳回能反馈给同一会话 |
| AG-05 | 事件/检查点回放 | 可解释每次检索、工具结果、建议和失败 |
| AG-06 | 失败恢复与幂等 | worker 重启不重复调用写入工具 |
| AG-07 | 模型/提示/工具版本钉住 | 结果能复现其配置边界；无模型时明确“等待配置” |
| AG-08 | 会话管理页 | 用户可搜索、查看、暂停、取消和审计本人有权会话 |

[Palantir Agent Stack](https://www.palantir.com/docs/foundry/aip/agent-stack)展示 Session、Context、Event、State 的产品方向；[LangGraph 持久化和检查点](https://langchain-ai.github.io/langgraph/concepts/durable_execution/)及[人工中断](https://langchain-ai.github.io/langgraph/concepts/breakpoints/)说明可借鉴的恢复模式。是否引入 LangGraph 要在本地持久性和现有 Celery/PG 方案的 PoC 中裁决；不因为“有框架”就默认满足 Project 4 的权限与 Action 需求。

### 8.2 评估与成本

先从可免费运行的确定性、规则及固定数据集开始。评估集按四类组织：对象定位、证据充分性、权限边界、Action 候选正确性；再加故障恢复和上下文变更。每个 case 保存输入、允许资源、期望对象/证据、不可接受行为、人工判定与版本。Agent 的回答须提供 object_refs、evidence_refs、假设、不确定性和候选 Action；没有足够依据时明确拒答或请求更多证据。付费模型与学校 GPU 的使用由用户另行授权。

| ID | 要求 | 验收 |
|---|---|---|
| EV-01 | 至少覆盖三类数据和木材场景的固定回归问题 | 版本升级前后可比较对象命中、来源、结论与耗时 |
| EV-02 | 越权与提示注入用例 | 不读取无权字段、不因文本指令绕过 Action |
| EV-03 | 工具链与 Agent 输出可追踪 | 每一步有 trace ID、输入摘要、输出摘要和错误 |
| EV-04 | 人工反馈进入评估集而非自动改变事实 | 争议样本可复核，保留历史版本 |
| EV-05 | 成本与预算可见 | 模型、token、时间、工具调用数和失败重试分别计量 |

[MLflow 的 trace 评估](https://www.mlflow.org/docs/latest/genai/eval-monitor/running-evaluation/traces/)可用于研究“已有运行轨迹再评分，避免重复付费推理”的方法；它是候选评估底座，不是本 PRD 的强制依赖。

## 9. 横向治理、观测与数据可信

### 9.1 P0 必须贯穿的治理底线

| ID | 要求 | 验收 |
|---|---|---|
| GV-01 | 统一主体：用户、角色、资源、对象、字段、Action、Session | 同一身份在查询/图/Agent/Action 得到一致可见性 |
| GV-02 | 权限决定包含 allow/deny、理由、策略版本、permission digest | deny 覆盖 allow；前后端不得各自推断 |
| GV-03 | 所有写回、审核、拒绝和失败有追加审计事件 | 可由对象或 Case 反查；不能因失败丢记录 |
| GV-04 | 证据、运行、结果和日志按来源权限脱敏 | 无权者不能从导出、日志、提示、模型上下文旁路获取字段 |
| GV-05 | 结果显示 source/version/status | 无来源、过期或部分失败的结果有醒目标记 |
| GV-06 | 跨服务 correlation/trace ID | 从 API 请求追到 Celery、PG、Falkor 投影和页面错误 |

本地单用户模式只是演示部署，不代表平台级多用户治理已完成。P1/P2 再增加 group、service identity、用途限制、敏感分级、资源级配额、保留/删除和跨环境发布策略。细粒度关系授权可研究 [OpenFGA](https://openfga.dev/) 的建模方式，但 P0 优先使用现有授权服务扩展并证明全路径生效；不能同时维护两个互不一致的策略真相。

### 9.2 血缘与数据健康

血缘的最小路径是 Source → DatasetVersion → Transform/Construction Run → Ontology Revision/Object/Evidence → Analysis/Scenario → DecisionCase → ActionRun → App/Export。每条边记录生成方式、版本、时间与可见性。用户从“为什么这个结论是 43.75%”可以反向定位模型版本、输入 manifest、Run 和需求明细；从源数据 schema 变化可以正向列出可能影响的对象、指标、Action 和页面。

| ID | 要求 | 验收 |
|---|---|---|
| LN-01 | DatasetVersion 与 Run 的输入输出 lineage 事件 | 重跑不覆盖旧血缘；失败 Run 也有可查状态 |
| LN-02 | 字段/对象/指标/Action 依赖索引 | 变更前可查询影响范围与 owner |
| LN-03 | 媒体、动态事件、时序行的定位信息 | 决策可回到原件、事件或数据行 |
| DH-01 | 数据质量规则覆盖完整性、唯一性、schema、freshness | 规则版本与运行结果可见，可设阻断或仅告警 |
| DH-02 | 连接与任务健康：最近成功、失败、积压、延迟 | 管理员可区分“无数据”与“源故障” |
| DH-03 | 质量问题反馈到受影响对象/应用 | 可追踪修复、重跑和结果恢复 |

[OpenLineage 的 Job/Run/Dataset 模型及 Facet 扩展](https://openlineage.io/docs/1.53.0/spec/facets/)、[DataHub 的下游影响分析](https://docs.datahub.com/docs/act-on-metadata/impact-analysis)、[Great Expectations 的可执行数据断言](https://docs.greatexpectations.io/docs/core/define_expectations/)都是可借鉴的建模或工具候选。P0 先实现本项目最小血缘和质量事件，不把引入整套元数据平台作为三条闭环的前提。

### 9.3 可观测性与事故恢复

每次 API/Run/Action/Agent 调用传播 trace_id；记录耗时、状态、错误代码、重试次数、队列等待、投影延迟、权限拒绝、模型使用/成本。日志不落入原始敏感资产或完整提示文本，必要时保存经过权限过滤的摘要。P1 提供按对象、Run、Case、Trace 搜索的运营视图，P2 增加阈值告警和资源配额。[OpenTelemetry](https://opentelemetry.io/docs/)提供 traces、metrics、logs 的中立规范；[Palantir Observability](https://www.palantir.com/docs/foundry/observability/overview)作为产品覆盖面参照。

| 故障 | 用户看到什么 | 系统恢复要求 |
|---|---|---|
| PostgreSQL 事务失败 | Action 失败，未写回 | 保留错误与审计；可安全重试 |
| FalkorDB 不可用 | 权威事实可查，图标记待同步 | 恢复后 reconcile；不能谎称图已更新 |
| Worker 中断 | Run queued/running 有超时/重试状态 | 幂等继续，不重复业务效果 |
| 媒体资产缺失/损坏 | 显示缺失模态和来源信息 | 不伪造空图；允许补证据 |
| 数据源 schema 漂移 | 来源健康告警、受影响资源清单 | 暂停不安全构建，人工裁决映射 |
| 模型不可用或超预算 | 规则/人工流程仍可使用 | 不自动切换到付费或低可信模型 |
| 审核后对象变化 | 预览失效、409 冲突 | 重新预览与审核，不能直接执行 |

## 10. P2：平台化能力与可扩展生态

### 10.1 连接、调度与自动化

统一 Connection/Source 合同应覆盖文件、对象存储、关系库、REST、事件/流和未来企业系统适配。一个连接资源包含 owner、secret reference、访问范围、schema digest、运行模式、检查点、最新成功/失败、重试/回填策略和成本归属。明文凭据不能写入 PRD、前端状态或运行日志。CDC/流式引入前先把快照与增量的切换、重复事件和来源 offset 写成可验收合同；[Debezium 的 CDC 架构](https://debezium.io/documentation/reference/stable/architecture.html)提供设计参考，但并不意味着 P2 必须采用 Kafka。

| ID | 能力 | 交付门槛 |
|---|---|---|
| CN-01 | 连接目录与健康 | 测试连接、schema 漂移、owner、最近成功/失败可见 |
| CN-02 | 持续同步/CDC | 快照、增量、offset、重放和重复处理有恢复演练 |
| CN-03 | 任务编排 | 构建/质量/索引/回填依赖可视，失败只重跑受影响阶段 |
| AU-01 | 事件触发 Automation | 条件、作用域、执行身份、频率上限和禁用开关 |
| AU-02 | 审批队列与通知 | 通知只指向 Case；执行仍由 Action Runtime 负责 |
| AU-03 | 工作流版本与迁移 | 正在等待审批的任务在版本升级后可继续或安全取消 |

当前 Celery 适合现有异步 Run；只有当长生命周期、复杂等待/恢复、跨服务补偿的需求及成本收益经过 PoC 证明，才考虑 [Temporal 的持久工作流](https://docs.temporal.io/workflows)等运行时。引入新编排器不能让 Scenario、Agent 与 Action 再长出第三套审核语义。

### 10.2 应用编排与交付

P2 目标不是制作一个无限制低代码编辑器，而是把已稳定的对象列表/视图、图、指标、时间线、证据、DecisionCase 和 Agent 面板作为版本化组件。应用 Module 声明输入输出、权限、依赖资源、导航参数、发布版本和迁移策略。供应链、设备维护、质量复核三个用例应由同一组件库组合，业务差异通过类型与配置表达，避免复制三套前端页面及后端专用逻辑。

| ID | 能力 | 交付门槛 |
|---|---|---|
| AP-01 | 配置化 Module 合同 | 输入/输出类型、默认状态、错误状态和权限校验 |
| AP-02 | 草稿、预览、发布、回滚 | 用户可比较配置版本和依赖变化 |
| AP-03 | 跨模块上下文传递 | object/context/scenario/time window 在导航中不丢失 |
| AP-04 | 用例包 | 页面、对象类型、查询、Action、规则、质量检查和权限可列依赖 |
| AP-05 | 受控共享与协作 | 审核、注释、共享不扩大无权数据可见性 |

### 10.3 开发者工具与集成

先稳定 OpenAPI 与内部类型，再生成 TypeScript/Python SDK；事件订阅和 Webhook 必须有签名、重试、去重与订阅者权限。MCP 工具只能暴露经过同一权限裁决的只读查询或受审核 Action，不允许把内部数据库查询直接映射成通用工具。每个公共接口要有版本、速率限制、错误合同和弃用计划。

| ID | 能力 | 交付门槛 |
|---|---|---|
| DX-01 | 公共 API 合同 | OpenAPI、示例、变更日志、兼容规则与稳定错误码 |
| DX-02 | TS/Python SDK | 类型化 ObjectRef、ScenarioContext、ActionRequest 与分页 |
| DX-03 | Webhook/事件订阅 | 签名、重试、死信/失败检查、消费者幂等 |
| DX-04 | MCP 工具层 | 工具范围、只读/写入标记、权限、审核和 trace |
| DX-05 | 开发者控制台 | 资源、依赖、版本、调用量、错误和凭据引用可见 |

### 10.4 多租户、规模与运营

多租户只能在资源身份、授权投影和审计链稳定后引入。需评估租户隔离、项目/用例资源继承、查询与写入配额、成本归属、备份恢复、跨环境发布、故障域和保留策略。性能门槛按明确数据规模与硬件记录，不用固定 frePPLe/FactoryNet fixture 的速度宣称生产级容量。

| ID | 能力 | 交付门槛 |
|---|---|---|
| OP-01 | 租户/项目/用例资源层级 | 查询、Action、Agent、日志与导出无跨租户泄漏 |
| OP-02 | 配额与成本 | 来源、查询、模型、存储、运行分别计量并可限流 |
| OP-03 | 重建与恢复 | PG/Falkor/MinIO 的备份、投影重建、目标恢复时间有实测 |
| OP-04 | 发布治理 | dev/staging/prod 配置和 schema 迁移可审查、回退 |
| OP-05 | 规模基准 | 对象量、关系量、事件速率、媒体体量、并发写回逐档验证 |

## 11. 开源项目调研：可借鉴模式、适配点与取舍

下表基于项目官方文档，描述“可以从哪里学到什么”，不是立即引入的依赖清单。PoC 需分别核对当前版本、许可证、部署/运维开销、与 PostgreSQL/FalkorDB/Celery 的兼容、权限及数据驻留；本文不代替具体选型评审。

| 项目与官方资料 | 可借鉴的模式 | Project 4 适配建议 | 主要取舍 |
|---|---|---|---|
| [DataHub：血缘影响分析](https://docs.datahub.com/docs/act-on-metadata/impact-analysis) | 上下游依赖、按实体/owner 过滤、变更影响 | 借鉴资源依赖和影响页；P2 再评估部署 | 单独元数据平台会增加同步成本 |
| [OpenLineage：Job/Run/Dataset](https://openlineage.io/docs/1.53.0/spec/facets/) | 统一运行与数据血缘事件、可扩展 facet | P1 以事件形状对齐，先内部发射 | 标准数据集血缘不足以独自表达对象/Action 决策链 |
| [Dagster：资产与检查](https://docs.dagster.io/getting-started/quickstart) | 资产依赖、物化、质量检查、自动化 | 研究构建/质量任务的资源视角 | 与现有 Celery 并存需避免双调度 |
| [Great Expectations：Expectation Suite](https://docs.greatexpectations.io/docs/core/define_expectations/) | 可版本化的数据断言及验证结果 | P1 先定义质量规则合同，可按需嵌入 | 规则维护和失败阻断策略需业务治理 |
| [Debezium：CDC](https://debezium.io/documentation/reference/stable/architecture.html) | 快照加变更流、offset 与连接器生命周期 | P2 针对真实数据库源 PoC | 复制槽、消息中间件和运维负担 |
| [Apache Flink：事件时间](https://nightlies.apache.org/flink/flink-docs-stable/docs/learn-flink/overview/) | 水位、迟到、状态与快照 | 通用动态/流式场景扩大后再评估 | 固定演示不需要集群级复杂度 |
| [Temporal：持久工作流](https://docs.temporal.io/encyclopedia/workflow-message-passing) | Signal/Query/Update、长等待与恢复 | P2 对审批、补偿、跨系统流程 PoC | 多一套运行时和确定性编排约束 |
| [LangGraph：持久化与中断](https://langchain-ai.github.io/langgraph/concepts/durable_execution/) | 会话检查点、人工中断、恢复 | P1 可对 Agent Session 做窄 PoC | 必须把工具权限和 Action 边界由本系统掌握 |
| [OpenFGA：细粒度授权](https://openfga.dev/) | 关系式权限模型与授权检查 | P1/P2 比较现有授权服务的扩展成本 | 策略模型和事实同步可能形成第二真相 |
| [OpenTelemetry：三类观测信号](https://opentelemetry.io/docs/) | trace、metric、log 的统一传播与导出 | P0 建 trace_id，P1 接入仪表和告警 | 遥测数据需脱敏与保留策略 |
| [CVAT：质量复核](https://docs.cvat.ai/docs/qa-analytics/quality-control/) | 人工复核、质量控制与争议处理 | P0 借鉴复核流程；P1 研究标注互操作 | CVAT 的能力可能随版本/产品线不同 |
| [Apache Superset：自助探索](https://superset.apache.org/docs/using-superset/exploring-data/) | 保存图表、交互式分析与 dashboard | P1 借鉴分析资源和探索体验 | SQL 语义不能取代对象权限和来源 |
| [MLflow：Trace 与评估](https://www.mlflow.org/docs/latest/genai/eval-monitor/running-evaluation/traces/) | 运行轨迹复评分、反馈与质量回归 | P1 对 Agent eval 做 PoC | 评估分数不能直接当业务正确性 |
| [OR-Tools：约束优化](https://developers.google.com/optimization/cp/) | 可行性、约束与解的状态 | 供应链/排程研究候选 | 必须有真实约束和不确定性，不应包装成自动最优 |
| [Eclipse Ditto：数字孪生](https://eclipse.dev/ditto/) | reported/desired/current state、设备连接与资源策略 | P2 研究设备 Ontology 与现场状态边界 | 接物理系统需要更强安全和部署约束 |

### 11.1 初步选型裁决规则

1. 先定义 Project 4 的产品合同，再比较框架；不能因为开源项目有功能就把它变成当前需求。
2. P0 复用现有 PostgreSQL/FalkorDB/MinIO/Celery。若新增组件，必须证明三条闭环不能以当前组件可靠完成。
3. PoC 使用本地非敏感固定数据，要求有功能收益、权限一致、恢复演练、运维成本和退出路径。
4. 若框架将审核、权限、来源或 Action Run 记录转移到另一真相源，先明确单一权威与迁移方案。
5. 当前不引入任何付费 SaaS、付费模型或学校 GPU；涉及费用时由用户另行决定。

## 12. 可继续研究的产品方向

这些方向超出三条 P0 闭环，但不应在 PRD 中消失。每项都要有业务问题、数据前提、可验证指标、最小 PoC 与停止条件，不能仅靠视觉效果或技术流行度立项。

| 候选方向 | 可回答的问题 | 进入 PoC 的前提 | 目前判断 |
|---|---|---|---|
| 多目标供应链优化 | 交付率、成本、库存和韧性如何权衡 | 约束、成本单位、方案可行性可验证 | 高价值；先基于木材模型做可解释约束 |
| 预测性维护 | 哪些设备何时值得检修 | 足够标签、观察窗口、偏差与误报成本 | 研究候选；不能由 FactoryNet 回放直接宣称完成 |
| 时序异常与变点 | 何时出现明显状态变化 | 序列质量、缺失/迟到规则、人工评估集 | P1 可先用确定性基线 |
| 事件模式/复杂事件处理 | 哪些连续事件组成风险模式 | 稳定事件类型、时间语义和重放夹具 | P2 |
| 多模态主动学习 | 哪些样本值得人工优先复核 | 样本标签质量、复核成本、模型误差 | P1/P2 |
| 多模态定位与几何证据 | 质量问题在图像/点云哪里 | 可靠标注、空间配准、可复核视图 | 研究候选 |
| 反馈驱动规则改进 | 哪些规则误报、漏报多 | 审核结果与规则版本持续积累 | P1 |
| 混合对象/文档检索 | 合同、工艺文档与对象如何共同解释决策 | 文档权限、段落级来源、召回评估 | P1 |
| 决策回放与因果解释 | 当时为何作出该决定、换条件会怎样 | 完整输入版本与模型/规则日志 | P1/P2；因果结论需额外验证 |
| 数字孪生 | 设备报告状态与期望状态如何同步 | 真实设备接入和安全边界 | P2 之后 |
| 地理供应链风险 | 地点与物流事件怎样影响订单 | 有位置、路线与合法数据 | 研究候选 |
| 绿色制造/能耗 | 决策如何影响能耗或碳指标 | 权威测量和单位换算 | 研究候选 |
| 跨组织协作 | 客户、供应商如何分享有限对象与决策 | 多租户、资源 ACL、合同与审计 | P2 之后 |
| Agent 协作 | 多参与者如何分工而不越权 | 单 Agent 可靠性与工具边界先达标 | 后置研究 |
| 边缘/离线运行 | 现场断网时如何收集与同步 | 数据同步和冲突模型成熟 | 后置研究 |
| 可移植用例包 | 如何把同一工作流部署到新工厂 | 资源依赖和环境迁移合同稳定 | P2 |

## 13. 分阶段交付路线与依赖门槛

路线按依赖与可验证结果排序，不写没有证据支持的日历承诺。每一阶段都交付可演示路径、API/数据合同、迁移说明和失败恢复证据；不能只以页面存在为完成标准。

| 阶段 | 主要交付 | 进入条件 | 退出门槛 |
|---|---|---|---|
| R0 演示恢复 | 木材 Study 三 Case、固定指标、七阶段、证据和刷新持久性 | 本地栈与固定数据可用 | 同一 Study 三结果兼容、可比较；重启恢复另行验收 |
| P0a 共同内核 | Canonical Object/Evidence/ExecutionContext、DecisionCase、统一 Action 核心、权限/审计底线 | 当前双轨 API 和迁移影响已盘点 | live/Scenario/UI/API 用同一预览与审核/执行语义；旧接口兼容 |
| P0b 三条闭环 | 多模态质量复核、动态事件处置、时序维护复核 | P0a 合同稳定；三类当前样例可定位原始证据 | 三条分别完成正向与拒绝/失败路径，人工审核后本地写回，重启可回看 |
| P1a 探索与分析 | Explorer/View、指标资源、Scenario tracking/rebase | P0 对象/Action 身份稳定 | 非开发者能从业务问题完成探索、比较与 Action |
| P1b Agent 与观测 | 持久 Agent Session、Tool Registry、评估集、trace/lineage/health 初版 | P0 权限和 Action 边界已覆盖工具 | 会话重启恢复、无越权/无绕审、结果有证据 |
| P2 平台化 | 连接/CDC、Automation、应用编排、SDK、资源治理、规模/恢复 | P1 资源/权限/观测合同稳定 | 新用例主要由复用模块构建，真实数据与规模/故障验收 |

**不可颠倒的依赖**：完整低代码 Builder 依赖稳定组件与类型；外部写回依赖可靠 Action/审批/补偿；多 Agent 协作依赖单 Agent 会话、权限与评估；生产 CDC 依赖 Source/DatasetVersion/质量/血缘合同；多租户依赖端到端资源授权。P0a/P0b 的一些 UI 与后端可并行开发，但共同合同必须先冻结。

### 13.1 P0a 最小交付包

1. 统一 ObjectRef、EvidenceRef、ExecutionContext、ResultManifest 和错误码；明确旧 Entity/Relation 的兼容读取边界。
2. DecisionCase 与审核事件持久化；Action preview/审核/执行采用同一内部服务。
3. 三类数据各定义一个受控本地 ActionType，使用相同参数与状态机原则。
4. 以木材 Run 作跨入口回归样例；确保固定结果不因新内核被改写。
5. 把权限、来源、状态和 trace ID 放到 P0 页面与 API 响应。

### 13.2 P0b 三线联合演示脚本

1. 多模态：打开 I-BADAS 样例 → 查看四类媒体及来源 → 创建质量复核事项 → 审核人批准/驳回 → 对象状态与审计回看。
2. 动态：开始 FactoryNet simulated-live → 单步观察来源事件及关系有效区间 → 规则/人工发起待处置事项 → 审核 → 写回处置状态 → 重启续播不重复处置。
3. 时序：选 FactoryNet episode/窗口 → 查看曲线、图和原始行 → 发起维护复核 → 审核 → 写回检查状态 → 重放相同窗口仍能解释原决定。
4. 横向：从任一结果转到 Object View，比较来源版本、权限、审核与 Action Run；切换用户身份检查无权字段。

## 14. 非功能需求与度量方法

### 14.1 正确性、安全与可恢复

| 维度 | P0/P1 最低要求 | 测量方式 |
|---|---|---|
| 业务幂等 | 同一 client_request_id 与同输入最多一次业务效果 | 重复请求，比较对象修订与 Run 数 |
| 修订安全 | expected_etag 不符不写回 | 两用户或两请求并发模拟 |
| 审核绑定 | preview 内容改变后不能沿用批准 | 修改参数/对象/权限版本后重试 |
| 权限 | 无权者无法从查询、图、证据、Agent、日志获取受限字段或执行 Action | 跨入口权限矩阵 |
| 可追溯 | 每个 P0 完成的 Case 能追到输入版本、证据、审核与 Action Run | 从 UI 和 API 双向抽查 |
| 恢复 | 后端/worker/图服务中断后，权威事实与运行状态可恢复 | 重启前后比较 Case、Run、指标和投影 |
| 失败可见 | 部分失败不显示完成 | 人为制造缺媒体、投影故障、worker 中断 |
| 非生产声明 | 合成、模拟和模型候选始终被标记 | 页面、导出和讲稿检查 |

### 14.2 性能与可用性

先为当前硬件、fixture 和并发建立基线，再确定各阶段 P95 和容量目标。固定 308 对象/329 关系的演示速度不能外推到生产。每个基准报告必须记录数据量、查询形状、硬件、缓存状态、并发、权限模式、P50/P95、错误率与投影滞后；大数据集应另设分档验收。产品层先保证加载、空态、运行中、等待审核、失败与恢复状态可区分，长任务不阻塞页面。

### 14.3 产品与模型质量

- 三条 P0 流程完成率：3/3，且每条都有批准、驳回、失败或冲突案例。
- 审计覆盖率：每次审核、执行和失败都能检索到对应事件；缺记录即 Gate 未通过。
- 证据覆盖率：每个正式写回至少关联一条可定位的输入或人工理由；对“无证据”情形必须明确阻断或人工例外理由。
- Agent 质量：P1 先报告对象命中、来源充分、越权、Action 候选有效、恢复成功等分项，不用一个总分掩盖风险。
- 用户可用性：用未预演的业务问题观察目标用户是否能找到对象、证据和下一步；记录任务完成、耗时、求助和误解，不让开发者代操作。

## 15. 验收方案：按真实范围证明完成

本节列的是产品验收场景，不声称现在已经通过。历史脚本或旧分支结果只能辅助定位；每个正式 Gate 要在目标分支、目标环境、目标数据版本上记录当前证据。已有木材演示的固定数值见第 2.2 节。

| Gate | 必做场景 | 不通过的例子 |
|---|---|---|
| R0-WOOD | Baseline/B/C 完整 Run 七阶段、Compare 数值、需求影响、证据、页面刷新及后退/前进 | 某 Case 无兼容结果、指标错、上下文串 Case |
| R0-RECOVERY | 分别重启 backend、worker、FalkorDB，刷新同一 Study；比对 Run ID、digest、指标和图 | 数据只在页面缓存；重启后图/结果丢失 |
| P0-CONTRACT | 同一 ObjectRef 在列表、图、详情、Case、Scenario、Agent、Action 中一致 | ID 或修订不一致、同名对象混淆 |
| P0-MM | 多模态样例正向批准、驳回、缺失媒体、过期资产、无权访问 | 模型候选未经审核成为已验证标签 |
| P0-DY | 逐事件、暂停/单步/恢复、重复 push、迟到/乱序、处置 Action | 重启后事件重复或处置两次 |
| P0-TM | episode/window、原始行、维护复核、重叠窗口、重放与恢复 | 建议无法回到原始时间序列 |
| P0-ACTION | preview/审核/执行/撤销可支持效果；无权、冲突、重复请求、投影故障 | 未发布 Action 可执行、未知 effect 静默跳过 |
| P0-GOV | 查询/图/导出/Agent/日志权限一致，审计链完整 | 隐藏字段从另一入口泄露 |
| P1-AGENT | 会话多轮、等待审核、拒绝、重启恢复、工具重试、上下文版本变化 | 工具绕开 Action 或重复写回 |
| P2-OPS | 真实 PG/Falkor/MinIO 故障、备份恢复、迁移、资源配额、压力分档 | 仅凭固定 fixture 断言生产级可靠 |

R0 记录应同时保存本地 API 的 study/compare/run 摘要、用户可见页面截图和运行环境；截图不能单独证明数据持久性。正式验收记录应标清“已验证/未验证/失败”、日期、分支 commit、数据版本、操作和结论，不能用计划中的目标数填已通过栏。

## 16. 风险、约束与待裁决事项

### 16.1 主要风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 旧 Entity/Relation 与 semantic core 双轨 | 查询、Action、UI 对同一对象解释不一致 | 先定 canonical ID、兼容适配和迁移审计 |
| 两套 Action 运行时并行扩展 | 权限与副作用语义漂移 | P0a 统一核心，旧路由只作适配 |
| 新增 Case/Agent 表重复已有 Run | 多个状态真相与维护负担 | 明确 Case、Session、Run 的职责；迁移复用现有事实 |
| 审核通过后数据变化 | 已批准预览和最终效果不同 | 绑定 digest/etag，执行时重检 |
| 图与 PG 不一致 | 用户看到旧状态，误判 Action | 投影状态、reconcile、对象页标版本 |
| 媒体/模型输出污染事实 | 错误质量标签或无证据结论 | 候选与人工结论分层、版本/来源、复核 Gate |
| 固定 Demo 过拟合 | 非固定数据与新用户不会使用 | 未预演问题、真实数据分档和跨入口验收 |
| 外部框架提前引入 | 运维、权限和数据迁移复杂化 | 窄 PoC、退出路径和统一合同先行 |
| 过度宣称 Palantir 对等 | 评审可信度受损 | 明确公开资料范围、原型边界和未验收项 |
| 费用失控 | 付费模型或云服务意外使用 | 默认本地/免费，任何付费使用先征得用户同意 |

### 16.2 待实施阶段用证据裁决的问题

这些问题不阻碍本 PRD 的 P0 产品方向，但每项有明确裁决点，不能留给实现者在编码时随意决定。

1. **Action 核心的事务边界**：P0a 以 PG 原子事实+运行记录为最低保证；若对象实体仍由其他存储权威保存，先证明提交顺序和失败补偿，再开放写回。
2. **DecisionCase 是否成为 canonical Ontology Object Type**：P0a 比较“专用业务资源 + 对象引用”和“直接一等 Object Type”的查询/权限/迁移代价；两者只能选一条权威写路径。
3. **动态与时序的共享身份**：以 dataset_version、episode_id、run_id 和 source_row/event ref 建桥；只有证明映射稳定后才合并 UI，不直接合并历史表。
4. **细粒度权限引擎**：先补齐现有授权服务的端到端证明；若复杂关系权限显著超出它的能力，再 PoC OpenFGA。
5. **Agent 编排器**：先证明 PG 事件/检查点加现有异步任务可否满足恢复；若不足，再比较 LangGraph/Temporal 的收益与运维成本。
6. **外部连接器顺序**：由真实数据所有者、访问方式、更新频率、错误容忍和维护成本排序，不根据集成目录的数量决定。

### 16.3 文档维护规则

每项需求保留 ID、owner、阶段、状态、验收证据与变更记录。PRD 更新不得把“计划”无证据改为“已实现”；重大范围变化需同步修改阶段门槛、API 合同和演示脚本。开源项目的具体版本、许可、云/社区功能边界在真正选型前重新核查，不能使用本文搜索日期作为安装依据。

## 17. 资料索引与修订记录

### 17.1 当前仓库证据

- [Palantir 能力补集与目标架构](PALANTIR_PROJECT4_COMPLEMENT_AND_TARGET_ARCHITECTURE.md)：当前项目分层、差距和原路线；作为参考资料，不作为自动执行指令。
- [Ontology 差距审计](PALANTIR_ONTOLOGY_GAP_AUDIT_2026_09_23.md)：类型系统、Action、Explorer 等具体缺口。
- [木材供应商演示操作稿](DEMO_SUPPLIER_SCENARIO_WORKBENCH.md) 与 [历史工程验收](VERIFY_SUPPLIER_SCENARIO_WORKBENCH.md)：固定 profile、流程与历史边界。
- backend/app/models/v2/agent.py、action.py、scenario.py、temporal_replay.py、dataset.py、construction.py：当前模型证据。
- backend/app/routers/v2/logic_actions.py、supplier_studies.py、multimodal.py、dynamic_data.py、temporal_replays.py：当前接口证据。
- data/frepple_demo/whatif_synthetic_manifest.json：木材 profile、模型版本与 synthetic 声明。

### 17.2 官方产品与开源资料

产品对照：[Palantir 开发者导航](https://www.palantir.com/docs/foundry/developers)、[Ontology](https://www.palantir.com/docs/foundry/ontology)、[Object Explorer](https://www.palantir.com/docs/foundry/object-explorer/overview/)、[Object Views](https://www.palantir.com/docs/foundry/object-views/config-overview)、[Workshop](https://www.palantir.com/docs/foundry/workshop/overview)、[Action Log](https://www.palantir.com/docs/foundry/action-types/action-log)、[Agent Stack](https://www.palantir.com/docs/foundry/aip/agent-stack)、[Workflow Lineage](https://www.palantir.com/docs/foundry/workflow-lineage/overview)、[Observability](https://www.palantir.com/docs/foundry/observability/overview)。

开源与公共工程资料：第 11 节逐项链接 DataHub、OpenLineage、Dagster、Great Expectations、Debezium、Flink、Temporal、LangGraph、OpenFGA、OpenTelemetry、CVAT、Superset、MLflow、OR-Tools、Eclipse Ditto 的官方文档。其公开能力用于识别可复用产品模式；是否采用仍须 PoC。

### 17.3 修订记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-10-22 | 在木材演示恢复后建立完整差距清单；把三类数据的人工审核与本地 Action 写回定为 P0；扩展统一运行时、Agent 会话、治理、平台化和开源调研 |
