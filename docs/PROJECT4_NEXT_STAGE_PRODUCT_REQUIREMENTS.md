# Project 4 下一阶段产品需求与双人开发计划

| 项目 | 内容 |
|---|---|
| 版本 | v4.76（Pipeline 持久定时调度源码增量） |
| 日期 | 2026-11-01 |
| 作者 | Jie Zhang |
| 状态 | 需求/验收计划与实现进展并列；整体目标未验收 |
| 范围 | 对照公开的 Palantir Foundry/AIP 与图特摩斯 OntoFlow 产品资料，明确 Project 4 差距、PRD、验收基准、两人分工和已验证实现增量 |

> **v4.51 当前增量（静态编译与空白检查通过；业务运行验收未做）：** 为注册 Scenario 的 ActionRun 增加带 attempt 与到期时间的执行租约，并新增 append-only digest chain 记录 claim、续租、过期与终态。相同幂等请求只在租约到期、审计链有效且没有已提交 ChangeSet 时接管；Scenario revision 发布前以当前 token/attempt fencing，已出现 ChangeSet 但 ActionRun 仍为 running 时转人工核对。旧版无租约的 running 记录不自动重放。新增 migration 0090，尚未应用。相关后端模块与 migration `py_compile`、`git diff --check` 通过；未运行自动测试、ORM/API、数据库迁移、HTTP/ACL、worker/FalkorDB 或业务 E2E。SQL 提交前写入确定性图 key 仍可能在进程中断时留下孤立图数据；跨存储 outbox/孤立图恢复、M4/E2E-05 与 M0.1 Gate 保持开放。

> **v4.52 当前增量（静态编译与空白检查通过；运行验收未做）：** 注册的 `/actions/{action_id}/run` 在写 FalkorDB 前，先把确定性 graph key/revision 与资源摘要链写入 ActionRun；随后按 Scenario → ActionRun 锁顺序重锁，并复核情景 head/etag、ACL、父视图可用性和 compiled-edit 权限，最终 SQL 发布事务会同时清除构建标记。新增 Migration 0091 与每分钟 Celery recovery：只有 claim 链有效、租约已失效、无 QueryDataView 引用且无孤立 ChangeSet 的 failed/cancelled 或过期 running 运行才会删除图；引用已有视图时保留图并记录事件；完成、需人工核对或证据不完整时 fail closed。工作台和供应商研究等 attempt=0 的事务内路径保持原合同。本增量只覆盖注册 leased Action 路径，其他 Scenario 写图早于 SQL commit 的调用仍待治理。相关 Python 模块与 migration `py_compile`、`git diff --check` 通过；Migration 0091 尚未应用，未运行自动测试、ORM/API、数据库迁移、HTTP/ACL、Celery/FalkorDB 或进程退出故障注入。outbox、M4/E2E-05 与 M0.1 Gate 及整体验收继续开放。

> **v4.54 当前增量（静态编译与空白检查通过；运行验收未做）：** `PATCH /{ontology_id}/scenario-studies/{study_id}/cases/{case_id}` 改为复用 fenced Scenario ActionRun claim 与 0091 图构建恢复。初次 ActionRun claim 独立持久化；Scenario 图构建完成前不改写 case 参数。revision 在写图前提交 graph marker 后，按 Study → Scenario → case 顺序重新锁定并复核上下文、etag 与 preview；case 参数/版本号只在 View、Revision、ChangeSet、ActionRun 的最终 SQL 提交前一起更新。revision transaction hooks 只允许与有效 leased ActionRun 同用；失败则结束当前 claim，孤图留给恢复任务按 SQL 引用和审计链检查。rename/archive case 写入口也固定同一锁顺序。无新增 migration。`0090/0091` 尚未应用，未运行测试、ORM/API、HTTP/ACL、Celery/FalkorDB 或故障注入。其他直接 Scenario revision、live merge、Agent/DecisionCase 等跨存储写入仍需统一。

> **v4.55 当前增量（静态编译与空白检查通过；运行验收未做）：** 通用 Scenario revision 新增服务端 typed ChangeSet 授权预览 `POST /{ontology_id}/scenarios/{scenario_id}/revisions:preview`，返回绑定 Scenario ETag/head、父视图内容摘要、编辑载荷和权限摘要的 `preview_digest`；正式 `.../revisions` 发布必须带回同一 digest。`ScenarioService.publish_revision()` 在发布前持久化 leased ActionRun；`ScenarioService.revision()` 将未带 lease 的调用统一委托到该入口，并拒绝无 fenced claim 的 ActionRun。Checkpoint restore、Scenario duplicate、Tracking rebase 与冲突解决复用同一 claim、graph marker 和恢复边界。Tracking rebase/resolve 的 `base_view_id`、时间戳、revision 0 元数据现在与最终 View/Revision/ChangeSet/ActionRun 一起提交，避免图构建前先改换 Scenario 基线。此增量只补齐 typed ChangeSet 的预览摘要与持久恢复，尚未把这些 edits 收敛到统一 `ActionRequest` compiler，也未提供完整 before/after 与审批 UX。无新增 migration，复用 0090/0091。相关 Python 模块 `py_compile` 与 `git diff --check` 通过；当前隔离 Python runtime 缺 SQLAlchemy，未完成模块运行时导入；未运行自动测试、ORM/API、数据库迁移、HTTP/ACL、Celery/FalkorDB、并发或进程退出故障注入。直接 live merge、Agent/DecisionCase 等跨存储写入与 M4/E2E-05、M0.1 Gate、整体验收仍开放。

> **v4.56 当前增量（静态编译与空白检查通过；运行验收未做）：** typed Scenario revision preview 增加逐操作 `changes` 差异：属性编辑展示授权可见的 before/after 值、对象创建/删除展示存在性变化、关系编辑展示关系存在性变化；差异基于 Action 的对象/字段/关系授权投影，避免从原始图属性暴露隐藏字段。`authorize_compiled_edits()` 现在拒绝以 `create_object` 覆盖同一 Scenario 中已有对象，并先应用对象读取策略再返回冲突，避免通过冲突码探测不可见对象。无新增 migration，复用 0090/0091。三个相关后端模块 `py_compile` 与 `git diff --check` 通过；未运行自动测试、ORM/API、数据库迁移、HTTP/ACL、Celery/FalkorDB、并发或进程退出故障注入。此为预览 API 的差异数据增量，尚未提供直接 ChangeSet 编辑界面/审批 UI；当前 runtime import 因隔离环境缺 SQLAlchemy 未完成。Live merge、Agent/DecisionCase 跨存储写入、M4/E2E-05、M0.1 Gate 与整体验收仍开放。

> **v4.57 当前增量（静态编译与前端生产构建通过；运行验收未做）：** 将授权差异计算接到用户实际使用的 Scenario Action Preview；后端复用 compiler 读取的同一 Scenario 快照，返回按可见字段裁剪的逐操作 changes，并继续以原 Action preview digest 绑定提交内容。通用情景工作台现展示修改/清除属性的前后值、对象创建/删除及关系存在性，并保留权限摘要、预览摘要和显式提交按钮。无新增 migration。相关 Python 模块 `py_compile`、frontend TypeScript/Vite production build 与 `git diff --check` 通过；未运行自动测试、ORM/API import、HTTP/ACL、数据库、worker/FalkorDB、并发或故障注入。该界面仍是 Action Type 编译预览，不提供任意 ChangeSet 编辑器或独立审批角色；Live merge 与 Agent/DecisionCase 跨存储统一、M4/E2E-05、M0.1 Gate 和整体验收仍开放。

> **v4.58 当前增量（静态编译与前端生产构建通过；运行验收未做）：** Scenario Live Merge 预览现在返回按当前授权投影裁剪的逐操作差异，并把差异纳入 `preview_digest`。工作台同时展示待合并属性前后值、对象存在状态与关系状态；若创建目标已在主本体中存在，系统在确认该对象对当前用户可见后将其保留为合并冲突，差异只展示本次提议写入的字段，不返回原对象属性；隐藏对象仍 fail closed，直接创建覆盖仍被拒绝。无新增 migration。相关 Python `py_compile`、frontend TypeScript/Vite production build（2,834 modules）和 `git diff --check` 通过；未运行自动测试、ORM/API import、HTTP/ACL、数据库、worker/FalkorDB、并发或故障注入。此预览尚未把 live/Scenario/DecisionCase 写入统一到唯一 Action Runtime；M4/E2E-05、M0.1 Gate 与整体验收继续开放。

> **v4.59 当前增量（静态检查通过；数据库运行验收未做）：** 移除 `main.py::_run_schema_migration()` 在开发环境迁移或 schema 校验失败后调用 `Base.metadata.create_all()` 的回退。失败时保留 `not_ready`、释放连接池并返回失败；启动 seeding/backfill 不执行，写请求由 readiness gate 拒绝，`/health/ready` 返回 503。生产环境仍在启动时失败退出；开发环境进程可继续提供只读检查与 liveness。没有新增 migration。仅做 Python 静态编译和 diff 空白检查；未运行自动测试、启动应用、连接/更改数据库、应用 migration 或验证 HTTP/readiness 行为。该改动降低未知 schema 被意外改写的风险，但 M0.1 Gate、运行验收与整体验收仍开放。

> **v4.60 当前增量（静态编译通过；图/数据库运行验收未做）：** 新增共享 `reauthorize_edit_batch()` 执行边界；Scenario Live Merge 图写入前基于当前 FalkorDB 快照重新计算对象/字段/关系权限，并必须匹配审核时的 `permission_digest`。人工恢复路径也对当前图重新核权；部分创建已落图时仅在目标对象对执行者可见且可写的前提下允许恢复收敛。缺少权限摘要或权限变化均在副作用前 fail closed。无新增 migration。相关 Python 文件仅做静态编译和 `git diff --check`；未运行自动测试、ORM/API、数据库、FalkorDB、HTTP/ACL 矩阵、并发或故障注入。M4/E2E-05、统一 Action Runtime 与 M0.1 Gate 仍开放。

> **v4.61 当前增量（静态检查通过；恢复运行验收未做）：** 修正 v4.60 恢复授权快照选择：若不完整合并已从图中删除对象、SQL 尚未提交，当前图快照不再包含授权器必须检查的对象。恢复现在在当前策略下针对持久化的固定合并前快照重核权限摘要；随后恢复器独立确认当前对象/关系状态属于该编辑批次可产生的合法前缀，才幂等收敛 SQL 与 FalkorDB。正常首次写入仍针对当前实时图快照核权。无新增 migration。仅后端 Python 静态编译及 `git diff --check`；未运行自动测试、数据库/FalkorDB、HTTP/ACL、并发或故障注入。M4/E2E-05、统一 Action Runtime 与 M0.1 Gate 仍开放。

> **v4.62 当前增量（静态检查通过；HTTP 隐私验收未做）：** Construction MappingTask 失败时不再把 provider/异常原文存入 `MappingTask.error` 或 `ConstructionDraft.error`，而是映射到固定错误码和安全中文提示；mapping-task 详情只从有效 append-only attempt 链读取错误码，并对旧任务行始终返回安全提示，防止历史原文泄漏。任务输入、模型摘要和 attempt 事件按既有资源访问控制返回。无新增 migration。后端静态编译与 `git diff --check` 通过；未运行自动测试、ORM/API、HTTP/ACL、模型服务或异常 canary 泄漏验收。M0.1/M2/E2E-01 隐私与资源 ACL Gate 仍开放。

> **v4.63 当前增量（静态检查通过；HTTP 隐私验收未做）：** ConstructionRun 的共享更新入口不再把 free-form/provider 异常原文写入 `error`，而是持久化稳定错误类别；共享序列化对历史原始错误也只返回通用安全消息和类别码。覆盖 Construction 草案、常规/多模态及 Temporal Construction 共用的 `ConstructionRun` 读写路径。无新增 migration。后端静态编译与 `git diff --check` 通过；未运行自动测试、ORM/API、HTTP/ACL 或异常 canary 泄漏验收。M0.1/M2/E2E-01 隐私与资源 ACL Gate 仍开放。
> **v4.64 当前增量（静态检查通过；HTTP 隐私验收未做）：** What-if run 失败时仅持久化 allowlist 中的稳定错误码，不再保存 `WhatIfError` 或通用异常原文；运行详情序列化对历史原始错误同样只返回安全提示和类别码。覆盖 What-if worker 失败、取消、排队后权限/所有权/输入复核失败和旧历史行，无新增 migration。后端静态编译与 `git diff --check` 通过；未运行自动测试、ORM/API、HTTP/ACL 或异常 canary 泄漏验收。M0.1/M5/E2E-03 隐私与 What-if 资源 ACL Gate 仍开放。
> **v4.65 当前增量（静态检查通过；运行/HTTP 隐私验收未做）：** 关闭 TemporalReplay worker 镜像 ConstructionRun 时绕过共享更新入口、直接持久化原始错误文本的路径；现在复用共享 Construction 错误分类器，仅存稳定错误码。无新增 migration。相关后端静态编译与 `git diff --check` 通过；未运行自动测试、ORM/API、worker、HTTP/ACL 或异常 canary 验收。TemporalReplay 自身错误字段仍有独立序列化与持久化路径，未包含在本增量内；M0.1/M2/E2E-01 隐私 Gate 与整体验收仍开放。
> **v4.66 历史增量（静态检查通过；运行/HTTP 隐私验收未做）：** TemporalReplay 与 TemporalStream 服务层的 run/batch/event/projection-attempt 序列化开始统一返回 allowlist 错误码和固定安全提示；新 worker、投影和派发失败只持久化稳定类别，旧记录中的自由文本在这些 serializer 读取时归一化。TemporalReplay worker 结果及 ConstructionRun 镜像沿用共享分类器；Temporal 路由的 Celery 本地 fallback 不再写异常原文。后续复核发现 `GET /{replay_id}/batches` 仍有直接路由序列化旁路，同步请求异常也有原文响应；由 v4.67 收口。无新增 migration。Python `py_compile` 与 `git diff --check` 通过；自动测试、ORM/API、worker、HTTP/ACL、异常 canary 与真实数据库/队列验收未做。
> **v4.67 当前增量（静态检查与时序错误边界收口；运行验收未做）：** TemporalReplay 的批次列表接口改用共享安全 serializer，旧批次错误文本不再由独立路由原样返回；TemporalStream 的 `StreamError` 和投影重建拒绝响应统一通过 allowlist 错误码、固定安全提示和数值型安全附加字段构造。TemporalReplay 的来源读取、时间值校验、未知序列和控制接口不再将异常原文或任意异常附加信息返回客户端。旧版 Temporal 路由移除源对象 URI 回显和源文件解析/安装异常原文；`parse_temporal_bytes()` 不再把 Parquet/XLSX/JSON 解码器异常写进 ValueError。TemporalDatasetProfile 失败只保存稳定类别码，详情 serializer 对历史异常原文也返回固定安全提示，worker 日志只记录类别码和异常类型。Temporal vector-index 与 graph-projection worker 失败不再持久化 free-form error_message，历史任务仍由既有 serializer 按错误码映射安全提示，worker 日志仅记录异常类型。无新增 migration。相关 Python `py_compile` 与 `git diff --check` 通过；未运行自动测试、ORM/API、HTTP/ACL、异常 canary、worker、数据库或队列验收。M0.1/M2/E2E-01 隐私 Gate 与整体验收仍开放。

> **v4.68 当前增量（源码实现、Python 3.11 运行时导入、静态检查与前端生产构建通过；业务运行验收未做）：** Curated 预览可读取审核固定的 DatasetVersion，并为每行生成绑定版本、行序号和原始行摘要的确定性身份令牌；重新打开审核时从同一版本投影已保存修改，旧的无版本审核只读，检测到较新数据版本时允许发起新审核。编辑请求限字段/值长度和单批 500 条，在写入审核历史前按固定版本验证行令牌/唯一旧身份、字段存在性、old-value 链和可转换的基础值类型；重复 cell、字段新增/删除、主键修改、行身份歧义、过期令牌或分叉修改均拒绝。Mapping Apply 与 Link Mapping worker 将审核 pin 版本传入应用器，并在写入本体前再次完整校验。无需新增 migration。相关 Python 3.11 模块导入、`py_compile`、`git diff --check` 与 frontend TypeScript/Vite production build（2,834 modules）通过；未运行自动测试、HTTP/ACL、数据库/worker/FalkorDB 或端到端审核/映射流程。M0.1 Gate 和整体验收继续开放。
> **v4.69 当前增量（源码修改与静态检查通过；数据库/worker/图运行验收未做）：** 对 strict approved `MappingApplyRun`，跳过 SQL 事务前的 best-effort Neo4j 概念节点写入；该路径由 PostgreSQL 中的 RowChange 事实驱动 SQL commit 后的 FalkorDB 概念/实例投影，legacy/non-strict 路径维持原写入方式。这样避免 strict Run 在 durable RowChange 恢复合同之外留下 Neo4j 副作用。`mapping_service.py`、`mapping_row_projection.py`、`mapping_apply_runs.py` Python 3.11 编译/import、静态 Alembic heads（单一 code head `0091_scenario_graph_build_recovery`）与 `git diff --check` 通过；无新增 migration。未查询/迁移数据库，未运行自动测试、HTTP/ACL、Celery worker、FalkorDB 或端到端映射流程。M0.1 Gate 和整体验收仍开放。
> **v4.70 当前增量（迁移代码修改与静态检查通过；数据库运行验收未做）：** 0046 `downgrade()` 在删除来源归属/软删除字段前，除 RowChange 与实例 tombstone 外，现检查是否存在非空 `EntityInstance.source_mapping_id` 或 `Entity.is_source_deleted=true`；任一事实存在即停止降级，防止数据来源归属或实体软删除标记被静默丢弃。无新增 migration revision；修改现有 0046 降级保护代码。Python 3.11 静态编译、Alembic 单一 code head 与 `git diff --check` 通过；未运行自动测试、真实 SQLite/PostgreSQL 升级/回退，也未查询或更改开发数据库。M0.1 Gate 和整体验收仍开放。
> **v4.71 当前增量（迁移代码修改与静态检查通过；数据库运行验收未做）：** 0047 `downgrade()` 现检查 PipelineRun 的 `pipeline_version_id`/`execution_mode`/`execution_manifest`/`execution_digest` 与 PipelineVersion 的 snapshot/definition digest/snapshot digest，任一非空则拒绝删除 lineage。0048 现检查 request actor/fingerprint、所有 next/lease/claim/task/time/error 字段及非 `legacy` dispatch 状态；升级时自动回填的 `legacy` 单独保留为可降级合成状态。未增加 migration revision。Python 3.11 静态编译、Alembic 单一 code head 与 `git diff --check` 通过；未运行自动测试、SQLite/PostgreSQL 升级/回退或数据库查询/写入。M0.1 Gate 和整体验收仍开放。
> **v4.72 当前增量（迁移代码修改与静态检查通过；数据库运行验收未做）：** 0054 `downgrade()` 在删除 `projection_lease_until` 和 `projection_current_attempt_id` 前检查两列的非空事实，并继续保护 attempt events、manual attempt count 与 `projecting/dead_letter` 状态。当前 0065 已为 attempt event 提供 PostgreSQL/SQLite UPDATE/DELETE trigger，模型也拒绝 ORM update/delete；本次同步修正 M0.1 记录中关于不可变保护缺失的过时结论。无新增 migration revision。Python 3.11 静态编译、Alembic 单一 code head 与 `git diff --check` 通过；未运行自动测试、数据库迁移或真实 PostgreSQL/SQLite 行为验证。M0.1 Gate 和整体验收仍开放。
> **v4.73 当前增量（迁移代码修改与静态检查通过；数据库运行验收未做）：** 0042 `downgrade()` 现检查 ActionRun 投影 attempt count/next/last retry 时间，存在任何恢复事实即拒绝删除；0043 现检查 Review `dataset_version_id`，任一审核 pin 非空即拒绝回退。无新增 migration revision。Python 3.11 静态编译、Alembic 单一 code head 与 `git diff --check` 通过；未运行自动测试、真实 SQLite/PostgreSQL upgrade/downgrade，也未查询或修改开发数据库。0042 的积压峰值与 0043 FK `ON DELETE SET NULL` 保留合同仍开放。M0.1 Gate 和整体验收继续开放。
> **v4.74 当前增量（源码与迁移修改、静态检查通过；数据库运行验收未做）：** 新增 0092，将 CuratedReview 的 DatasetVersion FK 从 `SET NULL` 改为 `RESTRICT`；Model metadata 与之对齐。Dataset 删除接口在有 Review 时默认拒绝，显式 `cascade=true` 才删除 Review/RowEdit 并继续删除版本；Curated 删除接口也要求显式确认，并先删 Review/RowEdit 再删 DatasetVersion。普通 Dataset 删除将对象存储清理移到 SQL commit 之后，避免数据库拒绝删除时先丢源对象。0092 downgrade 在存在固定版本的 Review 时拒绝恢复 `SET NULL`。Python 3.11 编译/import、Alembic 单一 code head 与 `git diff --check` 通过；PostgreSQL 0091→0092 offline DDL 生成通过；SQLite 离线 batch DDL 需要在线表反射，本轮未运行真实 SQLite migration。未运行自动测试或应用迁移，未查询/操作数据库。M0.1 Gate 和整体验收继续开放。







> **v4.53 历史增量：** `PUT /{ontology_id}/scenarios/{scenario_id}/actions` 为批量 Workbench Action 建立首个持久 execution claim；同请求可重放完成结果或在租约过期后 fenced 接管。成功提交清除图构建 marker，失败结束当前 claim并交由既有 recovery 清理符合条件的孤图。无新增 migration。静态编译与 `git diff --check` 通过；运行验收未做。

> **v4.52 历史增量：** 见下方修订记录。该版本当时仍将 Workbench 与供应商研究的事务内 attempt=0 路径留在原合同；v4.53 已将 Workbench 批量 Action 提交接入租约恢复，供应商研究与其他直接 revision 路径继续开放。

> **v4.50 历史增量（静态编译与空白检查通过；业务运行验收未做）：** FR-05 增加 Action 专用内部字段 allowlist，当前 allowlist 为空；Action 输入快照、compiled edit 权限检查、DecisionCase target-state/历史 preview 序列化都默认过滤或拒绝 `_` 开头属性。DecisionCase 执行与补偿 ActionRun 的 before/after JSON 快照只保存本次 Action 涉及的允许字段；完整对象状态仍以摘要验证，撤销按每个属性的变更序列核对前后快照，再仅恢复有净变化的字段，避免把未改动的内部字段复制进可见预览或审计负载。无新增 migration。两个相关服务模块 `py_compile`、`git diff --check` 与未跟踪文件空白检查通过；未运行自动测试、ORM/API、HTTP/ACL 角色矩阵、数据库、投影 worker/FalkorDB 或业务 E2E。Action 全入口统一 Runtime、durable outbox、M4/E2E-05 与 M0.1 Gate 保持开放。

> **v4.49 历史增量（静态检查通过；业务运行验收未做）：** 修复 Action 授权快照在字段脱敏后又复制原始图属性、使 `_` 开头内部字段重新进入 compiler 输入的问题。`authorize_action_snapshot()` 现在只从 `redact_record()` 的结果构造可见对象属性；共享 Action Runtime、Scenario Action 编译及复用该快照的 Scenario Study 图读取均沿用同一投影。未新增 migration。相关后端模块 `py_compile` 与 `git diff --check` 通过；未运行自动测试、ORM/API、HTTP/ACL 角色矩阵、数据库、worker/FalkorDB 或业务 E2E。FR-05 的其他 Action 字段/关系授权和跨入口验收、M4/E2E-05、M0.1 Gate 与整体验收继续开放。

> **v4.48 历史增量（静态检查通过；业务运行验收未做）：** DecisionCase 属性回退新增只读预览和显式确认。预览绑定目标 etag、当前字段权限摘要、原 ActionRun 和属性差异；确认要求理由、预览摘要与客户端请求 ID，SQL 事务内写入新的补偿 ActionRun、恢复属性并将原运行/决策事项标记为已回退。同一请求可回放同一补偿运行，修改请求内容会冲突；新运行通过 `revert_of` 关联原运行，避开每个 DecisionCase 只能有一个主 ActionRun 的唯一约束。图投影自动重试和管理员人工重试可沿该关联回到原决策事项。对象操作页先显示差异和理由输入；浏览器本地保留请求信息以支持同请求恢复。未审核的旧运行继续要求人工补偿。无新增 migration。四个后端模块 `py_compile`、frontend TypeScript/Vite production build（2,834 modules）与 `git diff --check` 通过。backend 项目运行环境缺少 SQLAlchemy，本轮未完成 ORM/router import；未运行自动测试、HTTP/ACL 角色矩阵、数据库事务、FalkorDB/worker 故障恢复或浏览器 E2E。M4/E2E-05、跨入口唯一 Action Runtime、M0.1 Gate 与整体验收保持开放。

> **v4.47 历史增量（源码、Python 3.11 runtime import/ORM mapper、前端构建和 diff 检查通过；业务运行验收未做）：** Reasoning 推理写图不再允许 `run(apply_to_graph=true)` 一步创建新 Run 并直接投影；该请求现在明确要求先保存只读预览，再由独立保存操作继续。保存操作要求 `client_request_id`，在图投影前持久化 `OntologyActionRun`，绑定 ReasoningRun、本体修订、权限摘要和结论摘要；成功/确定未写入/结果未知分别保存 completed/failed/reconciliation_required。FalkorDB 派生边继续使用稳定的 `(reasoning_run_id, conclusion)` fact ID，未知结果的同键重试恢复同一 ActionRun 并幂等收敛。Reasoning 页在本地保留未确认的请求 ID，显示 ActionRun，并提供同请求恢复入口。没有新增 migration；`py_compile`、Python 3.11 router import/SQLAlchemy mapper、frontend TypeScript/Vite production build（2,834 modules）与 `git diff --check` 通过。没有运行自动测试、HTTP/ACL、数据库写入、FalkorDB 或进程退出故障注入。该写入仍是专用推理图投影，不是 canonical `EditBatch` compiler 或统一 Preview/Review/Commit；M4/E2E-05、跨入口 Action Runtime、M0.1 Gate 与整体验收继续开放。

> **v4.46 历史增量（源码实现、静态编译/前端构建通过，运行验收未做）：** Scenario merge 现在在持久化 `ScenarioAudit` intent 的同一 SQL 事务创建具名 `OntologyActionRun`，固定请求、预览、权限与编辑摘要，并追加 created/status 事件。合并成功、提交前失败、跨存储结果未知分别记录为 `completed`、`failed`、`reconciliation_required`；经授权的恢复完成后把原 ActionRun 收敛为 `completed`，而不是另造一个成功 Run。合并页回显 ActionRun ID。新增共享生命周期转换 `reconciliation_required → completed`，没有新增 migration。后端两模块 `py_compile`、前端 TypeScript/Vite production build（2,834 modules）和 `git diff --check` 通过；没有运行自动测试、ORM/API/ACL、数据库、Celery/FalkorDB 或崩溃故障注入。此为 M4 的 ActionRun 审计与状态薄片；merge 仍使用专用编译/`apply_live_edits()`/reconciliation 路径，尚未形成跨 Live/Scenario/DecisionCase/Agent 的唯一 Action Runtime 或 PostgreSQL 权威 outbox。

> **v4.45 历史增量（只读实现，运行验收未做）：** 管理员可为已批准 Link Mapping 生成旧 `Relation` 对账预览，按关系类型有界扫描最多 5,000 条、返回最多 100 个候选，展示端点类型、数据集重合、映射签名、旧 ID 约定与精确目标边证据，并对扫描结果生成 digest。Curated 页面仅管理员可见，明确预览不改 PostgreSQL Relation 或图数据库；普通 Apply 的旧关系 fail-closed 检查保持不变。当前只实现线索分类，不做历史关系归属确认、删除、认领或迁移。新改动的 backend 模块 `py_compile`、前端 TypeScript/Vite production build（2,834 modules）与 `git diff --check` 通过；当前系统 Python 缺 SQLAlchemy，新增 router/service runtime import 未核验。未运行自动测试、HTTP/ACL、数据库、Celery、FalkorDB 或业务 E2E；历史 Migration 0084–0089 均未应用。关系身份决策、后台恢复运行/权限矩阵、跨 SQL/图故障注入、FR-03 完整有效时间事件语义与 M0.1 Gate 仍开放。

> **v4.44 历史增量（源码实现、静态/隔离 schema 检查通过，运行验收未做）：** 管理员 Link relation projection replay 从同步 API 改为持久后台任务：API 写入带批量上限、幂等 request ID、操作者、范围、原因和 digest 的请求后派发 Celery；worker 以独立执行租约按关系恢复，Beat 每分钟恢复丢失派发、队列超时和过期 worker，自动重试有独立预算与退避时间。Curated 页面显示重放状态、已处理数量、派发尝试、错误码和原因，并在请求活跃时刷新。Migration 0089 扩展请求状态、批量大小、派发/执行租约和错误摘要；保留 0088 对请求身份与终态的保护。管理员预算仍每关系最多 5 次，自动预算保持 8 次，ApplyRun 原终态不被改写。Python 3.11 `py_compile`、ORM mapper 与挂载 router/service/task import、frontend TypeScript/Vite production build（2,834 modules）、单一 Alembic code head、临时 SQLite 0088→0089 直接升级、0088→head PostgreSQL offline DDL 和 `git diff --check` 通过。未运行自动测试、线上 migration、HTTP/ACL 角色矩阵、Celery worker、FalkorDB 或业务 E2E；0084–0089 均未应用。旧 Link Relation 对账、异步恢复运行/权限矩阵、跨 SQL/图故障注入、FR-03 完整有效时间事件语义与 M0.1 Gate 仍开放。

> **v4.43 历史增量（源码实现、静态/隔离 schema 检查通过，运行验收未做）：** Link relation outbox 增加逐关系自动/人工 `started`/`finished` SHA-256 事件链，worker 依靠当前 attempt token 和有效租约完成投影，过期尝试记录 outcome-unknown 后用稳定 `_fact_id` 恢复。自动预算仍为每关系 8 次；管理员人工重放要求当前 ontology scope、原因、幂等 request ID 和独立每关系 5 次预算，不清零自动计数；重放请求固定 actor、role、scope 摘要和请求 digest，并按不超过 100 条的批次执行。API 展示投影队列状态、尝试历史和历史摘要校验；原 ApplyRun 终态保持不变。新增 Migration 0088。此前通过 Python 编译、ORM mapper、router/service import、frontend production build、单一 Alembic head、临时 SQLite 0087→0088 升级、0087/0088 PostgreSQL offline DDL 和 `git diff --check`；未运行自动测试、线上 migration、HTTP/ACL、Celery worker、FalkorDB 或业务 E2E。该版本人工恢复仍同步执行，运行验收保持开放。

> **v4.42 历史增量（源码实现、静态检查通过，运行验收未做）：** 已批准的 Link Mapping 可先生成不暴露行值的关系变更预览，再由用户以预览 digest 和请求幂等 ID 确认。ApplyRun 固定关系审核、两端已批准数据审核/版本、实体映射审核/ApplyRun 与完整快照摘要；worker 将关系事实和 append-only SHA-256 变更账本/图投影 outbox 原子持久化，再以稳定 `_fact_id` 幂等写入 FalkorDB，严格检查端点是否匹配。Run 有租约、20 次派发/8 次执行上限、失败分类和 Beat 恢复；API 可查看状态，界面只在 Run succeeded 且投影待办为零时报告完成。新增 Migration 0087。Python 编译、ORM mapper、路由/任务导入、前端构建、单一 code head、临时 SQLite 迁移与 `git diff --check` 通过。未运行自动测试、在线 migration、数据库、HTTP/ACL、worker、FalkorDB 或业务 E2E；旧 Link Relation 对账、dead-letter 人工重放、FR-03 时间事件语义仍开放。

> **v4.41 当前增量（源码实现、静态检查通过，运行验收未做）：** Ontology Link Mapping 现有候选冻结、独立审核、拒绝/要求修改后修订和追加事件历史；关系映射页支持创建草稿、修订、提交、批准/退回/拒绝并展示候选 digest 与审核人。新增 Migration 0086；静态迁移图为 92 个 revision、单一 code head 0086，SQLite 与 PostgreSQL 离线 DDL 均生成通过；相关 Python `py_compile`、SQLAlchemy mapper 配置、frontend TypeScript/Vite production build（2,834 modules）和空白检查通过。没有运行自动测试、在线 migration、API/ACL、worker、数据库或业务 E2E。批准只发布关系映射定义；关系图写入的持久 ApplyRun 仍未接入，旧 build-all 仍关闭，页面明确不将批准显示为“已构建”。

> **v4.40 历史增量（源码实现、静态检查通过，运行验收未做）：** Mapping Apply API 不再返回数据库中保存的原始 worker 异常正文；对旧 Run 也按稳定错误码生成安全提示。新派发/执行失败只持久化分类错误码与安全摘要，日志记录 Run ID 与异常类型，不写异常正文；执行/派发预算和重试行为不变。后端相关模块 `py_compile` 与 `git diff --check` 通过；未运行自动测试、API/ACL、worker 或数据库。既有数据库中历史错误正文未通过 migration 批量擦除，只在 API 序列化时隐藏。

> **v4.39 历史增量（源码实现、静态检查通过，运行验收未做）：** 新增 MappingApplyRun append-only worker attempt events，记录每次开始、成功、失败及租约过期后的 outcome-unknown；每条事件绑定 worker/system actor、Celery task ID、稳定错误码、前序摘要与 SHA-256 digest。事件和 Run 状态转换在同一事务提交；序列由 Run 行状态转换串行化。运行列表响应会重算摘要链并区分 complete、in-progress、legacy-untracked、invalid；Curated 数据详情页提供逐次执行历史。新增 Migration 0085，静态迁移图共 91 个 revision、单一 code head 0085；SQLite offline DDL 已生成检查。相关后端文件 `py_compile`、SQLAlchemy ORM `configure_mappers()`、前端 TypeScript/Vite production build（2,834 modules）及 `git diff --check` 通过。没有运行自动测试、在线 Migration、数据库/HTTP/ACL、worker、图数据库或业务 E2E。Migration 0084/0085 尚未应用，旧 Run 没有追溯补造 attempt history。Mapping Apply 每个 Mapping/Data review 唯一运行的既有约束仍在；其他 M2.3 语义写入口、完整权限传播、跨 SQL/图存储恢复与整体验收仍开放。

> **v4.38 历史增量（源码实现、静态检查通过，运行验收未做）：** 补齐被要求修改后的 Mapping 修订路径：编辑实体类型和字段映射后，单个事务保存草稿并生成新的不可变待审快照；事件关联前一审核版本。只允许未提交草稿或被拒绝/要求修改的候选编辑；待审、已发布、已应用、已禁用或存在 ApplyRun 的 Mapping 不可原地修改。列表 API 按角色、状态、最新审核和 ApplyRun 计算 `can_edit`；Mapping 页面提供 JSON 字段映射编辑、取消与原子“保存并提交新版本”。Apply 入队与 Mapping 行锁对齐。三项后端文件 `py_compile`、前端 TypeScript/Vite production build（2,834 modules）与 `git diff --check` 通过；未运行自动测试、ORM mapper、数据库迁移、HTTP/ACL、worker、图数据库或业务 E2E。

> **v4.37 历史增量（源码实现、静态检查通过，运行验收未做）：** Mapping 定义独立版本审核冻结候选快照和 digest，独立审核者可批准、拒绝或要求修改，批准后发布精确版本并追加审核事件；Apply 固定已批准的 CuratedReview/DatasetVersion 与 MappingReview/digest。MappingApplyRun provenance 扩展表记录请求 actor 和最近 worker。相关 Python `py_compile`、前端生产构建与 `git diff --check` 通过；Migration 0084 未应用，worker 历史仍只保留最近 actor；旧 E2E 脚本仍待迁移。

> **v4.35 历史增量（源码实现，未运行验收）：** Construction 候选审核下游影响预览同时覆盖结构修订和查询元数据变化：按实体 ID/名称精确匹配规则与 Action；查询元数据变化时追踪 Object Set、Object View、QueryDataView 与持久 Scenario 引用；结构修订变化时追踪固定当前本体版本的 What-if ScenarioContext、What-if Run、DecisionCase，以及持有相关 What-if 引用的 AgentSession 和 AgentRun。manifest 只保存聚合数与精确资源/引用集合的哈希摘要，不显示私有情景输入、查询定义、运行载荷、会话内容或决策内容。审核决策和常规/多模态 worker 在继续前重算，发现资源或引用漂移即拒绝。Migration 0083 仍未应用；本轮未运行自动测试、ORM、迁移、HTTP/ACL、worker、FalkorDB、模型或业务 E2E。M2.3 的其他语义写入口、完整权限传播、跨 SQL/图存储恢复仍开放。

> **v4.36 历史增量（源码实现、静态检查通过，运行验收未做）：** 旧 Mapping raw-row Apply 与 build-all HTTP 写入口现返回 410；保留的 Apply 入口必须提交精确 `CuratedReview` ID，验证当前数据集已批准、审核固定不可变 DatasetVersion、申请人与决定人为不同用户，并通过既有幂等 `MappingApplyRun` 派发。Worker 在写入前重验审核身份、Mapping digest 与 disabled 状态。旧快捷“批准”入口改为创建 pending 审核；正式审核服务要求独立审核者，若已指派审核人也必须由该人决定。Mapping 创建向导只保存草稿，不再报告实体/关系已构建；本体页按固定审核版本提交运行，并可查看持久运行状态。相关 Python 文件 `py_compile`、前端 TypeScript/Vite production build（2,834 modules）与 `git diff --check` 通过；未运行自动测试、HTTP/ACL、ORM、数据库迁移、Celery/FalkorDB、模型或业务 E2E，Migration 0083 未应用。Mapping 定义当时仍缺独立候选审核/版本发布合同；现有 3 个全域 E2E 脚本仍引用旧 quick-approve/build-all 行为，迁移后再做有效 E2E；其他 M2.3 语义写入口、完整权限传播及跨 SQL/图存储恢复仍开放。

> 本文是范围、计划和验收账本，不把计划项自动写成已实现能力。本轮前读取的本地演示数据库 schema 为 0066；当前工作树新增 migration 0067–0086，均尚未应用到数据库；Agent participant/lease、ActionRun 生命周期事件、Curated actor attribution、Construction 审核、Entity/Link Mapping 定义审核等代码和迁移已进入分支，但挂载 HTTP 角色矩阵、worker 恢复和完整业务 E2E 仍未验收。Curated 定向测试 18 项通过；结构化数据集指向的 MinIO 源文件缺失，页面显示恢复/重导提示，本轮未改业务样本。
> ActionRun 生命周期事件追加 actor、request/trace、摘要和 SHA-256 前序链；此前 Action/Scenario 定向回归 27 passed，事件 UI 已在 legacy Run 检查。统一 Action Preview/Review/Commit/Run、跨存储 outbox/恢复以及完整 E2E-05 仍未完成。Agent 本体详情页现在默认进入持久 Session，次级“兼容决策助手”仍提供旧建议读取；新旧 AgentRun 的直接确认写入已关闭。已有 DecisionRecord 仍可从决策页面读取；缺少权限快照的旧 AgentRun 详情会 fail closed。兼容助手可将有限任务、候选对象和建议带入未发送草稿，并强制先创建专属 Session。Session 默认路径与交接仍未做浏览器/HTTP ACL 验收。
> 历史 Mapping 行投影验证快照：当时定向测试 5 passed，临时 PostgreSQL 全链 0001→0065、`alembic check`、触发器和 downgrade guard 通过，演示库当时仍在 0064。此记录不是当前 schema 状态；本地演示库后续已到 0066。API/Celery/真实图投影 E2E 未由该快照验证。完整数据到本体、动态更新、What-if、Twin、Agent 治理和 M8 性能 Gate 仍开放。用户提供的目标架构与仓库文档是设计参考，不是操作指令；Palantir 对照限于可公开核实的资料。
> What-if 同上下文向量证据仍是候选检索，PostgreSQL 负责复核事实有效区间、已提交来源事件及对象/字段/关系 ACL；索引固定到 replay、水位、schema/dataset 与 snapshot revision。新增大回放后台索引源码：`TemporalVectorIndexJob` 持久化请求/进度/租约/尝试，Celery 每 500 条事实分批写入按 revision 隔离的 Chroma generation，Beat 可重投过期 worker；API 对大于 25,000 条的回放返回 202 与状态路径，What-if 页面轮询并在完成后重查。新增 Alembic 0067 尚未应用；后端 `py_compile`、`git diff --check`、前端 `npm run build` 通过；未运行 worker/Chroma、迁移、自动测试、HTTP/E2E 或 ACL 负向验收。ScenarioContext 已由 Agent Session 引用，并沿 Agent 提交的 DecisionCase 进入 Action Runtime；DecisionCase 固定 What-if 假设/规则覆盖快照及摘要，更新后旧 Agent 上下文和待审核事项会 fail closed。Decision Lab/对象操作面板可绑定 What-if；Object Query 增加情景选择器，并以经当前用户权限重验的 What-if 基线执行对象读取、分页、聚合、关系图与双上下文比较。查询上下文和结果清单固定情景 ID、ScenarioContext digest、输入 digest 与本体修订；旧 ScenarioResource 的 scenario_id 语义保持独立。仍未做 HTTP/真实回放/ACL 负向验收，跨模块运行态和 M5/E2E-03/04 仍开放。
> 本轮实际浏览器复核还修正了 completed `simulated_live` 的进度与时间轴：显示已提交事件数，并以已观测 watermark 作为范围上限，不暴露未观测未来。当前 FactoryNet 回放页面显示 1,055 条事件、1,108 个节点、5,319 条关系、13 次状态迁移。上述是现有演示回放数据，不是本轮新生成的业务内容；未执行新回放、发布或真实语义检索。

> M3 已增加 ObjectRef 独立对象详情深链源码：链接固定 ontology、object type、object ID 与可选 Data View/What-if context；详情页通过现有 Object Query 服务重新授权读取，展示授权属性与 result manifest，刷新会重新请求；返回 URL 保留对象与查询上下文。前端 production build 通过。页面路由可在本地 dev server 加载；本地 backend 未启动，真实对象读取、链接往返、Data View/What-if ACL 与撤权行为未验收。

> **M0.1r Pipeline LLM 状态与 provenance 源码增量（2026-11-01，未验收）：** `llm_structurize`/显式 `auto_extract` 与 `llm_enrich` 默认 fail closed；只有版本化 Pipeline 步骤显式设置 `fallback_policy=rule_based` 才会在模型缺失、调用失败、空响应或无效 JSON 时整源回退。语义增强分批结果为原子提交，失败时不保留部分增强；规则回退行单独计数，LLM 成功数为 0。Run 持久化每个 DatasetVersion 的 requested/effective method、policy、稳定原因码与调用状态；回退将 PipelineRun 标记为 `degraded`，候选数据仍为 `pending_review`，质量状态标为“未判定”。新 Run manifest 固定 Route C 结构化提取/增强的 ModelConfig ID、非秘密配置摘要与静态 Prompt ID/digest；worker 按精确配置复核漂移，配置或 Prompt 变化时不静默换型。每次模型调用新增关联 PipelineRun、来源 DatasetVersion、attempt 序号、配置/Prompt 摘要、请求/响应 hash 与稳定终态；不保存提示词、响应正文或凭据。Transform Run 详情显示 pin 和调用摘要。模型/provider 制品复现性仍为 `unverified`。新增 Alembic 0078/0079，未应用。

> **M0.1r Pipeline LLM 不可变调用事件链源码增量（2026-11-01，未验收）：** 每次 Route C provider call 的 `ModelInvocation` 状态变化在同一事务追加到 `PipelineLLMInvocationEvent`；事件包含开始、provider 结果和输出校验失败，固定请求/响应 hash、稳定错误码、Run/source/config/Prompt lineage、序号、前序摘要和 SHA-256 事件摘要。PostgreSQL/SQLite trigger 与 ORM hook 拒绝 UPDATE/DELETE；唯一约束固定同一 invocation 的事件序号，父调用被事件引用后不能删除。Run Detail 按事件链重算摘要，并在 Transform 展示已验证、校验失败或旧记录无事件三种状态；不会为迁移前记录伪造历史事件。调用 attempt 序号在 Run 行锁下跨 worker 续增，并有唯一部分索引防止重号。ModelInvocation 当前态仍用于快速显示；事件表是 Pipeline Route C 的不可变历史，不代表其他 Construction/Mapping/LLM 路径已经统一。新增 migration 0079，未应用；没有运行自动测试、ORM mapper、migration、worker、数据库、HTTP/E2E、ACL 或浏览器验收。M0.1/M2/E2E-01 完整 Gate 仍开放。

> **M0.1r Construction MappingTask 固定 prompt/parser 与不可变尝试事件链源码增量（2026-11-01，未验收）：** MappingTask 输入 manifest 新增来源分析/本体映射 Prompt digest、parser/normalizer 版本与 tool 版本清单；worker 重算 manifest 时发现这些合同摘要变化会失败关闭。每个 MappingTask attempt 和两阶段 provider call 追加 `ConstructionMappingAttemptEvent`，保存 DatasetVersion、ModelConfig、Prompt/parser/normalizer 摘要、请求/响应 digest、状态与稳定错误码，不保存提示词、响应正文、多模态字节或凭据。事件以 task 行锁分配序号并串成 SHA-256 链；PostgreSQL/SQLite trigger、ORM hook 与唯一约束保护 append-only 和序号，父任务/调用被事件引用后不能删除。GET MappingTask 返回链校验状态与最近事件，常规/多模态构建页可展开查看。旧任务缺少固定 manifest 时 worker 与手动 retry 均失败关闭，要求重新预检创建新任务。新增 migration 0080，未应用。后端 `py_compile`、前端 production build（2,833 modules，659ms）、迁移图静态检查（88 revisions、单 head 0080）和 `git diff --check` 通过；未运行自动测试、ORM mapper、migration、worker、模型、HTTP/E2E 或 ACL 验收。Construction 资源级读取授权、模型制品可复现性、M0.1/M2/E2E-01 仍开放。

> **M0.1r 旧版 Extraction 写入口退役源码增量（2026-11-01，未验收）：** 文档抽取 POST 接口对本体所有者/管理员返回明确的 410 退役结果；InfoTab 不再提供直接抽取操作，并可跳转到版本化常规数据构建页。新构建页读取 `ontology_id` 参数并预选可追加的本体。旧 Worker 对已排队任务仅原子 claim 后标记失败，不调用模型、不写 canonical ontology；旧直接抽取 CLI 与带固定凭据的全流程脚本已停用。遗留任务状态读取按本体所有者/管理员授权并隐藏私有 pin。无新增 migration；backend 静态编译、frontend TypeScript/Vite production build（2,833 modules，816ms）与 `git diff --check` 通过。未运行测试、模型/worker、HTTP、数据库或 E2E。现有 `test_data` 与 Playwright 全域脚本仍调用旧接口，须改接 Dataset-based Construction 流程后才能作为有效 E2E；新流程的独立发布审批也待实现。M0.1 ACL Gate 与 M2.3 未关闭。

## 0. 决策摘要

Project 4 的下一阶段目标不是增加更多演示页面，而是让数据接入、Ontology、对象查询、时序情景、AI 推理、受控 Action 和审计在同一业务上下文中工作。主线是先冻结稳定的对象、版本、来源、权限和执行合同，再把 Wood、FactoryNet、I-BADAS 等演示接到统一运行时。

本轮结论：

1. **木材演示关系图使用恢复的 Cytoscape 力导向布局。** 最近只读 HTTP 复核 Baseline/B/C graph 均为 200，返回 289/309、292/313、292/313 个对象/关系，context token 匹配；Supplier B 页面可见。无 golden screenshot 和历史截图签收，因此页面视觉相似度仍未评分。
2. **最新的上游功能代码与 GitHub 默认分支不是同一个 ref。** `origin/master` 当前是 2026-07-24 的提交；找到的最新上游功能分支是 `origin/feature/whatif-reasoning`（2026-09-07）。建议把后者作为新开发的功能参考基线，同时保留 `origin/master` 为上游默认分支基线。二者不能混称为“GitHub main”。
3. **Action/Agent Session 仍在逐步收敛。** Agent 的公开 HTTP 轮次持久排队并由 Celery worker 执行；本地演示 DB 已到 0064（包含 0056 participant 与 0058 lease schema），owner-issued viewer、scope 等价校验、撤权和租约控制源码/UI 已存在，但真实 HTTP 权限矩阵、崩溃恢复与 E2E 尚未验收。本体页默认进入持久 Session→DecisionCase；旧 AgentDecisionTab 仅作线索迁移兼容入口，新 proposed 运行不能再直接确认写入，已有 DecisionRecord 只读；未固定权限上下文的旧 AgentRun 详情需重新运行。工具目录 schema digest 已绑定新 AgentRun 与 checkpoint，并在执行/恢复时核对；兼容的旧运行仅在完整工具名/版本仍一致时可继续。版本化 Agent Definition、可配置工具授权、scoped Manager、checkpoint/tool-attempt 恢复和跨应用续接仍开放。Action 的 Preview/Review/Commit/Run 状态机与跨存储恢复也未统一。源码/定向测试不等于跨模块 E2E 与生产治理验收通过。
4. **最重要的缺口是共享合同和运行证据。** 具体包括统一资源登记和身份主体、单一 Action 执行路径、持久且可协作的 Agent Session、可验证的数据来源与版本链、对象/字段权限贯穿、跨系统 trace、数据质量与恢复演练、规模基准。
5. **两人并行、一个共享合同：** 人员 A 负责语义核心和数据可信；人员 B 负责 Action/Agent 运行时与用户工作台。双方先冻结 API/状态/权限/事件合同，再纵向集成，避免继续形成两套解释器。
6. **OntoFlow 对照补充了产品交付层差距：** 产品 README 描述多轮 Explore、Agent 与独立发布、数据资产目录、可视化数据到本体 Workflow、Query 复用为页面/API/Skill/MCP、隔离推演、知识库、孪生页和系统设置。Project 4 需补齐端到端 workflow、共享查询与发布、Agent Definition/Session、Scenario checkpoint/branch、知识库生命周期及平台控制面。OntoFlow 产品仓库目前只公开 README/License；较细的公开技术文档主要在其底层 AbutionGraph 文档库，二者不能混称为 OntoFlow API 规范。性能、安全和交付成熟度均待独立验证；3D Twin 状态存在材料差异。
7. **当前 M0–M8 不能承诺“对整个 Palantir 页面和功能达到 90%”。** 对 11 个制造/供应链工作面分别冻结直接参照、组合参照和 Project 4 专属页面；只对有逐页官方 UI 参照的直接可比页面计算 Palantir 页面结构/交互相似度，均分目标 ≥90，至少 90% 的纳入页面单页 ≥90，P0 页面 ≥90、其余 ≥85。组合参照对齐度和 Project 4 专属页面 UX 分数各自单列，均分目标 ≥90，P0 页面 ≥90、其余 ≥85；它们不冒充 Palantir 逐页相似度。Palantir 可比功能覆盖率和本项目 Must-have 任务通过率仍分别以 ≥90 为目标，所有 P0 与权限/写入安全任务须 100% 通过。上述均为计划指标，完成前不能宣称已达到；M0.4 必须公开 11 页分类及分母，不能静默缩小范围后称作全产品 90%。
8. **按用户要求先冻结计划，再按完整目标推进实现。** [M0.1 当前能力与证据盘点](PROJECT4_M0_BASELINE_CAPABILITY_INVENTORY.md)、[页面与任务验收矩阵](PROJECT4_SCREEN_AND_TASK_ACCEPTANCE_MATRIX.md)、[Palantir 可比功能目录](PROJECT4_PALANTIR_FUNCTION_PARITY_CATALOG.md) 和 [M0 端到端验收方案](PROJECT4_M0_ACCEPTANCE_SCENARIOS.md) 是 M0 候选基线；功能目录当前列出 170 个候选叶项，覆盖 38 个父项；另对 ACT-03、APP-02、CONN-01、DEV-02、MAP-01 提出本期外排除提案。上述叶项和排除仍待 M0.4 签署，170 不是正式分母或通过数；下一步仍是完成逐路径裁决，冻结 11 个工作面的任务/角色/数据/证据及功能目录范围决定，并复核估算，再按 M1–M8 关闭实现与验收。H1 本体模型器已有局部 Object/Property/Link/Rule 表单和 Settings 权限策略创建/查看/删除入口；时序 UI 当前证明的是来源/运行配置，不是 EventType schema 编辑。版本 diff、影响预览、独立审批发布/恢复、权限编辑/生效预览和跨页面撤权仍待验收；M2.3 已拆出对应工作包，M0.4 冻结高级类型子集。目标架构明确要求的批量 Action、人工覆盖、项目运营队列和人员负载分配必须纳入 Must-have，M0.4 冻结具体语义、公开参照与验收分母。计划 Gate 尚未签收；Agent 后台轮次只是已验证的局部实现，不代表整体目标完成或允许宣称相似度达标。M0.1 计划材料边界：尊重 `.gitignore` 将 `docs/*` 作为内部材料、不进行 Git 同步的现有规则；本地 PRD、目标架构、页面矩阵、功能目录、E2E 与能力盘点互相引用供本机规划使用，不宣称全部随分支分发。原 87/88 项状态台账和 111 路径匹配均为历史快照；当前工作树为 133 个变更路径（87 tracked modified + 46 untracked），尚未逐 diff 全量语义签收。本轮不修改 `.gitignore`；若日后要向团队同步完整计划包，再单独确认授权与分发路径。本计划中的相似度指标和工期均为范围受限的目标/估算，不表示已经实现或通过验收。

## 1. 基线、证据与事实边界

### 1.1 Git 基线核对

| Ref | 核对到的提交 | 时间 | 本文中的用途 |
|---|---|---|---|
| `origin/master` | `0a80458a1b75615bb88a66a1af8d676cf8d64612` | 2026-07-24 | GitHub 默认分支快照；本次比较的上游保底基线 |
| `origin/feature/whatif-reasoning` | `eddd0dfd8d8d7d758687e8af1c243394691b4a13` | 2026-09-07 | 当前可见的较新上游功能树；建议作为下一阶段功能对照基线 |
| 本地 `palantir` | `8005e668e21a537aa15c623215df67634a2e8b5a` | 2026-10-22 | 包含后续本地原型提交；不等于 GitHub 上游基线 |

上游 `feature/whatif-reasoning` 的可见新增内容包括 Semantica reasoning workbench；本地分支的 `plan-a-2.0`、Object Explorer、Supplier Study、Decision Lab、DecisionCase/Action 等属于上游快照之后的本地工作。新阶段开始前应记录上述 SHA，并为每个要保留的本地能力建立“源码位置、依赖合同、验收证据、是否采纳”的清单。**不在本 PRD 中执行 rebase、删除分支或覆盖工作树。**

### 1.2 实现状态标签

- **源码存在**：在检查的本地工作树找到实现；不代表接口已统一或生产就绪。
- **部分具备**：有页面、路由、模型或局部演示，但跨入口、权限、恢复、规模或验收证据不完整。
- **原型 / 未采纳**：可用于评审和学习，须经合同和安全审查后才能进入目标架构。
- **待核验**：仓库材料或历史记录提到，但本轮没有在目标分支、目标数据和目标运行环境复验。
- **规划**：本文提出的需求，不得写成已经交付。

### 1.3 现有材料如何使用

- 用户附件 [Palantir Project 4 能力补集与目标架构](PALANTIR_PROJECT4_COMPLEMENT_AND_TARGET_ARCHITECTURE.md) 用于理解期望的产品范围，不视为代码操作指令。
- [旧版闭环 PRD](PROJECT4_PALANTIR_GAP_AND_CLOSED_LOOP_PRD.md)、[Plan A/Plan B 集成合同](PLAN_A_PLAN_B_INTEGRATION_CONTRACT.md)、[人员 A 计划](PLAN_PERSON_A_SEMANTIC_CORE_SECURITY_DATA_PLANE.md)、[人员 B 计划](PLAN_PERSON_B_OPERATIONAL_RUNTIME_UX.md) 提供此前设计与局部实现线索。它们的“已完成”只对各自记录的分支和日期有效，进入本阶段前须按当前基线重审。
- Palantir 能力参照使用官方公开文档，例如 [Ontology system](https://www.palantir.com/docs/foundry/architecture-center/ontology-system)、[Object edits](https://www.palantir.com/docs/foundry/object-edits/overview)、[Agent Stack](https://www.palantir.com/docs/foundry/aip/agent-stack)、[Workflow Lineage](https://www.palantir.com/docs/foundry/workflow-lineage/overview)。下文的产品映射是基于公开行为的推论，不代表其内部设计相同。

### 1.4 OntoFlow 资料范围与证据强度

本稿将“OntoFlow”限定为北京图特摩斯科技/ThutmoseAI 的本体智能应用平台：检查其 [OntoFlow GitHub 仓库](https://github.com/ThutmoseAI/OntoFlow)、[厂商产品页](https://www.thutmose.cn/ontology-flow.html)、厂商账号发布的 [AtomGit 产品说明](https://gitcode.csdn.net/6a0be7e010ee7a33f273a4ce.html)，以及底层图数据库 [AbutionGraph 的官方文档目录](https://github.com/ThutmoseAI/AbutionGraph/tree/main/docs)。截至本次核对，OntoFlow 仓库页面只有 README 和 MIT License；目录中的技术文档属于 AbutionGraph，包括技术简介、快速开始、功能概览、扩展、解决方案和 Aremlin API（含查询、过滤、转换、聚合、数据源与元数据主题）。它们可说明公开的数据模型/API 示例，不能直接证明 OntoFlow 产品内部实现或 API 兼容性。网上还存在名称相同、用途不同的 [H2020 OpenModel OntoFlow](https://github.com/H2020-OpenModel/OntoFlow) 和 [InfAI ontology development workflow](https://gitlab.com/infai/ontoflow)；这两个不作为本节产品对照对象。

| 资料 | 能证明什么 | 不能据此推定什么 | 本 PRD 的用法 |
|---|---|---|---|
| ThutmoseAI/OntoFlow GitHub README | 厂商公开描述的产品模块、用户流程和目标；仓库公开显示 README 与 MIT License | README 不展示 OntoFlow 实现源码、API 规范、运行时故障/恢复/安全验证；仓库许可证也不能自动代表独立 AbutionGraph 产品的许可 | 提炼“数据到本体流程、Query/Skill/API/MCP 复用、Agent 发布、隔离推演”等目标合同；逐项标为待验证 |
| 图特摩斯官网产品页、厂商账号说明 | 供应商公开列出的功能范围与产品定位；说明多源接入、GraphRAG、Agent、Action、RBAC 等能力方向 | “100+ 接入”“毫秒/亚秒”等宣传指标未附本项目可复现的硬件、数据、负载和方法；不能直接作为对比跑分 | 借用产品闭环和用户体验问题，不把宣传数字复制为 Project 4 的 SLO |
| AbutionGraph 官方 README/技术文档 | 底层公开了 T/P/F/Agg/Action/Role、查询与转换/过滤/聚合示例、时序/流式、向量检索、用户/图权限和动态函数等技术主题 | 不能说明 OntoFlow 与 AbutionGraph 的具体集成方式，也不能证明 Project 4 当前依赖具备等价语义、隔离强度或事务能力；文档中的动态编译函数没有给出本项目可复用的沙箱安全合同，MemoryGraph 还被文档标为实验能力 | 借鉴资源建模和 typed query 的问题拆分；动态代码只作为风险提示；保留 PostgreSQL 为权威存储，技术替换另做 PoC |
| Twin 状态差异 | GitHub README 描述可交互可发布的孪生应用；官网将“数字孪生 3D 可视化”标成“在建”；厂商说明也把行业 Twin 描述为用户自建 | 公开材料不能确认 3D Twin 在何种版本、客户租户或部署形态已交付 | Project 4 的必做验收限定在可复现的 2D/关系/时间/指标业务 Twin；3D Builder 另列后续，不宣称 OntoFlow 已普遍交付 |

所以，本节报告的是**供应商公开产品描述与 Project 4 目标/实现状态的差距**，不是第三方实测的竞品能力审计。外部产品若无可核 API、部署包或可重复 Demo，记录为“公开声称、待验证”，不推断内部实现；AbutionGraph 示例只帮助补充设计问题，不作为 OntoFlow 的 API/安全合同。

## 2. 项目目标对照：八项都要落到验收

| # | 项目目标 | 本地代码可见能力 | 主交付阶段 | 验收任务 / 发布 Gate | 主要缺口 / 本阶段完成信号 |
|---|---|---|---|---|---|
| 1 | 原始数据到 Ontology 的可视化转换链 | 文件、数据库/API、Pipeline、Dataset、构建与映射入口；结构化、时序、多模态演示 | M2.1–M2.5 | E2E-01/02 · G1 | 统一 `Source → DatasetVersion → TransformRun → OntologyRevision → Object` 的身份、状态和可回看血缘；一次失败可定位、重试、回填 |
| 2 | LLM 辅助 Ontology 构建 | 有 LLM 抽取、概念/实例分离、提示词/Reasoning 相关能力 | M2.3 | E2E-01 · G1 | 候选固定到不可变 DatasetVersion、来源选择、模型/Prompt/Tool/parser/normalizer 版本与输出 digest；候选生成、人工确认和独立发布审批分离；旧 extraction 写入口不得绕过该发布闸；可用固定 benchmark 比较版本 |
| 3 | 数据变化时动态更新，不全量重建 | 有动态数据/演化、变更和时序原型 | M2.4；时间语义扩展在 M5 | E2E-02/03 · G1/G3 | 数据-only 更新固定复用 approved OntologyMapping ID/digest 与 OntologyRevision/schema hash；不创建本体构建任务或触发 ontology mapping 调用；schema drift 走独立提案/影响预览/审批。通用更新合同覆盖事件 ID、offset/checkpoint、幂等、迟到事件、增量影响范围、失败恢复；结构化快照删除前证明 manifest 与完整读取，截断输入零删除；保留旧版本和变更来源 |
| 4 | what-if 与时序模拟，含 Temporal KG / 向量检索 | 有 temporal construction/replay、scenario、graph、部分向量检索 | M5 | E2E-03/04 · G3 | 一个时间上下文贯穿对象查询、图、Agent、Scenario 和 Action；区分业务有效时间与系统接收时间；向量只辅助召回，不作事实权威 |
| 5 | Digital Twin 与决策模拟 | Wood/FactoryNet/AI4I/I-BADAS 等领域演示和模型原型 | M4–M5 | E2E-04/05 · G2/G3 | 明确观测、推断、期望、模拟四种状态；可回放、可比较、可解释；Wood Supplier B 影响链与推荐卡绑定已签收的规则版本、输入快照和证据；不能形成可靠建议时说明原因；接受建议只进入 DecisionCase/Action Preview，真实写入仍单独审批和隔离 |
| 6 | 可视化辅助工具和演示 | 中文工作台、Object Explorer、图、时序、What-if、Demo 路径 | M3、M5–M7 | E2E-01/03/04/05/06；11 页视觉 Gate · G2–G4 | 共享 Object View/Panel、查询状态、图和时间轴；每个结果面板显示授权范围内的来源、权限裁剪、修订版本和运行状态；状态切换不丢上下文；E2E-04 从指标沿影响链到对象/证据并显示可解释推荐；“演示适配器”不冒充通用生产入口 |
| 7 | LLM/Agent 推理和决策支持 | 有 Reasoning/Logic 原型；Session/Event/Checkpoint 已有 queued Celery worker、Beat 补投和 queued 状态 UI，定向测试通过 | M4、M7 | E2E-05/06 · G2/G4 | Agent Definition/版本/发布、participant ACL、Manager、租约 fencing/恢复演练、选定 Function/Logic 版本/评估/监控、正式 Tool Registry 与统一授权、TS/Python API clients 和跨应用共享仍待实现；Agent 不绕过唯一 Action Runtime |
| 8 | 稳定性、吞吐、响应和生产准备 | 健康检查、局部基准、幂等/运行记录和重启材料 | M8 | E2E-08 · G5 | 建立目标硬件与数据档位的 P50/P95、吞吐、错误率、投影滞后、恢复时间；备份恢复、审计、限流、告警和发布回滚有证据 |

状态说明：上表是对当前本地工作树和历史记录的产品审计，不是对 `origin/feature/whatif-reasoning` 的完整验收，也不表示八项已达生产质量。功能是否真正贯通，以统一合同和第 12 节的验收门槛为准。

### 2.1 老师提出的四类横向硬要求

下面四类横向能力与八项目标同属最终验收范围；不能因为它们跨多个页面，就只做成某一个页面的展示字段。

| 硬要求 | PRD 落点 / 主阶段 | 验收任务 / 发布 Gate | 最终验收证据 |
|---|---|---|---|
| 行业本体建模工具：可视化配置对象、关系、规则、事件与权限 | FR-01/05/08；M1、M2.3、M8 | E2E-01 · G1 | 管理员能创建、校验、发布带版本的 Object/Link/Rule/Event/Permission 配置；坏引用、非法约束和未授权发布会被阻断并留审计 |
| ERP、MES、SCADA、IoT、文档和图像到统一语义的数据映射 | FR-02/03/11；M2.1–M2.4、M7 | E2E-01/02/03 · G1/G3 | 为项目选定的每种来源族提供至少一条可复现导入/映射路径；预览字段和多模态片段到 Ontology 的候选映射、质量结果及来源证据；缺少真实连接器时标注适配器/fixture，不宣称通用连接器已完成 |
| 可执行的业务规程、权限与合规策略 | FR-05/06/08；M1、M4、M7、M8 | E2E-05/06/07 · G2/G4/G5 | 同一服务端策略决策约束 Query、Agent 工具与 Action 写入；策略版本、拒绝原因、人工批准和审计事件可查询 |
| Ontology、数据、模型、Prompt、工具、输出之间的版本与血缘 | FR-01/02/06/08/09；M1、M2、M4、M5、M7、M8 | E2E-01—08 · G1—G5 | 任一业务结论或 ActionRun 可追溯到输入数据版本、Ontology revision、模型/Prompt/Tool/Function revision、执行人/权限摘要和输出 digest；可重放或解释不可重放原因 |

## 3. Palantir 有、Project 4 缺少或尚未统一的功能清单

Palantir 公开资料把 Ontology 定义为把企业数据、逻辑、操作和安全连接起来的决策模型；对象不仅用于显示，还服务于分析、工作流和 AI。按本项目的目标范围，主要补集如下：

| 能力域 | 对方公开可见能力 | Project 4 当前判断 | 优先补齐什么 |
|---|---|---|---|
| 统一业务对象 | Object / Link / Property / Action / Function / Security 共用语义 | 新旧 Entity/Relation/semantic resource 并存；部分页面各有模型 | Canonical ID、revision、typed property/link 和统一 object context |
| 数据连接与加工 | 多源连接、批/流式 ingest、调度、质量、回填和数据健康 | 已有若干 connector、pipeline、construction 和 demo adapter | 连接生命周期、schema drift、checkpoint、质量闸口和重放合同 |
| Object Backend | 对象集合查询、关系遍历、过滤、聚合、索引、写回/物化 | Postgres/FalkorDB/Object Query/Object Set 已有，但跨入口行为需证实 | 统一查询授权、快照、游标、投影滞后与写回语义 |
| Explorer / Object View | 搜索、filter、pivot、保存集合、对象面板、Action 和导出 | 有对象查询、图和详情页；状态/组件尚未完全复用 | 查询 builder、可分享 saved view、full/panel 视图和跨页选择上下文 |
| 分析与应用编排 | 对象分析、时间序列、图分析、可嵌入业务应用 | 有多个专用页面和局部图表 | 版本化分析资源和可重用工作台组件；先有稳定组件合同，再做低代码 builder |
| Action / Function | 类型化参数、验证条件、权限、预览、提交、执行日志、服务端逻辑 | live、Scenario、Supplier 等运行路径仍需统一；Logic 多为本地执行器 | 唯一 Action Compiler/Runtime，统一 Live 与 Scenario 的 EditBatch、鉴权、幂等和审计 |
| Scenario / Simulation | 隔离情景、编辑、比较、策略评估、合并/写回 | 有 `ScenarioResource/QueryDataView` 与 `WhatIfScenario/WhatIfRun` 两套模型及多组路由；跨页传播、baseline/ACL/time 合同不一致 | M1 裁定 canonical Scenario API/model，并定义旧路径适配、迁移或退役；Tracking/Pinned 语义、基线、数据版本、执行上下文、冲突和比较可重现 |
| AIP / Agent | 有 Ontology context、tool/logic 编排、持久会话、trace、评估和治理能力 | 本地工作树已有 AgentSession/Event/Checkpoint、本地模型、人工审核提案和后台 queued turn worker；当前会话读取按 owner 过滤，未证明参与者 ACL、Manager、跨应用续接或完整 Definition/Publish/评估闭环 | 稳定 Tool Registry、参与者授权/撤权、Agent Definition 版本发布、跨应用续接、证据版本链、离线评估和与唯一 Action Runtime 的端到端集成 |
| 平台级治理 | 资源权限、对象/字段可见性、数据标记、lineage、审计、AI observability | 有权限服务、manifest、运行记录和局部 benchmark | 所有请求同一权限决策；端到端 lineage、字段脱敏、不可变审计、trace/log/metric |
| 运维与生态 | 开发者 API/SDK、环境发布、资源健康、配额、告警、应用交付 | 以本地 API/React 页面和 Compose 为主 | 先完善本地部署/备份恢复/升级/限额；外部 SDK 与插件后置，Tenant/Project/Use-case 隔离是否纳入本期由 M0.4 裁决 |

这份清单只陈述公开可见能力和本项目的差距方向，不意味着每项都要照搬。项目优势应聚焦制造/供应链业务语义、可审计决策闭环和可复现实验，而不是追求菜单数量或复制产品外观。

### 3.1 下一阶段功能清单（优先级与交付边界）

| 优先级 | 功能包 | 具体交付能力 | 当前判定 / 完成信号 |
|---|---|---|---|
| P0 | 统一语义与执行上下文 | 查询、图、Scenario、Agent、Action 共用 ontology/data/scenario revision、principal、权限摘要、trace 和供页面消费的 ResultManifest | 当前部分具备；相同请求上下文在各入口返回相同对象、版本和权限结果，且结果面板显示相同来源/权限/版本/状态语义 |
| P0 | 行业 Ontology 可视化建模器 | Object/Property/Link/Rule/Event/Permission 的配置、关系校验、版本差异、影响预览、草稿/发布 | 老师要求的必须交付能力；M2 先交付受控类型和约束的可用建模器，不扩展任意脚本编辑器 |
| P0 | Source 到对象的版本血缘 | Source、DatasetVersion、TransformRun、Quality、OntologyRevision、Object/Evidence 可追溯 | 有若干 manifest/来源能力；须可定位具体来源、重放构建并回看历史版本 |
| P0 | 唯一 Action Runtime | typed ActionRequest、preview digest、授权复核、人工审批、幂等、冲突、审计与补偿 | 有多条 Action/Scenario 路径原型；完成条件是所有写入口共用一条 runtime |
| P1（目标架构必做） | 选中对象集的批量 Action | 固定 ObjectSet、批次预览、逐对象授权/版本校验、单项结果、幂等重试和批次审计 | 官方 Object Explorer 提供直接页面参照；M0.4 冻结对象集任务、UI 规模边界、耐久批次策略和评分 rubric，不得从 Must-have 中删除 |
| P1（目标架构必做） | 有审计的人工覆盖/例外处理 | 由有权角色调整推荐/自动决策或显式例外；记录理由、前后值、关联 Case/Run、政策版本和批准链 | M0.4 冻结可覆盖的决策种类、角色和组合参照 rubric；即使无法一对一映射到公开 Palantir 页面，也保留为 Project 4 Must-have |
| P0 | Agent 定义、持久会话与管理控制面 | 版本化 Agent Definition、Session/Event/Checkpoint、Agent Manager 式定义/会话列表与详情、运行状态、暂停/恢复/取消/撤销、人工接管、预算、版本锁定、证据引用、评测与发布状态 | 单次 Agent Run 与持久会话原型不等于产品能力；操作员能按权限管理已发布定义与活动 Session，重启恢复且不会重复副作用才算完成 |
| P0 | 平台治理贯穿执行 | 统一服务端授权、对象/字段安全、不可变审计、trace/log/metrics、保留/脱敏 | 有局部权限和运行记录；Agent/Action/Query 均须通过同一授权与审计链 |
| P0 | Scenario 与时间语义 | Tracking/Pinned 生命周期、valid/event/ingest time、固定输入、可重复比较 | What-if/时间页面部分具备；历史对象、图、证据和结果需在同一时间上下文 |
| P0 | Supplier B 影响链与推荐卡 | 从 Wood Baseline/B/C 的指标变化钻取至受影响供应商、物料、订单和证据；按经签收且有版本的决策规则呈现推荐、备选、权衡、风险和数据缺口 | 属于目标架构 §12 P0 的代表业务任务。规则/输入未签收、过期、不完整或受权限裁剪时必须显示“无法形成推荐”及原因；接受建议只能创建 DecisionCase/Action Preview，不直接提交 |
| P1 | Object Explorer 共用工作区 | 搜索、过滤、pivot、保存集合、table/graph/timeline、全屏/侧栏对象视图 | 有对象查询、图和详情；完成后可跨页保存与恢复同一上下文 |
| P0 | Data-to-Ontology 可视化流程 | 版本化 Workflow/Node/Run；代表性 ERP/MES/SCADA/IoT/文档/图像来源的 Source、Transform、Mapping、Quality、Ontology Publish 节点；支持校验、dry-run、日志、重跑 | 老师要求的必须交付能力；连接器按已验收的代表路径声明范围，不以数量替代可重放语义映射 |
| P1 | Query/Skill/API/MCP 复用发布 | 一个有版本、有输入输出类型和权限合同的 Query/Function 能供 Explorer、应用、Agent、HTTP API/MCP 共用 | 当前查询与调用面尚未成为统一发布资产；同一 query 的返回结果和授权须一致 |
| P1 | Logic/Function 版本、发布、评估与监控 | 为项目选定的确定性/LLM 辅助函数建立 typed I/O、稳定 ID、不可变版本、发布/撤回、固定评估集、运行记录与成功率/失败率/P95 指标 | 当前有本地 Logic Asset/deterministic executor，但未证明完整生命周期；不扩展为任意代码或外部函数沙箱。M1 冻结资源/API 合同，M7 做版本与评估，M8 接入运行指标 |
| P1 | 选定能力的 TypeScript/Python API 客户端 | 从 OpenAPI 为已发布 Query/Function/Agent API 生成或维护最小 typed TS/Python client；共享类型、分页/错误处理和 caller scope | 原目标架构 P1 明确要求 SDK；纳入本期受限能力。完整 OSDK、Developer Console、Marketplace 和多语言生态仍是 P2 |
| P1 | 业务应用与 Twin 工作页 | 把已批准 Query/Scenario 组合成可分享的运营页，显示状态、趋势、告警、影响链与问数入口 | 先交付可复用 2D 页面/组件；3D Builder 不作为本阶段阻塞项 |
| P1 | 调度、Monitor 与运行历史 | durable run、cron/event trigger、并发策略、取消/重试、阈值告警、完整 Run/Action trace | 当前有局部运行记录；需要统一为可观察、可取消、可重放且幂等的任务生命周期 |
| P1 | 数据增量、质量与恢复 | checkpoint、offset、迟到事件、schema drift、重放/回填、失败隔离 | 有动态演示原型；需以显式 Source/Mapping/Revision/时间策略形成通用合同 |
| P0 | LLM 本体建议与人工审阅 | 类型/关系/映射/CQ 候选、来源、提示/模型版本、不确定性、差异预览和批准 | 老师要求的必须交付能力；候选不直接进入 canonical 本体，固定 benchmark 衡量质量 |
| P1 | Knowledge Base 生命周期与混合检索 | 文档接入/解析、Chunk 与 Index revision、权限继承、混合召回、来源引用、增量更新/重建/删除 | 有文档和向量原型；运行态、血缘、权限过滤和 Agent/Explore 共用入口未闭合 |
| P1 | 最小平台控制面与资源登记 | 为 Ontology、Dataset、Workflow、Query、Action、Scenario、Agent、Knowledge Base、Application 建立稳定 resource ID/type、owner、ACL、revision、lifecycle、依赖关系；统一 human/group/service/system principal；补齐 Provider/Tool scope、凭证引用、发布撤回、持久任务与审计 | 已有模型配置、调用日志、用户角色、数据权限和 cron 配置原型；缺统一资源目录、身份/凭证生命周期、跨入口授权与 durable schedule/run 运维合同。单项目资源治理属于本期；多租户隔离与 marketplace 后置 |
| P1 | 运维与规模证据 | 项目级任务队列、候选负载分配、P50/P95、吞吐、投影延迟、配额、备份恢复、发布/回滚 | 需基于实际部署与固定数据档位设 SLO，不能从 Demo 规模推断 |
| P1 | 面向本项目的应用组合层 | 用已批准 Query、Object View、分析卡片、Scenario 与 Action 组成可分享的制造/供应链工作页；支持配置、预览、版本、发布/撤回 | M6 验收；覆盖目标业务流程，避免每个垂直演示复制一套页面 |
| P2 | 通用低代码 Builder 与完整交付生态 | 任意拖拽应用搭建、公开 SDK/市场、完整多租户平台/资源市场、广泛连接器扩展 | 不作为本期 90% 目标分母；须在语义、权限与应用模块稳定后另立项目 |

P0 是建立老师要求的可安全集成底座，并完成数据转换、LLM 建模等核心能力；P1 是产品化核心页面、可复用工作流和配置式应用组合；P2 才进入通用低代码 Builder 与完整外部生态。该优先级是下一阶段建议，不代表本轮已经实现。

### 3.2 OntoFlow 产品资料对照与项目取舍

| OntoFlow 公开描述的产品能力 | Project 4 目前状态 | 本计划的具体补齐与取舍 | 主责 |
|---|---|---|---|
| 可视化串联“需求 → 数据源 → 处理 → 子图映射 → Ontology → 应用”，AI 辅助生成处理代码 | 有多条数据构建向导、mapping、pipeline 和领域 Demo；未统一为可版本化且可重放的跨阶段 Workflow Run | 先支持固定、强类型的核心节点；节点配置、代码/Prompt 版本、输入 digest、质量结果、输出 revision 与失败重试进 manifest。生成代码只作为待审核候选，禁止自动发布/执行未知代码 | A：节点契约/语义；B：运行器/画布；共同冻结 Workflow API |
| 数据目录、来源元数据、连接器和表字段自动采集 | 已有 Source/Connector/Dataset 页面与部分映射能力 | 做统一 Source Registry 与字段级 mapping preview、schema fingerprint、数据质量/新鲜度和 source-to-object lineage；先做项目现有数据库/文件/API/事件源，不追“100+ connector”数量 | A |
| 一条本体 Query 可以给页面、接口、Skill、MCP 和 Agent 使用 | 有 Object Query、局部 Function/Reasoning 和 MCP/Agent 原型；缺少版本化的统一发布资产和统一权限传播 | 新增 FR-09：将 Query/Function/Skill 作为 typed、versioned resource；从同一版本生成 UI/API/MCP/Agent tool binding；为输入输出、权限摘要、超时/页大小、兼容与撤回立合同 | B 消费 A 的 Query/Authorization contract；共同 |
| 对话工作台、多轮 Agent、Memory、工具/知识库挂载、Agent 独立发布 | 有单次 Agent Run/context 和持久会话工作树原型 | FR-06 扩展 Agent Definition 生命周期：draft → eval → approved → published → deprecated；Session 锁定 Prompt/Model/Tool/Ontology revisions；每个 Agent 发布链接受鉴权、额度、审计、撤销与保留策略 | B；A 负责其 Ontology/字段授权接口 |
| 统一 Explore 工作台与模型/工具配置 | 有 Agent、Reasoning、对象查询等分开的页面和原型；会话、模型选择、知识库、数据表、Ontology Skill 和外部工具未形成同一工作上下文 | 将 Explore 的 session/context 与 AgentSession 共用事件、权限、引用和版本合同；Provider/Model/MCP/Plugin 连接通过集中注册表管理密钥引用、可用性、范围和调用限额，不把密钥写入 Session | B：工作台/配置 UX；A：资源与授权合同 |
| 推演沙盘在隔离世界执行，支持 checkpoints、branch compare，默认不写回生产 | 有 Pinned/Tracking Scenario 与业务 What-if；跨对象、历史上下文、恢复点与 branch 的统一合同未完备 | 明确 Scenario 数据隔离、Clone Scope、checkpoint、branch/rebase/diff、run replay；默认无 live side effect；从 Scenario 提交到生产必须经 FR-05 的 Action Preview/Approval/Commit | B；A 提供版本化 Object/Link snapshot 与授权 |
| Ontology 原生 Type/Property/Function/Aggregate/Action/Role/Monitor，加时间、流式、向量/全文检索 | 新旧语义资源并存；时序、Chroma 向量检索、图投影分散在不同服务 | Canonical resource 补齐 Function/Aggregate/Monitor/Alert/Role，并区分 observed/derived/simulated；把 Postgres 权威事实、FalkorDB 投影、Chroma 索引边界写入 query/evidence contract；先做评测，不以换成 AbutionGraph 作默认路线 | A；B 负责调用侧和告警工作台 |
| GraphRAG/VectorRAG 混合检索并挂载到 Agent | 有 Chroma、图查询和文档/证据能力但未统一检索授权与来源引用 | 将 vector、全文、图邻居作为同一 Retrieval Request 的可插拔候选通路；所有召回先做权限过滤，证据带 source/version/time，向量索引可重建且不能独立成为事实 | A：检索/来源 contract；B：Agent 引用与 UX |
| 文档知识库生命周期 | 有文档/向量/证据原型；解析、分段、索引版本、权限、增量更新和删除尚未形成可运维的知识库资源 | 增加 KnowledgeBase/Document/Chunk/IndexRevision 生命周期，批量导入、解析失败追踪、混合检索、来源引用、权限过滤、重建/删除；与 Object Query 同一 caller principal | A：数据/检索血缘；B：管理界面和 Agent/Explore 接入 |
| 图谱级/子图级权限、角色、脱敏、审计、API Key | 有权限服务和 UI/运行审计，但所有读写入口未证明使用同一授权决策 | 明确 resource/ontology、subgraph/object、property/field、action/tool 四层策略及跨域关联裁剪；API/MCP/Agent 沿用 caller principal；记录决策摘要和审计 trace，不能只做菜单/RBAC | A：授权模型；B：入口和操作日志 |
| 定时任务、并发控制、运行日志、告警；数字孪生大屏/发布 | 有任务与页面原型；缺少统一 Scheduler/Monitor 合同。孪生 3D 在官网标为“在建” | 先交付固定触发器、可靠 Run 状态和 2D 业务页面；泛化调度器、低代码应用画布、3D Editor 排到后续，前提是 G2/G3 权限和运行时通过 | B；A 提供状态/时间指标 |
| 平台级系统设置与已发布业务应用 | 已有 ModelsPage 的模型/provider/API Key 配置、测试与调用记录；SettingsPage 有用户角色、数据源、数据权限等设置，连接调度也有 cron 配置入口。缺口是这些页面/服务未证明共享同一发布、授权与审计生命周期，业务 Twin 仍是固定实现 | M8 不重造现有配置页：把现有模型/用户/数据权限配置接到统一的资源版本、凭证引用、caller scope、发布/撤回与操作审计；把当前 cron 配置升级为可持久查询、取消、重试的 Scheduler/Run。通用拖拽应用/Twin Builder 列 P2 | A：配置/授权与服务端合同；B：设置页/运行页及调度观测 |

OntoFlow 的公开产品清单提示 Project 4 还要把“能建 Ontology”推进到“可复用、可发布、可监控的业务应用”。官方资料进一步暴露出四项横向能力：共享 Explore 上下文、文档知识库治理、模型/工具集中配置和发布后的运营页。项目先做可验证的最小闭环，不按菜单数量追齐；OntoFlow README 属产品描述、AbutionGraph 文档属技术示例，二者都不是 Project 4 的实现规范。AbutionGraph 的时序/向量/权限能力只作为对照，不改变 PostgreSQL 权威存储决定。

## 4. 目标产品与用户路径

### 4.1 产品定位

面向制造与供应链团队的 Ontology 决策工作台：把多个系统的数据转为有类型、有权限、有时间和来源的业务对象，帮助用户发现异常、追溯证据、比较候选方案、审批并执行本地受控动作。

### 4.2 核心闭环

```text
Source / Event
  → DatasetVersion / Quality / Lineage
  → Ontology Object / Link / Revision
  → Object Explorer / Evidence / Temporal Context
  → Reasoning / Scenario / DecisionCase
  → Action Preview / Human Approval / Action Runtime
  → Audited EditBatch / Projection / Reconciliation
  → Session, trace, lineage and outcome review
```

### 4.3 首要用户

| 用户 | 核心任务 | 主要交付 |
|---|---|---|
| 数据/本体工程师 | 接源、映射、构建、版本演化和追溯 | 可审核的数据到对象血缘与 schema 变更预览 |
| 领域分析员 | 搜索对象、看关系/时间/证据、比较情景 | 可复现的业务解释和方案比较 |
| 复核/审批人 | 核对证据、预览影响、批准或拒绝写回 | 有理由、绑定版本和权限摘要的决策记录 |
| Agent/自动化开发者 | 配置受限工具、评估运行、恢复会话 | 可回放的 Session、工具事件和批准边界 |
| 平台维护人 | 看健康、延迟、失败、成本和恢复状态 | 端到端 trace、告警、审计和运维手册 |

### 4.4 参考视频流程与 Project 4 对照

用户给出的 Palantir 入门演示是 Ontology/Object Explorer 教程，并不完整讲解 AIP Agent、统一 Action Runtime 或企业治理。因此以下只把视频中看得到的步骤作为产品工作流参照；Action、Agent Session 和治理的要求来自官方产品文档与本项目目标，不能说成视频内容。YouTube 原视频页面读取受限，本表据第三方整理的标题、摘要和 transcript 要点，并以 Palantir 官方 [Object Explorer](https://www.palantir.com/docs/foundry/object-explorer/overview)、[Workshop](https://www.palantir.com/docs/foundry/workshop/getting-started/index.html) 文档交叉核对。

| 演示步骤 | 视频展示的用户效果 | Project 4 现状 | 目标补齐/验收 |
|---|---|---|---|
| 1. 建立项目空间 | 在项目文件夹里放置本次 Ontology 资产 | 有多种 Demo、adapter 与工作台页面，但缺少统一、可版本化的“演示/应用包”入口 | Demo Manifest 固定 Ontology、来源、数据版本、页面入口、权限和预期结果；可重复初始化/清理 |
| 2. 安装教程资源并校验 | 选择 Fresh Air 教程资产、Ontology 和命名空间，完成校验与安装 | 有 Mapping、Ontology 构建和 LLM 建议，但通用的 import/preview/validate/publish 生命周期需统一 | 资源导入前做类型、标识、链接、权限和依赖检查；预览变更后由用户批准发布；失败可重试 |
| 3. 识别业务对象与关系 | 通过 airline、aircraft、airport、runway、route、flight、alert 等类型理解业务模型 | 有 Entity/Relation 和 v2 semantic resources；仍需稳定 Object Type/Link/Property/Revision 跨入口语义 | 显示类型、属性、链接方向/基数、来源映射和修订；对照已批准 schema 验证 |
| 4. 搜索并打开一个对象 | 在 Object Explorer 查找对象，读 Overview 与 Properties | 有对象查询/详情/图；搜索、筛选、详情和图的共享上下文尚未完全复用 | 从搜索结果打开标准 Object View/Panel，保留查询、revision、权限和时间状态 |
| 5. 沿关系透视到对象集合 | 从机场/航线等对象沿 links 到相关对象集，继续回答问题 | 有 Object Query、Graph 和部分 traversal；Saved Exploration、pivot 和跨页恢复未形成通用体验 | 用户可沿命名关系 pivot、查看过滤路径、保存/分享 object set，并回到前一层 |
| 6. 以业务对象逐步回答问题 | 从模型浏览推进到业务发现，而不是只看孤立数据表 | Scenario、Evidence、Reasoning、What-if 页面存在，但统一证据、时间上下文和版本引用仍需补齐 | 每个结论链接到对象、来源、版本/时间和查询条件；结果可复现、权限过滤可见 |
| 7. 从发现转为受控业务动作 | 这是本项目希望扩展的后续操作闭环；入门视频不是该能力的完整说明 | Action、DecisionCase 和 Agent 执行有部分原型/并行路径 | 用 FR-05 的唯一 Runtime 做 Preview → Approval → Commit → Audit；Agent 恢复后不重放成功副作用 |

视频明确呈现的是项目/教程资源准备、业务对象/关系和 Explorer 浏览/透视。它适合作为 Object Explorer 与 Ontology onboarding 的产品体验参照，不足以证明真实数据接入、Action 编译、Agent 持久化或平台治理已实现。

## 5. 功能需求

### 5.1 FR-01：统一语义与执行上下文

1. 每次查询、图操作、Scenario、Agent 调用和 Action 请求均携带 `ontology_id`、`ontology_revision`、`data_view_id`、`scenario_id`（若适用）、`principal_id`、`permission_digest` 和 `trace_id`。
2. Object Type、Object、Property、Link Type、Interface/shared property、Value Type、Struct/field、Derived Property、Action Type、Function、Aggregate、Monitor/Alert、Role、Evidence 和 DatasetVersion 有稳定身份与可比较版本；高级类型的本期子集由 M0.4 冻结，不能把后端类型定义存在等同于可视化编辑与发布完成。
3. 旧模型经兼容层读取；新功能不得继续新增第二套对象/关系解释器。
4. 每个 API 明确读/写语义、返回版本、权限裁剪状态、部分失败和重试规则。
5. 每个查询/计算结果返回共享 `ResultManifest`：至少包含可公开的 source/dataset version、ontology 与适用的 query/function/metric-or-analysis/action/scenario revision、ObjectSet/DataView ID 与查询/筛选 digest、时间窗口、权限裁剪/脱敏状态、freshness/run 状态、输出 digest 与 trace/correlation ID；UI 按授权显示并可打开详情。指标与结论必须能够定位其计算定义版本；未授权的对象、字段和值不得通过 manifest 暴露。
6. 当前态查询、图、统计、语义同步、编辑和导出必须统一排除来源删除的对象行/实例，并排除指向当前不可见端点的关系；计数须使用相同 revision 与权限裁剪范围。来源数据行删除、`EntityInstance.is_deleted` 与映射 Entity mirror 的 `Entity.is_source_deleted` 必须服从一项一致、可恢复的合同；Ontology Editor 中 Entity Type 的 impact-check/delete 属于独立的版本化本体操作，不得与来源行 tombstone 混为一谈。新建 snapshot 的每条关系两端必须属于该 snapshot 的 Entity 集合，active Instance 及总数只计算集合内 owner；历史 revision、审计和历史导出只读该版本的不可变 snapshot/hash，不与之后的 live 表拼接。恢复需从已验证的目标 snapshot 创建新 revision，并核对规范化内容/hash 与恢复目标语义一致；目标 snapshot 不完整时 fail closed。图质量接口可报告被隐藏/孤儿关系，但须区分原始残留数与当前可见 edge/count；重建或同步投影不得重新生成端点不可见的关系/节点。恢复只影响新版本，不改写过去。

### 5.2 FR-02：可追溯数据接入、构建与 LLM 建议

1. 每个来源具有 `source_id`、owner、连接类型、schema fingerprint、credential reference、健康状态和刷新策略；密钥不进入普通日志/manifest。
2. 每次 ingest/transform/construction 保存固定输入版本、代码/配置版本、起止状态、质量结果、输出 digest 和来源行/媒体引用。
3. LLM 可建议类型、属性、关系、约束、mapping 和 competency question；每条建议必须附原文/数据样例、模型和提示版本、置信/不确定性及拒绝原因。
4. 用户确认前，候选不进入 canonical Ontology，不触发构建写回、Action 或 Agent 工具副作用。
5. 每次发布可回滚到历史 revision；兼容/破坏性变化先展示受影响数据、查询、Action、Function、Scenario 与 UI。
6. 可视化转换流程以版本化 `WorkflowDefinition/NodeDefinition/WorkflowRun` 表示；run 锁定节点参数、代码/Prompt digest、输入/输出版本、授权摘要、质量结果和日志。AI 生成的处理逻辑先进入人工审阅与受限试运行，不可未经批准自动发布。
7. Mapping Apply 与其行级投影事件必须区分并保存请求者、Curated 审核者、执行用 system principal/worker、人工重放操作员；后台 worker 不得借用请求者或审核者身份。Run/RowChange 需关联 Mapping 与输入版本、policy scope/decision、trace/correlation ID 和不可覆盖的尝试账本；行投影自动预算按 `(mapping_id, object_id)` 工作项计算，不混入 Run 级 20 次 dispatch/8 次 execution 预算；人工重放另存理由、授权范围、操作者及结果，不重置或覆盖自动尝试历史。
8. 用户明确请求 LLM 的节点默认 fail closed：模型配置缺失、选择器异常、provider 超时/错误或输出无效时，不得静默转成规则成功。只有版本化 Workflow 明确允许 rule fallback 时才可继续；Run/节点必须保存 requested/effective method、fallback policy/reason、模型/Prompt 版本及调用状态，并显示 `degraded`。规则候选仍留在 pending-review，不能计入 LLM 成功或质量通过；凭据不进入 manifest、事件、日志或结果面板。
9. ConstructionDraft 的 MappingTask 必须在 dispatch 前固定 draft revision/content digest、selection/processing digest、精确 DatasetVersion ID/content+schema hash、ModelConfig ID/revision、非秘密配置指纹与非秘密凭据引用版本、可验证的 provider/model 制品摘要、Prompt/Tool/parser/normalizer 版本、caller/policy digest 和输出约束。来源分析与本体映射两个阶段及中间数据打包必须消费同一 manifest；worker/retry 不得重新解析 mutable latest。无法解析原 pin 时 fail closed。每个重试保留不可变 attempt ID/序号、trace 和 ModelInvocation；同一输入可在同一 manifest 下新建 attempt，输入/配置变化必须创建关联旧任务的新 MappingTask。草案关联任务处于 queued/running 时不得原地改写该任务输入：可拒绝编辑或产生新 draft revision/task；旧 task 结果只能写回其绑定的原 revision，不得覆盖或重新挂接到更新后的草案。任何密钥、API key 或可逆凭据不得进入 manifest/日志。客户端在 queued/running 期间修改输入或离开当前任务时，不得仅清除本地 task ID 并遗失观察入口：应有审计的取消请求，或保留可刷新恢复的 task/attempt 历史；后台执行结果只能绑定其不可变 draft revision。

10. Ontology audit 必须绑定实际审计输入：优先读取指定 Ontology revision 的不可变 snapshot/hash；如产品明确选择“排队时当前态”，则在 enqueue 时生成不可变 `AuditInput` manifest/hash。校验 revision 属于目标 Ontology；worker 不得只保存 `revision_id` 却重新读取后续可变 Entity/Relation/Logic/Evidence 表。审计请求哈希、ModelInvocation、发现结果与来源/权限摘要必须指向同一输入 digest，且实体/关系端点及计数在选定 revision 内一致。RepairDraft 以该输入 revision 为 base；确认时若 current 已变化，必须展示 diff 并授权 rebase 或 fail closed，不能把新 live snapshot 当内容却把旧 revision 记为 parent。

### 5.3 FR-03：增量更新与时间语义

1. 事件记录 `event_id`、`source_id`、`source_offset`、`event_time`、`ingested_at`、`effective_from/to`、schema revision 和幂等键。
2. 重放相同 offset/event 不重复生成事实或处置；晚到事件按可配置 watermark/修订策略处理并保留原事件。
3. schema 演进先跑 compatibility/impact preview；增量影响范围可解释，必要时可回填，不默认全量重建。
4. 查询明确使用“当时有效”“系统何时获知”还是“当前最新”语义；图、对象属性、Agent 证据和 Scenario 使用同一时间上下文。
5. 向量库仅提供语义召回和相似证据；返回来源 ID、版本、时间和权限过滤。KnowledgeBase/Document/Chunk/IndexRevision 可追踪解析、索引、刷新和删除状态；PostgreSQL/规范来源仍是业务事实权威。
6. 通用时序 push 的 `delete` 以追加式 tombstone 保留历史：当前视图隐藏已删除对象，删除前的 as-of 查询保留原事实，后续 upsert 恢复当前对象但不改写删除区间；恢复后再次查询删除时点仍应返回删除态，恢复时点则返回新事实。重复事件幂等、同 ID 同操作不同载荷或不同操作均拒绝。此合同与结构化 Mapping 快照删除对账分开验收；FactoryNet observation/file replay 保持 upsert-only。
7. 事件幂等摘要覆盖规范化后的完整 envelope：source、event ID、operation、对象/episode、sequence、时间类型/值/时区、payload digest 与 source reference。相同 ID 只允许完全相同的 envelope 重放；任一字段冲突均拒绝。来源特例必须显式登记；例如当前 FactoryNet 的 hash 仅覆盖 payload，不能静默视作全 envelope 幂等。
8. 对象与关系都使用追加式删除/恢复合同。M1 明确 sparse upsert 是 patch 还是完整快照：patch 只改提交字段；完整快照必须提供 schema/version 与完整性证明，缺字段不能隐式触发删除。关系新增/删除必须可追溯到事件、证据和有效区间。
9. 对每个时间字段固定 event time、valid time、system/ingest time 的语义、时区、精度与查询选择；迟到事件按版本化策略处理并保留原输入。用户不能把 ordinal、source sequence、epoch 时间戳或系统接收时间混为一个无类型的 at 参数。
10. SQL 中持久化事件与投影意图后，由具备持久 lease、心跳和 attempt ledger 的 worker 投影图；自动重试有界，失败进入可见终态/隔离队列，人工重放须单独授权、留理由并保留原尝试。FalkorDB 可由权威事实重建，并有 reconciliation 与投影滞后指标。
11. 发布 snapshot 需绑定 schema revision、DatasetVersion、事件水位、完整性计数和 hash；并发 publish 使用显式版本比较/原子切换，不能让过期 worker 覆盖更新 snapshot。
12. 数据-only 更新复用已批准的 `OntologyMapping ID + mapping digest` 和目标 Ontology schema/revision，仅对实例/关系事实应用增量；不得因此创建 ConstructionDraft/本体映射任务或调用 `source_analysis`、`ontology_mapping` 建模阶段。schema drift 必须停止数据应用并进入独立的映射/schema 变更提案、影响预览和审批；获批的新语义版本走单独迁移/回填路径。若数据 Transform 本身使用 LLM，必须作为独立 transform 节点固定版本并记录，不能计作本体重建或替代映射审批。

### 5.4 FR-04：Scenario、What-if 与 Digital Twin

1. `Tracking` Scenario 用于跟随当前数据的交互分析；`Pinned` Scenario 固定基线/版本用于审计和复现。刷新、rebase、TTL、冲突和合并行为必须明确。M0.4 对照 Palantir 官方公开的 31 天默认生命周期和 10 分钟自动 rebase 参照，建议将两者做成可配置的 Project 4 默认值；若数据保留/运行成本要求不同，冻结偏离值、告知行为和评分影响。
2. Scenario 明确隔离范围和可见对象/关系；创建后保存 checkpoint，可基于 checkpoint 建分支、逐步运行、比较差异并恢复到前一状态。
3. 每个 Run 固定输入 snapshot、Action/Function/Query 版本、参数、规则、模型、时间窗口、权限摘要和结果 digest；相同输入可回放。
4. 推演默认不对 live 数据或外部系统产生副作用；需要把结果应用到 live 时，必须经过 FR-05 Preview/Approval/Commit，不能通过 Scenario 的“应用/合并”按钮绕开同一 Runtime。
5. UI 并列展示 Baseline/Scenario 的 KPI、对象级影响、原始证据、风险和缺失数据。Supplier B 推荐卡只能由已签收、版本化的决策规则及当前有效输入生成，并展示规则版本、输入/结果版本、metric/analysis definition ID/version/digest、关键指标、受影响对象和证据、备选方案、权衡、风险与缺失数据；规则未批准、输入过期/不完整或所需数据不可见时，显示“无法形成推荐”及具体原因，不用模型补猜结论。接受建议只创建关联该证据与版本的 DecisionCase 和 Action Preview；实际批准/提交由 E2E-05/FR-05 控制。
6. Digital Twin 至少区分：观测值、推断值、期望值、模拟值、已批准值。任何连接物理设备/ERP 的真实写入作为独立高风险能力，必须经过人工审批、设备白名单、超时/停止和补偿策略。
7. 时序图支持时间游标、事件列表、对象属性快照、有效期关系和数据完整性提示。Schema 将带 start/end 时间的 Event 对象与记录测量值的 time-series property 分开建模；时间序列字段需保留单位、时间索引、采样/质量和来源版本。M1/M2.3 负责定义/校验，E2E-03 验证它与 stream source/runtime 的区别；本阶段先保证 2D 关系/趋势/指标工作页可复现，3D builder 不作为上线前提。
8. ScenarioContext 是对象查询、图、TemporalFact、向量证据、What-if、Agent 和 Action 共用的不可变输入合同，至少 pin ontology/schema revision、已发布 DataModelSnapshot、DatasetVersion/hash、replay 与时间 kind/value/timezone/watermark、Scenario branch/checkpoint、principal 与 permission digest、query/evidence digest 和 vector index revision。
9. What-if/Scenario 的每个 context、create/list/read/update/run/cancel/evidence 入口均按 ontology/object/link/field/evidence 资源执行服务端 ACL。授权在 baseline 读取和结果序列化时都生效；无权对象、字段、EvidenceRef 文本及其存在性/计数不得泄露。
10. Baseline 由服务端从授权且固定版本的对象/关系快照构造并校验。客户端提交的 baseline 只能作为选择提示，不能直接被信任；跨本体、过期 revision、失效 ACL digest、混入未授权对象或缺少时间 pin 时拒绝运行。
11. 向量检索使用与 ScenarioContext 相同的 revision、DatasetVersion、时间范围、权限 digest 和索引版本；只返回授权裁剪后的来源/版本/片段引用。向量结果是证据召回，不是业务事实，也不能单独改变规则执行结果。
12. What-if run 必须从已发布 Temporal snapshot/TemporalFact 或明确版本化的领域 adapter 读取基线；页面显示使用的时间语义、snapshot、权限裁剪、证据与结果 digest。若无法形成同一上下文，明确拒绝或展示缺失原因，不回退到含混的当前图邻域。
13. Graph read/authorization 按固定 Ontology revision 下的 canonical Link Type resource/version ID 解析关系，并同时校验 source/target semantic resource ID 与方向。旧 `Relation.type` 仅作显示/兼容标签，不是唯一键；存在 `legacy_relation_id` 映射时须验证其唯一性及端点一致。遇到未知、过期、端点/方向不符或真正歧义时 fail closed，返回可识别的结构化错误；API 将领域异常映射为稳定 HTTP 响应，页面分别呈现 loading、有效空结果、error 与 retry，不得永久停留在 loading。不得通过选首项、删行或合并同名关系绕过授权。
14. Snapshot `edge_count`、响应实际返回边数与 digest 的口径须在 M0.1/M0.3 定义并一致：说明计数发生在 ACL 裁剪前还是裁剪后；对未授权边不得泄露隐藏对象或关系的存在性/数量。当前观察到的 view/adapter 计数差异必须作为独立数据完整性问题复现、解释并关闭，不得和 Link Type 歧义合并成同一个根因。

**当前静态边界（待 M0.1 完整审阅；按当前 `codex/prd-what-if-provenance-20261101` 工作树更新）：** What-if 的 context/create/update/run 接入本体访问和共享对象/属性/关系投影；情景与运行按创建者隔离，管理员可审计；通用 TemporalReplay 基线仅从已提交事件和授权后的回放图构造，EvidenceRef 只保留可追溯元数据而不带原始正文；worker 在领取、执行和提交结果前复核 owner、权限、输入摘要和当前基线。当前新增 frozen、`extra=forbid` 的类型化 `ScenarioContext`，并由 context GET response model 对外公开该子合同；固定本体 revision/hash、已发布 DataModelSnapshot/hash、DatasetVersion/checksum、replay/source/episode、时间 kind/value/timezone/watermark、branch/checkpoint、principal/permission/query/evidence digest 与可选 vector-index revision；scenario create 绑定 branch ID，run 与重建前验证合同摘要。基线拒绝无已发布 replay、无 checksum、缺失/不匹配发布快照或与 committed events/facts 不一致的水位，并移除旧当前图邻域 fallback。上述为源码和静态编译证据，不代表真实 FactoryNet/非 FactoryNet create-run E2E。v3.94 快照当时仅接入 What-if；v3.95/v3.96 加入 Agent Session `what_if` 与 DecisionCase/Action Runtime 上下文传递和再次校验；v3.97 加入 Decision Lab 和对象操作面板的情景选择。Object Query 仍未接入，新增 Agent/表单链路尚无运行态/ACL 负向验收。向量索引 revision 也只在实际执行向量检索时有值。M5/E2E-03/04 仍需 FactoryNet 与至少一个非 FactoryNet fixture 验证相同 API/规则/审计合同、时间语义、ACL、重放和 fail-closed；M0 Gate 与整体目标未验收。

**2026-11-01 源码增量记录（未验收）：** 上述静态边界描述的是既有审计快照；当前分支另有通用 TemporalReplay What-if 代码增量，已支持非 FactoryNet 回放目标、时间/来源血缘和授权字段候选。通用来源表单现可声明可选 `mapping.links`；后端以固定 schema revision 的 canonical `link_type_id` 将源字段映射到同一实体类型两端的自关系，写入带有效期和 EvidenceRef 的 `TemporalFact`，并在边上保留 canonical 关系资源 ID；目标对象后到时补投影。通用 temporal 图从事实表还原关系边，What-if 可沿当前两跳邻域读取。映射目前只支持单个目标对象 ID，不支持跨类型或多目标；均尚无前端构建、测试、运行态、真实回放、ACL 负向和 FactoryNet/非 FactoryNet E2E 验收证据。M5、E2E-03/04、M0 Gate 仍开放。

**M5 通用来源计划要求：** 当前 What-if 实现是 FactoryNet 专用切片，不等同于行业通用时序推演。实现阶段需以版本化 TemporalSourceAdapter 解析 `replay_id + source_id + entity_key + episode + as_of`，输出绑定 Ontology/schema revision、DataModelSnapshot、DatasetVersion/hash、时间类型/值/时区/ordinal/watermark、权限/evidence digest 和向量索引 revision 的不可变 ScenarioContext。FactoryNet 与至少一个非 FactoryNet temporal fixture 必须共用 What-if API、规则执行和审计合同；未支持的来源或缺少关键时间/版本信息时稳定拒绝。该范围计入 M5、E2E-03/04 和 M0.5 工期重估，不从功能分母中静默移除。

### 5.5 FR-05：唯一 Action Runtime

1. UI、API、Scenario、DecisionCase 和 Agent 最终都生成同一 `ActionRequest`，由唯一 compiler 生成 typed `EditBatch`。
2. Preview 返回目标对象、前后值、影响关系、条件/权限检查、目标 revision 和 preview digest；审核绑定上述摘要，内容变化必须重新审核。
3. 提交前再次验证权限、目标版本、提交条件和幂等键；并发修改明确冲突，不允许静默覆盖。
4. 未知 effect、参数、目标类型、权限或规则一律在写入前稳定拒绝，禁止“跳过未知项仍返回成功”。
5. 每次写回产生不可变 ActionRun、actor/agent、输入输出、状态迁移、错误、耗时、trace、来源和可补偿记录。
6. live 与 Scenario 使用同一执行解释器；ExecutionContext 只决定目标隔离、权限范围和版本解析，不能绕开授权。
7. 数据库事务只保证自身 ACID；跨 PostgreSQL、FalkorDB、对象存储及外部系统通过 Outbox/投影重试/幂等协调，并明确最终一致和补偿边界。
8. 后台维护写入（如自动 rebase）必须使用登记的 system principal、窄化 scope、幂等键和审计记录；不得由普通请求参数或通用 `system_write` 标志跳过授权。系统主体只可执行经合同批准的操作，并绑定数据/Scenario 版本与影响范围。
9. 自动 Action 投影重试与人工重试必须将原发起人、审批人、system principal/worker 和人工操作员分开记录；自动与人工尝试使用不可抹除的分类账本，人工重试需要理由、授权 scope 和独立预算，重置当前自动调度计数不得删除或覆盖历史尝试。
10. 批量 Action 绑定服务端生成的 ObjectSet/筛选快照、Action 版本、目标 revision、调用者权限摘要和确定性选择 digest；提交前重新校验成员资格、逐对象授权、版本与幂等键。Preview 显示批次汇总和逐对象结果；失败、拒绝、冲突与部分成功不得折叠成单一“成功”。M1 冻结全有/全无或逐项部分成功语义、批次上限及重试策略；每个对象结果可独立追踪且重试不会重复副作用。
11. 人工覆盖必须是一类显式、受限、可撤销并可审计的业务决定；提交前重验角色、scope、对象/字段权限和硬性不变量。记录被覆盖的决策/规则版本、前后值、理由、发起者、批准者、时间及关联 Case/Run；不得用覆盖入口跳过授权或安全条件，硬性策略例外须有单独授权合同。
12. compiler 只能读取经服务端投影允许的业务字段；以下划线开头的内部元数据必须通过显式 allowlist 控制，不能被 Action 条件或绑定 Function 用作隐藏输入，也不能进入用户可见结果或审计负载。

**Action reversibility contract：** 每个 Action Type 声明可逆/可补偿/不可逆及其影响；可补偿操作以新的、幂等且经授权的补偿 ActionRun 撤回，并保留原 Run/审计历史。不可逆操作须在审批前明确标识；数据库回滚不能用来声称外部副作用可撤回。

**当前静态边界（待 M0.1 完整审阅）：** `action_runtime.py` 提供共享 compiler 前后的对象/字段/关系授权；v4.49 修复内部图属性在脱敏快照组装时回流，v4.50 增加 Action 专用字段名校验、显式内部字段 allowlist（当前为空）、DecisionCase 目标/历史预览过滤，并将 ActionRun 补偿快照限制为 Action 本次涉及的允许字段。DecisionCase 原有人工投影重放保留理由、`client_request_id`、独立预算与追加 attempt 事件，但仅覆盖 DecisionCase；其他入口尚无统一 Action/outbox 全局账本。Action 全入口仍分属不同 Preview/Review/Commit/Run 合同，FalkorDB/外部投影未统一到 PostgreSQL 权威 Outbox，HTTP/E2E 与权限负向矩阵仍未验证。M4 完成仍要求所有保留入口收敛到同一请求、审批、提交、审计及恢复合同；本段为源码静态边界，不记为运行验收结论。

**补充静态边界（Scenario ActionRun 与 ScenarioRevision 提交恢复）：** 注册 Ontology 的 Scenario `/actions/{action_id}/run` 先单独提交 `OntologyActionRun(status="running")` 作为幂等请求占位，再编译并调用 `ScenarioService.revision()`；修订服务在一个 SQL 事务中写入 ChangeSet、ScenarioRevision、Scenario head/etag，并在提交前完成同一 ActionRun。因而修订后的第二次状态提交窗口已消除，但若进程在占位提交后、修订事务完成前退出，ActionRun 仍可能永久停在 running；当前无 lease/fencing 或同键安全恢复。新运行的 ActionRun/ScenarioRevision 有一个 SQL 权威事务，FalkorDB 图快照仍在该 SQL commit 前同步写入，硬退出可遗留无引用的图快照。需在 M4/E2E-05 实现可恢复的运行 claim 与 durable projection/outbox，并注入进程退出、重放同键和异 payload 冲突验证；未运行故障注入。

**HTTP 可达性边界（修正兼容分支定性）：** `logic_actions.router` 在注册 Action 路由前统一挂载 `require_action_ontology`；该依赖对不存在的 `OntologyProject` 返回 404，并对已注册项目检查 Ontology 授权。因此 `run_action_type()` 中的 pre-Project 旧编译分支不是当前可从 v2 HTTP API 调用的入口。`test_run_published_set_property_action` 直接调用 Python endpoint 函数且未创建 `OntologyProject`，绕过 FastAPI router dependencies；它只证明兼容函数分支行为，不证明实际 API 的鉴权、幂等或生命周期。M0.1/E2E-05 必须以挂载后的 HTTP/TestClient 或浏览器请求验证注册项目的 Live→DecisionCase 与 Scenario Action 路径；直接函数调用和前端 mock 不计作 API 闭环证据。pre-Project helper 应在 M4 明确删除或隔离为 fixture adapter，不能把它当作当前用户可达的治理绕过路径。

**Scenario Workbench Actions 的当前提交与剩余投影窗口（源码静态复核）：** `PUT /{ontology_id}/scenarios/{scenario_id}/actions` 现在在同一请求事务中创建 `OntologyActionRun(status="running")`，把 `runtime_manifest` 与 `revision_id` 写入 `ScenarioChangeSet.validation_report`，并由 `ScenarioService.revision()` 在 SQL commit 前完成 ActionRun。因此原先“修订先提交、Action 摘要随后提交”的二阶段窗口已在当前源码消除；同请求回放可从完整 ChangeSet/ActionRun 恢复相同修订。图快照仍在 SQL commit 前同步写入 FalkorDB，进程硬退出可能遗留孤立图键；没有 durable outbox 与自动清理/重放，需纳入 M4/E2E-05 的故障注入与恢复验收。该结论来自静态调用顺序检查，未运行测试或复现。

**M4.1 live-write 路由补充审阅（2026-10-24，静态）：** 当前 `POST /{ontology_id}/scenarios/{scenario_id}:merge` 在路由中直接调用 `apply_live_edits()` 写 FalkorDB/SQL companion，之后才提交 `ScenarioAudit`；`MergeCommand` 没有 `client_request_id`。`apply_live_edits()` 的图修改先发生，当前 `restore_graph()` 只覆盖可捕获异常，不能证明进程硬退出后的 SQL/图恢复。该路径与 Scenario Action `/run`、Workbench `PUT .../actions` 的恢复窗口不同，必须独立纳入 M4.1/M4.3/E2E-05：让 merge 复用统一 `ActionRequest`/preview/approval/ActionRun，增加 request-key 和不可变 merge manifest，以 PostgreSQL 权威记录及 durable outbox/reconciliation 收敛投影；在获准的隔离环境注入图更新后、SQL/Audit 提交前退出，验证 same-key 回放、异 payload 冲突和零重复副作用。补偿 API `POST /{ontology_id}/action-runs/{run_id}:revert` 也须按新 ActionRun 的补偿合同验收；Wood Study `PATCH .../scenario-studies/{study_id}/cases/{case_id}` 只修改隔离 ScenarioRevision，Agent `propose_action` 只创建 DecisionCase，不得因此视为 live commit。均为静态路由/控制流判断，未运行或复现。

**M4.1 后续源码增量（2026-11-01，静态）：** 上述旧审阅之后，merge 已增加 `client_request_id`、preview digest、持久 intent、专用 reconciliation API 和工作台恢复交互。v4.46 又使 intent 和 `OntologyActionRun(status=running)` 在同一事务创建，成功/失败/未知状态都进入 ActionRun 生命周期事件；reconciliation 按同一 Run 收敛。该改动没有替换 `apply_live_edits()` 为统一 typed Action 执行/outbox，也没有通过进程崩溃、HTTP、ACL 或跨存储故障注入；原段列出的唯一运行时与 E2E-05 退出条件继续有效。

### 5.6 FR-06：持久 Agent Session 与工具治理

1. `AgentDefinition` 保存稳定 ID、版本、owner、model/provider、prompt、可调用 Query/Skill/Tool、知识库、访问范围、预算/限额、评测结果与发布状态；每次发布形成不可变版本并支持撤回/回滚。
2. `AgentSession` 保存 owner、显式参与者/角色授权、conversation/goal、上下文引用、ontology/data/scenario revision、AgentDefinition/model/prompt/tool 版本、token/cost budget、状态和保留期。M1 冻结角色与 ACL 变更合同；M7 实现 owner、受邀参与者和可撤销访问。
3. 会话权限须与对象/字段/工具权限取交集，Session ACL 不能扩大 caller principal 的权限；每次读取、恢复、工具执行和 Action 提交均重新校验。撤权立即阻止后续读取/续跑，且在执行副作用前再次校验。
4. Explore、Agent 工作台及其他授权入口可继续同一 Session 和上下文；事件记录真实 actor/principal、参与者角色与权限决定，便于跨应用追踪。分享会话不得复制或扩大原 owner 的权限。
5. `AgentEvent` 以追加方式记录 user/assistant/tool/approval/error/checkpoint，敏感字段按策略遮盖；可按事件顺序重建状态。
6. 长会话可暂停、恢复、人工接管和继续；发生重启后从最新有效 checkpoint 恢复，不重复已成功写入的 Action。
7. Tool Registry 记录 schema、read/write 分类、权限、数据域、超时、限额、版本、是否需人工批准和结果格式；Agent 发布时只挂载被允许、已测试的工具版本。
8. Agent 默认先用只读工具；写入只通过 FR-05 Action Runtime。运行前后校验对象/字段权限，不能因模型提出参数就扩大权限。
9. 已发布 Agent 可生成受控运行入口；不同用户/调用方沿用 caller principal，支持限额、禁用、访问范围变更、审计和会话清理。
10. 回答中的重要事实链接 Evidence/对象/时间/版本。证据不足、权限过滤或工具失败时需明确说明，不生成貌似确定的结论。
11. Explore 与已发布 Agent 共享持久 session/event/checkpoint 基础，但保存各自的定义与生命周期；上下文切换模型、知识库、数据表、Ontology Skill 或外部工具时记录版本和授权，不把 API key/secret 写入会话事件。
12. 提供离线回归：工具选取、事实召回、引用正确性、拒绝越权、审批边界、发布前验证、恢复行为、延迟和成本；业务正确性由领域标注/规则验证，不由 LLM 自评分替代。
13. 每个 Run/Event 明确区分发起 caller、Session owner/participant、model、tool、worker/system principal、Action requester/approver/executor；事件顺序、权限决定、输入输出摘要和 trace 可关联，但 actor 字段不能互相代替。
14. Durable Run 使用可续期 heartbeat/lease 与 fencing token 判断执行所有权；以 checkpoint 和逐 tool/action attempt 的不可变状态恢复。过期 worker 不得提交结果；重复投递、worker 并发和人工恢复不能重放已成功的副作用。运行中 pause/cancel 作为持久控制请求处理，只有 worker 确认停止或 lease 安全过期后才能形成终态；被取消的旧 worker 不得恢复提交。
15. Session、AgentRun、Event、Checkpoint、trace 和导出证据应用统一的数据分类、字段级脱敏、访问审计与保留/删除策略。测试用合成 canary 验证原始 task、回答、上下文属性、工具参数/结果和异常文本不会未脱敏写入越权可读的持久记录；审计仍保留可关联的 digest、版本和 actor，而不泄露秘密或被权限过滤的内容。
16. 目标架构提及的 Agent-to-agent 委派/交接与“多人共享同一 Session”“人工接管”是不同能力。是否在本期支持由 M0.4 单独裁决；若纳入，M7 必须定义 typed task/context/evidence 传递、父子 Run/trace、双方 Agent Definition 版本 pin、caller/worker 身份和权限取交集、预算/跳数/循环限制、取消/重试/恢复与 Action Runtime 边界。若排除，记录为目标架构开放项，不把参与者共享或人工接管表述成多 Agent 编排已完成。

**FR-06 初始 HTTP/ACL 盘点（历史快照）：** 当时 `agent_sessions` 路由提供 tools、Session create/list/get、events/checkpoints、context add/remove/refresh、turn、propose-action、resume/pause/unpause/cancel；服务以 `owner_id` 限定 Session，未见 participant grant/revoke API。旧 `/agent/runs` 路由仍由 `AgentDecisionTab` 调用：runs/context-drafts 创建持久 AgentRun，context-preview 只读，context PATCH/generate 更新 Run；confirm 在 editor 与 owner/admin 检查后直接插入 `DecisionRecord(status=confirmed)`，不经过 ActionRequest/ActionRun，但本路由不直接写 Entity/Relation。M0.1 建议保留历史读取/兼容能力，先迁移 UI 提案确认到统一 DecisionCase/Action Runtime，再依据真实 HTTP 证据退役旧确认写入口。全仓测试搜索只找到一次挂载的 `POST /agent/sessions` HTTP 测试，turn/context/resume/failure 等多为 service 直调。该判断是源码/测试搜索结果，不代表权限漏洞已被运行复现。

**FR-06 当前实现增量（2026-10-24，源码状态）：** 新增 Session participant 数据表与 0056 migration；owner 可按已有账户邮箱授权 `viewer`，查看参与者与已撤权记录，并立即撤销访问。授权前要求受邀者已获 ontology 访问且角色、修订和适用策略集合与 owner 的数据范围一致；后续每次 Session 读取重新核验该范围与 owner 当前 pin，漂移后 fail closed。共享列表/Session 安全摘要/events/checkpoints 可读；context 修改、turn、proposal、resume/pause/unpause/cancel、participants 管理仍为 owner-only。会话输出及 context 中可能含有会话级数据，因此分享限于策略范围等价的主体；session ACL 不授予 ontology/object/property/tool 权限。本地演示 DB 已达到 0064，包含 0056 schema；迁移已进数据库不代表路由权限矩阵或共享功能通过 HTTP 验收。Manager、跨应用 Session 续接、operator/write participant 角色、逐路由 HTTP 验收、旧 `/agent/runs` 迁移/禁写和 E2E-06 均开放。满足同权限判定的语义边界仍须安全审阅并由真实 HTTP 矩阵验证。

**FR-06 Agent Run 租约与运行中取消增量（2026-10-24，源码状态）：** AgentRun 持久字段新增 worker task owner、递增 lease generation、heartbeat/expiry、cancel requested/acknowledged 时间；Celery worker claim 后按独立数据库连接定时续租，完成写回前复核当前 owner、generation 与未过期租约，旧 worker 不能提交。运行中 cancel 将 Session 标为 `cancelling` 并持久记录请求；worker 在推理前、结果提交前或异常收敛时确认取消，若 worker 消失则 Beat 在租约过期后自动核对并恢复一次、标记 interrupted，或安全终止取消请求。本地演示 DB 已达到 0064，包含 0058 schema；尚未完成挂载 HTTP、并发、进程崩溃、checkpoint 或 E2E-06 验收。本切片尚不提供从中间 tool attempt 恢复，且 Agent Definition/完整 Tool Attempt ledger、数据脱敏/留存与管理器仍开放；因此只算 M7.3 源码增量，不视为 Durable Run 或 FR-06 验收通过。

### 5.7 FR-07：对象工作台、图与演示一致性

1. 搜索、filter、pivot、saved exploration/list、table/graph/timeline、object full/panel view 共用同一 Object Set 和版本上下文。
2. 打开、刷新、前进/后退及跨页面返回后仍恢复活动对象、Scenario、时间窗口和过滤条件。
3. 每个演示使用固定 manifest、输入摘要、模型/规则版本和预期指标；页面清楚标注 synthetic、采样和许可限制。
4. Wood Study 是回归演示，不用它替代动态、时序、多模态三线各自的业务闭环。
5. 保留当前中文工作台语言和品牌；参考公开产品的交互层级和信息密度，不复制商标、专有图像或误导性产品声明。
6. Object、Graph、Workflow、Analysis/Scenario、Agent/Evidence、Action Preview 与 Monitor 的每个结果面板消费同一 `ResultManifest` 展示授权可见的来源、版本、权限裁剪/脱敏提示和运行状态/新鲜度；点击可追到允许访问的血缘与 Run。字段被过滤时不得泄露敏感字段名、对象存在性或计数；加载、过期、部分结果、失败和无权限状态需区分。

7. 至少有一条 P0 业务任务从业务问题出发：用户可用“Supplier B 延迟影响哪些订单、原因和可选方案”等领域表达，从主工作台找到业务对象、关系与证据；任务说明和 UI 不要求用户先知道物理表名、列名或 SQL。该要求不强制使用自然语言模型，验收关注入口和业务语义，而不是查询技术。

### 5.8 FR-08：平台治理与运维

1. 人类用户、组、service principal 与 system principal 具有可区分身份、角色和 scope；Ontology/Graph ACL、子图/对象可见性、属性/字段脱敏、Agent/Tool/Action 以一个服务端授权决策为准；UI disabled 仅为体验，不能代替后端检查。
2. Resource Registry 为本期核心资源（Ontology、Dataset、Workflow、Query、Action、Scenario、Agent、Knowledge Base、Application）登记稳定 ID/type、owner、ACL、revision、生命周期和依赖关系；资源操作遵循同一发布/撤回/审计合同。当前基线按单项目隔离；M0.4 必须裁决是否纳入 Tenant/Project/Use-case 隔离。若纳入，将 tenant scope 纳入 principal、Resource Registry、policy 与 E2E-07，并在 M0.5 重估；若不纳入，显式登记为范围缺口，不得宣称多租户治理已具备。
3. 数据血缘贯穿 Source、DatasetVersion、Transform、OntologyRevision、Object/Evidence、Function、AgentSession、ActionRun、Application。
4. 统一 trace/log/metric/correlation ID，最少展示任务状态、队列等待、运行阶段、错误、P50/P95、吞吐、投影延迟和资源用量。
5. 具备备份、PostgreSQL 恢复、FalkorDB 投影重建、对象存储校验、schema migration 升降级/前向修复和恢复时间记录。
6. 通过持久任务/调度器支持手动、cron 和事件触发；明确串行/并行策略、幂等、取消、超时、重试上限、死信、队列限额和重新运行规则。
7. Monitor/Alert 规则可绑定已授权指标、对象/关系范围和时间窗口；保存触发条件、消警/确认状态与下游 ActionRun 关联，不让模型自由创建可执行告警规则。
8. 明确审计、session、事件、媒体、published Agent、workflow run、trace 和 `MappingRowChange` before/after 行快照的分类、最小访问权限、保留期、删除/脱敏流程；发布/撤回/权限变更同样进入不可变审计。
9. 运营负载分配规则仅在当前项目、当前权限可见且明确可承接的操作员集合中分配；自动分配依据、负载/容量快照、技能标签、排除原因和回退到人工分派的状态均可审计。负载算法不构成权限授予，也不得将任务分给已撤权或不可用主体。
10. 每个 Schedule 是可授权、可审计的持久资源，保存稳定 ID/revision、owner/steward、service principal、目标资源与版本 pin、触发类型/时区、enabled 状态、并发策略、最近/下次触发、错过触发策略、重试/取消和 Run 历史。明确 DST 跳过/重复时间、Beat 停机后的 misfire（跳过/补跑/合并）、同一 tick 的幂等键和 overlap 策略；从调度定义生成的 WorkflowRun 必须保留 schedule revision 与 caller/service actor。HTTP 返回“scheduled”或只存 cron 字符串不能单独作为已持久注册的证明。

11. Schema migration 必须作为可观测、可审计的部署步骤执行；Alembic upgrade/stamp 失败、head 缺失/分叉或 `current != expected head` 时应用保持 not-ready 且禁止业务写入/任务派发，不能无条件回退 `Base.metadata.create_all()` 并继续启动，也不能 stamp 到 head 掩盖缺失迁移。数据库达到预期 revision 且 schema 验证通过后才运行依赖迁移后 schema 的 seed/worker/Beat。开发环境如需 legacy `create_all` 兼容，须通过显式模式启用、限定可处理的 baseline 情形，并记录其未等同于增量 Alembic migration；`/health` 的 liveness 与 schema/dependency readiness 分别报告，单一 HTTP 200 不得作为可接流量证明。

### 5.9 FR-09：可复用 Query、Skill、API 与 MCP 发布

1. `FunctionDefinition/LogicDefinition/QueryDefinition/SkillDefinition` 有稳定 ID、不可变版本、typed input/output、依赖的 Ontology revision、权限要求、超时/分页/返回大小限制、测试样例和 owner；Function/Logic 的发布、撤回和回滚均留审计。
2. Explore/Object Explorer、Twin 工作页、Agent、Scenario、HTTP API、MCP 以及选定的 TS/Python clients 都引用同一个已发布版本；调用输出、权限裁剪和错误码一致。
3. 发布流程至少包含 draft → validate → evaluate → approve → publish → deprecate/revoke，并支持向前/向后兼容检查和可回滚版本。
4. API Key/MCP Client 绑定主体、Scope、限额和过期/吊销状态；不会把 Agent 作者的权限继承给终端调用者。
5. 为冻结的 Query/Function/Agent API 提供最小 TypeScript/Python typed client，类型由 OpenAPI 生成或有同步校验；客户端必须传递当前 caller principal/scope，不得继承 SDK 发布者或 Agent 作者权限。
6. 仅允许调用注册表中的 Query/Function/Skill/Action；禁用任意 SQL、任意图语言和未经审查的动态代码作为 Agent/MCP 默认工具。任意用户代码沙箱和完整 OSDK 仍不纳入本期。

### 5.10 FR-10：可视化数据到本体 Workflow 与发布

1. Workflow 由有类型的 Source、Transform、Mapping、Ontology Build、Quality Gate、Publish 等节点构成；连线的输入/输出 schema 在保存时校验。
2. WorkflowDefinition/NodeDefinition/WorkflowRun 均版本化；一个 run 锁定节点配置、代码/Prompt、来源、输入摘要、Ontology revision、principal 和执行环境。
3. 支持 dry-run/preview、单节点运行、从 checkpoint 恢复、取消、重试、运行差异和输出血缘；相同输入与版本可以重放。
4. Workflow 输出成为 Ontology revision 或候选 revision；发布到 canonical revision 必须经过质量门、影响预览和用户审批。
5. 初期画布仅实现项目批准的节点类型和 connector contract；不把任意脚本/外部插件直接注入高权限执行进程。
6. 旧 Pipeline `/run`、受限 `/run-sync` 与增量触发均适配同一 WorkflowRun 状态合同。WorkflowRun/Run 是执行事实来源；Pipeline 级状态只作为明确定义的生命周期或摘要投影，不得与子 Run 成功/失败相矛盾。worker 在取得/加载 Run、读取输入或写入终态的任一阶段异常，都必须进入可恢复/终态记录；不得留下无告警 pending。E2E-01 覆盖父状态投影、worker 早期失败、派发失败与安全重派。
7. 产品 UI 使用持久异步 WorkflowRun：运行请求快速返回 Run ID；页面按服务端状态/节点进度观察，刷新或重新打开后可按 ID/历史恢复；不得用本地动画或未经 Run ID 绑定的旧 Run 状态代替执行事实。`/run-sync` 限于开发/验收，或须提供相同的持久状态、取消、幂等、恢复和权限合同。definition 保存失败时不得继续执行旧 definition；同一 request key 的重复提交/网络重试只能映射到同一逻辑 Run。
8. Pipeline 发布必须原子创建不可变 `PipelineVersion` 快照与 definition digest；已发布 definition 的编辑产生新草稿/版本，不得改写既有版本。生产 `PipelineRun` 必须固定 `PipelineVersion` ID/digest；草稿试跑走显式 preview，并标清不可发布状态。Pipeline CRUD、validate、publish、run、版本和 Run 详情均按本体/项目资源 scope 鉴权；editor、publisher、runner 分权、可审计。跨 scope 或无权操作应拒绝且不泄露资源存在性、不产生副作用。

### 5.11 FR-11：文档知识库与检索生命周期

1. 支持获准的 PDF、Word、Markdown 和文本来源；每个 Document 绑定 source、owner、版本、校验摘要、解析器版本、访问策略和删除状态。
2. 解析/切分/索引按可重放的 IndexRevision 执行；保留失败原因、页码/段落位置、索引模型版本和重建记录。
3. RetrievalRequest 可选择向量、全文、Ontology 对象/关系召回；结果经 caller 权限过滤，返回可定位的 Evidence 引用。
4. Document、Chunk、Embedding 和缓存不得扩大源文件权限；源文件更新/撤回/授权变更能使旧索引失效或重新评估。
5. Agent/Explore/业务页共享同一知识库与检索授权合同；用户能查看引用来源、版本和时间，不能把无引用的生成文本当成知识库事实。

## 6. 目标架构合同

```text
Connectors / Files / SQL / REST / Events
                ↓
Source + DatasetVersion + WorkflowRun + TransformRun + Quality + Lineage
                ↓
Canonical Ontology Revision + Object/Link/Function/Aggregate/Monitor/Role/Evidence + KnowledgeBase/Document/IndexRevision
                ↓
Object Query / Object Set / Temporal Context / Vector recall
                ↓
Versioned Query/Skill/API/MCP + Scenario Checkpoints + AgentDefinition/Session + KnowledgeBase Retrieval + DecisionCase
                ↓
Single Action Runtime → EditBatch → Approval → Audit/Outbox
                ↓
PostgreSQL authority · FalkorDB rebuildable graph projection
MinIO source assets · Chroma/vector index secondary to source facts
```

### 必须保持的不变量

- PostgreSQL 继续作为权威对象/版本/运行记录存储；FalkorDB 是可重建的图/查询投影；MinIO 保存原始/媒体资产；Chroma 是检索索引，不得成为唯一事实来源。
- 所有副作用经过统一的身份校验、对象/字段权限、Action Runtime 和审计。
- 后台自动化使用独立、可撤销的 system principal；触发来源、scope、策略版本、输入版本、变更摘要和 trace 均可追溯。该主体不得继承用户的隐式宽权限，也不得通过通用 bypass 标志跳过写入政策。
- LLM/Agent 产出的 schema、关系、风险和建议是带来源的候选；模型输出不能直接成为事实或写回。
- `observed / derived / simulated / approved` 状态有明确区分，并携带来源、时间和模型/规则版本。
- 引入新数据库、流处理器或 Agent 框架前先做有退出路径的本地 PoC；默认复用现有 PostgreSQL、FalkorDB、MinIO、Redis/Celery。
- 学习 OntoFlow 的产品闭环与底层数据建模概念，不把专有数据库/任意运行时代码视为默认依赖；运行效率、权限、恢复与产品声明都按本项目可复现的验收合同证明。

## 7. Benchmark 与数据集候选：按任务选，不做大杂烩

### 7.1 首选基准组合

| 候选 | 适合测什么 | 建议优先级 | 约束/不适合之处 |
|---|---|---|---|
| [CQ4OE](https://github.com/oeg-upm/cq4oe-benchmark) | competency question 到术语、属性、层级和 ontology 结构；保留 CQ 与 gold ontology 的对应关系 | **第一优先：本体生成质量** | 新近研究；固定 release、许可、评测器和评分实现 |
| [OntoURL](https://arxiv.org/abs/2505.11031) | ontology 理解、推理、学习的多任务 LLM 回归 | **第一优先：模型能力回归** | v3 论文报告 40 个 ontology、8 个领域、15 项任务及 57,303 个问题；其他快照数字不同，必须固定具体版本 |
| [LUBM](https://swat.cse.lehigh.edu/projects/lubm/) | 可配置合成 RDF/ontology 数据的加载、查询和并发规模 | **第一优先：查询性能** | 大学领域合成数据；不能作为工业 Ontology 语义质量或生产写回能力证明 |
| 仓库内 Wood + FactoryNet bounded fixture | 供应商 What-if、动态/时间序列构建、来源追踪和产品演示回归 | **第一优先：Project 4 集成验收** | 是固定 fixture，不是公开全集、工业通用性能或生产效果证明；按 manifest 锁定 |
| [TGB 2.0](https://arxiv.org/abs/2406.09639) | 时间图/异构图未来链接预测、数据加载和算法性能 | **后续：Temporal KG 算法基准** | 测试任务是链接预测，不等同 What-if、数字孪生或业务决策仿真 |

选择依据：CQ4OE 适合带证据地评价 competency question 覆盖；OntoURL 是多任务模型回归；LUBM 用于可控加载/查询；TGB 2.0 评估时间图任务。几种结果应分别报告，不能合并成一个“总体 AI 分数”。

### 7.2 全部候选数据/Benchmark 清单

以下逐项保留本轮讨论过的全部候选。**纳入清单不等于已经下载、获批或纳入验收**；第一阶段只冻结首选组合，其他数据集须先审阅版本、任务、许可和预算。

#### A. Ontology、schema、知识抽取与自然语言查询

| 数据集/项目 | 适合 Project 4 的任务 | 建议与限制 |
|---|---|---|
| [CQ4OE](https://arxiv.org/abs/2609.26029) | 从领域问题归纳可回答的术语和 ontology 结构；生成建议并对齐证据 | 第一优先；论文报告 CQ2Term 99 条、CQ2Onto 118 条；需固定论文/代码 release 与 license |
| [OntoURL](https://arxiv.org/abs/2505.11031) | ontology 问答、推理和模型能力回归 | 第一优先；版本统计有差异，采用带版本号的数据快照，不用未注明来源的旧数字 |
| [SCOPE/SCION](https://arxiv.org/abs/2607.21610) | 原始文本的关系/事件 schema induction 与融合 | 小规模研究试点；2026 新工作，逐个核对源语料许可证、处理脚本和可复现性 |
| [OAEI 2026](https://oaei.ontologymatching.org/2026/results/) | Ontology matching、实体/属性映射、跨本体对齐 | 选择具体 track 独立报告；campaign/赛道合集，不是单一数据集或单一总分 |
| [LLMs4OL Challenge](https://github.com/sciknoworg/LLMs4OL-Challenge) | 术语分类、taxonomy discovery、非分类关系抽取 | 各届任务和数据不同；逐届锁定 edition、任务、split 和许可证 |
| [OSKGC](https://github.com/HeraclesWang/OSKGC) | 已知 schema 条件下，从文本生成结构化 KG | 数据许可为 CC BY-NC-SA 4.0、代码 MIT；只进入获批的非商业研究流程，不用于商业演示包 |
| [WebNLG](https://synalp.gitlabpages.inria.fr/webnlg-challenge/challenge_2020/) | RDF 三元组到自然语言文本；可辅助测 KG 文本表达/反向抽取 | 本身主要是 data-to-text generation，不是工业 schema 生成基准；挑战/年份/拆分和原始数据 license 分开核验 |
| [BIM-NLQ](https://github.com/MengtianYin/BIM-NLQI) | 建筑 IFC/Ontology 对象的自然语言查询、关系/属性过滤 | 有 110 个开发问题和 225 个测试问题，但原项目未提供完整 gold program/result；需人工核对模型结果；仅作为可选 NLQ 试点 |
| [MeSH-Rel-4K](https://arxiv.org/abs/2607.17902) | 领域关系类型/层级识别、小模型 ontology 建议评估 | 4K 关系来自 2025-01 MeSH 快照；新近论文，数据下载位置和再发布许可尚需确认；仅候选研究试点 |
| [SemMedDB](https://lhncbc.nlm.nih.gov/temp/SemRep_SemMedDB_SKR/SemMedDB_download.html) | 大规模生物医学 predication/关系抽取和检索压力测试 | VER43_R 最终版覆盖至 2024-05-08，约 130.48M predications；下载需 UTS 账号并受 [UMLS 许可](https://www.nlm.nih.gov/databases/umls.html)限制；NLM 页面注明相关工具自 2024-12-31 起不再维护；先解决访问与使用权，不进入默认公开样例 |

#### B. 时间知识图与通用知识图

| 数据集/项目 | 适合 Project 4 的任务 | 建议与限制 |
|---|---|---|
| [ICEWS](https://doi.org/10.7910/DVN/28075) | 按日/时间戳事件流、迟到/重复事件、时间图预测 | 使用标准子集（如 ICEWS14、ICEWS05-15）测试 temporal pipeline；政治事件域与制造业务差异大，不作业务准确性证明 |
| [GDELT Events](https://data.gdeltproject.org/events/) | 大规模新闻事件接入、时间切片、增量解析与查询压力 | 使用有界时间窗并固定检索日期/文件清单；全站持续更新，不能把“latest”当可复现版本 |
| [TGB 2.0](https://arxiv.org/abs/2406.09639) | temporal graph link prediction 与较大规模时间图 ingest | 后续算法/容量测试；任务定义与指标保持独立，不代替 What-if 仿真 |
| [YAGO 4.6](https://yago-knowledge.org/) | 通用知识对象导入、链接、过滤和知识图查询 | 固定 release 与 license；当前站点版本是通用 KG，若要测时间区间需另选 temporal 子集 |
| [YAGO11k](https://github.com/dair-iitd/tkbi) | 区间时间事实、valid-time 查询和 temporal completion | 小型 TKG 子集（约 10.6k entities）；适合时间语义回归，不是大规模性能基准；沿用原论文 split |
| [Wikidata12k](https://github.com/dair-iitd/tkbi) | 时间区间事实与时间预测/过滤 | 小型 temporal 子图（约 12.6k entities）；冻结 Zenodo 数据快照并注意原始 Wikidata license/派生数据条款 |
| [DBpedia-TKG](https://github.com/dbpedia/dbpedia-temporal) | 时间事实压缩、KG 导入、版本/差异与大规模数据试验 | 论文描述约 1.7B triples 和 270M 个不同变更时间点（不是“270M intervals”）；发布版与生成代码许可不同（数据 CC BY-SA 4.0、generator GPLv3）；只做离线容量试验 |

#### C. 制造、供应链、建筑和设备运维

| 数据集/项目 | 适合 Project 4 的任务 | 建议与限制 |
|---|---|---|
| Wood Supply Resilience / frePPLe fixture | 供应商、物料、需求、延期关系、What-if 与演示回归 | 当前产品固定演示；业务 profile 为 synthetic，成本单位不是美元；保持本地 manifest，不作为外部 benchmark |
| [FactoryNet](https://huggingface.co/datasets/FactoryNet4/factorynet) | 生产线时序、事件构建、故障/维护和动态工作台 | 版本/镜像统计不一致；当前 dataset card 有 40.6M 行的 train split；只用锁定的受限 fixture，记录 revision/hash、采样与许可，不能沿用旧统计概括全集 |
| [I4.0 KG / football production line](https://www.semantic-web-journal.net/system/files/swj3431.pdf) | Industry 4.0 生产线 RDF/ontology 对象及时间增长情景 | 领域直接相关；论文展示按 10/20/30 天生成的数据；具体数据是否可下载、再利用条款须先核实，未核实前不纳入 CI |
| [SCANIA Component X](https://www.nature.com/articles/s41597-025-04802-6) | 设备多变量时序、维修工单、规格信息与证据关联 | 真实车辆组件/售后/维修数据，数据约 1.54 GiB 且存在隐私扰动/匿名化；适合作为条件性 PdM 试点，不是 gold TKG；确认访问与 license |
| [BTS building dataset](https://arxiv.org/abs/2406.08990) | 建筑时序信号与 Brick ontology 的分类/zero-shot forecasting | 3 栋建筑、多年时间序列；可评估 ontology/time-series 关联，但建筑运维与工厂流程不同；使用前 pin paper/data release 和许可 |
| [XJTU-SY](https://github.com/WangBiaoXJTU/xjtu-sy-bearing-datasets) | 轴承 run-to-failure、退化趋势和维护窗口 | 适合设备预测维护模型或时序 ingest，不是 Ontology/平台性能基准；固定划分并遵循原数据条款 |
| [Bosch Production Line Performance](https://www.kaggle.com/c/bosch-production-line-performance/data) | 生产工序/站点特征和质量缺陷预测 | 数据受 Kaggle 竞赛条款约束；不能默认公开商用/再分发。可作为隔离研究数据，不进入产品演示或 CI，除非另行确认许可 |
| EITKG（Ecological Industrial Temporal Knowledge Graph，用户给出的候选名） | 若确有该数据版本，可能用于生态/工业 temporal KG 组合 | 本轮未找到可验证的权威项目页、数据 release、任务定义或 license；列为 **未核实/暂不采用**，需有来源证据后再核验 |
| 企业/供应链业务数据（没有指定单一公共数据集） | ERP/MES 订单、库存、供应商、物料等关系建模和业务决策闭环 | 本轮使用 Wood/frePPle 固定 synthetic fixture 做产品回归；真实企业样本须脱敏并获得授权，不能把 synthetic 结果称为现场业务效果 |

#### D. 多模态、因果审计、云服务和网络安全

| 数据集/项目 | 适合 Project 4 的任务 | 建议与限制 |
|---|---|---|
| [PCCA Industrial CKG Audit Benchmark](https://huggingface.co/datasets/Yukko-kurisu/PCCA) | 从半合成工单、时序、文档/因果 KG 中测 Agent 证据审计和拒答 | 225 个审计案例，数据卡标示 CC BY 4.0；适合早期 audit/eval 试点，不证明真实生产因果效度；需确认论文、评测脚本和目标版本 |
| [IMAD-DS](https://dcase-repo.github.io/dcase_datalist/datasets/anomalous/imad-ds.html) | 工业机器人多模态异常检测、音频/加速度/陀螺仪接入 | 约 5.1 GB，数据卡/论文标示 CC BY-SA 4.0；用有界采样测试时序/媒体血缘，不能当作 temporal KG 链接预测集 |
| [ChronoGraph](https://arxiv.org/abs/2509.04449) | 服务依赖图、指标时间序列、事故传播和事件关联 | 论文描述 708 services、约 6 个月/8,005 steps；云微服务而非制造；数据可获取性与 license 单独核实，适合条件性 graph/time 试点 |
| [Edge-IIoTset](https://napier-repository.worktribe.com/output/2969370/edge-iiotset-a-new-comprehensive-realistic-cyber-security-dataset-of-iot-and-iiot-applications-for-centralized-and-federated-learning) | IIoT 资产/流量/攻击关系、访问控制和安全事件演示 | 网络入侵检测域，涵盖多类攻击；论文数据发布许可需逐项审阅；只作为安全数据建模/流式 ingest 试点，不替代制造决策评估 |

### 7.3 推荐的数据/基准分层

1. **持续集成最小集**：LUBM 小/中档 + Wood 固定 fixture + 项目自有 competency-question 黄金集；不联网下载大型外部文件。
2. **Ontology/LLM 离线评估集**：CQ4OE + 冻结版 OntoURL；SCOPE/SCION 先做 license/reproduction audit 后再选入。
3. **时间语义/图算法集**：YAGO11k、Wikidata12k 或 ICEWS 子集用于小型回归；TGB 2.0 用于独立 temporal link prediction；GDELT/DBpedia-TKG 只做选定版本的离线容量运行。
4. **工业领域试点**：FactoryNet bounded fixture 为首个可控 demo/集成样本；I4.0 KG、SCANIA Component X、BTS、XJTU-SY、Bosch 等按任务与许可分别立项，不能合并报告。
5. **专项候选**：BIM-NLQ、MeSH-Rel-4K、SemMedDB、PCCA、IMAD-DS、ChronoGraph、Edge-IIoTset、WebNLG 只回答各自任务的问题，不作为通用平台总榜。
6. **未核实候选**：EITKG 不进入里程碑验收，除非拿到权威源、数据文件/版本、任务与许可证据。

### 7.4 Benchmark 发布规则

1. 每个 benchmark 有 `manifest.json`：官方来源、release/commit、下载时间、SHA-256、license、抽样方法、字段映射、任务定义和硬件预算。
2. 本体构建报告至少分别给出类/属性/关系精确度与召回、层级/约束质量、CQ answerability、来源完整性和人工修订量；不以名称相似度单独打分。
3. 增量更新报告至少给出新增/更新/撤销事实准确率、重复事件数、迟到处理、全量/增量耗时和变更影响对象数。
4. Action/Agent 报告至少给出正确工具选择、证据命中、越权拒绝、需人工审批命中、恢复重复率和成本/延迟。
5. TGB/Future-link 指标单列，不转换为业务方案质量或数字孪生准确度。
6. 每个外部数据集的准入记录要写明：官方 release/commit、使用/再发布许可、数据清理与抽样、是否含 PII/保密条款、磁盘/CPU/GPU 预算、任务与结果可复现命令；未有 license decision 的候选不进入 CI 或公开 Demo。

## 8. 里程碑和依赖顺序

### M0.4 GOV-06 决策卡：Tenant/Project/Use-case 治理范围

**当前代码证据（静态、范围有限）：** `User` 只有 user ID/role；`AuthorizationContext` 以 ontology/principal ID/role 为核心；`OntologySecurityPolicy.subject_kind` 是 user/role；ObjectSet 与 Scenario grant 的 principal 外键指向 users。定向检索未在 `backend/app` 找到 Tenant、tenant_id、group membership、service principal 或统一 Resource Registry 模型。附件 §6.1/Phase 6 明确列出 user/group/service/tenant、resource/project/use-case、scope、lineage、quota 与灾备等平台治理目标。上述差异表示尚不具备可证明的多租户合同，不是运行时越权漏洞结论。

| 选择 | 方案 | 适用边界与代价 | 验收/报告结果 |
|---|---|---|---|
| A | 当前阶段维持单项目授权模型 | 实施范围最窄；后续增加 Tenant/Project/Use-case 可能需要跨资源回填和迁移 | E2E-07 只验单项目资源权限；多租户明确作为未完成目标，不得报告租户隔离通过 |
| B（推荐，待签收） | M1 冻结 tenant-aware Principal/ResourceRef/Policy/Audit 合同，首期只部署/配置一个 tenant；M8 完成真实跨租户运营验证 | 先固定作用域和资源归属，后续逐阶段交付 tenant 管理面、委派、队列、配额与运维；需 M0.5 估算回填/迁移与跨服务传播 | E2E-07 用两个 tenant 覆盖 list/read/search/graph/vector/session/queue/Action/Mapping，验证零泄漏/零副作用；生产声明前必须全通过 |
| C | 首期交付完整多租户管理与运营面 | 覆盖目标最完整，但同时增加组织/租户生命周期、管理员 UI、委派、配额、运维和迁移工作 | M0.5 单独估算；E2E-07 覆盖身份、资源、数据面、运营队列和恢复路径的跨租户隔离 |

**建议及决策门：** 为保持目标架构的完整方向，同时允许当前 Project 4 单 tenant 先运行，推荐 B；这是规划建议，尚未获用户/业务负责人签收。须在 M0.4、M1 数据模型冻结前选定 A/B/C。若届时仍未决定，不可悄悄把全局 ID 当 tenant scope；将 GOV-06 标为开放，暂停相关 schema 定稿，并由 M0.5 重估。无论选哪项，user/role 的单项目资源授权仍须通过 E2E-07。

### M0.4 AGT-05 决策卡：Agent-to-agent 委派与交接

目标架构 §3.9 将“multi-agent or participant handoff”并列提出。当前 PRD/AGT-02 计划了参与者共享 Session、跨应用续接和人工接管，没有定义一个 Agent 把受限任务交给另一个 Agent 的执行合同；两者不可互相替代。

| 选择 | 范围 | 代价与验收 |
|---|---|---|
| A | 本期只做单 Agent Run、参与者共享 Session 和人工接管；多 Agent 编排留待后续立项 | 不增加 M7/E2E-06 执行面；公开记录为目标架构开放项，不能以已有 Session 分享声称覆盖多 Agent 委派 |
| B | 本期纳入受限、顺序式 Agent-to-agent handoff；不默认支持无界 fan-out 或自治循环 | M0.5 估算额外 Definition/Run/trace 状态和 UI；E2E-06 增加父子 Run、定义版本 pin、权限不升级、预算/跳数/循环上限、取消/恢复、证据 lineage 与 Action 只经 M4 的验收。是否存在可直接比较的 Palantir 公开任务由 M0.4 另行核验；未核验前不计入可比覆盖分母 |

**决策门：** M0.4 冻结 A/B、任务分母、验收 owner 和估算；未冻结前不得把此项算作既定 Must-have 或 90% 通过项。无论选项如何，AGT-02 参与者授权与 AGT-03 人工接管仍按各自合同验收。

### M0.4 ONT-05 决策卡：高级语义类型与派生属性

**当前证据（静态）：** canonical 后端已有 Interface、Value Type、Struct 等资源模型/校验线索；当前本体建模页面尚未证明这些类型具有专用的创建、版本、影响预览、发布和恢复流程。目标架构 §3.3 将它们列为一等语义资源。因此“后端类型存在”不等于建模工具完成，也不能把复合父项计作一个已通过功能。

| 选择 | 范围 | 代价与验收 |
|---|---|---|
| A | 本期只交付 Object/Property/Link/Rule/Event/Permission 编辑与治理；高级类型留作显式开放项 | 近期工作量较低；目标架构高级语义建模仍未完成，不得将 ONT-05 计入已通过分数或删除该开放项 |
| B（建议，待签收） | 保留 ONT-05 为 P1 Must-have；冻结四个独立子项：Interface/shared property、ValueType、Struct/field、Derived Property | 每项单独分配参照类别、验收分母、owner 和 M2.3 子 Gate；执行确定性 schema 校验、版本/diff/影响预览、审阅发布/恢复及 Object Query/Mapping 消费验收；派生属性只运行受限、无副作用的表达式，依赖图必须无环。M0.5 按四个子项重估 |
| C | 扩展为通用表达式/函数与任意复杂类型平台 | 工作量和执行安全面显著扩大；必须另立运行隔离、权限、预算和发布生命周期方案，M0.5 单独估算，不以 B 的估算替代 |

**建议及决策门：** 推荐 B，以满足目标架构的一等语义类型方向，同时限制 Derived Property 不执行任意代码。M0.4 要把四个子项拆成可独立失败的评分原子；每项必须绑定官方公开参照 URL，无法证明直接对应时标为组合参照/不可比，不能从一个父项继承同一分数。若选择 A，记录为范围外而不是通过；若选择 C，M0.5 必须重排预算和 M2/M7 依赖。尚未由用户/业务负责人签收，不能开始按 B 实现。

以下是两名全职开发者的初步区间估算，不是承诺日期；兼职按实际投入延长。后一个阶段不能通过堆 UI 绕过前一阶段合同。

| 阶段 | 预计时间 | 主要交付物 | 退出条件 |
|---|---:|---|---|
| M0 基线、页面矩阵和 Demo 恢复 | 3–5 个工作日（初估；M0.5 复核后可调整） | 记录 SHA/环境/当前各模块；冻结 11 个工作面的页面—任务—角色—数据—验收矩阵与范围外清单；木材关系图与 Baseline/B/C 操作稿；确认风险原型清单 | 11 个工作面各有可验证的 Must-have 任务、代表数据、成功/拒绝/空/错状态和证据负责人；木材 Study、Run、结果、Compare、证据和截图相符；状态标签能区分历史与当前；M0.5 明确 M0 是否按期收口及所需额外投入 |
| M1 语义、来源与权限合同 | 1–2 周（补入 Function/Logic、Mapping 端点授权和客户端合同后由 M0.5 复核） | Canonical object/revision、source lineage、Workflow/Query/Function/Skill schema、ExecutionContext、caller/system principal（若 M0.4 纳入 GOV-06 则含 tenant scope）、ResultManifest schema/可见性规则、审计输入快照与 revision/hash pin；冻结字段化 ScenarioContext（Ontology/schema/DataModelSnapshot/DatasetVersion/hash、时间类型/值/时区/水位、Scenario branch、权限/evidence/vector digest）、可信服务端 baseline、What-if 资源级 ACL 与 canonical route 的 adapter/迁移/退役合同；Agent Session ACL、Resource Registry schema、Action 双轨裁决、Mapping/Construction/旧 Extraction 写入口授权和版本 pin 合同 | UI、query、Scenario、Agent、Function、Action、Mapping、Construction 对同一对象、revision、principal 返回一致的权限/来源解释；What-if 不信任跨资源、过期或未授权客户端 baseline；保留入口产生同一 canonical Context/digest 或明确领域专属；Mapping/LLM 未授权、未批准、latest 漂移、撤权后零副作用；其他 M1 既有退出条件均满足 |
| M2 Ontology Modeler、数据映射/LLM 与增量更新 | 3–5 周（M0.5 后重估；高级类型编辑器纳入后再评估） | 可视化 Object/Property/Link/Rule/Event/Permission 建模；若 M0.4 纳入 ONT-05，补齐冻结子集的 Interface/shared property、Value Type、Struct/field、Derived Property 编辑/校验/影响预览/版本发布；Source/Mapping/Quality/Publish Workflow；固定来源与模型/提示版本的 LLM 候选、候选 diff、独立审批/发布；统一新版 ConstructionDraft 与旧 extraction/Mapping 写入口；为 ERP/MES/SCADA/IoT/文档/图像来源族选定代表适配路径；WorkflowRun、event offset/idempotency、schema preview/checkpoint/replay 与完整快照读取证明；所有保留 Mapping endpoint 使用资源级授权，所有 Mapping Apply 写路径统一经过 approved-review pin 与 durable MappingApplyRun 或等价状态机 | 同一输入可重放、同一事件不重复；失败 run 可续跑且不得部分发布；破坏性 schema 变化阻断；本体变更可预览/发布；高级类型引用、派生依赖及兼容性可校验，坏定义不得发布；候选绑定精确 DatasetVersion/hash、来源选择、模型/Prompt/Tool/parser/normalizer 版本和输出 digest；latest 前进不得导致静默漂移；未授权、未审核、伪造/过期/截断输入零写入和零删除；旧 extraction 入口必须适配候选/审批/发布合同或已关闭写入；10,001 行快照完整读取，10,000 行截断输入被拒绝且零删除 |
| M3 Object Explorer 与 Object View 产品化 | 1–2 周（深链接/ACL/历史证据复核后由 M0.5 重估） | 全局对象搜索、filter/Search Around/Pivot、saved exploration/list、compare、stable ObjectRef Full View route 与 Panel、共享对象上下文、ResultManifest 来源/权限/版本/状态面板；从 Graph/DecisionCase/Agent evidence 打开同一 ObjectRef | 用户可从业务对象搜索、沿链接钻取、保存探索、打开 Full/Panel View 并恢复合同内上下文；Full View 可直接打开/复制/刷新/后退，modal 只作为 Panel 扩展。ObjectSet/DataView/filter 与权限上下文按冻结合同恢复；关键对象和 manifest 按授权裁剪并能打开允许访问的来源/Run。Action 仅展示入口/Preview，提交写回等 M4 Runtime |
| M4 唯一 Action Runtime + DecisionCase | 2 周（M0.5 复核 rebase/system actor 工作量） | 统一 Preview/Review/Commit/Run/Audit、Live/Scenario context、规则/合规条件、Outbox/retry、Action 发布边界和 system-principal 写入合同；实现 ACT-04 固定对象集批次与逐项结果、ACT-05 受限人工覆盖/批准合同；分开定义有界自动投影重试与 admin 人工重试预算、必填理由、尝试历史和告警规则；拆分原发起人、批准人、自动 worker/system principal 与人工操作员；替换 `system_write` 布尔授权绕过 | Live/Scenario 对同一 Action 生成一致 typed edits；并发冲突、权限变化和重复提交可解释且不重复写入；无权请求不产生副作用；后台 rebase/维护路径不能通过通用开关或布尔参数绕过授权；必须使用受限 system principal、明确 scope、保留原发起 actor 与实际执行 actor，并具备策略决定审计；自动 worker 事件记录 system actor 并关联原 initiated_by；人工重试可查实际 actor、scope、理由和累计序号，不清除或掩盖自动重试耗尽状态 |
| M5 Temporal KG、What-if、分析与 Digital Twin | 2 周初估（M0.5 复核；新增领域规则签收、路由收敛、ACL 与可靠投影后重估） | valid/event/ingest/system time、历史快照与对象/关系 tombstone、upsert patch/full snapshot、canonical ScenarioContext、Tracking/Pinned、TTL/expiry/自动 rebase cadence、clone/checkpoint/branch/diff/rebase/冲突决议、What-if 路由与可信 baseline、Event object 与 time-series property、同上下文向量证据、durable event outbox/worker lease/attempt ledger/FalkorDB reconciliation、影响链、Baseline/B/C 比较、Supplier B 决策卡、规则/输入/证据版本与事实/推断/模拟状态 | E2E-03/04/07 可重放；对象、关系、向量证据、Scenario 与所有保留路由共享时间语义、revision、DatasetVersion/snapshot 和权限 digest，或以领域专属边界单列；What-if context/list/run/evidence 的资源负向授权通过；服务端 baseline 校验；Tracking/Pinned 刷新、过期和自动 rebase cadence 符合 M0.4 冻结策略，过期清理幂等且审计留存；branch/checkpoint 可恢复；并发 rebase 展示三方 diff，冲突经授权决议并记录用户/system actor、reason 与 policy scope，越权/过期 rebase 零写入；删除后当前视图隐藏、历史 as-of 保留、恢复 upsert 不改写过去；SQL 为权威状态，图投影可重放/对账或进入明确终态；向量只召回同版本同权限证据；Scenario 默认不写 live；推荐只进入 DecisionCase/Preview；Wood 闭环可重放 |
| M6 配置式应用组合层 | 2–3 周 | Object View/分析卡片/Scenario/Action 组件组合、工作流参数绑定、预览、版本化发布/撤回、2D 运营/Twin 页面；预留版本化 Agent 模块绑定 | 同一套模块配置可形成供应链、设备维护、质量审核三类工作页；不复制三套业务前后端；关键页面具备空/错/加载/无权限状态。Agent 模块在 M7 完成后接入，不以空壳组件计覆盖 |
| M7 Agent Definition/Session、知识库、Function/Logic 生命周期与能力发布 | 2–3 周（补入函数生命周期和 TS/Python clients 后须在 M0.5 重估，可能上调） | Session/Event/Checkpoint 与 owner/participant ACL、跨应用会话续接、版本化 Agent Definition、Agent Manager 式控制台、Tool Registry、受限 Function/Logic registry（typed contract、版本/发布/撤回/评估）、Document/IndexRevision 生命周期、Query/Skill/API/MCP 发布、选定 API 的 TS/Python typed clients、评估集与人工介入；将 Agent 模块绑定到 M6 应用组合层 | owner 与获邀参与者按各自权限续接同一会话；撤权即时阻断后续读取/续跑/工具调用；重启恢复不重复副作用；KB 证据/权限一致；Query/Function 版本、SDK 与 Agent 使用同一输入输出和权限合同；评估结果、函数健康指标与 trace 可追溯；写入工具只通过 Action Runtime；发布/撤回/暂停/取消有审计 |
| M8 平台治理、Scheduler、Monitor 与生产准备 | 2–3 周（M0.5 纳入工作队列后重估） | Resource Registry 核心资源目录与生命周期、durable schedule/run history、trace/metrics/告警、Provider/Tool Registry 与凭证引用、用户/组/service/system principal 与 Token scope、跨资源 lineage、project-scoped 人工运营工作队列、人员负载分配与手动接管、备份恢复、限额、发布/回滚手册、migration readiness 和失败恢复策略 | 核心资源都有稳定身份、owner、ACL、版本、生命周期与依赖；配置与权限变更可审计；任务可取消/重试；工作队列条目链接权威 Case/Run/Session，分派、优先级、到期/SLA、升级与状态变化按权限和 actor 留痕；人员负载规则、资格/可用性快照与手动回退可审计；迁移未完成/失败时服务不可写且 readiness 正确；恢复/安全演练有记录；对 M0 冻结的硬件和数据档位达到约定 SLO |

#### M2 已有实现证据（结构/局部运行证据截至迁移 0045；0046 为待验收切片；`palantir` 工作树，2026-10-23）

- **已实现并验证的薄片**：真实 `DatasetVersion` 预览、映射应用和 `EntityInstance.source_version` 血缘；审核开始时固定版本，若预览后版本变化则返回 409 并要求刷新；审核通过后 API/worker 只会应用该审核对应的版本；审核行编辑应用于固定版本的本次映射输入，不覆写原始数据。另有持久 `MappingApplyRun`，以 mapping/review 唯一性和幂等键去重，保存映射快照与 digest、状态、重试/租约和结果；审核批准与全部运行记录在同一 PostgreSQL 事务提交，之后再派发 Celery 任务；worker 与定时 reconciler 可恢复待派发/过期租约运行；过期 worker 写入受 fencing 条件保护；审核详情 API/UI 可查看运行状态。
- **运行证据**：迁移 `0044_mapping_apply_runs` 曾在本地开发数据库应用；此前核查记录了审核映射运行状态路由、Worker 的 `v2.mapping_apply` / `v2.mapping_apply.reconcile` 注册、每分钟 reconciler、43 项相关后端回归、前端 TypeScript/Vite production build 和 API `/health`。这些证据针对截至 `0045` 的映射运行薄片，不覆盖后来加入的 `0046` 快照删除对账。当前数据库已到 `0046`；映射运行表和行变更表计数均为 0。此前运行证据不证明端到端 UI 视觉验收或生产负载能力，相关代码仍在未提交工作树。
- **状态边界**：这只是 M2 的审核版本、持久映射派发/恢复、已批准快照 schema gate 和结构化快照对账的代码薄片，不代表完整 M2 完成。尚缺通用 Source→Dataset→Workflow→Mapping 的 event offset/checkpoint/replay、schema drift gate 与 quality gate 到发布的统一状态机，以及 ERP/MES/SCADA/IoT/文档/图像各代表来源的端到端验收。下方的时序推流 tombstone 与 0046 结构化快照对账属于不同流程；二者都不提供通用消息代理 offset/checkpoint 消费器。PostgreSQL 与图投影之间的跨存储 exactly-once 尚未证明；不得据此宣称 exactly-once 或生产级可靠性。

#### 通用时序推流的追加式删除事实（2026-10-23）

- **能力**：通用 push 事件接受 `operation=upsert|delete`（默认 `upsert`）；delete 会关闭该实体的活动状态事实并追加 `__deleted__` tombstone。当前视图隐藏已删除实体；as-of 查询保留删除前状态；后续 upsert 关闭 tombstone 并恢复实体。事件身份校验包含操作类型，重复事件幂等、同 ID 不同操作/载荷拒绝。FactoryNet observation 与文件回放保持 upsert-only。
- **证据**：迁移 `0045_temporal_stream_tombstones` 已应用；通用时序 push 回归 4 项通过；Python 3.12 compileall 通过；本地 API `/health` 为 `ok`，运行 OpenAPI 的 push-event schema 显示 `operation` 的 `upsert|delete` 约束。
- **限制**：这仍是按 replay 的时序事实流，不是生产消息代理的通用 offset/checkpoint 消费器；迟到事件仍按既有策略拒绝；外部图投影与 PostgreSQL 之间仍是可重试投影，不宣称跨存储 exactly-once。

#### 已批准 Mapping 的快照结构校验（2026-10-23）

- **能力**：审核通过后只对审核固定的不可变 `DatasetVersion` 执行校验与映射。空快照、缺少已映射源列、行级缺列、缺失/空主键、不稳定行哈希主键、主键与已有对象身份不一致，以及非订单实体的重复主键会阻断写入。校验产生列清单和 schema fingerprint；新增但未映射的列只出现在报告中，不会被自动并入已批准 mapping。自动推断出的稳定主键仅用于本次有效映射视图，不回写已批准定义。
- **证据**：针对 Mapping apply 和 schema gate 的后端回归 24 项通过；Python 3.12 `compileall` 和 `git diff --check` 通过。该改动未新增数据库迁移。
- **限制**：这是审核快照应用前的输入合同检查，不是完整 schema evolution/workflow；目前没有基于 fingerprint 的人工重新审核界面、通用来源兼容性矩阵或质量规则发布闸。本节记录的快照 gate 本身不提供删除/撤销、offset/checkpoint/replay；结构化快照缺行对账的后续代码状态见下一节，不能将两者合并称为通用增量同步。

#### 结构化 Mapping 快照变更对账（0046，工作树代码；尚未验收，2026-10-23）

- **代码范围**：已加入 Mapping 行来源归属、软删除状态、每次持久 MappingApplyRun 的追加式行变更记录，以及 FalkorDB 投影重试队列。持久审核 Mapping 的非空固定快照可识别新增/更新/未变化/缺行删除；删除保留 SQL tombstone 和最后已知行内容，投影由定时 reconciler 重试。多 Mapping 共享同一 Ontology/实体类型时拒绝应用，避免在来源所有权语义未统一时误删其他来源的数据。
- **此前只读状态快照**：上次记录的本地 Alembic 为 `0046_mapping_snapshot_reconciliation (head)`；计数为已归属 Mapping 实例 0 条、有 `source_version` 但未归属的历史实例 307 条、实例删除 tombstone 0 条、实体来源删除标记 0 条、`v2_mapping_apply_runs` 0 条、`v2_mapping_row_changes` 0 条。本次 M2 源码计划审阅没有重新查询数据库，也没有应用 Mapping 快照。该历史快照只供风险规划，M2 Gate 前必须只读刷新；不得猜测 307 条历史实例的归属或将其纳入删除对账。
- **验收状态**：源码和 migration 存在于未提交的 `palantir` 工作树；本地数据库已应用迁移。该核对没有运行回归测试，也没有证明服务重启后的端到端映射/删除/图投影恢复。故此项标记为“代码已存在、数据迁移已应用、功能未验收”，不计入 M2 完成或 90% 通过率。
- **后续计划与限制**：先为未归属历史行制定可审计的来源认领/隔离策略，再用新的受控 fixture 验证首次映射、更新、缺行删除、恢复、重复任务和图投影重试。当前不支持复合主键配置、空快照全量删除、多个来源对同一实体类型的合并语义，也没有结构化 Mapping 的 as-of 查询 API；通用 offset/checkpoint/replay 和跨存储 exactly-once 仍在后续范围。没有通过上述验收前，不将该代码视为生产可用。

#### M2 分阶段实施顺序（计划稿；M0.5 冻结后才排期）

当前已有 Connection、Dataset、Pipeline、Curated、Construction、Mapping Apply 和 legacy Extraction 等多套局部流程。M2 不以新增一个大画布为起点；先用 M1 定义共同合同，再把这些切片接成一条可追溯、可恢复、可授权的 Source-to-Ontology 流程。当前静态代码盘点和边界见 [M0.1d](PROJECT4_M0_BASELINE_CAPABILITY_INVENTORY.md) 与 [M0.1l](PROJECT4_M0_BASELINE_CAPABILITY_INVENTORY.md)。

| 顺序 | 工作包与负责人 | 交付物 | 退出 Gate |
|---|---|---|---|
| M2.0 前置封口（M0/M1） | A+B 共同；A 主责来源/语义合同，B 主责任务/页面状态合同 | 完成 87 条工作树记录处置；审查 `0040–0046` 可复现/回滚风险；冻结 `ResourceRef`、DatasetVersion、WorkflowRun、Principal、LineageRef、错误/幂等合同、快照 manifest/完整读取证明，以及 DatasetVersion、CuratedReview、MappingApplyRun 的保留/删除语义；冻结 Construction MappingTask 的 draft/source/model/prompt/tool pin 与 append-only attempt ledger；明确 Mapping Apply 请求者/审核者/worker/人工重放操作者字段及尝试账本；确定 ConstructionDraft 与 legacy Extraction/Mapping 各入口的 pin、权限、发布状态与处置；决定 307 条无 Mapping owner 历史实例的隔离政策 | 空库/数据副本/降级保护计划可审阅；0041 唯一约束有重复值预检；DatasetVersion 与审核/运行血缘的留存边界明确；LLM 候选/审批/发布和旧入口处置、prompt/model 版本证据均已冻结；任务矩阵、Palantir 范围目录、来源/许可证、fixture 与通过证据位置已冻结。历史无主记录不能被自动纳入删除对账；worker 身份与业务发起/审核身份不得混同 |
| M2.1 来源与版本 | A：Source/Adapter/DatasetVersion/Manifest；B：Connector、导入与版本选择界面 | 保留三类可复用传输路径：关系型/平面文件/API、watermark/event ID 时序、文档/图像 manifest；但 ERP、MES、SCADA、IoT、文档、图像六类业务来源必须各有独立验收子用例，并分别映射到 M1 签收的共同 Ontology 类型/关系/来源证据合同；逐类标注 native connector、adapter 或 fixture。当前 MongoDB/REST 是测试/预览路径，不能计作导入连接器 | 六类各有获准、可复现的来源样本/manifest、schema/主键或 event identity、时间/单位语义、目标 mapping、license/敏感性和权限裁剪证据；未就绪类别保持 Must-have 未通过，不能由另一类 fixture 替代。某一 fixture 只证明固定范围，不能推广为通用 ERP/MES/SCADA/IoT connector；若目标包含厂商在线连接器，必须在 M0.5 单独估算并冻结供应商/协议范围 |
| M2.2 工作流与质量 | A：Transform/Mapping/Quality/Lineage 数据合同；B：Workflow Builder、Run History、错误/重试状态 | 以一个 parent WorkflowRun 串起 Source→冻结的 PipelineVersion/definition digest→每个来源的精确 DatasetVersion ID/hash→Transform→QualityReport→Mapping/Construction/Publish；将现有 PipelineRun、ConstructionRun、MappingApplyRun 保留为可追溯 child/adaptor；运行前持久化不可变输入 manifest、caller/policy digest、节点计划和幂等键；Construction MappingTask 另固定 draft/selection/processing digest、DatasetVersion 与 ModelConfig revision，并让来源分析、内容打包和映射阶段共用该 pin；Run History 暴露有权限裁剪的 MappingTask→attempt→phase 状态、输入 pin 和失败/重试关系；定义成功、警告、degraded、失败、取消、重试、dispatch recovery 和节点 checkpoint 状态；保留 requested/effective method、fallback policy/reason、模型配置/Prompt pin 与调用结果；LLM 请求默认 fail closed，显式规则回退才可继续且不得计作 LLM 成功；质量阈值与人工豁免原因有版本和审计 | E2E-01/02 验证任务排队后 Pipeline definition、ConstructionDraft、Dataset latest 或 ModelConfig 改变时仍读取各自原 pin，或在任何输出前明确拒绝；Construction 两阶段来源描述、打包数据和模型调用必须同 manifest；不能在 worker/retry 内静默解析 mutable latest；同 pin 重试生成新 attempt 并保留历史，输入变化创建关联新任务；完成/失败后刷新页面仍可在授权范围内回看每个 task、attempt、phase 与 pin摘要；重复同步/重复派发只产生一个逻辑 Run；broker 暂不可用时 durable outbox/reconciler 不丢 Run；多来源中途失败时只允许留下 run-bound staging/candidate，不能留下已批准/发布的部分本体状态；失败节点按同一 manifest 幂等恢复；质量阻断时不发布对象，所有阶段可从来源版本追到输入/输出 digest；缺失模型/调用错误/无效输出与有意规则模式有不同且可见状态；显式回退需用户授权并标 `degraded`，Run History 与转换 UI 不得把它显示成 LLM 完成 |
| M2.3 LLM 候选与本体建模 | A：冻结 Object/Property/Link/Rule/Event/Permission 与高级类型子集的 schema、引用和兼容合同；B：补齐候选表单、diff/影响预览、审阅/批准 UI、权限有效性预览及发布/恢复工作流 | 保留来源字段/样本或 media asset、selection、精确 DatasetVersion/content+schema hash、ModelConfig ID/revision、非秘密指纹与凭据引用版本、model/provider/model 制品版本摘要、Prompt/template digest、requested/effective extraction method、fallback policy/reason、Tool/parser/normalizer 版本、两阶段请求/响应 digest、MappingTask attempt ID/序号及输出 digest；候选与已发布 revision 隔离；映射确认与独立 reviewer 发布批准分开；统一 ConstructionDraft 与 legacy Extraction route；时序来源/运行 UI 与 EventType schema 定义分开；权限策略支持 revision、编辑、有效性预览、撤权传播与审计；任何付费模型调用需先获用户授权；高级类型纳入时提供创建/编辑 | 对象、属性、关系、规则、事件定义、权限策略各有可视化创建/修改/校验；旧 base revision、非法引用/基数/时间定义、循环派生、冲突策略和越权操作 fail closed；每次变更展示 revision diff 与受影响对象/查询/Action；作者只能提交候选，独立审阅者可审批发布/拒绝，发布可恢复；操作者、输入/输出版本、requested/effective method、降级原因、decision、权限摘要和 trace 可查；ConstructionDraft 页面不得只展示当前进度而丢失历史 attempt，须提供权限裁剪后的数据/模型版本及阶段调用状态；拒绝/撤权/失败零 canonical 写入，所有保留旧入口同合同或不可写 |
| M2.4 增量、删除与恢复 | A：offset/watermark、事件 ID、checkpoint、schema fingerprint、来源所有权、快照完整性与投影对账；B：增量状态、停/续/重放、漂移/删除/恢复 UI | 明确 batch snapshot、watermark append、event stream 的不同合同；Mapping snapshot 删除前必须验证 manifest 预期行数/内容摘要与完整分页或流式读取证明；任何缺失、截断或不匹配输入都 fail closed，产生 0 个删除 delta；实现去重、迟到事件政策、可恢复 checkpoint、schema drift 审核；将 MappingApplyRun dispatch 20 次、execution 8 次设为覆盖所有状态转换的硬上限（包括 queued 消息超时、dispatching/running 租约失效；超限后不得重置 pending 或自动重投），并与 MappingRowChange 投影预算分离；投影另设最大尝试/时间预算、终态隔离/dead-letter、告警和授权重放；SQL 事实应用与图投影分别呈现状态，SQL apply 成功但 projection pending 时不得显示整体映射完成；人工重放采用独立预算并记录操作者、理由、scope/授权决定，不覆盖自动历史；冻结 requester、reviewer、worker/system principal 的 actor lineage，以及 `MappingRowChange` 行快照分类、最小权限、保留/删除/脱敏规则；将 temporal tombstone 与结构化 Mapping snapshot deletion 分开验收；对未知 owner 的 307 条记录先隔离 | `v1/v2/v3/schema_drift` 生命周期 fixture 覆盖新增/更新/删除/恢复、重复、乱序/迟到和投影恢复；新增 `F-MAP-SNAPSHOT-COMPLETENESS`，用 10,001 行合成快照验证完整读取无漏行，并用声明 10,001 行但实际只有 10,000 行的截断输入验证失败且零删除、原对象不变；重复事件无重复副作用。反复制造队列消息丢失及 worker lease 过期，验证 dispatch/execution 计数在所有 recovery 路径均不超过 20/8，超限进入可见终态且停止自动重投；反复制造图投影失败后须到达独立的可见终态并告警，不能无限重试；SQL apply 与 graph projection 分态展示，图待投影时不得声称闭环完成；修复原因后由有权主体重放，确认无重复写入且 SQL 与图投影一致；requester、reviewer、system worker、manual operator 必须可分别追溯，worker 不得冒用其他主体；破坏性 schema 变化必须重新审核 |
| M2.5 联调与页面 Gate | A+B；一起完成 M2 API/UI、权限与证据包联调 | Data Connections、Pipeline/Workflow、Dataset/Curated Review、Ontology Modeler 形成一个真实业务任务；统一 ConstructionDraft 与旧 Extraction/Mapping 写入口；具备空/加载/错误/无权/成功/重试/终态隔离/人工重放状态；Construction MappingTask 刷新后可查看 task→attempt→phase 历史、固定输入/模型版本摘要及失败关系；保持木材演示数据与布局基线 | 跑 E2E-01、E2E-02、`F-MAP-LIFECYCLE` 与 `F-MAP-SNAPSHOT-COMPLETENESS`；额外覆盖排队后 Pipeline/Dataset 版本变化、重复同步与 dispatch 故障、多来源中途失败及同一 manifest 恢复；核对精确版本 pin、模型/Prompt 追溯、独立发布审批、旧 Extraction 入口不得绕过审核或留下部分发布；核对超 10,000 行完整读取、不完整输入 fail-closed，以及图投影重试耗尽、告警和授权恢复；验证行快照不会绕过访问和留存政策；页面只按 M0.4 固定视口/角色/fixture 评分。源码/静态 Demo 不算功能覆盖。M2 后仍须经过 M3–M8 及 11 页总体验收才可报告受限范围 90% |

**当前本地开发库只读证据（2026-10-24）：**`MappingApplyRun=0`、`MappingRowChange=0`；`OntologyActionRun=1` 且为 `completed/synced`。36,783 条 `EntityInstance` 全部没有 `source_mapping_id`；其中 307 条 `source_version` 非空，按 0046 migration 的概念 ID 公式核对后，5 个身份组均无当前 Mapping 候选；36,476 条 `source_version` 为空，来源性质未证实，均不自动分配 Mapping owner。此为本机 `local_single_user` 数据库快照，不代表生产状态或 Mapping 闭环通过；在可恢复数据副本追查/隔离前，不允许这些无主记录进入结构化快照删除对账。当前无持久行投影记录，不会消除静态代码中的无限重试风险。

**Mapping 行投影预算子合同：**一个重试工作项定义为 `(mapping_id, object_id)`，与 MappingApplyRun 的 dispatch/execution 次数、一次 Celery 调用数及 RowChange 行数分别计量。自动预算必须有有限次数和/或明确时间上限；耗尽后写入持久 terminal/dead-letter 状态并从定时 reconciler 的可领取集合排除。每次自动/人工尝试以追加记录保存序号、输入/目标摘要、时间、错误类别、下次调度或终态、actor 与 policy decision；不得只在“最新 RowChange”上覆盖计数和错误。若同一对象有多条 RowChange，须冻结折叠/顺序语义，并保证一个对象失败不阻塞其他对象；人工重放使用独立预算和新尝试记录，不能清零或改写既有失败历史。验收须覆盖终态跨越至少一个 reconciler 周期后仍不再领取。

**M2.4 数据-only 更新子 Gate：**以 v1 已批准映射的 ID/digest、OntologyRevision ID 和 schema hash 为基线；v2 更新、重放与 v3 恢复必须保持这些 pin 不变，只产生预期 `MappingApplyRun` 和对象/关系 delta。验收记录须证明没有新增 ConstructionDraft/本体映射 MappingTask，也没有 `source_analysis`/`ontology_mapping` ModelInvocation；单纯数据 Transform 的模型调用单独分类。发现 schema drift 时只生成带 diff/影响范围的独立变更提案，在单独批准并签发新映射/本体 revision 前不得静默改 schema 或自动重建。

**M2.3 本体模型器子 Gate**：现有四类基础表单、权限策略 Settings 入口和 temporal source/run 页面都只作为起点；E2E-01 必须实际覆盖 Object、Property、Link、Rule、EventType 与 Permission 的新增/修改/校验、版本差异、影响预览、独立审阅发布、拒绝和恢复。事件定义与事件接入运行分开计分；政策更新、有效权限预览、撤权后跨页面拒绝均要验证。Revision 合同必须把 legacy 与 canonical semantic resource/source mapping 纳入共同 manifest/hash 和 diff；从历史 revision 恢复时创建新 current revision，保留旧 revision 不变，并验证跨资源引用、来源映射和新旧 reader 的 revision 对齐。任一类别只有源码或静态页面而没有固定角色/fixture/运行证据时记 `not-run`，不能计入 M2 或 90% 功能通过。

**M2 退出定义**：所有关键输出有 Source、每来源精确 DatasetVersion ID/hash、PipelineVersion ID/definition digest、parent WorkflowRun 与 child run 关联、Ontology revision、Mapping/LLM 配置、Prompt/Tool/parser/normalizer 版本、分离的 requester/reviewer/worker/manual operator、主体/策略决定和输入输出 digest；映射确认、语义审批与正式发布可区分；旧 Extraction/Mapping 写入要么经同一状态机，要么已停写；队列后的 latest/definition 漂移、质量/权限失败、dispatch 故障和中断不产生部分发布；Run 级 20/8 次预算在所有 lease recovery/重投路径都强制执行，行投影也有独立上限、终态和不可覆盖账本；SQL apply 与图投影分别可观测，二者不一致时不得报告整体完成；可对已批准输入重放并解释不可重放原因；各行业来源的 connector、adapter 与 fixture 边界明确。M2 通过只意味着数据到本体闭环过 Gate，不等于页面/全产品相似度已达到 90%。

上一版初估总计约 **16–23 周**（两名全职开发者），仅作为 M0.5 前的历史基线。当前路线图 M1–M8 各阶段区间机械串行加总为 **15–22 周**，但阶段中存在候选并行工作、共享集成与缓冲未估，且还未计 M0.1–M0.5；两组数字的计算口径不同，均不能直接作为当前日历排期承诺。对照原目标架构 P1 后，本版把受限 Function/Logic 生命周期与选定 TS/Python clients 纳入 M7；M0.5 必须按最终 scope、两位 owner 实际可用度、阶段依赖和 fixture/许可状态，给出低/基准/高日历工期及置信度。完成前不报签收完成日期。M2 原有 3–5 周估算也仍须按来源族、质量闸和增量恢复工作包复核。兼职按实际投入延长。关键路径为：**M0 范围/证据冻结 → M1 语义/权限合同 → M2 Ontology Modeler 与可重放数据 Workflow/增量 → Explorer/Object View → 唯一 Action Runtime → Temporal/Scenario/Twin → 应用组合 → Agent/Function/KB → 治理与生产验收**。本估算只覆盖制造/供应链目标范围；完整 OSDK/Developer Console/Marketplace、任意函数代码沙箱、Map/地理空间、3D Builder、完整多租户平台和广泛连接器暂列范围外；Tenant/Project/Use-case 隔离边界由 M0.4 签署，纳入时追加 E2E-07 与 M8 工作量，不得因当前单项目基线而默认计为完成。

### 8.1 Palantir 相似度结论与本期量化口径

**对整个 Palantir Foundry/AIP 产品组合：本路线不能达到或承诺 90% 页面与功能相似。** Foundry/AIP 的公开应用范围横跨 Object Explorer、Object Views、Quiver、Workshop、Slate、Carbon、地图、数据集成、Agent Stack 和平台可观测性；本计划有意不实现完整地图/3D、任意低代码 Builder、广泛企业连接器、多租户平台和全套开发者生态。本期纳入目标架构 P1 中受限的 Function/Logic 生命周期和选定 TS/Python clients，但不把它们等同完整 OSDK 或通用代码运行平台。Palantir 官方把这些描述为用途不同的应用/平台能力，而不是一个可用少数页面覆盖的产品。[Ontology-aware applications](https://www.palantir.com/docs/foundry/ontology/applications/index.html) [Data integration](https://www.palantir.com/docs/foundry/data-integration/overview) [Agent Stack](https://www.palantir.com/docs/foundry/aip/agent-stack) [Observability](https://www.palantir.com/docs/foundry/observability/overview)

**对本项目限定的制造/供应链决策工作流：可以把“90%”设为目标，但当前只能称为规划指标，不能说已经做到。** 目标范围包含以下 11 个页面/工作面，每个都必须有可完成的代表任务，而不是只有菜单或静态 mock：

1. Ontology Modeler：对象、关系、属性、规则、事件、权限与版本；
2. Source/Data Catalog 与数据到本体映射工作流；
3. Object Explorer：全局搜索、筛选、Search Around、Pivot、保存与比较；
4. Full/Panel Object View：对象、关联、时间、证据与 Actions；
5. Graph/Temporal Explorer：关系、事件、时间窗口与上下文切换；
6. Quiver-like Analysis：影响链、聚合、时间序列、指标和基线比较；
7. Scenario/What-if/Digital Twin：隔离、checkpoint、branch、比较与重放；
8. Decision/Action Center：建议、Preview、审批、Commit、拒绝、审计和补偿；
9. Explore/Agent/Knowledge Base：多轮会话、工具/知识检索、引用、暂停与恢复；
10. 配置式 Application Composer 与已发布的 2D 业务工作页；
11. Governance/Monitor：角色与策略、数据/资源血缘、运行健康、日志/指标/trace、恢复。

本期“90%”按下面口径验收：

| 指标 | 目标值 | 计算/限制 |
|---|---:|---|
| 目标页面覆盖 | 11/11 | 每个工作面至少完成一个真实端到端代表任务；不以空白页、菜单入口或预置静态结果计入 |
| Must-have 功能任务通过率 | ≥90% | M0/M1 冻结逐条任务、角色、输入数据和预期结果；通过数 / 冻结任务总数；所有 P0 与权限/写入安全任务必须 100% 通过 |
| Palantir 可比功能覆盖率 | ≥90% | M0.4 从官方公开资料冻结原子化参照功能清单；每项记录来源链接、用户任务、工作面、优先级、Project 4 对应实现与验收证据。仅当真实端到端行为通过（含该项要求的权限、错误/恢复状态）才记通过。按 P0/P1/P2 权重 3/2/1 计算通过权重 / 纳入范围总权重；P0 和安全项必须 100% 通过。另公布纳入、范围外和不可比项目，避免把范围受限的 90% 说成 Foundry/AIP 全产品 90%。 |
| 跨模块核心闭环 | 8/8 | 对齐 E2E-01—08：原始数据→映射/质量→Ontology revision；源变化→既有对象增量更新；时序图查询→同上下文向量证据；Baseline/B/C Twin 比较→可追溯 DecisionCase；统一 Action Preview/审批/执行/恢复/审计；Agent 有证据检索/持久续接→Action Preview；跨入口撤权→拒绝且不泄漏；性能/故障/迁移恢复→有界重放与对账。每条均须在 M0.3 固定的角色、fixture 和证据条件下单独通过。 |
| 来源/版本/策略追溯 | 100% 的 P0 结果 | 每个关键结论/ActionRun 能回到对象、来源、版本、模型/Prompt/Tool、策略决策和执行记录 |
| 页面参照相似与 UX | 直接可比页面均分 ≥90/100；至少 90% 纳入页面单页 ≥90；组合参照及项目专属页面各自均分 ≥90 | M0.4 对全部 11 页冻结直接参照、组合参照或 Project 4 专属分类、官方 URL/标题/核对日、参考图示类型与截图 SHA、Project 4 截图状态、视口/角色/fixture/hash/代码 SHA。只有有逐页官方 UI 参照的页面进入 Palantir 相似度分母；P0 页面 ≥90，其余直接可比页 ≥85。组合参照对齐度与 Project 4 UX 分数分别计算，各类均分 ≥90、P0 ≥90、其余 ≥85，不混入 Palantir 均分。11/11 页面仍须完成 Must-have 任务与页面质量评审；缺失 Must-have 状态直接不通过 |

页面打分采用五维量表，评审前为每个工作面固定参考状态和对应 Project 4 状态。每维 0–5 分，再按权重折算为 100 分：信息架构/任务层级 25%、布局/密度/响应 25%、核心控件与交互路径 25%、加载/空/错误/权限/成功反馈 15%、视觉层级/可读性 10%。0=缺失，1=仅有名称或概念，2=主要结构/操作不匹配，3=主流程相同但差异明显，4=高度接近且有少量差异，5=结构与交互状态接近参考。每页先归为直接参照、组合参照或 Project 4 专属页面；只有官方材料提供可逐页比较的 UI 页面/截图时，才计入 Palantir 页面相似度。截图须使用同一视口、角色、fixture 与 UI 状态，并记录来源页面/文档标题、精确 URL、核对日、截图 SHA 和证据类型。公开文档中的架构图或功能说明不能作为产品 UI 截图。两名评审差异超过 1 分时记录理由并复评。公开材料不等同可访问的真实 Foundry 租户。

功能相似度与本项目任务通过率不是同一个分数。前者只对照 M0.4 冻结的官方公开、可观察且可比的功能清单；后者覆盖 Project 4 自身的八项目标，可能包含没有直接 Palantir 对应项的时序知识图谱/特定 What-if 工作流。每项功能应拆到可独立验收的行为，标记 `in-scope / out-of-scope / non-comparable`，并保留未纳入项及理由。M0.4 未完成清单冻结前不得声称已建立功能相似度分数；不以本项目 E2E 任务通过率代替 Palantir 功能覆盖率。

因此，准确结论是：**计划可分别追求 Palantir 直接可比页面相似度 ≥90、组合参照对齐度 ≥90、Project 4 专属页面 UX ≥90、范围内可比功能覆盖率 ≥90、Project 4 Must-have 任务通过率 ≥90；前提是 M0.4 冻结全部 11 页的参考类型和分母。** 不同类型的分数分别报告。当前没有证据证明任何指标已达标。Object Explorer 的历史受控 API 截图评审只覆盖该工作面的历史状态，不能外推到其他页面或当前真实服务。M3、M5、M6 是页面/分析/应用相似度的关键阶段；M7、M8 负责 Agent 与平台治理闭环。全平台 Foundry/AIP 能力不在本期比较分母中。

### 8.2 下一步：M0 计划基线的具体交付

本阶段先完成规划基线，冻结后再据此进入功能实施。当前仍在 M0.1；截至 2026-11-01 v4.74 工作树，`git status --short` 汇总为 313 行状态项（171 tracked + 142 untracked，未跟踪目录按目录折叠）；使用 `--untracked-files=all` 展开后为 727 条路径（171 tracked + 556 untracked）。仍未逐 diff 全量签收，数据库/迁移风险也需按计划刷新和复核，因此当前不得越过 M0 进入 M1/M2 全面实施。M0.1m/n 记录高级类型、批量 Action 与运营差距；M0.1o 记录 85→87 快照差异并按现有规则裁定支持文档为本地内部材料；M0.1p 记录 Temporal Stream、What-if、向量证据和事件投影恢复的首轮静态审阅，完整 diff 与运行可达性仍待核；M0.1q 记录公共 queued Agent turn 固定非秘密 ModelConfig digest 并对配置漂移 fail closed，v3.53 进一步 pin 可得的本机 Ollama artifact digest、worker 执行前复核并记录 Run/Event/Checkpoint/UI 模型血缘；该实现未运行真实推理，tag digest 不可得时标记 `unverified`，不可变配置 revision、完整 provider/model 复现和共享会话治理仍待补；M0.1r 的 Pipeline LLM 请求/降级状态源码切片已加入 `llm_enrich` fail-closed/显式 fallback、原子语义增强、LLM 成功数与规则输出数分离及候选质量未判定标识，运行态 E2E 仍开放；目标架构必做项不得从 Must-have 中裁掉，M0.4 冻结语义、公开参照类别和分母，M0.5 复核 M2/M4/M8 工期。M0 的工作顺序如下：

| 步骤 | 负责人 | 交付物 | 完成判据 |
|---|---|---|---|
| M0.1 状态盘点 | A 主责 Source/Ontology/数据库/Lineage；B 主责 UI/Action/Agent/Scenario；双方共同裁决 | [能力盘点（本地内部材料）](PROJECT4_M0_BASELINE_CAPABILITY_INVENTORY.md)（按现有规则不进行 Git 同步）；[当前工作树状态](../PROJECT4_M0_1_WORKTREE_DISPOSITION.md)：`palantir` 当前 SHA、136 个变更路径（89 tracked modified + 47 untracked；新增三项待逐路径核对）、迁移/数据风险、O1—O8 和 H1—H4 证据状态；M0.1a–s 覆盖 Action、Agent、治理、数据/LLM/增量、Scenario/What-if、Mapping 授权/旧入口、构筑版本 pin/旧 Extraction 发布路径、高级语义类型与运营队列、Temporal event envelope/时间语义/What-if 资源 ACL/向量证据 pin/durable projection/reconciliation、ModelConfig 不可变 pin、LLM 请求与规则回退状态，以及 Construction MappingTask 双阶段输入/模型 pin 与 attempt lineage | 每条记录按盘点 §7.1 填写基线/diff 范围、调用链、数据迁移影响、权限/副作用、验证证据边界、处置理由、责任人及后续 Gate；`unknown` 必须有关闭条件。0040—0046 复现/升级和 downgrade guard 计划可审阅；0041 唯一约束先做重复值预检；0042 downgrade guard 已保护 retry attempt/next/last 数据，但 pending/failed 回填会使积压同时到期，仍需估算峰值并制定分批/限速；0043 的 FK 删除策略现由新 0092 改为 `RESTRICT`；Dataset 删除在存在 Review 时需要显式 `cascade=true`，仅该路径会删除 Review/RowEdit 后再删版本；新迁移尚未应用，运行时并发与级联行为待验收；0046 downgrade guard 已在源码加入 `source_mapping_id`、Instance tombstone、RowChange 与 `Entity.is_source_deleted` 的 fail-closed 检查；0047/0048 downgrade guard 现逐列保护 Pipeline snapshot/version/manifest/digest 与 request actor/fingerprint/dispatch lease/task/error 等事实，自动回填的 `legacy` 状态作为可安全删除的合成值保留例外；上述分支仍需 disposable PostgreSQL/SQLite 覆盖与全链回退验证。另审阅 `main.py::_run_schema_migration()` 全环境 catch + `create_all` fallback，冻结生产迁移失败关闭与 readiness 语义；对照 Scenario Workbench/FactoryNet What-if/What-if Demo 三组 API 与两套模型，裁定 canonical route、ACL/time/revision 合同及适配/迁移/退役计划；旧 raw-row Apply/build-all 已改为 410，快捷批准已改为 pending review adapter，主 Mapping/Curated 路径已加入资源 scope 与角色检查；逐端点验收资源 ACL/撤权矩阵、旧版和 Link Mapping 兼容入口，并冻结适配/退役决定；审阅新版 ConstructionDraft 的 latest 读取/版本漂移、model/Prompt/template provenance、`mapping_confirmed` 与独立 publish approval 区分，以及旧 Extraction worker 在运行时读取当前 File/ModelConfig/Prompt 并直接分阶段 upsert canonical rows 的路径；明确其 scope 授权和 adapter/retirement 决定，冻结失败零部分写入 Gate；审阅 `system_write` 绕过授权及 actor 归因；明确 DatasetVersion/Review/Run 留存策略；审阅 `AuditTask.revision_id` 与 worker live-table snapshot 的不一致、revision 内活动 Entity 与 Relation endpoint/count 一致性，并定义当前态/固定历史态语义；逐项核对软删除跨入口消费和 MappingRun 双层重试/主体策略缺口；刷新数据库版本/行数快照并隔离 307 条无主记录；不把代码存在等同功能验收，不在当前库试降级 |
| M0.2 页面矩阵 | B 主责页面/角色/交互；A 复核数据、API 和权限边界；双方冻结分母 | [页面矩阵](PROJECT4_SCREEN_AND_TASK_ACCEPTANCE_MATRIX.md)：11 个工作面的页面、目标用户/角色、主任务、关键状态、现有路由、差距，以及 2.4 逐页参照冻结台账；补充统一 ResultManifest 字段/可见性矩阵和工作面 5/7 的 ScenarioContext、可信 baseline 验收；Agent 工作面注明当前 owner-only 证据边界 | 每页追溯至少一条 Must-have 用户任务、角色、现状和负责人；逐页冻结直接/组合/项目专属参照分类、URL/标题/核对日、参考图类型/截图及 SHA，Project 4 对照状态与浏览器/viewport/DPR/角色/fixture/hash/代码 SHA；结果面板标明来源、修订、权限裁剪、状态和无泄漏检查；Scenario/What-if 绑定 revision、数据版本、时间类型/值/时区及 ACL digest；Session owner/participant 及撤权语义进入 P0；负责人签收任务与各类评分分母 |
| M0.3 端到端任务与数据准入 | A 主责来源/fixture/许可/manifest；B 主责 UI/API 操作脚本与证据；双方签收预期结果 | [E2E 方案](PROJECT4_M0_ACCEPTANCE_SCENARIOS.md)：数据映射发布、对象搜索钻取、What-if 比较、Action 审批审计、Agent 共享会话/撤权/恢复、权限撤销、故障重放等 8 条流程；E2E-01 覆盖 ConstructionDraft 与旧 Extraction 路径、精确 DatasetVersion/model/Prompt pin、独立发布审批及 R1/R2/R3 审计快照隔离；E2E-03/04/07 固定 ScenarioContext 时间/版本/ACL 与可信 baseline，并覆盖 event ID/envelope 冲突、对象/关系 delete-restore、跨资源撤权、evidence_text 裁剪、同源向量检索和 SQL/FalkorDB 投影恢复；E2E-08 迁移失败与 readiness 隔离；对每类结果面板检查 ResultManifest | 每条任务固定输入、角色、步骤、预期/拒绝结果和证据；冻结 license/隐私/成本；新增 Mapping 完整性 fixture 按 10,001 行完整、10,000 行截断且零删除定义；跨 E2E 核对来源/权限/版本/状态显示与不泄漏；M0 仅冻结计划，不运行验收 |
| M0.4 评分和范围 | A 主责功能覆盖分母；B 主责页面视觉/交互分母；双方冻结适用范围和签字 | [Palantir 功能目录](PROJECT4_PALANTIR_FUNCTION_PARITY_CATALOG.md) 与页面矩阵 §2.4：功能目录 §3.3–3.9 当前提出 170 个候选原子叶项覆盖 38 个父项，并对 5 个父项提出本期外排除提案；M0.4 签署范围/参照类别/唯一权重，父项只汇总不重复计分（当前 105 是未过滤机械权重和，不是正式分母）；再冻结 11 页逐页参照分类；冻结 ONT-05 的类型子集；对 GOV-06 签署单项目或跨租户隔离范围；为目标架构必做的 ACT-04/05、OPS-01/02 冻结具体语义、直接/组合/项目专属参照与评分口径，不得从 Must-have 中删除；分别报告直接页面相似度、组合参照对齐度、Project 4 UX、可比功能覆盖、Must-have 任务和闭环/追溯率 | 11/11 页面必须有真实任务和页面质量记录；只有直接逐页 UI 参照页面进入 Palantir 相似度分母，平均 ≥90、至少 90% 纳入页面 ≥90，P0 ≥90、其余 ≥85；组合参照对齐度和项目专属页面 UX 各自均分 ≥90、P0 ≥90、其余 ≥85，分别报告；功能及 Must-have 目标 ≥90%；静态 Demo/假数据/未复验原型不计通过；P0 权限与写入安全 100%；若纳入租户隔离，跨租户负向用例必须零泄漏、零副作用 |
| M0.5 计划重估 | A 估算语义/数据/来源工作；B 估算 Action/Agent/UI/治理工作；双方评估关键路径 | 修订 M0 与 M1–M8 的依赖、工期、并行关系、人员投入、视觉验收成本和风险登记 | 确认上一版 16–23 周基线是否适用于当前完整范围，并给出重估依据；若 GOV-06 纳入，估算 tenant scope、身份改造和跨租户验收；计入当前工作树全部逐 diff、0040–0056 复现/升级/downgrade 与启动期异常处理、0042 due 回填的队列峰值/限速、0043/0092 Review pin 的 RESTRICT 与显式 cascade 删除合同、迁移应用/回退验证、0046 来源归属和 Entity flag downgrade guard 覆盖、迁移失败 readiness Gate、Dataset/Review/Run 血缘留存、Mapping `limit=10000` 快照风险、Mapping/Curated 剩余端点、Link Mapping 与 legacy adapter 的逐资源 ACL/撤权矩阵；raw-row Apply/build-all 已退役，approved Apply 绑定 Review 与版本 pin、ConstructionDraft latest version 漂移及准确 DatasetVersion pin、model/Prompt/template/parser/normalizer 血缘、候选确认和独立发布审批、旧 Extraction worker 收敛/关闭与部分写入原子性、MappingApplyRun 已有 20 次 dispatch/8 次 execution 与行投影未见上限的差异及独立预算、Mapping 请求者/审核者/worker/人工重放操作者拆分和不可覆盖尝试账本、Action system actor 审计、MappingRowChange 全行 JSON 的分类与保留/访问策略、What-if client baseline 校验/按资源鉴权/同时间版本证据绑定、What-if context 的 EvidenceRef 文本访问边界、Temporal event envelope 与对象/关系 patch/delete/restore 语义、事件 outbox/worker lease/attempt ledger/FalkorDB reconciliation、Scenario Workbench 与 FactoryNet What-if/What-if Demo 的 canonical route、双模型兼容/迁移/退役和同一 Context 证据成本、`system_write` rebase 的授权/主体改造、`AuditTask.revision_id` 与实际 worker 输入不一致及 revision snapshot 关系端点/计数治理、软删除当前/固定历史态合同、M1 OpenAPI 类型生成/跨入口契约与 ResultManifest/AuditInput UI 验收、307 条无主实例、Agent participant viewer ACL 源码已增但尚无 participant route-level HTTP 矩阵、E2E-06 和 migration 验证；公共 queued local turn 已固定可得的本机 Ollama artifact digest，在 worker 前复核，并将模型血缘写入 Run/Event/Checkpoint/UI；但仍无不可变 ModelConfig revision、完整 provider/model artifact 复现证明，且本次 runtime probe 显示 Ollama configured=true、reachable=false、installed=false，Agent 本地推理不可用；LLM 请求回退时 Run/UI 未保留请求意图与原因、Construction MappingTask 两阶段重新解析 latest/ModelConfig 与重试覆盖 trace 的风险、Resource Registry 和其他待验收工作；冻结前不排实现日期 |

#### M0.1s Construction MappingTask 双阶段 lineage 与用户可见任务历史（只读，2026-10-24）

定向审阅 `construction_drafts.py`、`workbench_task.py` 和 `ModelInvocation` 字段。当前 `generate_mapping()` 的 payload hash 仅覆盖 data class、selection、processing，入队记录 `MODEL_NAME` 路由名而非配置版本；worker 开始时才选 ModelConfig。`_source_descriptor()` 与 `_pack_transfer_content()` 分别解析 Dataset latest，两个 M3 阶段之间来源版本变化时，第一阶段描述与第二阶段实际打包样本可能不一致。ModelInvocation 记录 provider/model name 和 phase，但没有 ModelConfig ID/revision；retry 复用同一 MappingTask ID、重置 trace 并重新读取草案、来源和配置。定向搜索到的普通/多模态前端仅轮询当前 task 的 stage/progress/trace 并显示最近 3–4 条事件；完成/失败后结束轮询，页面类型不消费 source_plan/transfer_manifest，也未找到 attempt 或 ModelInvocation history UI。当前 GET 返回 task/source_plan/transfer_manifest，但未返回 invocation/attempt 列表。以上均为静态代码边界，未做运行复现。

**计划裁决：** M1 冻结并版本化 draft/input manifest，至少含 draft revision/content digest、selection/processing、DatasetVersion ID/content+schema hash、ModelConfig ID/revision/非秘密指纹/凭据引用版本、可验证的 provider/model 制品摘要、Prompt/Tool/parser/normalizer 版本、caller/policy digest；敏感凭据不得入 manifest。M2 worker 的来源分析、内容打包、映射调用必须共享完全相同的 manifest；Dataset/model config pin 不可用时在输出前 fail closed；provider/model 制品 digest 不可验证则标记 `reproducibility=unverified`，不计可复现质量证据。每次 retry 使用 append-only attempt ID/序号、trace 和 invocation history；相同输入可在原 manifest 下重试，输入/配置变化则创建关联的全新 MappingTask，不得覆盖旧 attempt。运行详情须能在刷新后按权限显示 task→attempt→phase、各阶段状态/错误、输入版本与非秘密模型配置指纹；不得向 UI 暴露密钥、完整敏感 Prompt 或未裁剪数据内容。M0.3 E2E-01 要在队列等待期及两阶段之间更改 Dataset latest、草案选择和 ModelConfig，并分别验证旧 pin 被一致使用或 fail closed；随后执行同 manifest retry，核对 attempt 增量和历史保留，再验证变更输入需新 task。此门槛未有运行证据，不计作已实现。

**M0.4 必须分别签署批量 Action（ACT-04）、受限人工覆盖（ACT-05）、工作队列（OPS-01）和负载分配（OPS-02）的 Must-have 任务、权限角色、硬约束、公开参照证据与评分分母；自动负载分配缺少权威容量/资格数据时先留在人工派单范围。M0.5 将这些必做包的对象集快照、逐项写入、人工覆盖审批、队列审计、人员容量与运维成本纳入估算。

**M0 退出后**才进入 M1 合同冻结与 M2 数据到本体工作流；若来源所有权、权限合同或代表性数据集未冻结，相关映射不得进入真实数据对账验收。

M0.1 的当前源码入口、工作树状态和能力边界见[能力与证据盘点](PROJECT4_M0_BASELINE_CAPABILITY_INVENTORY.md)。它是 M0.5 重估依据，不替代功能或视觉验收报告。

## 9. 木材演示：恢复基线与验收办法

### 9.1 当前证据与待签收目标

Git 历史显示，`dda5143` 使用 Cytoscape `cose` 力导向布局，`7ab1cd1` 改为按节点/边排序并使用 `deterministicGraphPositions` 与固定 `preset`；当前 palantir HEAD `8005e66` 保留确定性布局，未提交工作树中的 GraphCanvas 又切回与 `dda5143` 相符的 Cose 参数。2026-09-21 的工程验收记录称完整 Wood Study Demo 曾通过，但未找到其截图产物。历史实现与验收记录支持将 Cose 作为恢复候选，不证明它与原像素画面完全一致。

当前 checkout 与项目 worktrees 中均未找到 Supplier Study 专用历史截图或操作稿列出的 01-open.png 至 12-recovery.png。当前工作树 GraphCanvas 已回到历史 Cose 参数，且 Docker 前端容器挂载当前 frontend/；源码和挂载不能证明页面已恢复。最近一次带上下文的运行采样中 Baseline、Supplier B、Supplier C 的 graph GET 均为 HTTP 500，页面持续 loading；该故障画面只可作为诊断证据，不能签收或评分。M0.2 冻结目标参照、历史图检索、评分 rubric 和采集 manifest，不把错误页面定为新基线。M5 修复 canonical link 授权解析、ScenarioError HTTP 映射及前端显式失败/重试态；在版本对齐的隔离环境确认三方案 graph GET 3/3 为 200 且 ScenarioContext、view count/content digest 一致后，再采集健康的 12 张截图。有旧图时按固定代码 SHA、角色、fixture、浏览器、视口和 DPR 比对并签收；没有旧图时，修复后的画面可签为“重建新基线”，但不得宣称恢复历史像素效果。健康截图签收前视觉分数保持未评分。

### 9.2 固定数据和结果

| 项目 | 固定值 |
|---|---|
| Study ID | `9a4048fb-e1a9-4ed7-adb6-072401d272d6` |
| Ontology ID | `frepple-d7eb98078882b234c395fd05` |
| Baseline Run | `f2b048f3-08a9-451e-8897-2ba26ee56c76` |
| Supplier B Run | `f000a347-b1f1-4a55-8bc2-a5268a8b61aa` |
| Supplier C Run | `a55a8a39-8939-4b82-b676-00c021ad7cb3` |

| Case | 准时率 | 采购成本 | 缺货数量 |
|---|---:|---:|---:|
| Baseline | 62.5% | 22,500 cost units | 0 |
| Supplier B | 43.75% | 10,260 cost units | 220 |
| Supplier C | 68.75% | 14,100 cost units | 0 |

成本单位不是美元。B/C 使用项目明确标注的 synthetic supplier profile；frePPLe 样例及合成假设不能说成生产供应商数据。完整原流程见 [木材演示操作稿](DEMO_SUPPLIER_SCENARIO_WORKBENCH.md)。

### 9.3 恢复后的手工验收

1. 从 `/what-if` 打开已准备好的 Wood Supply Resilience Study，不重导数据、不新建空白 Case。
2. 确认 Baseline、Supplier B、Supplier C 和参数概览存在，切换 Case 后关系图、Summary、Impacts、Evidence、Run details 使用同一 Case 上下文。
3. 关系图应使用原来的自动力导向布局；节点可选，关系和对象面板可读，切换方案后图内容随 Case 更新。
4. Compare 指标与上表一致；七个持久阶段、输入/输出摘要、source manifest 和独立 result view 可打开。
5. 刷新页面后保留 Study/Case；运行/写入需要编辑者权限；只读用户仍可查看对象、结果和证据。
6. 规则经 M0.2 签收且输入有效时，推荐卡显示规则/输入/结果版本、对象影响链、Evidence、备选项、权衡和风险；缺少签收、数据不完整或权限不足时应拒绝推荐并说明原因。
7. 从推荐打开 DecisionCase 与 Action Preview 后不产生 live 写入；实际审批/提交只按 E2E-05 受控验收。
8. 保存目标分支 SHA、环境、截图、三个 Run ID、指标对比、推荐卡或拒绝原因和错误记录；若 Run 当前不存在，应按操作稿用现有 API 正常生成，不直接修改数据库。

### 9.4 M0 视觉基线冻结与截图归档

M0.2 冻结采集规范与目标，不把当前故障画面当评分截图。M5 修复后，先在版本对齐的隔离环境确认 Baseline/Supplier B/Supplier C 的 graph GET 全部 HTTP 200，且 ScenarioContext、view count、content digest 与 Compare 结果一致，再按操作稿采集健康的 01-open.png 至 12-recovery.png。冻结并记录浏览器/版本、视口尺寸、device scale factor、角色、fixture manifest 与 hash、Study/Case/Run IDs、当前代码 SHA、截图时间和每张图的 SHA-256。对 cose 力导向图先连续采集两次，确认布局波动范围；评分关注节点/关系可见性、分组与可读性，并按冻结的视觉 rubric 评审，不要求非确定性节点坐标逐像素相等。

当前 500/无限 loading 截图（如采集）只放入诊断证据，不进入视觉评分。健康截图与 manifest 的候选版本化位置为 frontend/human_demo_screenshots/supplier_study_baseline/；frontend/test-results/ 当前被忽略，不作为签收归档位置。负责人签收健康基线后再用于 Wood 回归和 11 页相似度评分。若历史预期截图无法恢复，只能把修复后的新截图明确标为“重建新基线”，不可追认成 2026-09-21 或更早的历史状态；这不证明历史像素效果已恢复。

## 10. 明确不做与风险控制

- 不一次性复制 Palantir 所有产品线；先完成制造/供应链一条跨数据到动作闭环。
- 不因 OntoFlow 的产品描述直接替换 PostgreSQL/FalkorDB/MinIO/Chroma，或复制 AbutionGraph 私有运行时；任何存储/引擎替换须用同一权限、时间、查询、恢复和容量用例做可回退 PoC。
- 不让 Agent 直接访问数据库或 bypass Action Runtime；本阶段不接不受控的生产设备/ERP 写入。
- 不允许未经审阅的 LLM 生成脚本、动态函数或插件进入高权限进程；代码节点仅在隔离试运行、typed I/O、依赖/权限限界和人工批准后发布。
- 不在未检查 license、版本、预算和数据适用性前全量导入候选 benchmark。
- 不以单次本地 demo、历史验收记录或 HTTP health 为“生产级”证据。
- 不把 Scenario/Agent/Action 每个都做成自己的 object、权限和事件体系。
- 不因新框架/数据库流行就增加组件；每项新增依赖需有性能/恢复/权限收益和可退出方案。

主要风险：基线分支选择错误导致回归；旧/new 模型和 Action 双轨产生不一致；事件时间与数据库更新时间混用；权限只在 UI 控制；Agent 恢复时重复副作用；benchmark 许可不适合；开发范围过宽削弱演示；跨存储投影滞后而误报“事务成功”。M0/M1 必须先冻结并公开这些不变量。

## 11. 双人责任分工

### 人员 A：语义核心与数据平面

负责：

- Canonical Ontology、Object/Link/Property/Action/Function 稳定 ID、revision 和兼容迁移；
- Source/Connector/DatasetVersion/TransformRun/Workflow node schema/Quality/Evidence/Lineage 合同；
- LLM schema/mapping 建议、人工审核、版本、来源和 CQ/benchmark adapter；
- 动态增量 ingest、事件 offset/idempotency、迟到/撤销/schema drift、影响预览与恢复；
- Function/Aggregate/Monitor/Role 等 canonical 资源与对象/字段/子图授权策略；
- QueryDefinition/Skill 输入输出 schema、时间/证据/权限约束和可重建投影的服务端基础。

负责 Workflow 的 Source/Transform/Mapping/Quality/Ontology 节点数据合同，API/数据模块的完整实现和其 UI 所需的配置/观测入口；补齐 Document/Chunk/IndexRevision 与混合检索的来源、权限和重建合同，并定义 Provider/Tool/credential reference 的服务端资源边界。不得依赖演示名称、硬编码 ontology ID 或 service 私有函数表达通用 ingest；不单独实现任意脚本执行器。

### 人员 B：运行时、Agent 与工作台

负责：

- 唯一 Action Compiler/Runtime、DecisionCase 生命周期、preview digest、人工批准、幂等、冲突和审计；
- Agent Definition/Version/Publish 与 AgentSession/Event/Checkpoint、Tool Registry、暂停/恢复、证据引用、人工接管和离线评估；
- Scenario/What-if/时间上下文、Baseline/Scenario 对比和未来数字孪生状态展示；
- Object Explorer、共享 ObjectView/Panel、Graph/Timeline/Compare 和跨页上下文恢复；
- Workflow Run orchestration/scheduler/运行阶段、查询/Skill/API/MCP 发布 UI、任务恢复和可见失败状态；
- 可复用 2D Twin/运营页面和业务操作文档。

人员 B 负责节点编排、运行和发布表面，人员 A 提供节点 schema 与语义/授权合同；双方共同定义 workflow/query 生命周期。B 补齐 Explore/Agent 的持久上下文交互、知识库管理与检索体验、模型/工具配置面、published Agent 和只读 2D 业务页。所有写入消费 A 的 Canonical context 和 Authorization contract；不得在前端或 Action worker 再造一套对象、字段、权限或版本含义。

### 双方共同责任

- M0 共同签字确认上游基线 SHA、wood demo fixture 和实验代码处置清单。
- M1 第一周冻结 OpenAPI/typed payload、Workflow/Query/Agent/Action/KnowledgeBase 状态机、权限决策、错误码、版本和事件合同；A/B 各自实现可独立演示的纵向薄片，第二周完成合同集成，不让两个子系统各自造一套 Session/Run/Publish。
- 每个里程碑一起演示端到端用例，保存数据/代码/环境摘要和回归证据。
- 以业务纵切面切分页面与后端：A 交付可消费的版本/来源/权限元数据，B 交付可验证的编排运行、发布、动作与可视状态；双方共同验收同一条从原始数据到已批准 Ontology 应用的流程。
- 任何外部系统写回、付费模型或新增托管服务需单独评估并由项目负责人决定；本 PRD 不自动授权费用或生产接入。

### 11.1 分阶段结对与交接

| 阶段 | 人员 A 交付 | 人员 B 交付 | 两人集成点 |
|---|---|---|---|
| M0 基线 | 数据源、Ontology、血缘和权限现状清单 | Wood 演示、Action/Agent/Scenario 原型现状清单 | 一起锁定输入 fixture、refs/SHAs、恢复截图和保留/暂存裁决 |
| M1 合同冻结 | Canonical schema、Revision、ExecutionContext、caller/system principal、Authorization 与数据/知识库接口 | Action/Agent/Workflow/Query 生命周期与 API payload 草案；生成前端类型与契约测试用例 | 共同评审状态机、错误、trace、幂等和版本；明确后台维护操作主体、scope 和审计；针对同一 ObjectRef/ObjectSet/ScenarioContext/EvidenceRef/ActionContext 运行跨入口契约测试，并记录兼容适配例外；做一条只读 query + preview 薄片 |
| M2 Data/LLM/增量 | Source/Transform/Mapping/Quality/Ontology 节点 schema、代表来源族、lineage、LLM 候选证据、增量/checkpoint | WorkflowRun 编排、画布/配置、审阅/发布状态、失败恢复 UI | 跑通代表来源映射 dry-run → quality → candidate revision，人工批准后发布并重放失败节点 |
| M3 Explorer/Object View | Object/Link/Property 权限与查询合同、来源/时间上下文 | 搜索、筛选、Search Around/Pivot、保存/比较、Full/Panel Object View 和跨页状态 | 用只读/编辑角色完成同一组搜索/钻取任务，确认数据、权限和页面上下文一致 |
| M4 Action | Typed EditBatch、规则/合规条件、授权/版本接口 | 唯一 Action Compiler/Runtime、Preview/Approval/Commit/Run/Audit | 并发、权限变化、重试和重复提交都形成可审阅记录 |
| M5 Scenario/Twin | 时间快照、对象/关系版本解析、向量/证据权限合同 | Tracking/Pinned、TTL/expiry、自动 rebase policy、Scenario clone/checkpoint/branch/diff/rebase、三方冲突决议、影响分析、2D Twin 工作页 | 同一 query/action 在 Live 与 Scenario 的读权限和影响摘要可比对；基线变化后的无冲突 rebase 可确定性恢复，属性冲突须授权解决并留审计；TTL 到期后读写/运行/合并符合冻结策略、清理幂等且审计按期保留；Scenario 不产生未经批准的 live side effect |
| M6 应用组合 | Query/ObjectView/分析/Scenario/Action 配置 schema 和发布依赖 | 组件组合、参数绑定、预览、应用版本/发布与运营页 UX | 用同一组件合同配置供应链、设备维护、质量审核工作页并交付权限/错误状态 |
| M7 Agent/Knowledge | AgentDefinition revision、Session ACL/principal、Document/Chunk/IndexRevision、检索权限/引用与 Ontology tool contract | Definition 发布/撤回、跨应用 Session 续接、耐久 Run/Checkpoint/恢复、scoped Agent Manager、知识库与 Query 绑定 UX；裁决旧 sessionless Agent API | owner/participant/operator scope 与对象/字段/工具权限取交集；即时撤权；实际 actor/策略决定可审计；worker 重启不丢状态、不重复 Action；E2E-06 和 G4 通过 |
| M8 Governance/Ops | 通用 ResourceRef/Principal/Policy/Audit 合同落地；适配 Ontology、Dataset、ObjectSet、Scenario、Query、Action、Agent、Knowledge、Provider/Tool；备份/投影重建与跨资源 lineage | 资源/运行/配置目录，scoped 管理 UI，readiness/metrics/trace/alert，scheduler 与恢复工作台 | 主体和资源授权跨入口一致且即时撤权；关键变更可查 actor/scope/decision；备份还原与派生投影重建通过；按固定档位报告 SLO 并生成 G5 证据包。Agent Manager 复用 M1 合同 |

#### M7 Agent/Knowledge 实施顺序

M7 依赖 M1 冻结的身份、权限与 trace 合同，M3 的 Query/ObjectView 权限上下文，M4 的唯一 Action Runtime，以及 M6 的应用组合和上下文传递。M1 先定义 Resource Registry/principal 的稳定 ID 与 scope；M8 再补齐跨资源目录和运维控制面，避免 M7 被推迟到治理大项完成后才开始。

| 顺序 | 人员 A：语义、身份、数据与权限 | 人员 B：运行时、Agent 与工作台 | 交付 Gate |
|---|---|---|---|
| M7.1 Definition | 冻结 AgentDefinition/Revision、输入/输出 schema、模型/提示词/工具引用与 Ontology/Data/Scenario 版本合同；定义 ModelConfig ID/不可变 revision、脱敏非秘密配置指纹、凭据版本引用及 provider/model 制品版本或 digest | 做 Definition 草稿、校验、发布/撤回和离线评测配置 UX | 不可变 revision；每个运行在派发前锁定 Definition、ModelConfig ID/revision/fingerprint、provider/model 可验证制品版本或 digest、Prompt、Tool；不把密钥/凭据写入 Run；发布/撤回可审计，无法证明制品版本时明确标记未能复现 |
| M7.2 Shared Session | 实现 owner/participant/operator principal 与 Session ACL 同对象/字段/工具权限的交集规则 | 做邀请/加入、跨 Explore/Object View/Published App 的同一 Session 入口与当前参与者显示 | 逐请求鉴权；撤权立即阻断读、续跑和 Tool；跨页面继续同一 Session ID，不泄露 owner 独有数据 |
| M7.3 Durable Run | 冻结 run/event/checkpoint schema、权限决定、引用血缘和数据保留边界；定义入队时配置 pin 与凭据版本引用 | 建立耐久执行、worker lease、超时/取消/恢复状态机及用户可见运行记录 | HTTP 请求中断不丢 run 事实；重启后安全恢复或清楚终止；request id 幂等；不重放已创建的 DecisionCase/ActionRun；worker 只能解析 Run 固定的 ModelConfig/凭据版本，配置在排队后变更时使用已 pin 版本或 fail closed，禁止静默读取最新配置；Run 仅保留非秘密指纹/版本引用，秘密不进入事件、日志或证据包 |
| M7.4 Manager 与旧入口收敛 | 定义 scoped operator 对 Agent/Session/Run 的最小管理权限与审计字段 | 做 Manager 列表/详情、trace 查看、暂停/恢复/取消/撤销 UI；对旧 `/agent/runs` 作合并或弃用迁移 | 操作员只能管理授权 scope；每个控制操作记录 actor、目标、策略决定和前后状态；不存在绕过统一 Session/Authorization contract 的并行旧路径 |
| M7.5 端到端验收 | 固定 F-AGENT-GOLD、F-PRIV、免费/本地模型及证据留存格式 | 固定 E2E-06 浏览器/API 操作脚本、Manager/参与者状态截图和恢复记录 | 撤权、重启、模型失败、引用权限、重复 Action、人工审批均按 E2E-06/G4 通过；模型质量与运行时合同分开报告 |

**M7 退出 Gate**：定义发布可复现；跨应用共享 Session 的主体边界清楚；每个 actor 的读取和写入重新鉴权；持久 Run/Checkpoint 按 pinned versions 安全恢复；ModelConfig ID/revision、脱敏指纹和 provider/model 可验证制品版本在派发前固定，排队后配置变更不能导致静默换模；无法验证制品 digest 的运行标为未能复现；Agent 只能通过 M4 发起写入；Manager 操作有 scope 与审计；旧 Agent API 已合并或有明确弃用边界；E2E-06/G4 有绑定 SHA、fixture、角色和环境的证据。

#### M8 Governance/Ops 实施顺序

M8 的现状依据与缺口见 [M0.1 能力盘点](PROJECT4_M0_BASELINE_CAPABILITY_INVENTORY.md) 的 M0.1c。当前可见的是分域资源/ACL/审计模型、依赖健康探测和定时对账配置；统一 Resource Registry、group/service/system principal、全局审计血缘查询、指标告警与产品级备份恢复仍是规划要求，代码/配置存在不计为完成。

M8 建在 M1 的 Principal/ResourceRef/Authorization/Audit 合同之上，并汇总 M2 的来源版本、M3 的查询上下文、M4 的 ActionRun、M5 的 Scenario/Temporal Run 和 M7 的 Agent/Tool。若 M0.4 纳入 GOV-06，M1 固定 tenant scope，M8.1/8.2 强制资源归属、成员关系和策略隔离，M8.4 运营队列按 tenant/project scope 裁剪；否则维持单项目基线并记录该范围缺口。M8 不接管领域服务的权威数据或状态机，而为它们提供统一身份、检索、审计和运维控制面。先从人工认领/转派和受限负载建议与人工确认起步；必须有权威的技能/资格、可用性和工作量来源、公平/优先级规则及审计，缺少任一项即回退人工派单；不以自动派单替代队列权限校验。

| 顺序 | 人员 A：语义、身份、数据与权限 | 人员 B：运行时、平台与工作台 | 交付 Gate |
|---|---|---|---|
| M8.1 Resource Registry | 定义 kind/id/scope/version/owner-or-steward/lifecycle/ACL/dependency/retention 语义；为各领域资源编制现有 ID 与迁移适配表 | 做全局资源/版本/依赖目录与按权限过滤的详情页；保留现有领域 API 兼容适配 | P0 资源可用稳定 ID 找到；版本、状态、owner 和 dependency 与领域事实一致；无破坏性重建旧 ID |
| M8.2 Principal/Policy | 建 user/group/role/service/system principal、membership/delegation 和统一策略解释；冻结默认拒绝、deny precedence、scope 与撤权 cache invalidation | 做身份/组/策略管理、变更预览和授权检查 UI | Query/Graph/Vector/Scenario/Action/Agent/Worker 对同一 caller 产生一致决定；若纳入 GOV-06，则跨租户请求 fail closed；撤权在下一次读取/工具/提交前生效并可审计 |
| M8.3 Audit/Lineage | 定义 shared audit/trace envelope 和 resource dependency edges；连接 source/data/Ontology/model/prompt/tool/policy/run/output digest | 做全局运行搜索、对象/版本血缘面板和策略决定详情 | 关键结果能从输出反查 actor、输入版本、策略决定与关联 Run；领域事实记录仍保持可追溯、不可被目录覆盖 |
| M8.4 Ops/Observability 与人工工作队列 | 冻结关键指标、状态模型、保留期、安全日志字段及人工待办类型/SLA 规则；定义队列项到权威 Case/Run/Session 的引用、幂等更新和生命周期，不复制底层业务状态；冻结人员派单资格、容量/可用性、公平规则及人工回退 | 区分 liveness/readiness；提供 dependency/worker/queue/projection freshness、latency/error/backlog、trace 和告警工作台；建立 project-scoped 队列，汇总待审批、失败/阻塞 Run、Agent handoff 与投影终态异常；支持负责人/状态/优先级/到期时间/升级及受限负载分配；只由已登记事件生成事项，不提供通用任意事件自动化；配置操作有审计 | 一次本机健康响应不等同 SLO；队列列表/详情按当前权限裁剪，事项与底层状态一致且可深链；若纳入 GOV-06，还须按 tenant/project scope 隔离队列条目与认领操作；认领/转派/自动建议/人工回退均有 actor、时间、理由审计；自动分派只用有权威资格、可用性和容量数据的人选，缺数据回退人工；撤权后无泄漏；E2E-05/07/08 有证据 |
| M8.5 Backup/Recovery | 定义 PostgreSQL 权威事实和 MinIO 源文件备份、密钥/保留策略、RPO/RTO 及校验方法 | 做隔离环境 restore/rebuild 演练脚本与可审阅报告；确认 FalkorDB/向量索引从权威事实重建 | PostgreSQL/MinIO 可按文档恢复；关键行数与摘要校验通过；派生索引重建后关键查询一致；记录实测 RPO/RTO |
| M8.6 Production Gate | 按 M0 实测硬件和代表 fixture 确认容量档位、SLO、审计保留和风险接受人 | 做并发/故障/限流/撤权/worker 取消恢复演练与生产准备报告 | P50/P95、吞吐、错误率、积压、投影延迟、恢复时间达到经负责人冻结的目标；P0 权限与写入安全 100% 通过 |

**M8.6 性能基准合同：** F-PERF 必须使用固定 seed、生成器版本、fixture/input/output hash、硬件与容器限制、依赖版本、并发、读写 mix 和测量窗口组成 manifest。基准分为交互查询/Graph+ACL、Source→Mapping、时序 ingest/replay、向量检索、Action batch、Agent session/tool、队列/投影恢复；分别记录端到端与分阶段 P50/P95、吞吐、预期拒绝与非预期错误、队列等待、CPU/内存/连接池、投影滞后及恢复时间，并检查结果/权限正确。冷启动与预热稳态分开，稳态每档至少重复三次；预热长度、运行时长、S/M/L 容量档和绝对阈值由 M0.5 在目标硬件/预期用户与业务需求上签收，不从 Demo 推定。若只完成应用内计时而没有队列/依赖/资源观测，G5 保持部分通过。

**M0.1 已有性能证据盘点（2026-10-24，只读）：** 仓库不是“没有性能测试”。`backend/tests/v2/perf/test_search_latency.py` 覆盖 URI 解析/快速失败/Cron 本地计算，`test_pipeline_throughput.py` 覆盖内存内 10k CSV、1k JSON 与质量计算，`test_neo4j_ingest.py` 覆盖 1k 批次准备和 Mock 故障快速返回；它们是组件级微基准，不测真实数据库/网络下的并发吞吐。`backend/evidence/decision_lab_local_perf_2026-10-22.json` 记录单用户 arm64 本机 Docker、每端点 25 次串行预热 GET；两份 Plan A JSON 记录 PostgreSQL metadata parse/permission projection 的 10/25 个样本与 SQL 计数；`plan_b_runtime_benchmark_2026-10-05.json` 记录 WSL2 + disposable PostgreSQL/FalkorDB 的历史工作流 endpoint 请求基线。历史数字只能在环境、代码 SHA、数据及 endpoint 契约对齐后复用为对照，不能外推到目标生产容量。当前 M8 缺口精确定义为：尚未核实一套覆盖冻结 S/M/L 档、并发读写 mix、Source-to-Ontology/Temporal/Action/Agent 全路径、结果与 ACL 正确性、队列/资源观测及故障恢复的统一可重放 load harness；M8 应保留可适用基线并补齐缺项，而非重复从零测所有路径。本次只读盘点，没有运行这些测试或压测。

**M8 退出 Gate**：P0 资源目录有稳定 ID/owner/ACL/version/lifecycle/dependency；user/group/service/system scope 统一可解释且可撤回；关键动作和结论具备跨资源 audit/lineage；独立 readiness、指标、trace、告警可定位故障；待审批、失败/阻塞 Run、Agent handoff 与投影异常队列以及 OPS-02 负载分配/人工回退通过权限、幂等、分派和审计验收；PostgreSQL/MinIO 备份恢复与图/向量重建经过验证；生产 SLO 绑定硬件、数据档位、代码 SHA 和实测报告。G5 未过，不宣称生产可用。

约束：A/B 可并行写各自模块，但 M1 合同冻结前不并行实现共享 schema；每阶段结束一起跑同一条 Project 4 端到端用例并记录结论，避免“接口合并了但语义不同”。

## 12. 完成定义与发布 Gate

| Gate | 必须有的证据 | 通过条件 |
|---|---|---|
| G0 Baseline | refs/SHAs、工作树状态、依赖/数据 manifest、Wood 页面截图 | 每个代码来源被标为 upstream、adopted、prototype 或 parked；wood 结果与图布局可见确认 |
| G1 Semantic/Data/Workflow | OpenAPI/schema、生成的前端类型、跨入口 API 契约测试与 ResultManifest 契约/样例、WorkflowRun manifest、source-lineage trace、CQ/映射 benchmark 包 | object/revision/source/permission 在查询、Agent、Action 输出中一致；代表性 UI/API/Graph/Scenario/Agent/Action 入口通过共享类型与契约检查，所有例外有兼容适配记录；结果面板显示调用者可见的来源/权限裁剪/版本/状态而不泄露已过滤数据；失败节点可复放；publish 必须经过质量闸 |
| G2 Explorer/Action | 11 个目标页面的可追溯交互规格、Explorer 任务记录、ResultManifest 页面状态截图、Full/Panel 视图、Action preview/commit 审计、权限变化与并发证据、后台 system principal 操作清单、投影自动重试与管理员人工重试全序列 | Explorer/Object View 可完成代表任务；Object/Graph/Analysis/Scenario/Preview 的来源/权限/版本/状态语义与 API manifest 一致；同一 Action 在 UI/API/Scenario 使用同一 compiler；普通主体未授权、冲突或未知 effect 不产生副作用；后台写入只在登记 scope 内执行并可审计；投影故障可恢复，自动预算与人工预算分开，管理员人工重试有理由、有 actor/序号记录且不掩盖耗尽状态 |
| G3 Scenario/Application | 锁定数据、时间查询/回放、scenario branch/diff、分析基线比较、Supplier B 经签收规则驱动的推荐卡及拒绝态、DecisionCase/Action Preview 关联、应用模块配置/版本/发布记录 | 相同 snapshot 与规则可重复；推荐结论可回到输入/对象/Evidence；缺少有效前提时安全拒绝推荐；建议不绕过 Action Runtime；事实/推断/模拟/批准状态分开；至少三类工作页由共享组件配置，不复制三套实现 |
| G4 Agent/Capability Publish | AgentDefinition revision、Agent Manager 控制面操作审计、Session event/checkpoint、owner/participant ACL 与撤权记录、跨应用续接和 actor 归因、知识库引用与权限结果、MCP/API scope、发布/撤回/暂停/取消、重启恢复和离线评估 | 恢复不重复 Action；获邀参与者只能在 Session ACL 与对象/字段权限交集内访问；撤权立即阻止后续读取/续跑/工具调用；授权操作员只能管理 scope 内的 Agent/Session，所有控制操作有 actor 与审计；事件均归因到实际 actor；知识库证据不越权；Query 跨 UI/API/Agent 返回一致权限；发布 Agent 继承 caller scope 并可撤销；后置 E2E-04/05/06 集成 Gate 证明 Explorer/Object View→Scenario/Twin→DecisionCase/Preview→Agent/Evidence→ActionRun→Audit 共用同一 ObjectRef、revision、DatasetVersion、ScenarioContext 与 policy digest |
| G5 Governance/Ops | Resource Registry 与资源生命周期记录、user/group/service/system principal、scoped policy、Scheduler/Monitor、Provider/Tool 配置审计、Token scope、跨资源 lineage/audit、readiness/trace/metrics/alerts、PostgreSQL/MinIO backup/restore 与 FalkorDB/vector rebuild、发布回滚 | 核心资源有稳定身份、owner/steward、ACL、版本、状态和依赖；配置与授权变更有 actor/scope/decision 审计；在写明硬件/数据档位达到约定 P95、错误率、吞吐、积压和恢复时间；备份还原与投影重建校验通过；限流/告警/权限回归通过；Must-have 总体任务通过率 ≥90%，P0/安全任务 100% |

**Application composition 验收：** M6 至少用已发布的 Object View、Query、Analysis、Scenario、Action 和 Agent 模块配置供应链、设备维护、质量审核三类工作页；记录各组件引用的资源 ID/version 与权限上下文，验证预览、发布、撤回/回滚和空/错/无权限状态。工作页不得复制各自的业务对象解释器、查询授权或 Action 实现；增加新业务页应以组合既有资源为主，并以配置/依赖清单证明复用。

**M3 Object View Gate：** 当前静态 UI 中的 Full View dialog 不替代可直接定位的 ObjectRef 页面。M1 冻结 canonical ObjectRef URL 与 ObjectSet/DataView/ScenarioContext handoff；M3 对有权/无权角色验证直接深链接、新窗口打开、刷新、浏览器前进/后退、跨 Graph/DecisionCase/Agent 打开与返回恢复。保存/分享 ObjectSet 的权限不得扩大底层对象/字段访问。未在当前 SHA 下真实服务验收前不计 M3 或页面相似度通过。

**跨模块纵向集成 Gate（复用 E2E-04/05/06，不增加核心闭环数量）：** 在 M3/M4/M5/M7 退出后，用同一 Supplier B canonical ObjectRef 串起 Explorer/Object View/Graph、Scenario/Twin、DecisionCase/Action Preview 与运行、Agent Session/Evidence、Audit/Lineage；整个链固定 ObjectRef、Ontology revision、DatasetVersion/hash、ScenarioContext、policy digest、EvidenceRef 和 Case/Run 关联。Agent 只给出有来源的建议并经统一 Action Runtime；不能用同名替代对象、latest 版本或越权证据拼成“看似完整”的流程。单项 E2E 通过不自动代表此集成 Gate 通过。

任何 Gate 没有证据时都标“未验证/阻塞”，不从计划文字自动升级为完成。性能阈值先用 M0 实测硬件与数据量设基线，再确定目标；不得拿 308 对象/329 关系的木材样例推断生产吞吐。

## 13. 参考资料

### 产品能力

- Palantir [Ontology system](https://www.palantir.com/docs/foundry/architecture-center/ontology-system)
- Palantir [Object edits and materializations](https://www.palantir.com/docs/foundry/object-edits/overview)
- Palantir [AIP Agent Stack](https://www.palantir.com/docs/foundry/aip/agent-stack)
- Palantir [Workflow Lineage](https://www.palantir.com/docs/foundry/workflow-lineage/overview)
- Palantir [Object Explorer overview](https://www.palantir.com/docs/foundry/object-explorer/overview) 与 [Workshop getting started](https://www.palantir.com/docs/foundry/workshop/getting-started/index.html)
- 视频原地址：[How Do You Create Your First Ontology in Palantir Foundry?](https://www.youtube.com/watch?v=Tj0YAbm41vM&t=264s)；可检索摘要：[Glasp transcript summary](https://glasp.co/youtube/Tj0YAbm41vM)（二手摘要，只用来观察“业务对象、关系、浏览/透视”工作流，不作为工程契约）

### OntoFlow / AbutionGraph（供应商公开资料，不视为实现/安全验收证据）

- [ThutmoseAI/OntoFlow README 与公开文件列表](https://github.com/ThutmoseAI/OntoFlow)
- [OntoFlow 官方产品页](https://www.thutmose.cn/ontology-flow.html) 与 [厂商账号产品说明](https://gitcode.csdn.net/6a0be7e010ee7a33f273a4ce.html)
- [AbutionGraph 官方文档目录](https://github.com/ThutmoseAI/AbutionGraph/tree/main/docs)：[技术简介](https://github.com/ThutmoseAI/AbutionGraph/blob/main/docs/%E6%8A%80%E6%9C%AF%E7%AE%80%E4%BB%8B.md)、[功能概览](https://github.com/ThutmoseAI/AbutionGraph/blob/main/docs/AbutionGraph-%E5%8A%9F%E8%83%BD%E6%A6%82%E8%A7%88.md)、[Aremlin API 概览](https://github.com/ThutmoseAI/AbutionGraph/blob/main/docs/AbutionGraph-AremlinAPI.md)、[扩展功能](https://github.com/ThutmoseAI/AbutionGraph/blob/main/docs/AbutionGraph-%E6%89%A9%E5%B1%95%E5%8A%9F%E8%83%BD.md)、[解决方案](https://github.com/ThutmoseAI/AbutionGraph/blob/main/docs/AbutionGraph-%E8%A7%A3%E5%86%B3%E6%96%B9%E6%A1%88.md)、[快速上手](https://github.com/ThutmoseAI/AbutionGraph/blob/main/docs/%E5%BF%AB%E9%80%9F%E4%B8%8A%E6%89%8B%E4%BD%93%E9%AA%8C.md)；Aremlin API 目录另列数据源、查询、聚合、过滤、转换与元数据管理主题。

### 数据与评测

- [CQ4OE paper](https://arxiv.org/abs/2609.26029) 与 [benchmark repository](https://github.com/oeg-upm/cq4oe-benchmark)
- [OntoURL paper](https://arxiv.org/abs/2505.11031)
- [SCOPE/SCION paper](https://arxiv.org/abs/2607.21610)
- [OSKGC repository and license](https://github.com/HeraclesWang/OSKGC)
- [LUBM official benchmark page](https://swat.cse.lehigh.edu/projects/lubm/)
- [OAEI 2026 results](https://oaei.ontologymatching.org/2026/results/)
- [TGB 2.0 paper](https://arxiv.org/abs/2406.09639)；[TGB temporal hypergraph docs](https://tgb.complexdatalab.com/docs/thg/)
- [FactoryNet dataset card](https://huggingface.co/datasets/FactoryNet4/factorynet)；项目仅使用 manifest 锁定的子集，不等同于公开全集
- [XJTU-SY bearing dataset repository](https://github.com/WangBiaoXJTU/xjtu-sy-bearing-datasets)
- Temporal KG bundle：[ICEWS data record](https://doi.org/10.7910/DVN/28075)、[YAGO/Wikidata temporal benchmark code and data link](https://github.com/dair-iitd/tkbi)、[DBpedia-TKG release](https://github.com/dbpedia/dbpedia-temporal)、[GDELT Events archive](https://data.gdeltproject.org/events/)
- Domain and task candidates：[BTS paper](https://arxiv.org/abs/2406.08990)、[BIM-NLQ](https://github.com/MengtianYin/BIM-NLQI)、[MeSH-Rel-4K paper](https://arxiv.org/abs/2607.17902)、[SemMedDB download/access](https://lhncbc.nlm.nih.gov/temp/SemRep_SemMedDB_SKR/SemMedDB_download.html)、[WebNLG 2020 challenge](https://synalp.gitlabpages.inria.fr/webnlg-challenge/challenge_2020/)、[SCANIA Component X](https://www.nature.com/articles/s41597-025-04802-6)
- Industry/operations candidates：[I4.0 KG paper](https://www.semantic-web-journal.net/system/files/swj3431.pdf)、[IMAD-DS](https://dcase-repo.github.io/dcase_datalist/datasets/anomalous/imad-ds.html)、[PCCA](https://huggingface.co/datasets/Yukko-kurisu/PCCA)、[ChronoGraph](https://arxiv.org/abs/2509.04449)、[Edge-IIoTset](https://napier-repository.worktribe.com/output/2969370/edge-iiotset-a-new-comprehensive-realistic-cyber-security-dataset-of-iot-and-iiot-applications-for-centralized-and-federated-learning)、[Bosch Production Line Performance](https://www.kaggle.com/c/bosch-production-line-performance/data)

---

**修订记录**：v0.1（2026-10-23）核对 GitHub 上游 refs 和本地 `palantir` 原型；明确八项目标、能力差距、基准筛选、木材演示图布局恢复、两人分工与阶段 Gate。v0.2（2026-10-23）增加优先级功能清单、对照视频的分步映射和完整候选数据集任务/风险目录；校正 OntoURL、DBpedia-TKG 等版本/规模描述。v0.3（2026-10-23）增加 ThutmoseAI OntoFlow 产品资料对照。v0.4（2026-10-23）进一步核对 AbutionGraph 技术文档，补充 Explore/知识库/控制面差距、双人工作包、里程碑与估算。v0.5（2026-10-23）按老师提出的八项目标和四类横向硬要求补齐追溯矩阵；重排页面/运行时/治理里程碑，新增 11 个目标工作面的 90% 功能验收口径和全平台差距边界。v0.6（2026-10-23）更新 Agent Session/Action 权限原型状态，明确已实现与待验收边界。v0.7（2026-10-23）记录 ActionRun 单决策事项唯一性、数据图投影自动重试/审计状态和手动重试入口的实现证据；明确该投影恢复切片不等于 M4 Action Runtime 全里程碑完成。v0.8（2026-10-23）记录审核时固定数据版本、预览版本竞争检查、持久审核入口触发、已批准版本校验、审核行编辑应用和 Celery task 注册的实现/验证证据。v0.9（2026-10-23）记录持久 `MappingApplyRun`、审批与运行记录原子提交、Celery 恢复派发和状态查询薄片；明确通用映射事件重放、删除对账、质量/模式闸门及跨存储 exactly-once 仍未完成或未证明。v1.0（2026-10-23）补充通用时序 push 的追加式删除 tombstone、as-of 历史查询和后续 upsert 恢复证据，并区分它与结构化 Mapping 删除对账及生产 offset/checkpoint 的边界。v1.1（2026-10-23）补充已批准 Mapping 固定快照的 schema/主键 gate、schema fingerprint 与 24 项针对性回归证据；明确它仍不等于通用增量同步、删除对账或完整质量发布闸。v1.2（2026-10-23）把当前要求明确为计划先行，补充 M0 的逐页验收矩阵和具体步骤；核对本地迁移 `0046` 与结构化 Mapping 快照对账代码状态，记录 307 条未归属历史实例、零运行/对账记录和未完成的功能验收边界。v1.3（2026-10-23）新增页面与任务验收矩阵文档，按实际前端路由盘点 11 个工作面、8 条端到端任务、状态标签、90%计算口径和独立视觉验收条件，并将矩阵设为 M0.2 的候选基线。v1.4（2026-10-23）把 8 条跨页面流程细化为 M0.3 场景计划，核对当前 Wood/frePPLe 与 FactoryNet 演示材料、供应链样例文件和模型费用边界，记录现有/拟建 fixture、验收步骤、通过条件、证据要求及未执行状态。v1.5（2026-10-23）新增 M0.1 当前能力与证据盘点；强调本项目范围内任务覆盖目标、全平台功能范围和视觉相似度是三个独立口径。v1.6（2026-10-23）依据用户“页面和功能 90% 相似”的问题，补充页面结构/交互相似度量表及 M0.4/M0.5 的具体评分与重估要求，并引用 Object Explorer 当前工作树可见的历史受控 API 视觉证据及其复验边界。 v1.7（2026-10-23）补充当前 85 条工作树变更的分布以及 0040—0046 迁移文件未入当前 Git 提交的重现风险，将逐文件处置和迁移复现纳入 M0.1/M0.5 Gate。

补充修订记录：v1.8（2026-10-23）把页面验收口径明确为至少 10/11 页单页评分 ≥90、11 页加权均分 ≥90，防止将总体均分误读为页面达标比例。

补充修订记录：v1.9（2026-10-23）将 Scenario 自动 rebase 的 `system_write` 风险纳入 FR-05、M1/M4 与 G2，要求后台写入使用独立 system principal、显式 scope 与审计。v2.0（2026-10-23）将 Agent Session 当前 owner-only 访问列为已核实边界，并把参与者 ACL、跨应用续接、权限交集、actor 归因、即时撤权和对应 M0/M1/M7/G4 验收写入计划；本轮仍只更新计划材料。v2.1（2026-10-23）明确 11 个目标页面总分采用等权算术平均，每页权重 1/11，消除“加权均分”权重未冻结造成的评分歧义。v2.2（2026-10-23）将附件提出的平台 Resource Registry/身份治理落到 FR-08、M1、M8；明确 M3 先做只读探索、M4 才接入唯一 Action Runtime，M7 再将 Agent 模块接入 M6 应用组合层，避免里程碑依赖倒置。v2.3（2026-10-23）将 Agent Manager 式定义/会话运营控制面（查看、暂停、恢复、取消、撤销及审计）纳入 P0、M7 和 G4。v2.4（2026-10-23）将 0040—0046 空库升级、数据副本核对和 downgrade guard 的验证隔离范围写入 M0.1/M0.5，禁止把当前开发库降级当作回退演练。

补充修订记录：v2.5（2026-10-23）将 Palantir 可比功能覆盖率、本项目 Must-have 任务率和页面相似度定义为三个独立验收指标；要求 M0.4 冻结官方参照原子功能及范围裁决，禁止把范围受限的 90% 表述为 Foundry/AIP 全产品 90%。v2.6（2026-10-23）加入逐项 Palantir 功能对照目录，列出纳入/范围外/不可比的原子功能及加权覆盖规则。v2.7（2026-10-23）根据当前 Session/Run/API/前端源码，明确现有原型为 owner-only、请求内同步推理、超时后标记 interrupted、存在 sessionless 旧 API；为 M7 增加 Definition 发布、共享 Session ACL、durable worker/checkpoint 恢复、scoped Manager、旧入口收敛与 E2E-06/G4 退出 Gate。本轮仅更新计划材料，未实现或执行验收。

补充修订记录：v2.6（2026-10-23）新增候选 Palantir 可比功能目录，将附件与旧 PRD 的能力补集映射为带范围裁决、优先级、阶段和验收行为的对照表；补齐 LLM 辅助本体构建、事件类型与可视化权限配置要求。本次仍只更新计划材料。v2.8（2026-10-23）根据分域资源模型、SecurityPolicy、ObjectSet/Scenario ACL、领域审计、`/health`、data-plane 和 Celery 配置证据，细化统一 Resource Registry/principal/audit/lineage/observability/backup 的 M8 分解与 G5 退出门槛。本轮仅修改计划文档；没有实施、执行测试/迁移、启动服务或演练恢复。v2.9（2026-10-23）只读核对 Connection/Dataset/Pipeline/Construction/Curated/Mapping/Incremental 源码，明确连接器支持边界、LLM 候选确认现状、独立运行模型与 M2 缺失的统一 WorkflowRun/quality/offset/checkpoint/replay 合同；将 M2 拆为五个交付包并把估算调整为 3–5 周（总计 16–23 周，待 M0.5 重估）。v2.10（2026-10-23）静态审阅 0041 约束、0043 Review pin、Dataset 级联删除、MappingApplyRun 的无 FK 版本引用及 0046 downgrade guard 的实际覆盖；将 DatasetVersion/CuratedReview/MappingApplyRun 保留策略和 guard 不变量加入 M0.1/M0.5/M2.0 计划。未改产品代码或执行迁移/删除/测试。v2.11（2026-10-23）只读静态发现 Mapping Apply `limit=10000` 读取上限和结构化快照删除对账之间的潜在错配；新增 manifest 完整性合同、10,001 行全量读取及 10,000 行截断 fail-closed/零删除验收计划，并同步 M2.0/M2.4/M2.5、M0.5 重估、E2E-02 和 DATA-03。未实施或运行测试、迁移、服务或模型。

补充修订记录：v2.12（2026-10-23）为 O1–O8 与 H1–H4 增加主阶段、E2E 和发布 Gate 的直接追溯，并将快照完整性验收同步进总览 M2 退出条件；本轮仅更新计划材料，未实施或运行验收。

补充修订记录：v2.13（2026-10-23）将 M0.1–M0.5 的 A/B 主责、交付物和退出判据合并到步骤表，明确当前不得越过 M0 进入 M1/M2 实施，并把 M0 初步工期纳入 M0.5 复核；本轮仅更新计划。

补充修订记录：v2.14（2026-10-23）将 M0.1 每条变更的逐 diff 审阅字段、unknown 关闭条件、责任人与后续 Gate 写入能力盘点和阶段签收标准；本轮仅更新计划，未审阅全部 85 条 diff。

补充修订记录：v2.15（2026-10-23）将映射投影重试上限/终态处置、授权人工重放和 `MappingRowChange` 行快照分类、访问、保留、删除与脱敏要求纳入 M2/M8、M0.5 和 E2E-02；本轮只作静态审阅与计划更新，未运行故障注入或修改产品代码。

补充修订记录：v2.16（2026-10-23）对照目标架构附件补齐 M1 OpenAPI 前端类型生成与 ObjectRef/ObjectSet/ScenarioContext/EvidenceRef/ActionContext 跨入口契约测试，并将其纳入 G1 证据和 M0.5 工期复核；计划完整性审计确认 8 项主目标与 4 项横向要求均有阶段/Gate 映射，本轮未实施产品功能或运行测试。

补充修订记录：v2.17（2026-10-23）根据 DecisionCase :reconcile 当前仅管理员可调用且会重置投影计数的静态证据，要求 M4/G2 区分自动与人工重试预算、保留完整重试历史并审计理由/actor；将 ActionRun 投影故障路径加入 E2E-05 和 M0.5 重估。本轮仅计划更新，未实施或执行故障注入。v2.18（2026-10-23）在 FR-03、M5 与 E2E-03 对齐通用时序流 tombstone/as-of/恢复验收，并明确不与 Mapping 快照删除或 FactoryNet upsert-only 路径混淆；仅更新计划。v2.19（2026-10-23）根据候选测试源码的静态覆盖差异，要求恢复后回查删除时点，并分别验收同 ID 同操作载荷冲突和不同操作冲突；候选测试未运行，仅调整计划。v2.20（2026-10-23）对照原目标架构 P1 和官方 Functions/Logic/Evals/SDK 资料，将受限 Function/Logic 版本发布、评估、监控及选定 TS/Python clients 纳入 M7/DEV-03/FUNC-01/E2E-06，并要求 M0.5 重估；完整 OSDK 和任意代码沙箱仍列为本期外范围。

补充修订记录：v2.21（2026-10-23）对照目标架构 §12 P0 第 6 项，将 Wood Supplier B 影响链与推荐卡纳入 P0 功能清单、FR-04 和 M5；E2E-04 绑定经业务签收的 synthetic 规则、证据/版本与负向拒绝态，推荐只生成 DecisionCase/Action Preview；通用分析 canvas 仍为 P1，领域规则不自动进入 Palantir 可比功能分母。本轮仅修改计划。

补充修订记录：v2.22（2026-10-23）对照目标架构 §12 P0 第 8–10 项，将共享 ResultManifest 的来源/权限裁剪/revision/状态字段与无泄漏规则纳入 FR-01/07、M1/M3/M5、M0.2/M0.3 及 G1/G2；仍为计划，未实施。

补充修订记录：v2.23（2026-10-23）依据只读审阅发现审计任务的 `revision_id` 未用于 worker 快照、revision snapshot 仍可能包含活动实体以外的关系，加入 M0.1/M0.3/M0.5 与 M1 的固定 AuditInput/revision digest 合同和 R1/R2 tombstone 验收；仅更新计划，未修改产品代码或运行测试。

补充修订记录：v2.24（2026-10-23）将 E2E-01 与 M0.3 对齐为 R1 创建、R2 tombstone、R3 恢复，并明确旧 revision 结果不得被后续状态改写；未执行验收。

补充修订记录：v2.25（2026-10-23）依据 `main.py::_run_schema_migration()` 的静态证据发现 Alembic 异常会被全环境捕获并回退到 `create_all`，将迁移失败 readiness、M0.1/M0.5 风险重估和 M8 生产恢复验收纳入计划；未运行迁移或服务。

补充修订记录：v2.26（2026-10-23）依据 What-if 路由与邻域读取静态审阅，将客户端 baseline 校验、本体/字段 ACL、revision pin、ordinal/event/valid/ingest time 区分及同源同权限向量证据纳入 M1/M5 与 E2E-03/04/07，并计入 M0.1/M0.5；同步更新页面矩阵 v1.9 和 SCN-02 非可比验收边界；未实现或运行验收。

补充修订记录：v2.27（2026-10-23）只读检查 Action projection worker 和 DecisionCase `:reconcile`，补充自动 worker 不得把案例创建者记录为执行 actor、人工重试必须携带理由、自动/人工尝试账本需分离的 FR-05、M4、M0.5 与 E2E-05 验收要求；仍为计划，未运行 worker、测试或迁移。v2.28（2026-10-23）静态核对 Wood 当前布局代码与仓库截图状态，确认 `cose` 设置仍在但目标截图目录缺失，2026-09-21 验收仅为历史记录；将 12 张截图、固定采集上下文、图布局波动容差、SHA-256 和可版本化归档位置纳入 M0.2/M0.3 视觉基线 Gate；本轮未启动浏览器或服务。

补充修订记录：v2.29（2026-10-23）静态核对 MappingApplyRun、MappingRowChange、Mapping Apply 服务与行投影：Run 级 dispatch/execution 已有 20/8 次上限，但行投影未见最大尝试或 dead-letter，且 requester/worker actor 与策略快照未被显式保存。将不同主体、不同重试账本和人工重放授权计划同步到 FR-02、M2.0/M2.4、M2 退出条件与 M0.5 重估；未修改产品代码或运行测试。

补充修订记录：v2.30（2026-10-23）对照 8 项主目标、4 项横向要求、M0 步骤表和目标架构实施路线，确认规划覆盖映射齐全；将上一轮发现的 Mapping Apply actor lineage 与 Run/RowChange 双层重试缺口补入 M0.1 签收判据，使其先于 M0.5 重估和 M2 实施 Gate 完成裁决。仍处于规划阶段，未实施功能。

补充修订记录：v2.31（2026-10-23）只读复核 0040—0046 源码后，将 0042 pending/failed 投影任务同时转为 due、0043 删除 DatasetVersion 可通过 `SET NULL` 清除 Review pin、0046 downgrade 未保护 source owner/Entity flag 三项具体风险同步写入 M0.1/M0.5 与 E2E-08。未执行迁移、故障注入或实现修改。

补充修订记录：v2.32（2026-10-24）静态审阅 Entity/Instance tombstone 写入与列表、统计、Graph、revision、audit、semantic sync、editor、export 消费路径；将当前态过滤、活动关系端点/计数一致、历史 snapshot 固定读取与恢复不得改写旧 revision 加入 FR-01、M0.1/M0.5 和 E2E-01。未运行服务、测试、迁移或 API。

补充修订记录：v2.33（2026-10-24）发现 11 个工作面中有直接页面、多个产品面的组合参照及 Project 4 专属推演页面；将页面评分改为直接 Palantir 相似度、组合参照对齐度和 Project 4 UX 分数分开报告，并要求 M0.2/M0.4 冻结每页来源 URL、截图类型/hash、状态和可比性。仅更新计划，未实现或验收。

补充修订记录：v2.34（2026-10-24）静态审阅 Scenario Workbench、FactoryNet What-if 与 demo router，确认两套持久模型和不同 ACL/baseline/revision/time 路径；为 M0.1/M1/M4/M5、M0.5 与 E2E-03/04/05/07 增加 canonical route 决策、兼容/迁移/退役、逐资源授权与 system rebase actor 要求。仅更新计划，未实现、调用 API 或验收。

补充修订记录：v2.35（2026-10-24）对照 PRD、M0 验收方案与 Palantir 功能目录时发现 PRD 将核心闭环写作 7/7，而实际冻结候选为 E2E-01—08 共八条；已统一为 8/8，并逐条列明数据建模、动态更新、时序检索、Twin 决策、Action、Agent、权限和生产恢复闭环。仅修正计划口径，未实施或验收。

补充修订记录：v2.36（2026-10-24）只读审阅 Mapping API 与服务调用链，发现 router 仅显示登录依赖；原始 rows apply、latest-version apply-from-dataset、build-all 未显示复用 approved Review/DatasetVersion pin/MappingApplyRun 的合同。为 M0.1/M1/M2/M0.5 与 E2E-01/02 增加逐端点资源授权、旧路径适配/退役和未授权/未审核零副作用门槛。仅更新计划；未调用 API、运行测试或修改产品代码，静态证据不构成运行时漏洞确认。

补充修订记录：v2.37（2026-10-24）只读审阅新版 ConstructionDraft/MappingTask 与旧 Extraction worker，发现构筑时 latest DatasetVersion 未显式 pin、`mapping_confirmed` 后直接进入物化/revision 路径，以及旧 worker 读取当时的文件/Prompt/ModelConfig 并分阶段提交语义表。将精确版本与 Prompt/model/tool/parser 追溯、候选/审批/发布分离、旧入口 adapter/退役、失败零部分写入纳入 M0.1l/M1/M2.3/M0.5 与 E2E-01。仅更新计划材料；未调用模型/API、运行服务/测试/迁移或修改产品代码，静态审阅不构成运行时漏洞结论。v2.38（2026-10-24）有限源码审阅发现高级语义类型已有后端 schema/model/service 线索但未证实专用可视化编辑流程；定向搜索未找到独立运营工作队列模型/页面。新增 ONT-05 与 OPS-01 候选，将前者列入 M0.4/M1/M2 范围裁决，将后者列入 M0.4/M8 规划，并在矩阵/E2E 增加类型兼容性、队列分派、SLA、权限与审计验收；仅更新规划文档，未改产品代码或执行验收。v2.39（2026-10-24）将 M2.3 的高级类型编辑拆解为引用、派生依赖、兼容性和发布/恢复 Gate；将 M8.4 运营队列细化为项目范围、权威状态链接、幂等事件投影、权限裁剪与分派审计，并明确不包含通用外部自动化。仅更新计划，没有改产品代码或运行验收。

补充修订记录：v2.40（2026-10-24）依据目标架构 §7.3/Phase 2 与有限源码搜索（发现资源分配优化原型但未证实运营队列集成），将选中对象集批量 Action（ACT-04）、受限人工覆盖（ACT-05）和运营负载分配（OPS-02）列为 M0.4 待裁决候选；补充选择快照/digest、逐对象权限/版本检查、部分成功语义、覆盖理由/批准链和容量数据/人工回退 Gate，并纳入 M4/M8 与 E2E-05/08。仅更新计划文档，未改产品代码或运行验收。

补充修订记录：v2.41（2026-10-24）核验 Palantir 官方 Object Explorer 选中对象批量 Action（1,000 对象 UI 上限）、Automate 分批执行、Action 参数/Vertex Scenario override、Approvals inbox 与 Resource Queues 语义后，将 ACT-04 定为直接参照，将 ACT-05/OPS-01 分为组合参照、OPS-02 标为 Project 4 专属；目标架构要求的四项保留在 Must-have，M0.4 只冻结语义与计分方式。仅更新计划，未实施或运行验收。

补充修订记录：v2.42（2026-10-24）按当前 `palantir` 工作树快照统一活动计划中的状态盘点数量为 87 条（59 个已跟踪修改、28 个未跟踪文件）；依据局部静态代码审阅，细化 Action 周期投影、尝试与 actor 分离，以及 Agent owner-only、请求内同步推理和中断识别的计划边界，并将 M1/M4/M7/M8 拆成顺序工作包和退出条件。仅更新计划文档；未修改功能代码、运行测试/服务或查询数据库。

补充修订记录：v2.43（2026-10-24）发现 M0.1 能力盘点、目标架构附件、页面矩阵、功能目录和 E2E 计划被 `.gitignore` 的 `docs/*` 排除，只有 PRD 有例外放行；能力盘点中的 85 项清单是 2026-10-23 历史快照，而当前 `palantir` 工作树为 59 个已跟踪修改、28 个未跟踪文件，共 87 项。新增 M0.1o，要求签收前确定唯一主版本、分发路径与交叉链接；保持当前忽略策略不变，除非用户另行授权。仅更新计划，未改代码、测试、服务或数据库。

补充修订记录：v2.44（2026-10-24）依据 `.gitignore` 对 `docs/*` 的“内部/agent-only、不进行 Git 同步”注释，裁定目标架构、页面矩阵、功能目录、E2E 与能力盘点在本阶段作为本机内部规划依据；PRD 与根目录路线图/处置记录提供当前可读入口，不声称完整计划包已随分支分发。85 项旧清单与当前状态比较后确认只有两份根目录规划文件新增；本轮不改忽略策略，未来如需团队分发再单独确认。仅更新计划。
补充修订记录：v2.45（2026-10-24）将内部字段投影安全合同加入 FR-05，并记录共享 compiler 包装层尚不等于唯一 Action Runtime；同步明确 M3/M5/M6 逐阶段页面评分和复评计划。仅更新规划文档，未修改代码或运行验收。
补充修订记录：v2.46（2026-10-24）依据 Agent Session/API/服务/模型定向审阅，补充 caller/participant/model/tool/worker/system actor 区分及事件数据留存边界；更新 M7 规划退出条件。仅更新计划，未实现或运行验收。

补充修订记录：v2.47（2026-10-24）只读审阅 Temporal Stream、What-if 与向量搜索调用链，将资源级 ACL、服务端 baseline、ScenarioContext 时间/版本/权限 pin、对象/关系删除恢复、完整事件幂等摘要和 durable outbox/lease/reconciliation 加入 FR-03/04、M0.1/M0.5、M1/M5 与 E2E-03/04/07；ACL 风险仅为静态规划发现，未做运行复现或故障验收。本轮仅更新计划文档，未修改产品代码或运行测试/迁移/服务。

补充修订记录：v2.48（2026-10-24）核对木材 SupplierStudyPage.tsx 的 Git 历史：dda5143 为 cose，7ab1cd1 改为确定性 preset，当前未提交差异又改回 cose；仓库未找到 Supplier Study 专用历史截图。因此不再把任一算法直接视为“恢复原演示效果”，由 M0.2 先用可追溯截图/演示记录及用户或业务负责人签收视觉基线，M5 再按该基线实施和复验。仅更新计划，没有修改功能代码或运行验收。

补充修订记录：v2.49（2026-10-24）按当前工作树重新核对 Action Runtime 调用点，明确共享代码仅提供授权编译边界；DecisionCase Live、Scenario revision/duplicate、木材专用 Scenario 与 pre-Project 兼容路径仍有不同持久状态机，Agent 通过 DecisionCase 创建提案而不直接提交 Action。将入口→编译→状态机→副作用→actor 矩阵纳入 M0.1/M4.1；仅更新计划，没有实现功能或运行测试。

补充修订记录：v2.50（2026-10-24）静态审阅 PipelineRun、ConstructionRun、MappingApplyRun 与旧 Extraction 的输入解析和阶段提交路径；将 PipelineVersion/各来源 DatasetVersion manifest pin、统一 parent WorkflowRun/child lineage、dispatch recovery、重复触发幂等和中途失败仅保留隔离候选的门槛补入 M0.1、M1、M2.2/M2.5 与 E2E-01/02。没有运行 worker、模型、测试、迁移或业务写入。

补充修订记录：v2.51（2026-10-24）定向核对基础本体编辑表单、语义迁移预检、temporal source/run 和 Settings 权限策略入口；将局部 UI 覆盖与 EventType 编辑、revision diff/独立发布/恢复、权限更新/有效性预览/撤权传播的验收边界补入 M2.3、E2E-01 和功能目录。仅更新计划文档；未改产品代码或运行浏览器、测试、模型、迁移、服务或业务写入。

补充修订记录：v2.52（2026-10-24）对照目标架构的同一对象跨 Explorer、Twin、Action、Agent 与治理复用原则，新增 E2E-04/05/06 后置纵向集成 Gate，并写入 G4 完成定义；不增加第九条核心 E2E，不把单项通过等同全链路集成。仅更新计划。

补充修订记录：v2.53（2026-10-24）静态核对 Explorer URL state、ObjectSet 操作和 ObjectPanel Full View dialog；将独立 ObjectRef deep link、DataView/filter 上下文恢复与资源权限验证补入 M3 和页面验收。仅更新计划，未运行浏览器/API 验收。

补充修订记录：v2.54（2026-10-24）对照 MappingApplyRun 的 dispatch/execution 异常分支与 lease recovery，发现现有 20/8 上限未在所有恢复状态转换中显式强制；并确认行 SQL commit 成功、图投影仍 pending 时 Run 可标 succeeded，当前 UI 只呈现 Run 状态。将全路径硬上限、SQL/投影分态展示和消息丢失/worker lease-expiry 故障注入纳入 M0.1、M2.4/M2.5、M8 与 E2E-02。仅更新计划，未运行服务、测试或故障注入。

补充修订记录：v2.55（2026-10-24）静态审阅 Agent Session 推理调用链与 ModelConfig/AgentRun 字段后，发现 Run 先提交、随后才按可变名称读取配置，缺少派发前 ModelConfig revision/脱敏指纹及 provider/model 制品版本 pin。将不可变模型 lineage、秘密排除、不可验证制品标记和排队后配置变更 fail-closed 验收加入 M0.1q、M7.1/M7.3/M7 Gate 与 E2E-06。仅更新计划文档，未修改产品代码或运行测试/服务/模型。

补充修订记录：v2.56（2026-10-24）定向审阅 Pipeline LLM 选择器/调用异常、规则提取 metadata 和 Transform UI，发现用户明确请求 LLM 时可落入 `rule_based` 并将提取步骤标为 `done`，但没有请求方式与降级原因。将默认 fail-closed、显式 fallback 才可继续、degraded 状态/质量归类和界面一致性纳入 FR-02、M2.2/M2.3、M0.1r 与 E2E-01。仅更新计划材料，未改产品代码或运行测试/服务/模型。

补充修订记录：v2.57（2026-10-24）静态审阅 ConstructionDraft MappingTask 入队/worker/两阶段 M3 调用/retry 与任务模型，发现 Dataset latest 会在来源描述和内容打包时独立解析、ModelConfig 由 worker 延迟选取，且 retry 复用任务 ID 并覆盖 trace。新增 M0.1s、FR-02、M2.0/M2.2/M2.3 与 E2E-01 的不可变 manifest、同 pin 双阶段执行、fail-closed、append-only attempt 和配置/数据漂移验收；仅更新规划，未修改产品代码或运行任务。

补充修订记录：v2.58（2026-10-24）静态审阅普通/多模态 Construction 页面与 MappingTask 查询接口，发现 UI 只显示当前进度和近期 trace，页面未消费 manifest，接口也未返回 attempt/ModelInvocation 历史。将刷新后可查、按权限裁剪的 task→attempt→phase lineage UI Gate 纳入 M0.1s、M2.2/M2.3/M2.5 与 E2E-01；仅更新计划，未改产品代码或运行 UI 验收。

补充修订记录：v2.59（2026-10-24）静态核对 approved Review→MappingApplyRun 原型后，将数据-only 更新复用已批准 Mapping ID/digest 与 OntologyRevision、禁止隐式重跑本体映射、schema drift 单独提案审批及 Transform LLM 独立分类写入 FR-03、M2.4 和 E2E-02。仅更新计划，未改产品代码或运行测试。

补充修订记录：v2.60（2026-10-24）对照 FR-04 的 rebase/冲突合同与 E2E-04 当前步骤，补入 Tracking/Pinned、并发基线变化、三方冲突决议及 system actor 审计验收，并同步 M5 Gate。仅更新计划，未运行 Scenario 或修改产品代码。

补充修订记录：v2.61（2026-10-24）为避免把三类传输模式缩成三类业务数据，在 M2.1 明确 ERP/MES/SCADA/IoT/文档/图像六类都必须有独立来源族验收子用例并映射到共同 Ontology；缺项不得由另一格式替代，native connector 与 fixture 分开报告。仅更新计划，未验证许可或运行映射。
补充修订记录：v2.62（2026-10-24）将目标架构中的租户/项目/用例隔离设为 M0.4 明确裁决项，并对齐功能、直接页面、组合页面、项目专属页面 UX、Must-have 五项独立评分结果；当前仍按单项目基线规划，纳入多租户时须扩展 E2E-07 并由 M0.5 重估；明确 16–23 周仅为旧范围基线，不是当前完整范围排期。仅更新计划。

补充修订记录：v2.63（2026-10-24）依据 AgentSession/AgentRun 当前源码，将基于 created_at 的 90 秒 interrupted 判定与 worker lease/heartbeat 分开；补入运行中取消、fencing、checkpoint/tool attempt 幂等恢复及 Event/Run/Checkpoint/log/trace/export 的合成 canary 脱敏验收。仅更新 PRD，未运行模型或服务。


补充修订记录：v2.64（2026-10-24）依据 User/AuthorizationContext/ObjectSet/cron 路由与 Celery Beat 源码，区分全局 user/role 授权和 user/group/service/system principal 合同、局部 ObjectSet 目录与跨域 Resource Registry、cron 描述和持久 Scheduler；为 FR-08 添加 schedule revision、时区/DST、misfire、overlap、幂等及 Run lineage 语义。仅更新 PRD，未运行调度。

补充修订记录：v2.65（2026-10-24）静态对照 legacy revision snapshot/restore 与 canonical semantic resource/source mapping revision-copy 路径；未见 restore 显式复制 canonical-only 资源和来源映射。将共同 manifest/hash、双面 diff、恢复为新 revision、旧快照不可变、引用完整和 reader 对齐写入 M2.3/E2E-01。仅更新 PRD，未运行复现、测试或服务。

补充修订记录：v2.66（2026-10-24）依据目标架构 Identity & Tenant/Resource Registry/Phase 6 与 User/AuthorizationContext/Policy/Grant 当前模型证据，新增 GOV-06 A/B/C 决策卡；推荐 tenant-aware 合同先行、首期单 tenant、生产前跨租户验收，并要求 M1 schema freeze 前签收、M0.5 重估。仅更新 PRD，未改产品代码或运行授权验收。

补充修订记录：v2.67（2026-10-24）静态核对 E2E-08/F-PERF、health endpoint 和仓库负载/标准 metrics 接入搜索；发现性能基准档位与 harness 尚未形成。补入 M8.6 可重放 workload manifest、核心路径矩阵、冷启动/稳态分离、重复测量、正确性/ACL 与 M0.5 目标硬件阈值签收规则。仅更新计划，未运行压测或修改产品代码。

补充修订记录：v2.68（2026-10-24）静态核对 Pipeline `/run`、`/run-sync` 路由与 worker 的终态更新，发现父 Pipeline 状态投影在异步与同步入口间不一致，且早期 worker 异常可能留下 pending Run；为 FR-10 增加统一 WorkflowRun 状态来源和 E2E-01 故障验收合同。仅更新 PRD，未改产品代码或执行故障注入。

补充修订记录：v2.69（2026-10-24）对照 Action `:reconcile` 请求、projection_attempts 清零路径、候选测试及 FR-05 重试账本合同，记录当前测试断言与目标相反，要求人工重试理由/scope/actor、独立追加尝试和自动预算保留并更新该测试。仅更新 PRD，未运行测试或修改应用代码。

补充修订记录：v2.70（2026-10-24）静态追踪注册 Ontology 的 Scenario Action `/run`、`ScenarioService.revision()` 两次 SQL commit 与现有路由测试；记录 ScenarioRevision 已提交但 ActionRun 仍 running/后续失败，以及 response-loss retry 新建失败 Run 的窗口。将单一权威状态、request-key 回放、崩溃/异常恢复和零重复副作用加入 FR-05、M0.1、M4 与 E2E-05。未运行测试或故障注入。

补充修订记录：v2.71（2026-10-24）核对 `logic_actions.router` 的全局 `require_action_ontology` 与 direct-call 测试；确认 pre-Project 旧编译分支在 HTTP 层因缺少 `OntologyProject` 被拒绝，直接调用 endpoint 函数的测试绕过了路由依赖。将 API-level 覆盖和“不把直调/mock 当作路由证据”写入 FR-05、M0.1、M4 与 E2E-05，避免将 helper 误报成用户可达治理绕过。未运行测试。

补充修订记录：v2.72（2026-10-24）静态追踪 Scenario Workbench `submit_actions` 与 `ScenarioService.revision()` 的两次 commit；发现 revision/head 已提交而 `action_input_hash` 尚未落盘的窗口，且该路径不创建 `OntologyActionRun`。将 Workbench 专属 same-key recovery、原子 manifest 和 HTTP 故障注入 Gate 加入 FR-05/M0.1/M4/E2E-05；未运行测试或故障注入。


补充修订记录：v2.73（2026-10-24）静态核对 ConstructionDraft `PATCH` 在 queued/running MappingTask 期间仍可修改草案、selection 变更清理任务关联而 worker 终态会写回草案。明确活动编辑必须拒绝或产生新 revision/task，旧任务只能附着原 revision，并将并发写回验收补入 M0.1/E2E-01；未运行并发复现。

补充修订记录：v2.74（2026-10-24）依据常规/多模态构筑 UI 的步骤回退、草案 PATCH、本地 task ID 重置和服务端取消路由，增加 queued/running 期间旧任务隔离与取消/恢复可发现性要求；未运行 UI 或任务。

补充修订记录：v2.75（2026-10-24）静态核对 PipelineBuilder/List/TransformsTab、同步与异步接口及 Run 历史排序，增加持久异步 WorkflowRun UI、Run ID 刷新恢复、真实节点进度、保存失败阻断与 same-key 幂等要求；未运行服务或浏览器。
补充修订记录：v2.76（2026-10-24）静态核对 Pipeline 发布、编辑、Run 建立与 worker 取定义路径，增加原子不可变 PipelineVersion/digest、Run pin、草稿 preview 和 Pipeline 资源级角色授权要求，并同步 E2E-01；未改应用代码或执行运行验收。
补充修订记录：v2.77（2026-10-24）对照 `dda5143`、`7ab1cd1`、当前 GraphCanvas、2026-09-21 工程验收记录及 Docker 前端挂载，确认工作树恢复到 Cose 参数候选、截图基线仍缺。修正 PRD/roadmap 的不一致表述，要求先采集/签收视觉基线；未截图验收或修改产品代码。
补充修订记录：v2.78（2026-10-24）对照目标架构 §3.9 与 FR-06，识别 Agent-to-agent 委派尚未与参与者共享 Session/人工接管区分；新增 M0.4 范围决策、AGT-05 验收边界及 E2E-06 条件分支。仅更新计划，未实现或运行验收。

补充修订记录：v2.79（2026-10-24）静态追踪 MappingRowChange→60 秒 reconciler，确认行投影失败会继续保持 pending，重试延迟封顶但没有有限次数/终态；计数只累加在最新 RowChange，未形成追加尝试历史。将 `(mapping_id, object_id)` 定为独立工作项，补入有限自动预算、终态排除、同对象多行处理、跨对象隔离、逐次追加尝试账本和人工重放历史保留，并同步 E2E-02。仅更新计划，未修改应用代码、运行 worker 或测试。

补充修订记录：v2.80（2026-10-24）只读核对本地数据库的 ApplyRun、RowChange、ActionRun 与 EntityInstance 来源归属，确认当前无 durable Mapping Apply/投影记录，但 36,783 条实例均无 source_mapping_id；其中 307 条 source_version 非空且没有匹配 Mapping 候选，36,476 条来源版本为空。将这些记录的身份追查/隔离设为动态更新前置 Gate，并明确本机样本不代表生产或闭环通过。仅更新计划，未改数据库、应用代码或运行测试。

补充修订记录：v2.81（2026-10-24）依据 Palantir 官方 Event/Time Series、Ontology permissions 与 Scenario 文档，细化 Event object/time-series property 分层、Project resource 与对象数据 ACL、Scenario 默认 TTL/自动 rebase 策略；同步 FR-04、M5 和 E2E-03/04。31 天/10 分钟仅作可配置公开参照，默认值待 M0.4 签收。仅更新计划，未修改应用或验收代码。

补充修订记录：v2.82（2026-10-24）依据功能目录预审统计的 43 个候选父项、P0/P1/P2 数量与范围标签交叠，要求 M0.4 先拆独立可失败子项并互斥分类，父项仅汇总、父子不得重复计分；明确原始权重和 105 不是正式分母。仅更新 PRD/评分计划，未运行验收。

补充修订记录：v2.83（2026-10-24）只读复核当前健康接口、Celery worker/Beat 任务注册与投递、进程启动时间及挂载源码修改时间，识别当前运行态早于源码的版本漂移；同时从 Connection Cron 路由/服务确认其 `scheduled` 响应不等于调度注册，Pipeline 缺少 schedule 路由。FR-08 要求 M0.1/M8 记录运行源码/进程一致性，并按持久 Schedule/版本 pin/时区和恢复合同验收。未重启容器、执行任务、写数据库或运行测试。

补充修订记录：v2.84（2026-10-24）依据目标架构 §3.3 与当前 canonical type model/editor 证据，为 ONT-05 增加 A/B/C 范围卡；建议保留 Interface/shared property、ValueType、Struct/field、受限 Derived Property 四个 P1 子项，分别冻结参照类别、验收与 M0.5 估算。该建议未签收；仅更新 PRD/路线图计划，未改产品代码或运行验收。

补充修订记录：v2.85（2026-10-24）重新核实本地前端/后端健康及 Celery active/reserved/scheduled 为空，并在 672×767 本地浏览器视口观察到 `/ontologies` 页列出 22 条本体；把临时、未归档/哈希/签收的截图限定为页面可达性证据，不作为 Wood Study 视觉基线或功能验收。仅更新规划记录，未修改产品代码、运行测试或触发业务任务。

补充修订记录：v2.86（2026-10-24）通过只读 catalog 打开已存在 Wood Study；详情和 Compare 返回 200，三个方案的 graph GET 均返回 500，页面持续显示 loading。将图读取失败根因、UI 显式失败/重试态和隔离环境复现加入 M0.1/M0.2 退出项；未调用 bootstrap/Action/Run，未改产品代码或运行测试。

补充修订记录：v2.87（2026-10-24）静态追踪 Supplier Study 的 graph 读取异常包装和页面状态呈现，发现 ObjectQueryError 在 `case_graph` 入口没有本地 HTTP 映射，前端无 graph.error 分支并持续显示 loading；列为 Graph GET 500 的候选处理缺口，底层 FalkorDB/provider 异常仍待隔离诊断。仅更新计划，未改应用代码或运行测试。

补充修订记录：v2.88（2026-10-24）更正前一条根因假设：当前挂载源码的 Falkor 只读查询可成功，6 个 view 的 `authorize_action_snapshot()` 均复现 `ScenarioError(ambiguous_link_type)`；数据库内 8 组重复 Relation.type 对应不同 source/target 类型，属于需要按稳定语义 link ID/端点解析的模型合同，不应删行或合并。`case_graph` 未映射该 ScenarioError，前端无 graph.error 状态；live HTTP 500 与之相符，但 backend startedAt 早于授权源码 mtime，故待隔离版本对齐后最终确认。另将 4/6 view 的 edge_count 与实际只读读取数差异单列为 snapshot 计数合同核验。仅更新计划，未改应用代码、数据库或运行测试。

补充修订记录：v2.89（2026-10-24）只读确认当前 revision 已有 34/34 legacy Relation 到 canonical link_type version 的一对一映射，unique api_name 与 source/target semantic resource IDs 全匹配；下一阶段直接消费稳定 resource/revision 映射，并保留 endpoint/direction 校验，不需以删除或合并重名关系绕过授权。仅更新计划，未改应用代码、数据库或运行测试。
补充修订记录：v2.90（2026-10-24）将木材图的 canonical Link Type/端点/方向授权、结构化 API 错误与前端显式错误/重试态写入 FR-04；把 `edge_count`/返回边数/digest 口径及 ACL 后计数泄漏风险作为独立 Gate，要求 E2E-04 在进程源码版本对齐后复验。仅修改计划，未实现产品代码或执行测试。
补充修订记录：v2.91（2026-10-24）对照 M1–M8 阶段区间计算 15–22 周串行加总，并和 16–23 周历史估算明确区分；要求 M0.5 按最终范围、人员可用度、并行依赖和 fixture/许可给出低/基准/高日历估算与置信度，不把初步加总表述为完成日期。仅更新规划，未实现或运行验收。
补充修订记录：v2.92（2026-10-24）将 Ontology Audit 的实际输入绑定到 immutable revision snapshot/hash 或 enqueue 固化的 AuditInput manifest，要求 worker/ModelInvocation/findings 共用 digest，并为 stale RepairDraft 定义授权 rebase 或 fail closed；仅更新计划，未实现或运行验收。
补充修订记录：v2.93（2026-10-24）将 Alembic 单一迁移边界、失败时 not-ready/不可写、显式开发 legacy 兼容模式及 liveness/readiness 分离写入 FR-08；与 E2E-08 的 disposable DB 迁移失败注入 Gate 对齐。仅更新规划，未实施迁移或运行测试。
补充修订记录：v2.94（2026-10-24）依据 `snapshot_ontology()` 与 revision restore 静态路径，明确新快照内关系端点必须闭合、活动实例/计数必须限定于本快照实体集合，restore 必须创建经校验且与目标语义一致的新 revision；与 E2E-01 对齐。仅更新规划，未修改产品代码、运行测试或迁移。
补充修订记录：v2.95（2026-10-24）逐 diff 审阅 18 个 Ontology/Graph 文件后，为 graph quality 增加原始残留关系与当前可见 edge/count 分离语义，并要求 Neo4j/FalkorDB 和 semantic sync 不复活端点不可见的关系/节点；仅更新计划，未运行同步、测试或写库。
补充修订记录：v2.96（2026-10-24）依据 MappingApply 的对象行/实例双写及 Ontology Editor 的类型删除调用，明确来源行 tombstone 与 Entity Type impact-check/delete 是两种生命周期，并将 owner count 作为需覆盖全部构造路径的不变量；仅修改规划，未改产品代码或执行验收。

补充修订记录：v2.97（2026-10-24）对照当前 Temporal Stream 差异确认 FR-03 的完整事件 envelope hash 尚未由实现证明：FactoryNet hash 仅覆盖 payload；generic hash 覆盖部分字段但不含完整 source reference/time envelope。将 metadata-only 冲突和同 ID 完全相同 envelope 重放列为 E2E-03 逐来源验收；仅记录计划，不修改产品代码或运行测试。

补充修订记录：v2.98（2026-10-24）静态审阅 0042/0043 downgrade 路径，确认回退会移除 Mapping 投影重试元数据及 Review DatasetVersion pin；将 M0.1 数据保留/guard 裁决、M0.5 工期重估与 E2E-08 隔离回退验收明确化。仅更新 PRD，未执行迁移、测试或改动应用代码。

补充修订记录：v2.99（2026-10-24）逐项对照原目标架构 §14 的 10 项最终判据，补入业务问题入口、ObjectSet/DataView 与指标定义版本化、Action 补偿边界和配置式应用复用的可验证合同；计划未实现或运行验收。

补充修订记录：v3.00（2026-10-24）将六类来源按 DATA-01/02 拆成 12 个候选原子验收叶项，保留 ERP、MES、SCADA、IoT、文档和图像各自 Must-have，禁止父子双计或用 fixture 冒充 connector；分母类别待 M0.4。仅更新 PRD，未改产品代码或执行验收。

补充修订记录：v3.01（2026-10-24）将 ONT-01/02/03/04 与候选 ONT-05 拆成 15 个本体定义/治理验收叶项，并在功能目录 §3.4 与 E2E-01 明确它们和 Action Runtime、数据流、时序分析的边界；M0.4 范围、权重和参照类别仍待签收。仅更新规划，未改产品代码或执行验收。

补充修订记录：v3.02（2026-10-24）将 ACT-01/02 拆成 11 个候选 Runtime 叶项，并与 E2E-05 对齐，分别规定统一入口、typed request、权限/条件复核、Preview/Approval/Commit、冲突、投影恢复、补偿和审计证据；仍属 M0.4 待签收计划。未修改产品代码或运行验收。

补充修订记录：v3.03（2026-10-24）将 AGT-01–04 拆成 13 个 Agent Definition/持久 Session/Manager/Eval 候选项，将 GOV-01–06 与 OPS-01/02 拆成 26 个资源身份/策略/血缘/可观察性/调度/队列与负载分配候选项，并链接 E2E-06/07/08。AGT-05 与 GOV-06 范围待 M0.4 裁决；仅更新规划，未改产品代码或运行验收。

补充修订记录：v3.04（2026-10-24）将 DATA-03/04、LLM-01、SCN-01/02 拆成 30 个候选验收叶项，并与功能目录 §3.8、E2E-01—04、路线图 M0.4 对齐；目前已拆分 107 项仍是待裁决候选，另有 17 个父项尚未形成独立叶项，不能作为 Palantir 正式覆盖分母。仅更新规划，未改产品代码或执行验收。

补充修订记录：v3.05（2026-10-24）将 ACT-04/05、条件性 AGT-05、ANA-01、APP-01、DEV-01/03、FUNC-01 与 OBJ-01—04 拆成 63 个候选验收叶项，并提出 5 个父项的本期外排除边界；累计 170 项覆盖 38 个父项，分母仍待 M0.4 签署。仅更新规划，未改产品代码或运行验收。

补充修订记录：v3.06（2026-10-24）同步 M0.2 的 6 个直接页面候选/5 个组合或专属混合工作面候选；直接分母若为 6，则 ceil(0.9×6)=6，六页都须 ≥90。官方参考和项目截图未形成签收/哈希前保持未评分，不宣称相似度达标。仅更新规划。

补充修订记录：v3.07（2026-10-24）依据当前 HTTP 路由和 `apply_live_edits()` 控制流，发现 Scenario `:merge` 是独立 live 写入入口、缺少 request key，且可捕获异常的 graph restore 不能覆盖硬进程退出；将其与 Action `:revert` 补入 M4.1/M4.3/E2E-05，并明确 Wood Study 更新仅写隔离 Scenario、Agent 仅提案。仅更新计划，未修改产品代码或执行测试/故障注入。

补充修订记录：v3.08（2026-10-24）全仓搜索 Agent HTTP 测试与当前 Session/旧 AgentRun 路由，确认挂载 HTTP 证据目前仅覆盖 Session 创建；participant grant/revoke、Manager 控制和旧 API 处置仍缺。将 route-level owner/participant/revoked 矩阵纳入 FR-06、M7.2 和 E2E-06。未运行测试或模型。
补充修订记录：v3.09（2026-10-24）逐路由追踪旧 AgentRun API 的持久化效果与 `AgentDecisionTab` 调用，确认 `/confirm` 直接创建已确认 DecisionRecord 而不经过 Action Runtime；细化旧 UI 迁移顺序、历史兼容及 E2E-06 零旁路断言。仅更新 PRD，未运行测试、HTTP 请求或模型。

补充修订记录：v3.10（2026-10-24）只读盘点现存性能测试与历史 JSON 基准，纠正“没有性能测试/基准”的概括；区分组件微基准、串行单机历史基线与尚未核实的统一并发端到端容量 harness，并将历史结果复用条件、M8 缺项和禁止外推生产 SLO 的边界写入性能基准合同。未运行测试或压测。

补充修订记录：v3.11（2026-10-24）重核 M0.3 时序与制造来源候选文件，补充 MES/BTS/C-MAPSS 的大小、SHA 和许可核验边界，并将 `agent_gold`、SCADA tag/alarm 和 IoT envelope 资产缺口纳入 M0.3 及 E2E 准入清单；仅核对本地路径与 manifest 元数据，未做外部许可、内容质量或映射验收。

补充修订记录：v3.12（2026-10-24 05:08 UTC）只读复核本地服务与既有 Wood Study：/health、/ontologies、catalog/detail 为 HTTP 200，Baseline、Supplier B、Supplier C 的 graph GET 均为 HTTP 500。将服务可达和木材演示可用分开记录；未调用 bootstrap/Action/Scenario run，未修改产品代码或执行测试。

补充修订记录：v3.13（2026-10-24）静态审阅当前 13 个变更测试文件：唯一的 TestClient 用户流程是 Agent Session 创建；Mapping approval/status 测试直接调用 router function，Action/Session/投影多为 service/task 级替身。确认人工 projection reconcile 无 reason 输入并重置共享 attempts 计数，8→1 测试断言与 M4.3 不可覆盖重试账本要求冲突；将 route-level/真实队列 Gate 和测试断言修正写入 M0.1/M4/E2E-05。未运行测试或故障注入。

补充修订记录：v3.14（2026-10-24）校正 Wood Study 视觉签收顺序：M0.2 冻结参照/历史图检索/采集合同；当前 500/无限 loading 只作诊断；M5 修复后须先通过版本对齐隔离环境的 3/3 graph GET、ScenarioContext、count/digest 校验，再采集健康 12 图。无历史图时仅称重建新基线，不得宣称历史像素恢复。仅更新 PRD，未改代码或运行验收。

补充修订记录：v3.15（2026-10-24）记录 Agent 公共 turn 路由先持久化后 Celery 执行、任务 claim/上下文与权限重验、Beat 指数退避补投和 queued UI；12 条 Agent Session 生命周期/HTTP/Celery 定向用例通过，含隔离 SQLite worker task 执行；backend/worker/Beat 重载且新任务注册。另以只读 HTTP 与页面检查复核木材 Baseline/B/C 图为 200 且 context token 匹配。Agent 管理/参与者授权、恢复与整体 E2E/视觉 Gate 仍开放；M0.1 当前工作树计数须从历史 87 项台账补录为 88 项。未做真实模型调用或生产容量验收。

补充修订记录：v3.16（2026-10-24）将 `backend/app/tasks/v2/agent_sessions.py` 补入 M0.1 路径台账，当前 Git 快照为 59 个已跟踪修改、29 个未跟踪文件，共 88 项；本机 `/health` 当前返回 HTTP 200（约 0.05 秒）。这仅表示开发服务可响应，不代表有后台任务正在处理、真实模型已调用或生产容量已验证。全目标仍按 M0.1–M0.5 Gate 与 M1–M8 计划推进；M1–M8 的 15–22 周是串行初估，M0 基线另初估 3–5 个工作日，须在范围、资源和并行关系冻结后由 M0.5 重估。

补充修订记录：v3.17（2026-10-24）将 Scenario Workbench Action 的输入、预览、权限和请求 digest 放进 Scenario revision 的同一 SQL commit，并在写入时锁定 Scenario；相同 key/payload 重放已提交 revision，冲突复用拒绝。两文件通过 Python 编译及 diff whitespace 检查；backend 重启后 `/health` 返回 200，但未运行真实 Action HTTP 并发或跨存储故障注入。该切片收窄了同一入口的 response-loss/并发重复提交窗口，不代表 Live、Scenario、DecisionCase、木材专用和兼容 Action 状态机已统一，也不关闭 E2E-05。

补充修订记录：v3.18（2026-10-24）为 Agent Session 超过 90 秒且仍标记 running 的单个请求增加用户触发的最多一次重投；仅在输入上下文、权限摘要、本体修订、工具版本和本地 Prompt digest 仍匹配时复用原 AgentRun 入队，并通过 Session version 与 worker task ID 阻止迟到 worker 提交旧结果或改写新尝试状态；其余情况维持 interrupted。Python 编译、前端生产构建和 `git diff --check` 通过；改动后重载 backend/worker，健康检查为 200、Agent execute/reconcile task 已注册、active/reserved 为空，Wood Study catalog GET 为 200。未触发真实恢复、模型、测试或数据库写入。此项不代替带心跳 lease、自动 checkpoint 恢复、participant ACL、完整 E2E-06，也不关闭 M7。


补充修订记录：v3.19（2026-10-24）PipelineBuilder、PipelineList 与 TransformsTab 切换到持久异步 `/run` 并按 Run ID 观察服务端状态；Builder 移除假进度、保存失败时阻止启动、恢复最新历史 Run，Builder/TransformsTab 可恢复最近活动 Run。当前工作树更新为 63 个已跟踪修改与 29 个未跟踪文件，共 92 项；Pipeline 前端四文件增量的生产构建通过，`/health` 为 200。未发起真实 PipelineRun、未运行测试或写入业务数据。Pipeline definition/source version pin、刷新恢复全覆盖、same-key 幂等、统一父子 Run 状态和资源级权限仍属待做；本记录不等于 Pipeline E2E 或 M2 验收通过。


补充修订记录：v3.20（2026-10-24）记录 Pipeline 发布与执行快照增量：同事务保存完整 PipelineVersion execution snapshot 和定义/快照 digest；manual/sync/incremental Run 固定 Pipeline 与来源 DatasetVersion ID/checksum，worker 校验 manifest、Run 行模式/版本并只读取 pin 版本；Curated 输出保留 Run/source lineage，列表刷新可恢复活动 Run。`py_compile`、前端生产构建（0.8 秒）、`git diff --check` 通过；当前宿主缺少 `psycopg2`，模块导入未验证。没有运行测试、迁移、实际 PipelineRun、服务重载或业务写入，因此业务产物为 0。新增 0047 migration 未作隔离 DB 验证；legacy published snapshot 不能重建历史版本，可靠 outbox/same-key 幂等、统一 WorkflowRun、真实节点进度、多来源输出原子性、资源级授权及 E2E 仍属未完成项。


补充修订记录：v3.21（2026-10-24）记录 Pipeline Run 幂等派发与恢复：前端为不确定请求复用短期 Idempotency-Key，后端按 Pipeline/actor 唯一记账并用 fingerprint 检查 key 冲突；PipelineRun 同一持久行记录 dispatch intent、attempt、lease、下次重试和 task ID，Celery Beat 每分钟回收 pending/超时派发，worker 行锁防止重复消息并行执行。Celery include/Beat 已加入 Pipeline worker/reconcile。`py_compile`、前端 build（0.8 秒）、`git diff --check` 通过；没有运行测试、迁移、Celery reload、实际 Run 或业务写入；新增 0048 migration 未作隔离 DB 验证。运行中 worker 硬崩溃后的恢复、输出幂等、统一 WorkflowRun、多来源原子输出、资源授权及 E2E 仍未完成。

补充修订记录：v3.22（2026-10-24）补入 Pipeline Builder 阶段进度合同：Run manifest 绑定 connector node 与 DatasetVersion；worker 应持久化来源读取、转换与 Curated 输出阶段状态及失败原因，未独立执行的节点显式标记 skipped，UI 刷新后从 Run stats 恢复。当前实现将同类 transform 节点合并为一个 runtime 阶段；完整 DAG 节点/边独立调度、心跳与崩溃恢复继续列为开放项。静态编译、前端 TypeScript/生产构建和 diff 检查通过；未执行测试、迁移、真实 Run 或业务写入。


补充修订记录：v3.23（2026-10-24）记录 Scenario `:merge` 的请求幂等增量：新增必填 request key、actor/ontology/Scenario/revision/preview digest 绑定的 request digest、先持久化再执行的 merge audit intent、完成结果同键回放，以及 pending/reconciliation 状态拒绝盲目重放；前端按 Scenario/preview digest 保留 sessionStorage key。新增 0049 部分唯一索引迁移并设置重复键升级预检和有记录时拒绝 downgrade。Python 语法编译、前端 TypeScript/生产构建与 `git diff --check` 通过；chunk size warning 仍存在。未执行测试、migration、服务重载、HTTP merge 或业务写入，业务产物为 0。图与 SQL 的真正原子性、进程退出后的自动/人工协调恢复、operator UI 和 E2E-05 仍未完成。


补充修订记录：v3.24（2026-10-24）为 Scenario Action `/run` 增加持久幂等范围及 ActionRuntime same-key 查询；Run 在提交前验证 ETag、preview digest 与 request key；同键完成请求返回原 revision，异 payload 冲突，未知状态不自动重放。`ScenarioService.revision()` 将 ActionRun completed 状态/摘要与 ChangeSet、ScenarioRevision、head、audit 放入同一 PostgreSQL commit。新增 0050 scoped request-key 唯一索引迁移。Python 语法编译和 diff 检查通过；未运行测试、migration、HTTP Action、服务重载或业务写入。该项只解决 Scenario Action 的 SQL 状态一致性，不统一 Live/DecisionCase/Agent/其他入口，也未解决 FalkorDB 与 PostgreSQL 原子性、自动恢复或 E2E-05。

补充修订记录：v3.25（2026-10-24）集中 ActionRun 的 queued/running/completed/failed/cancelled/reconciliation_required/reverted 状态转换，并由 Scenario Action `/run`、DecisionCase execute/revert、兼容 Live Action 与 Live `:revert` 复用。此为局部状态转换统一，不代表统一 ActionRequest、Preview/Review/Commit/Audit、资源授权、幂等范围、跨存储恢复或 Agent 写入闭环。Python 语法编译与 `git diff --check` 通过；未运行测试、迁移、HTTP 请求或业务写入。

补充修订记录：v3.26（2026-10-24）为 `ActionRun` 增加 nullable `requested_by`/`reviewed_by`，并用共享 helper 将 requester/reviewer/executor 赋值集中管理；DecisionCase、Scenario/兼容 Live Action 与补偿 Run 记录对应 actor。DecisionCase 与 ActionRun list API 暴露 actor 字段，DecisionCase UI 显示三个角色。新增 0051 migration 添加可空 `users.id` 外键，不回填历史未知值；只要已有归因事实就拒绝 downgrade。当前修改通过 Python 语法编译、前端 TypeScript/生产构建（0.86 秒，存在大 chunk 提示）与 `git diff --check`；未执行测试、migration、HTTP Action 或业务写入。system/worker 与 manual operator 身份及不可覆盖尝试账本仍未完成。
补充修订记录：v3.27（2026-10-24）修正 DecisionCase 图投影人工 reconcile：要求 reason 与 `client_request_id`，同 actor/run/key 的同理由重放原 attempt、异理由冲突；自动计数不再被人工重试归零，独立 `projection_manual_attempts` 计数并增加 0052 migration。DecisionCaseEvent 追加 attempt start、finish 和 `outcome_unknown` 记录；system worker 的 actor_id 留空且 payload 标识 system。该切片仅覆盖 DecisionCase projection，不能视为唯一 Action Runtime 或全局 attempt ledger 已完成。
补充修订记录：v3.28（2026-10-24）记录候选切片的验证边界：Python 语法编译、前端生产构建（807ms）及 `git diff --check` 通过，Vite 有一个超过 500KB 的 chunk 提示；测试、migration、HTTP reconcile 和图写入均未运行。当前工作树路径台账已复核为 109/109。

补充修订记录：v3.29（2026-10-24）将组件 Spike 页的 ECharts 完整导入替换为 core 加 LineChart/Grid/Legend/Tooltip/CanvasRenderer 显式注册；构建产物中 ComponentSpikesPage chunk 从 596.08KB 降至 24.03KB，生产构建 794ms 通过且不再提示 >500KB chunk。该变化证明独立 chunk 缩小，不证明该路由总下载量、设备上的首屏时间或图表运行时性能已通过验收。当前工作树 110 条路径与处置台账 110/110 对齐；未运行测试或业务写入。


补充修订记录：v3.30（2026-10-24）Scenario Merge 增加人工协调恢复切片：恢复路径校验原始 pending/reconciliation audit、不可变 revision/changeset、content/permission/edit digest，只执行原批准 edits；操作员提交理由与独立幂等 key，SQL 对象行与 FalkorDB 节点/关系按最终目标态收敛，成功后同时记录原 merge 与 recovery audit 完成状态。原操作者或管理员可调用，PostgreSQL advisory lock 串行化同一原请求；Workbench 将原请求 ID 保存在 localStorage，刷新后显示恢复表单。0053 migration 增加 `merge_reconcile` 请求部分唯一索引。`py_compile` 和前端生产构建通过（806ms，无 >500KB chunk 提示）；未运行测试、migration、HTTP API 或业务写入。数据库仍在先前核实的 0046 head，本轮代码未热加载；该切片不是统一 Action Runtime，也未通过 E2E-05。

补充修订记录：v3.31（2026-10-24）为 Mapping Row 投影补入自动预算 8 次、dead-letter、15 分钟租约及租约过期 `outcome_unknown` 收敛；按对象隔离写入与重试预算，新增不可变 start/finish 事件和 actor/reason/request id；管理员对 Review/Run 范围内故障对象人工重放，独立计数并实现同键同理由回放/异理由冲突。Curated UI 展示 SQL Apply 与图投影分态、预算和尝试历史。Python 语法编译与前端 TypeScript/生产构建（792ms）通过；`git diff --check` 通过，工作树路径台账 112/112 对齐。新增 0054 migration 仅写入源码，未执行；未运行测试、服务重载、HTTP/图投影故障注入、实际 Apply 或数据库业务写入。该切片仍缺隔离数据库升级验证、真实 worker/故障路径和 E2E-02 证据，不构成 M2/M8 或生产 Gate 通过。


补充修订记录：v3.32（2026-10-24）修正 Mapping Apply Run 的 dispatch/execution budget 恢复缺口：pending/retry_wait 派发 claim 受 20 次派发与 8 次执行上限约束；queued/dispatching/running 过期租约恢复保留计数，超过任一预算写入可见 `failed` 终态和明确错误码；running 租约过期写入 `execution_outcome_unknown`，仅在预算有余量时退避重试；worker claim 原子校验执行预算，最终 broker 发布失败记录预算耗尽原因。Python 单文件语法编译通过，未运行测试、迁移、服务重载、HTTP Mapping Apply 或业务写入；当前容器健康状态与空 Celery 队列不证明本次源码已加载。状态机竞态、运行中硬崩溃后的副作用对账和 E2E-02 仍未验证，不构成 M2/M8 或生产 Gate 通过。

补充修订记录：v3.33（2026-10-24）实施 Construction/Mapping 来源数据版本固定源码切片：ConstructionDraft 保存 DatasetVersion ID；MappingTask 入队 manifest 绑定 dataset/version/checksum/schema snapshot/digest、选择和处理输入，M3 来源分析与内容打包共用同一版本，worker 在开始及写回前校验输入 digest；queued/running task 期间拒绝修改草案输入/目标；Build 与普通/多模态 ConstructionRun worker 使用精确 DatasetVersion 并校验 checksum/schema，任务卡显示 pin 和输入摘要。新增 0055 migration 仅写入源码、未执行。后端相关模块 `py_compile` 通过、前端 TypeScript/production build 通过（831ms）、`git diff --check` 通过；未运行测试、任务、API、服务重载或业务写入，无新增业务产物。ModelConfig/Prompt/Tool/parser/normalizer/provider 制品版本、append-only retry attempt、任务历史 UI、隔离迁移和版本漂移 E2E 仍待完成；不计 M2.2/E2E-01 验收。

补充修订记录：v3.34（2026-10-24）在上条输入 pin 的基础上，把预检所选 ModelConfig ID、数据库配置摘要、凭据密文单向指纹和 LiteLLM 路由摘要纳入 MappingTask manifest；任务执行按配置 ID 解析并重算摘要，配置缺失/变化时 fail closed；预检到排队之间配置变化会拒绝提交。模型调用记录增加 attempt/config digest，MappingTask GET 仅返回最近 20 条不含请求/响应密文的摘要，常规/多模态页面展示最近 4 条。retry 不再清空旧 trace；输入或配置摘要变化时拒绝在原 task 上盲重试，已完成 task 不可重跑。配置/路由 digest 不表示 provider/model 制品可复现，仍标为 unverified；没有独立 MappingAttempt ledger，私密规则模式也没有 ModelInvocation。`py_compile`、前端 TypeScript/生产构建、`git diff --check` 通过；未运行测试、调用模型、任务、API、迁移、服务重载或业务写入；无业务产物。Prompt/Tool/parser/normalizer 与 provider 制品版本、独立 attempt ledger、资源级 GET 授权和 E2E-01 仍待完成。

补充修订记录：v3.35（2026-10-24）实现 Agent Session 的 owner-issued viewer grant/revoke 源码与 UI：只有 ontology 访问和当前策略范围与 owner 等价的已存在账户可读；每次读取复核 participant membership、scope、会话 owner pin 和历史 Run 的权限/上下文 digest；撤权立即阻断后续读取，Session 写操作保留 owner-only。新增 0056 participant migration，尚未执行；没有 HTTP 角色矩阵、测试或 E2E-06 证据。后端语法编译与前端生产构建通过（804ms），未重载服务、未产生业务数据。该切片不关闭 M7.2、Agent Manager、definition/tool versioning、durable recovery 或完整平台治理 Gate。

补充修订记录：v3.36（2026-10-24）为标准 Live Scenario Action、DecisionCase、Scenario Workbench 与 Scenario duplicate compiler adapter 引入同一 typed `ActionRequest` / `ActionInvocationRequest`；请求 envelope 绑定调用顺序、参数、目标、执行上下文、Action Type 版本、ETag 和请求编号。Scenario Workbench Preview digest 加入 typed manifest，避免 Action Type 版本更新后沿用旧预览；完成后的 manifest 进入 ChangeSet runtime report。五个后端文件 `py_compile` 与 `git diff --check` 通过；未运行测试、HTTP、迁移、业务写入或服务重载。木材专用 compiler、pre-Project helper、Agent 旧 confirm 写入、统一审批/执行/跨存储恢复与 M4/E2E-05 仍未完成。

补充修订记录：v3.37（2026-10-24）将 Scenario Workbench `submit_actions` 接入共享 `OntologyActionRun` 创建/完成合同；Run 绑定 actor、scenario scope、typed invocation、request/preview/permission digest 与 ETag，并与 ChangeSet/ScenarioRevision/head/audit 同一 SQL transaction 提交。same-key 请求返回原 revision/Run，旧 ChangeSet 保持兼容重放。新增 0057 将 `client_request_id` 从 120 扩至 200 字符，downgrade 检查是否存在长键并拒绝截断。三文件 `py_compile` 与 `git diff --check` 通过；未运行测试、migration、HTTP、服务重载或业务写入。该切片不代表统一 Action Runtime 或 E2E-05 通过。

补充修订记录：v3.38（2026-10-24）将木材 Supplier Study 专用 `compile_supplier_action` 接入共享 typed `ActionRequest` 与权限编译边界，manifest 固定 scenario/case/study/source/time/scope 上下文、case definition revision、LogicAsset model version、case ETag 和请求编号。ActionRun、Case 参数/定义修订、Scenario ChangeSet/Revision/head/audit 同一 SQL transaction 提交；相同 request key 重放原 Run/Revision，异 payload 冲突。Run 记录 before/after view digest。当前标记 `compile_on_submit` / `single_step_submit`，v3.40 增加独立只读预览与确认提交源码，提交要求当前重编译 digest 与预览匹配。五个后端模块 `py_compile` 和 `git diff --check` 通过；未运行测试、迁移、HTTP、服务重载或业务写入；PostgreSQL/FalkorDB 窗口恢复及 E2E-05 未验证。

补充修订记录：v3.39（2026-10-24）将 Supplier Study 的可变运行上下文固定进 typed ActionRequest：Study ETag、model/source manifest、scenario time/scope/smoothing/projection digest 都进入请求 manifest。编译后以 Study→Scenario→Case 顺序持有行锁并重核 study context，防止设置更新与 Action 编译并发时提交陈旧参数；漂移返回 409 且不创建完成态 ActionRun 或修改 Case/Scenario。五个 Python 模块 `py_compile` 与 `git diff --check` 通过；未运行测试、migration、HTTP、服务重载或业务写入。SQLite 行锁并发与跨存储恢复仍待 E2E-05 验证。


补充修订记录：v3.40（2026-10-24）为 Supplier Study Action 增加两阶段 Preview→Confirm：预览调用只读服务端编译和授权，不写 Scenario/ActionRun；服务端返回绑定输入、compiled edits、权限摘要和运行上下文的 digest 及可读变更计数。确认提交重新编译并要求 digest 匹配，漂移时 409；前端编辑输入后失效预览，确认复用同一 request ID。页面沿用现有编辑区域，未改关系图布局。Python 语法编译与 git diff --check 通过。随后仅重启 backend 容器加载路由，health 200/OpenAPI 路由可见；Supplier B 只读预览返回 valid，影响 2 种木材、停用 2 项、创建 3 个对象与 6 条关系，共 11 项变更。演示页加载并显示 292 对象/313 关系；未在 UI 点击预览/确认，未运行测试/构建/migration，也未提交业务 Action。E2E-05 仍未验收。


补充修订记录：v3.41（2026-10-24）将 Ontology revision 快照一致性增量落入代码：不再把指向软删除/缺失类型的关系或无主活动实例作为有效快照内容静默计数；完整性摘要保留排除总数和有限样例，Restore 在任何破坏性 DML 前拒绝 degraded/结构无效快照。当前只读数据库抽样 clean；Python 语法编译和 diff 检查通过。未运行 Restore/API/测试或 migration；完整 revision snapshot/restore E2E、旧快照处理和跨存储发布仍开放。

补充修订记录：v3.42（2026-10-24）为 Ontology Revision Restore 增加只读预览和并发保护：预览摘要绑定目标快照、当前修订 ID 与 live snapshot digest，并按 live 状态显示 schema 差异和会阻止类型删除的实例依赖；确认请求必须显式提交预览时的当前修订 ID 与摘要，服务端在本体行锁下重算，旧预览/并发改动返回冲突。版本页与修改历史抽屉均先预览再确认，失败明确显示错误。两个后端模块 `py_compile` 与全工作树 `git diff --check` 通过；未运行前端构建、UI/API 交互、Restore、测试、migration 或业务写入，整体 M2.3/E2E-01 仍开放。

补充修订记录：v3.43（2026-10-24）为 AgentRun 增加 typed worker owner、递增 lease generation、heartbeat/expiry、cancel request/ack 与过期索引；worker claim 后通过独立数据库 session 每 20 秒续租，结束写回核对 owner/generation/有效租约；运行中取消持久进入 `cancelling`，worker 在推理前、结果提交前或异常路径确认；Beat reconciler 对过期 lease 按 pinned context/permission/tool 规则自动恢复一次、收敛取消或标记 interrupted。Agent Sessions 页面显示取消中状态。新增 0058 migration 未执行；backend/worker 未重载；只运行 Python `py_compile` 与 `git diff --check`，未运行测试、构建、API、migration、worker crash/concurrency 或业务 Run，业务产物为 0。该源码切片不关闭 M7.3、FR-06 或 E2E-06；中间 tool attempt 恢复、完整 Agent Definition/Manager 和事件脱敏/保留仍开放。

补充修订记录：v3.44（2026-10-24）为 Mapping 写入加入 DatasetVersion 全量快照读取合同：checksum 必须存在且匹配、完整读至 EOF、行对象校验与 rowcount 对账；Mapping Apply、`build_all`、link mapping 和 legacy dataset apply 统一读取固定版本并返回来源 manifest digest；新 CSV/XLSX/JSON 上传记录准确行数，解析失败不创建 Dataset。历史版本缺失 checksum 或 rowcount 错配时安全拒绝，需修复/重新导入。Python 语法编译与 `git diff --check` 通过；未运行测试、10,001 行完整性场景、服务/worker 重载、API、迁移或业务写入，业务产物为 0。当前 84 个 tracked modified + 42 个 untracked = 126 paths；E2E-02、旧版本修复策略及全量读取资源预算仍开放，不代表 M2 通过。

补充修订记录：v3.45（2026-10-24）为 Curated Dataset/Review 路由及旧 `/mappings/*`、`/link-mappings` 路由加入 dataset/ontology scope 检查与 editor-role 守卫；请求审核记录 `requested_by`，批准/拒绝记录 `decided_by`，只允许 pending review 编辑或决策。Review GET/latest/mapping-run 路径复核当前数据集与参与主体；Curated 页面能够续接最近 pending review，并展示 requester/decider 与固定 DatasetVersion。新增 0059 nullable actor-FK migration，保留旧 `reviewer_id`，已存在主体事实时 downgrade fail closed。相关 Python `py_compile` 和 `git diff --check` 通过；未运行测试、前端 build、migration、backend reload、HTTP/浏览器矩阵或业务审核，业务产物为 0。未映射 v2 Dataset 目前 admin-only；`CuratedRowEdit` 尚无 per-edit actor；直接 Apply/build-all 尚未全部要求 approved Review/持久 Run。该源码切片不关闭 M0.1、M2、平台治理或 E2E-02。

补充修订记录：v3.46（2026-10-24）映射 API scope 测试发现 `mappings` router 自建数据库依赖绕过共享 dependency override；已切换为 `app.deps.get_db`，并将两个映射任务测试改为断言 checksum-verified complete snapshot 和 source manifest。相关映射/Apply 测试三文件合计 41 passed、19 warnings（22.70 秒）；这不是全量后端测试。一次性 PostgreSQL 空库完成 0001→0059 migration chain，确认 CuratedReview actor 字段存在且有 actor 事实时 0059 downgrade 按设计拒绝；随后销毁临时库。没有迁移当前 Wood 演示数据库、运行前端 build、重启服务或写业务 Action/审核。当前 backend runtime 未证明载入本次最新改动；M0.1、M2、治理与 E2E 门仍需独立验收。


补充修订记录：v3.47（2026-10-24）为 `CuratedRowEdit` 增加 `edited_by`，提供受 Review ACL 保护的编辑历史 API 和最近修改 UI；两个 Curated 测试文件 18 passed，前端 production build 通过，Python 编译与 `git diff --check` 通过。disposable PostgreSQL 空库 0001→0060 升级成功，0060 在存在编辑者事实时拒绝 downgrade。检查运行页时发现两个已审核结构数据集的源对象在 MinIO 缺失，现 UI 呈现明确错误/恢复提示；未回写、重导或改变 Wood 演示数据。当前 backend 未 reload、运行数据库未迁移；API/权限矩阵、E2E 和 M0.1/M2/平台治理 Gate 均开放。

补充修订记录：v3.48（2026-10-24）新增 `OntologyActionRunEvent` 追加式生命周期账本：创建与状态转换事件记录 actor、request/trace ID 和输入/预览/权限摘要，按 Run 形成 SHA-256 链；ORM 与 PostgreSQL/SQLite trigger 阻止 UPDATE/DELETE，删除 ActionRun 不会级联删除事件。新增受本体/Run ACL 保护且限量的 `GET /{ontology_id}/action-runs/{run_id}/events`；即使响应截断，服务端也流式验证完整链，旧 Run 无事件会返回 history unavailable。Action/Scenario 定向集合 27 passed、12 warnings；`python3 -m py_compile`、`git diff --check` 和前端 production build（816ms）通过；一次性 PostgreSQL 空库 0001→0061 升级成功，数据库 UPDATE/DELETE 防护与有事件时拒绝 downgrade 均通过，测试库已删除。只读核对发现 `ontoprompt` 原本已在 0061，事件表 0 行、现存 1 条 completed ActionRun；本轮未迁移 DB。确认 Celery 无 active/reserved 后仅重启 backend，health 200、OpenAPI 新路由可见；ObjectActionRunner 弹窗已显示历史记录，浏览器验证了 legacy Run 兼容提示。未提交业务 Action；自动跨存储恢复与 E2E-05 仍未验收。

补充修订记录：v3.49（2026-10-24）统一 ActionRun 列表与生命周期事件读取的主体判定，管理员以及有明确 requester/reviewer/executor 归因的参与者可查看；仅具本体访问权的非参与者看不到其他主体的 Run。本次隔离 SQLite/TestClient 角色矩阵覆盖 admin/requester/reviewer/executor/非参与者和一个对象级 deny 目标；显式被拒绝的 requester 看不到目标 Run，列表为空且事件接口为 404；可访问目标的参与者读取成功。1 passed、3 deselected、31 warnings；测试不运行 app lifespan。线上 backend reload、生产策略数据和完整 M4/E2E-05 仍待验收。

补充修订记录：v3.50（2026-10-24）新增 Agent Session→DecisionCase→ActionRun 隔离 SQLite 贯通验收：编辑者从持久会话提出 Action，独立管理员完成只读预览、审核和 Action Runtime 写回，run 保留 requester/reviewer/executor 身份，ActionRun 追加事件分别记录提案人和执行人，Agent resume 将等待审核会话恢复为空闲。测试 1 passed、12 deselected、13 warnings（0.92 秒）；图投影同步在测试内替换为空实现，因此 SQL/服务链路通过不代表 FalkorDB 投影、HTTP/API、UI、迁移或崩溃恢复已验收。frontend `/ontologies` 与 backend `/health`、`/openapi.json` 当前 HTTP 200。当前服务可访问；完整 M4/E2E-05、Agent E2E-06 与平台级治理仍开放。


补充修订记录：v3.52（2026-10-24）Agent Session→DecisionCase→ActionRun 服务闭环已有一个隔离 SQLite 用例通过；新增持久会话 Action 提议表单、当前对象/证据上下文约束、属性类型参数控件、只读差异预览，以及提交、独立管理员审核和批准后执行状态 UI。前端 TypeScript/Vite production build 通过（2830 modules，1.30s），本地 Agent 页面已显示持久会话入口；当前本体无 Agent Session，尚未在 UI 中执行真实提议、审核或写回。未创建业务 Session/Case/Run、未迁移或重载 backend。该入口仅支持无外部副作用的本地属性 Action，不代表平台级 Action Runtime 已全面统一；投影、权限矩阵、E2E-05/06 和治理 Gate 仍开放。

补充修订记录：v3.53（2026-10-24）公共 queued local Agent turn 增加本机 Ollama exact tag/artifact digest pin、执行前 ModelConfig 指纹与 artifact digest 复核、漂移时 fail closed，以及 Run/Event/Checkpoint/UI 的凭据外模型血缘；本机 HTTP 请求不再继承环境代理。`py_compile`、前端构建（2830 modules，1.23s）、`git diff --check`、backend health、前端路由 HTTP 200、Celery ping 与 Agent execute/reconcile task 注册通过。未运行测试、真实模型推理、业务写入或 migration。Ollama 不提供可验证 digest 时显式 `unverified`；不可变 ModelConfig revision、完整模型复现、共享 Session/Worker 崩溃恢复和 E2E-06 仍未验收，M0/整体目标继续开放。

补充修订记录：v3.54（2026-10-24）发现 SupplierStudyPage 当前未提交差异把稳定 deterministic `preset` 图布局改为随机初态的 `cose`，使固定木材演示重复打开时位置变化；现将该图布局恢复为 `HEAD` 基线的稳定节点/边排序与固定坐标，同时保留同页受治理 Action 预览和提交流程。前端构建通过（2830 modules，1.09s）；直接访问既有 study 的只读 catalog/study/graph 路径并目视确认页面和图均已加载，显示 292 对象、313 关系及 Baseline/B/C 三列已完成指标。未调用 `/bootstrap`、未运行仿真、未产生业务写入。此证据只证明当前页面回到分支提交基线布局；因缺少早期视觉截图/hash，不能宣称历史像素级复原。M0.2 仍须由用户/业务负责人签收演示基线，Palantir 页面/功能相似度和整体目标仍未验收。

补充修订记录：v3.55（2026-10-24）重新核对真实 Agent 模型运行状态：`/api/v1/models/local/probe` 返回本地 Ollama 配置槽存在，但 `reachable=false, installed=false`；本机有 Ollama CLI，但没有运行服务进程，`qwen3.5:0.8b` 未安装。PRD/M0.5 的旧说法已同步为：排队 Agent 已 pin 可得的本机 Ollama artifact digest 并记录模型血缘，但真实本地推理还不能运行；下一步要明确 Docker-to-host 安全连通方式、模型安装/资源预算，随后以实际推理和 E2E-06 验收。未启动 Ollama、暴露网络端口、下载模型、运行推理或调用付费服务；这一状态是当前环境快照，不是永久配置结论。

补充修订记录：v3.56（2026-10-24）M0.1 检查运行态 Alembic：数据库 `current=head=0061`、单 head、`/health` 为 200，但 `alembic check` 退出 255，列出 28 项 schema 差异（26 项 index 命名/ORM 声明差异，2 项 ActionRun event FK 差异）；数据库 Inspector 确认该 FK 为 `ON DELETE CASCADE`，当前 ORM 与 0061 migration 源码未声明级联。Pipeline 的两个已迁移索引存在于 DB 但未声明于 ORM。源码同时确认启动迁移异常会在所有环境回退 `Base.metadata.create_all()` 后继续，而 health 仍报告 liveness 状态，不等同 schema readiness。以上让“服务运行正常”与“迁移/schema 已一致”明确分开。未执行 migration、测试、服务重启或业务写入。下一步须先裁定索引/FK 目标 DDL，再增加可追溯修复 migration 和 readiness/fail-closed Gate，并用 disposable DB 重放验证；M0.1 与整体目标仍开放。

补充修订记录：v3.57（2026-10-24）依据 Git 历史将 Wood Study GraphCanvas 从固定坐标 preset 恢复为 `dda5143` 与展示分支 `a57818a` 的 Cose 力导向参数，同时保留 Action Preview/Commit 改动。前端 production build 通过（2830 modules，826ms），当前页面可读既有研究和 Baseline/B/C 结果；本轮没有历史截图或 golden image，不能据此宣称像素级复原，视觉基线仍由 M0.2 签收。未运行测试、Scenario/Action 或写业务数据。

补充修订记录：v3.58（2026-10-24）将 Agent/DecisionCase/ActionRun ORM 索引名与历史 migration 对齐，补全 PipelineRun 已有索引声明；在 DB 仍为 0061 时只读 `alembic check` 从 28 项差异降至 2 项 FK 操作。新增 0062 前向 migration 规范 ActionRun event FK 为无级联，未在演示库执行；当前 DB 仍为 0061，而代码 migration head 为 0062，故最新 Alembic check 报数据库未到 head。`py_compile`、`git diff --check` 通过；未运行测试、迁移、服务重启或业务写入。当前工作树 135 项（88 tracked + 47 untracked），M0.1 路径台账待同步，schema readiness 与整体目标仍开放。

补充修订记录：v3.59（2026-10-24）将 schema readiness 做成启动合同：development 启动执行 Alembic upgrade 后核对单 head/current 并做一次 schema drift check；production 迁移由发布步骤预先执行，API 启动只核验、不自动 upgrade/stamp；开发 legacy 无版本库须显式 opt-in baseline stamp，迁移或 drift check 失败时 production 阻断启动；development fallback 仅保留为兼容诊断，且 readiness 为 not-ready、跳过 seed/backfill。新增 `/health/live`、`/health/ready` 并让主 Compose backend healthcheck 使用 readiness。静态 Python compile、`git diff --check`、Compose config 解析通过；未重启当前服务，故运行时与浏览器端点状态未验证。路径总数已达 136（89 tracked + 47 untracked），处置台账仍待补齐；请求 middleware 阻断、迁移 replay 和 E2E-08 仍待完成。

补充修订记录：v3.60（2026-10-24）将生产 migration 迁至发布步骤，API production startup 只检查 current/head 与 ORM drift；development 保留自动 upgrade，未版本旧库 stamp 需显式 `ALLOW_LEGACY_DB_BASELINE_STAMP=true`。Compose 让前端、Celery worker 与 Beat 等 backend readiness 通过后再启动。静态 Python compile、`git diff --check`、Compose config 解析通过；未重启当前进程，端点/依赖编排运行状态未验证。运行 DB 0061、代码 head 0062，迁移未执行；请求 middleware 阻断、worker 运行期 schema gate、disposable PostgreSQL migration replay 和 E2E-08 仍待完成。当前累计工作树 136 paths（89 tracked + 47 untracked）。

补充修订记录：v3.61（2026-10-24）主 Compose 增加健康 DB gate 与独立 `migrate` 服务，在 API/worker/beat/frontend 前运行 `alembic upgrade head && alembic check`；生产 API 自身只验证已升级的 head/schema，不进行隐式迁移。Compose 配置解析通过；没有启动 migration job、应用数据库升级或重启 API，因此 0062 行为和健康依赖运行态仍待 disposable PostgreSQL/运行时验证。旧 0061 DB 与新 0062 head 的状态保持开放，E2E-08 仍未通过。

补充修订记录：v3.62（2026-10-24）将 0062 的 downgrade 明确为 no-op：revision 0061 定义的 FK 已是 `NO ACTION`，回退 revision 时应保留该合同，不重新引入数据库现有的 CASCADE drift。更新 migration SHA-256；尚未执行 migration，必须在 disposable PostgreSQL 验证升级、回退、审计事件留存及父对象删除拒绝。未重启 API 或修改演示数据。


补充修订记录：v3.63（2026-10-24）为旧 Audit/Extraction worker 加入持久 owner/generation/heartbeat/expiry、事务提交前 fencing 与过期任务可见终态；失败任务不自动重放，避免重复模型调用。Extraction 的本体 SQL 写入合并为单个事务。新增 migration 0064 与每分钟 reconciler。静态 `py_compile`、`git diff --check` 通过；未运行测试、migration、服务重载或业务任务。演示数据库仍 0063、代码 head 0064，故 migration/worker crash/E2E recovery 尚未验证，业务产物 0。


补充修订记录：v3.64（2026-10-24）本地 Compose migrate 已将演示 PostgreSQL 升至 0064 并通过 `alembic check`；重载 backend/worker/beat 后 readiness current/head 均为 0064，frontend `/ontologies` HTTP 200，worker ping 正常且注册 `legacy_tasks.reconcile`、Audit、Extraction；Beat 已实际投递 reconciler，Celery result 为 `audits=0, extractions=0`。Audit/Extraction 数据行数和状态未变，未执行业务任务/模型调用。此项是运行接线证据；lease heartbeat/fencing、worker crash、过期回收、downgrade guard 和整体 M0/M1–M8 验收仍开放。

补充修订记录：v3.65（2026-10-24）0064 租约隔离回归新增 5 项测试并在项目默认 SQLite pytest fixture 下全部通过；覆盖重复 delivery 不重复 claim、持有者 commit 续租、过期/换代 owner 无法提交业务写入、过期任务 failed/cancelled 收敛且不自动重放。为恢复普通 SQLite fixture，修正 AgentSessionParticipant.user_id 重复创建同名索引的问题。最新只读快照：PostgreSQL readiness current/head=0064、frontend `/ontologies` HTTP 200、Celery worker 在线且 active/reserved 为空。没有调用模型、运行真实任务或进行吞吐压测，因此不可据此宣称生产容量或业务生成质量已验证；真实 worker crash/PostgreSQL 并发、migration 全链/downgrade、完整 M0 与 E2E/M1–M8 仍开放。

补充修订记录：v3.66（2026-10-24）租约隔离用例扩展至 SQLite/PostgreSQL 共 8 passed。PostgreSQL 并发 claim 仅一个 delivery 成功；过期/旧 generation worker 的提交不会留下业务写入；两个 reconciler 对单个 expired lease 只转换一次。全新 PostgreSQL 16 disposable database 从 0001 全量迁移到 0064，`alembic check` 无漂移；合成 lease history 阻断 downgrade，删掉测试行后 downgrade/upgrade round trip 和 schema check 均通过。临时库容器已删除，演示数据库保持原样。该验证不覆盖真实 Celery worker crash、worker/API 整体 E2E、压力或吞吐，因此 M0.1/M8 及整体生产准备仍未完成。

补充修订记录：v3.67（2026-10-24）SQLite/PostgreSQL/Celery crash 专项 9 passed。独立 PostgreSQL/Redis 中真实 Celery worker 收到 probe delivery，持有 AuditTask lease 并提交唯一 marker；对 worker 进程注入 SIGKILL 后等待 lease expiry，运行同一 `legacy_tasks.reconcile` 任务，验证 Audit 进入 failed、owner/expiry 清理且 marker 仍只有一条。全新 PostgreSQL migration 0001→0064 与 `alembic check` 无漂移、0064 lease-history downgrade guard 和无历史往返升级仍通过。测试专用 helper 位于 tests，不加入生产 task include；临时容器已删除，演示 DB 未改。生产 broker 长期运行、prefork 多 worker 故障重投、真实模型/业务工作流和吞吐压测仍未验证，不据此宣称生产容量或整体 M8 完成。

补充修订记录：v3.68（2026-10-24）`backend/tests/v2/test_scenario_action_authorization.py` 补充同名 Relation 按源/目标对象类型消歧，以及同名同端点仍拒绝歧义的回归；专项 11 passed（24 warnings，29.19 秒）。该结果证明当前服务层解析合同，不证明挂载 HTTP/木材图已在运行服务恢复。`case_graph` 的错误映射和 Supplier Study 的图错误/重试 UI 已在源码中；后续仍需运行态版本对齐、只读浏览器复验和 M0.2 视觉基线。完整数据到本体、动态更新、What-if、Twin、Agent/治理和 M8 性能 Gate 仍开放。

补充修订记录：v3.69（2026-10-24）当前 backend 容器启动时间晚于 `action_authorization.py` 与 Supplier Study graph router 文件更新时间；健康检查为 200、schema current/head=0064。只读查看已打开的木材 Supplier B 页面显示图 292 objects/313 relations，Baseline/B/C 既有结果可见，无图错误提示；未触发任何写入。此结果将图可读状态由源码/单测提升为采样时的挂载页面观察，但没有保存截图、重跑 API HTTP 用例或覆盖预览/提交/推演等点击路径，因此不等于完整木材 E2E 或视觉验收。M0.1/M0.2 和整个目标仍未验收。


补充修订记录：v3.70（2026-10-24）补入 Mapping 行投影的 8 次自动预算/dead-letter、对象级失败隔离、人工重试独立预算与幂等、追加审计事件不可变性。5 项定向测试通过；空 PostgreSQL 全链迁移到 0065、`alembic check` 无漂移，UPDATE/DELETE 与有历史时 downgrade 均被保护。演示 DB 保持 0064；真实 API/Celery/图投影 E2E、M0.1–M0.5、Action/Agent 全链与整体目标未完成。


补充修订记录：v3.71（2026-10-24）本体详情页默认进入持久 Agent Session→DecisionCase 工作面；旧一次性决策助手保留为次级兼容入口并说明其旧 DecisionRecord 路径。旧确认 API 尚未禁写，session 工作面的浏览器/HTTP 权限矩阵仍待验收；不计为 FR-06/E2E-06 完成。


补充修订记录：v3.72（2026-10-24）为旧 Agent 决策助手增加显式 Session handoff：任务、有限候选对象 ID、结论与建议只进入草稿；草稿不自动运行或带入已授权状态，必须创建专属 Session、重新固定引用并手动发送。旧确认 API 仍保留，Agent Runtime 收敛、浏览器/HTTP ACL 与 E2E-06 未通过。

补充修订记录：v3.73（2026-10-24）关闭旧 AgentRun `confirm` 路由的新直接写入：`proposed` 运行现在返回 409 并指向持久 Session/DecisionCase/预览/独立审核/ActionRun；已有 `confirmed` 历史结果仍可读取，不迁移或删除历史 DecisionRecord。兼容助手页面移除“确认并保存”，改为“转入持久会话复核”，同时将建议标为仅供复核。没有新增业务写入；本次未运行测试或浏览器 ACL/E2E，完整 Action/Agent 治理仍未验收。

补充修订记录：v3.74（2026-10-24）将工具注册表 canonical digest 暴露于 `agent/tools`，并写入新 AgentRun、入队/开始事件及 Session checkpoint；worker 执行前和恢复判断时同时核对工具名/版本与 digest，发现显式版本漂移则失败关闭，模型或 Action 调用前停止。旧运行若无 digest，仅在完整 name/version map 与当前注册表一致时按兼容路径继续。Session UI 显示入队时工具目录摘要前缀。没有改变工具权限集合或发起模型/Action 调用；`py_compile`、前端 production build（2830 modules，825ms）与 `git diff --check` 通过；未运行测试或运行态 Agent turn。Agent Definition/Manager、跨模块 E2E 和生产 Gate 仍未完成。

补充修订记录：v3.75（2026-10-24）限制 `/search/keyword`、`/search/semantic` 和统一 `/search` 端点的 query 长度为 1–1000、`n_results` 为 1–100，并用 Literal 拒绝未知 mode；空白查询返回 422。Chroma 服务侧对内部调用再封顶至 100 条/1000 字符，阻止非 HTTP 调用绕过预算。`py_compile` 与 `git diff --check` 通过；确认 Celery active/reserved 均为空后只重启 backend，运行 OpenAPI 已展示上述参数约束，`/health` 对 DB/Neo4j/FalkorDB/MinIO/Chroma 均为 `ok`，前端 `/ontologies` 为 HTTP 200。未运行搜索测试或真实向量查询。该改动只限制负载，不把当前静态 Chroma 候选检索描述为 TemporalFact/TemporalReplay 联查；时序向量检索和 M5/E2E-03/04 仍未完成。

补充修订记录：v3.76（2026-10-24）新增 `POST /temporal-streams/{run_id}/search/semantic` 与动态演化页的时序语义检索面板。按回放隔离索引，首次同步构建最多 25,000 条事实，按 `valid_from <= at < valid_to` 检索，再由 PostgreSQL 复核时间有效性；回放在索引期间变化则以 409 要求重试，超过索引上限、Chroma 不可用或索引失败均明确返回。后端 `py_compile`、前端 production build（2830 modules，905ms）及 `git diff --check` 通过；backend 重启后 OpenAPI 确认路由已挂载，`/health` 及 Chroma/DB/Neo4j/FalkorDB/MinIO 状态为 ok，frontend `/ontologies` HTTP 200。未运行语义查询/测试，也未做动态面板浏览器验证。只读查看本地 FactoryNet 回放 `278dc86b`：6,383 条事实、1,055 个 Observation 主体中 209 个可由 `episode_id:source_row_id` 对上 EntityInstance 行身份；没有直接 Entity UUID 全量映射。该结果支持事实级候选检索，不支持宣称统一 ObjectRef 或全量对象联动；异步索引、删除清理、评测、统一 ScenarioContext 和 M5/E2E-03/04 仍开放。

补充修订记录：v3.77（2026-10-24）实际浏览器检查发现 completed `simulated_live` 的总量因 blind horizon 隐藏而显示 `1055 / 0`，时间轴因未给起止边界显示 `0–0`。页面改为在总量未知时显示已提交数/未处理未知数，终态显示无剩余待处理，并把观测范围限制在首个已接收 ordinal 至当前 watermark。production build（2830 modules，924ms）通过，页面确认显示 1,055 条已提交、未处理 0、水位 105.4、范围 0–105.4。仍未运行时序语义查询、业务 Action 或测试；Wood Study 页面未修改。

补充修订记录：v3.78（2026-10-24）实现 FactoryNet 动态观测到稳定 `EntityInstance` 的 upsert：实例 ID 与静态 materializer 使用同一 `ontology + entity type + episode:source row` 哈希；挂接回放版本/Schema revision/Object Type resource，并在时序图节点公开 `object_ref_id`。SQL fallback 只返回数据库中确实存在的引用，前端点选优先用该 canonical ID 打开对象面板。语法检查、前端 production build（2830 modules，862ms）和 `git diff --check` 通过。只读复核现有回放仍为 1,055/1,055 事件、6,383 条事实、水位 105.4；已有匹配实例 209/1,055。该历史回放未回填，故新写入逻辑尚未由新事件运行验证；服务 health 全绿不等于本路径已端到端验收。Wood Study 未修改；真实时序检索、实例回填交互、Action/Agent/治理闭环、完整 E2E 和 M5 仍开放。

补充修订记录：v3.79（2026-10-24）时序语义检索结果增加 `object_ref_id`，仅当当前时间有效的 Observation 主体能解析到实际 `EntityInstance` 时返回；动态图检索卡可打开同一 canonical 对象面板。后端 `py_compile`、前端 TypeScript + production build（2830 modules，832ms）及 `git diff --check` 通过。未发起语义查询，未写入历史回放；因此接口/UI 新路径仍需在一条新事件和一次真实检索上验收。统一 ObjectRef 的范围当前只覆盖 FactoryNet Observation，不覆盖通用 temporal mapping、Agent/Scenario/Action 跨上下文。

补充修订记录：v3.80（2026-10-24）新增终止 FactoryNet 回放的 Observation 身份对账：只读 GET 预览，受编辑权限保护的 POST 按固定 dataset/schema revision 与分页执行，具有回放行锁和 per-run 幂等键；执行摘要写入新增 append-only 审计表 `v2_temporal_instance_reconciliations`（migration 0066），审计记录禁止 UPDATE/DELETE，并提供只读审计列表。动态演化页展示对账数量、版本、确认框和审计历史；FalkorDB 图投影读取时从 PostgreSQL 补充实际存在的 ObjectRef。migration 已应用于当前本地 DB（0066），`alembic check` 无漂移；后端重启后 `/health` 全绿、OpenAPI 路由存在；前端 TypeScript/production build（2830 modules，933ms）和 `git diff --check` 通过，实际页面已核验。只读预览 `278dc86b`：扫描 1,055/1,055，1,055 个唯一观测，已有 209、缺少 846、无效 0；500 条 Observation 图结果中 100 条暴露现存 ObjectRef；审计记录 0。本轮未调用 POST、未改写历史对象、未跑语义查询或测试。因此对账执行、幂等重试、审计拒绝变更、新事件写入与 ObjectRef 在语义检索中的实际打开仍未验收；木材演示未改动，Agent/Scenario/Action context 与 M5 仍开放。

补充修订记录：v3.81（2026-10-24）将持久 Agent Session 扩展为可固定已提交 FactoryNet temporal event：写入前复核回放/本体、事件状态、已物化 ObjectRef 的当前读取权限和对应 `EvidenceRef`；上下文摘要绑定 replay、dataset version、schema revision、事件 ID/ordinal、证据 ID/hash、对象 source version 与事件载荷摘要。Agent 推理只接收当前授权属性中的事件字段，并通过 source ref 引用证据；漂移时阻止运行，Action 目标仍必须是单独固定的业务对象。时序语义检索结果可显式转入专属持久会话，预填事件 ID 与草稿问题；用户须新建会话、手动加入引用并手动发送。Python `py_compile`、frontend TypeScript/production build（2830 modules，829ms）与 `git diff --check` 通过；本轮未创建 Agent 会话、未调用上下文 POST/推理、未执行历史对账或语义检索。该链路仍需以真实历史/新事件做 API + UI E2E；Temporal scenario/action context、Action Runtime 完整治理、M5 和木材双演示回归仍开放。

运行补充：backend 重启后 `/health` 及 DB/Neo4j/FalkorDB/MinIO/Chroma 均为 `ok`，前端 `/ontologies` 为 HTTP 200，OpenAPI 的 `ContextAdd.kind` 已包含 `temporal_event`。对当前已物化且有证据的历史事件执行只读 Agent resolver 检查成功，固定了 dataset/schema version、ObjectRef 与 evidence hash，并返回 57 个当前授权字段；没有创建 Session、写上下文或调用模型。真实 API/UI Session 流程仍未验收。

补充修订记录：v3.82（2026-10-24）将 Agent Session 固定的 temporal event 透传到 DecisionCase：Agent Action 提议自动附加关联 EvidenceRef，并将事件、回放、dataset/schema 版本、ObjectRef、事件载荷摘要、证据 hash 和对象权限摘要存入审核上下文；DecisionCase 创建、编辑及每次预览/提交审核/批准/执行前均重新核验来源与权限，目标只接受同一 Observation、同 episode 且同 source version 的 Episode，或对应同一事件的 DynamicIncident。审核详情显示固定时序依据；读上下文工具 schema 升至 v3。Python `py_compile`、前端 TypeScript/production build（2830 modules，839ms）及 `git diff --check` 通过，backend 重启后 health/依赖全绿、前端 HTTP 200。对真实已物化事件只读调用 Agent resolver、DecisionCase temporal validator 和 evidence binder 均接受；未创建 DecisionCase/ActionRun、未调用 Action API、未执行语义搜索或业务写入。真实 Agent→Case HTTP/API→人工审核→ActionRun E2E、Scenario 时序上下文、全局治理/性能与木材回归仍开放。

补充修订记录：v3.83（2026-10-24）为 FactoryNet What-if 基线补充服务端重建和内容 SHA-256：创建情景时重新读取选定对象/episode/ordinal/规则，并与客户端快照比较，不一致返回 409；运行前验证摘要及固定本体修订/数据集版本。基线带 source manifest，页面展示修订、数据集版本、回放编号和摘要；来源版本缺失时明确警告。What-if 查询、列表、修改、运行和取消路径增加本体访问校验；排队 worker 开始时再次检查提交者仍有访问权限。前端 TypeScript + Vite production build（2830 modules，870ms）、后端 `python3 -m py_compile` 和目标文件 `git diff --check` 通过。未运行测试、调用 What-if 业务 API、创建/执行情景或写业务数据；backend 重启后 health 及依赖全绿，OpenAPI 确认 What-if 路由已挂载。当前 What-if 图查询未提供可验证的回放/数据集版本 ID，因此本切片只能证明被冻结的图内容完整性，不能声称回放级血缘已经闭环。Wood Supplier Study 页面/数据未修改；Scenario temporal E2E、Agent/Action 联动、全局治理、M5 性能和双演示验收仍开放。

补充修订记录：v3.84（2026-10-24）将 FactoryNet What-if temporal 基线切换为复用 `stream_graph` 的时间过滤回放图；FalkorDB 投影为空时由 PostgreSQL TemporalEvent/TemporalFact 权威数据回退，服务端在完整回放快照内提取目标两跳邻域，超过 500 节点时失败关闭，不静默截断。后端 `py_compile`、前端 TypeScript/production build（2830 modules，834ms）与 `git diff --check` 通过；backend 重启后 health 与 DB/Neo4j/FalkorDB/MinIO/Chroma 均为 `ok`。只读 What-if context GET 对运行库实际 FactoryNet 本体、回放 `278dc86b`、episode `UMich_CNC_01`、ordinal `0` 返回 200，基线含 45 nodes/49 edges，明确绑定 replay、dataset version `79abb1e3`、schema revision `0a760645`、source event `f9d97bfc`、PostgreSQL temporal-facts backend 和 context digest；第一次请求使用了过期本体 ID，返回 422，随后只读核对运行库并改用当前本体 ID。没有创建情景、运行推演、写业务数据、触发历史回填或修改 Wood Supplier Study。该证据只关闭 temporal context read slice，不等于 What-if create/run、Scenario/Action/Agent 跨模块或 M5/E2E-03/04 完成。

补充修订记录：v3.85（2026-10-24）修复 Data Model→What-if 的回放身份传递与空投影页面：Data Model 响应暴露当前 `DataModelSnapshot.replay_id`，选择 Observation 时将 replay、episode、ordinal、mode 带入 What-if URL；当前快照 FalkorDB 无节点但 PostgreSQL 有已提交事件时，Data Model 使用 `stream_graph` 权威回退，避免显示空图。`py_compile`、frontend TypeScript/production build（2830 modules，836ms）和 `git diff --check` 通过；backend 重启后 health/六项依赖全绿。当前快照 `c3bb19d2` 的只读 Data Model GET 返回 200、回放 `b1049b08`、45 nodes/49 edges、backend `postgres-temporal-facts`；从同一响应身份构造的 What-if context GET 返回 200，绑定 replay `b1049b08`、dataset `79abb1e3`、schema revision `0a760645`、event `8844eef1`，摘要有效且 45 个节点全部在目标两跳内。未创建情景、运行推演、写业务数据或修改 Wood Supplier Study。页面点击/刷新路径尚未浏览器复验；M5、E2E-03/04 和全局 Action/Agent/治理仍未完成。

补充修订记录：v3.86（2026-10-24）修复 What-if 基线摘要中 Ordinal `0` 被逻辑或误显示为“—”，改用 nullish fallback 保留合法零值。前端 TypeScript/production build（2830 modules，821ms）通过。浏览器在独立标签直接打开当前快照回放 `b1049b08` 的 What-if URL，实际显示 Observation `FactoryNet:Observation:UMich_CNC_01:0`、Ordinal `0`、45 节点/49 关系、dataset/schema/replay/source-event 血缘与摘要；未执行“开始推演”，未创建情景或写入业务数据。浏览器尚未从 Data Model 实际点击入口，也未验证刷新持久性；Wood Supplier Study 未改。M5 完整 E2E 和整体目标仍开放。

补充修订记录：v3.87（2026-10-24）修正 FalkorDB 空回放由 PostgreSQL TemporalFact 权威回退时，数据模型分组仍查询 FalkorDB 并把真实类型数误报为 0。SQL temporal graph 响应新增基于完整过滤后节点集的 entity type counts；本体创建者/管理员使用完整过滤范围计数，其他用户仍按授权后可见节点计算，保留对象级权限边界。后端 `py_compile` 与 `git diff --check` 通过；backend 重启后 `/health/ready` 的 DB/schema 为 ready、当前 schema head 0066，前端 `/ontologies` HTTP 200。浏览器实际从 Data Model 的 45 nodes/49 edges Observation 点击“用于 What-If”后抵达绑定 replay `b1049b08`、episode `UMich_CNC_01`、ordinal `0` 的基线页；刷新后时间位置、来源版本、摘要和分组数量仍显示正确。分组计数显示传感器通道 39，刀具状态/工序阶段/机器/检测结果/生产过程/观测各 1。无 What-if 创建/运行、业务写入、历史回填或 Wood Study 修改。此处只关闭当前 Data Model→What-if 浏览器读取链路及计数显示缺陷；What-if create/run、跨 Agent/Scenario/Action E2E、M5、平台级治理、M0 Gate 和整体验收仍开放。

补充修订记录：v3.88（2026-10-24）为 What-if 接入共享对象/属性/关系权限投影，返回基线前过滤不可见节点、边和属性，并仅附可授权追溯的 EvidenceRef 元数据；情景与运行查询按创建者隔离（管理员可访问），创建/修改/运行前校验情景假设与固定本体修订下的规则，运行时绑定情景输入摘要、原子领取 queued run，并在开始及保存结果前重新校验场景所有权、权限和当前基线。新增真实基线对象/属性/关系裁剪、场景与运行归属隔离、queued worker 单次领取及重复投递测试；隔离 FastAPI + SQLite HTTP 测试覆盖 scenario create→queue→list/owner isolation，temporal baseline 使用测试替身。后端 `py_compile`、`git diff --check` 通过，What-if 定向测试 12 passed。backend 已重启；`/health/ready` 为 ready、schema current/head 0066，OpenAPI 挂载 8 条 What-if 路由，容器源码 SHA-256 与工作树一致，frontend `/overview` HTTP 200。尚未对真实 FactoryNet 回放执行 What-if create-run HTTP/浏览器 E2E，也没有创建情景或写入演示业务数据；因此隔离 API 测试不代表真实时序业务链路或 M5/E2E-03/04 关闭。`palantir` 工作树未提交。木材 Supplier Study 未修改；M0.1–M0.5、Scenario/Agent/Action 跨模块验收、M5 性能、双演示回归及整体验收仍开放。

补充修订记录：v3.90（2026-11-01）修正 What-if 基线规则来源与执行规则快照不一致：基线规则清单改为读取 TemporalReplay 固定的 OntologyRevision `logic_rules`，并在 provenance 写入 schema revision hash 和 rule-set digest，基线页展示两个摘要；回放缺少固定 revision、快照结构无效、规则 ID 重复或保存的 revision hash 与内容不符时 fail closed。此为源码增量，尚未运行测试、前端构建、真实回放或 HTTP/E2E；不关闭 M5/E2E-03/04、M0 Gate 或整体目标。

补充修订记录：v3.91（2026-11-01）修正 event_time What-if 基线的游标显示：选定 as-of 点通过来源事件时区换算为 ISO 时间值，和建立当前对象状态的来源事件时间分别记录；UI 根据 `time_kind` 区分事件时间与 Ordinal。此为源码增量，尚未运行事件时间边界测试、前端构建、真实回放或 HTTP/E2E；不关闭 M5/E2E-03/04、M0 Gate 或整体目标。

补充修订记录：v3.92（2026-11-01）为 What-if 基线增加显式同上下文时序向量证据检索：候选索引修订绑定 replay、dataset/schema 与 snapshot 元数据；按同一 as-of ordinal 检索后，由 PostgreSQL 再核对 TemporalFact 有效区间和同 replay/episode 的已提交来源事件，再按当前对象、属性、关系 ACL 裁剪。索引修订、query digest、permission digest、时间和证据指针封入基线摘要，情景创建与运行前重查并复核摘要；向量结果只作展示证据，不进入规则事实集。同步索引仍限制 25,000 条事实。仅完成源码、Python 语法与 diff 检查；未运行测试、前端构建、真实向量查询、HTTP/E2E 或 ACL 负向验证，M5/E2E-03/04 与 M0 Gate 仍开放。


补充修订记录：v3.93（2026-11-01）为超过 25,000 条 TemporalFact 的时序向量索引新增持久 Celery 后台构建：任务按 replay 与 source revision 幂等登记，记录进度、分发/运行租约与有限重试；索引按 revision 分 collection，分批 upsert 后才发布 ready，旧/未完成 revision 不参与查询；What-if 与 temporal semantic search 返回 202 和状态读取路径，What-if UI 轮询完成后重新读取并封存授权证据。新增 migration 0067、任务 worker/reconciler 与状态 API。当前只做源码，migration 未应用；后端 `py_compile`、`git diff --check` 与前端 production build 通过。未启动队列/worker、未访问 Chroma、未运行 HTTP/E2E、自动测试或 ACL 负向验证；过期 revision 清理与完整 M5/E2E-03/04 仍开放。

补充修订记录：v3.94（2026-11-01）新增类型化不可变 `ScenarioContext`，What-if 基线封入固定 ontology revision/hash、已发布 DataModelSnapshot/hash、DatasetVersion/checksum、replay/source/episode、time kind/value/timezone/watermark、scenario branch/checkpoint、principal/permission digest、query/evidence digest 与可选向量索引 revision；context GET 的 response model 暴露此子合同。情景创建后将上下文绑定 branch ID；运行和当前基线重建均校验合同 digest。What-if 基线现要求固定已发布 TemporalReplay、DatasetVersion/checksum、匹配的已发布快照以及与回放一致的事件/事实数量和 watermark；缺失或过期输入返回稳定 409，删除原先未 pin 回放的当前 FalkorDB 邻域 fallback。Temporal snapshot 发布与 What-if 复用内容摘要函数。后端 `py_compile` 与 `git diff --check` 通过；未运行自动测试、migration、服务/worker、Chroma、HTTP/E2E 或 ACL 负向验证。ScenarioContext 尚未跨 Object Query、Agent、DecisionCase、Action 集成；M5/E2E-03/04、M0 Gate 仍开放。

补充修订记录：v3.95（2026-11-01）Agent Session 新增 `what_if` 类型化上下文引用，保留既有 `scenario` 对 ScenarioResource 的引用语义。解析 WhatIfScenario 时按情景创建者范围检查，调用权限下的基线重建并校验 ScenarioContext branch、ontology、principal 与 digest；AgentContextItem 固定合同 digest/permission digest，运行时重新验证并在版本或权限变化时拒绝陈旧上下文。Local Agent 获取授权节点、边、规则与证据指针以及 assumptions/rule_overrides；提示词将情景输入与观测事实分开，并将向量结果定义为非权威候选召回。前端会话面板已加入新类型。`py_compile`、`git diff --check` 与 production build（2,830 modules，873ms）通过；没有自动测试、migration、服务/worker、Chroma、HTTP/E2E 或 ACL 负向验证。Agent 消费切片尚未完成共享会话权限、当前线上状态和完整 M5/E2E-03/04 验收；当时 Object Query、DecisionCase 与 Action 接入继续开放。

补充修订记录：v3.96（2026-11-01）Agent `propose_action` 将固定 `what_if` 与时序引用带入 DecisionCase 上下文；DecisionCase 创建和修改时重验 ScenarioContext、当前基线/权限主体、目标对象范围及可引用证据，预览/提交/审核/执行路径复用重验结果，拒绝陈旧或越权上下文。What-if 假设与规则覆盖值及其摘要同时封入 AgentContext/DecisionCase；会话上下文新增时、运行和 Action 提案时比对输入摘要，DecisionCase 在预览/提交/审核/执行前再次比对当前情景行，输入变更即要求刷新会话或审核事项。情景目标对象可从授权 What-if 基线节点选择；证据只能来自基线授权节点/边的 EvidenceRef 指针。ActionRequest/ActionRun 沿用 DecisionCase 上下文；审核历史显示情景 ID、基线与情景输入 digest、回放与时点。Agent Action 提案幂等摘要现在绑定上下文，避免同一请求号在不同情景引用间重放；what-if 引用快照上限 60 KB。后端 `py_compile`、前端 production build 与 `git diff --check` 通过；未运行自动测试、迁移、服务/worker、HTTP/E2E、ACL 负向或真实 Action 执行验收。本记录只代表静态源码增量；Object Query、非 Agent 直接 DecisionCase/Action 表单的 What-if 选择、运行态跨权限验证以及 M5/E2E-03/04、M0 Gate 仍开放。

补充修订记录：v3.97（2026-11-01）What-if list 增加精简 selector 响应，返回情景创建者、授权基线目标 ID、ScenarioContext、假设/规则覆盖快照与独立输入 digest，避免 DecisionCase 表单加载整个大基线。Decision Lab 和对象操作面板现可选当前用户创建且目标 ID 属于该基线的情景，将可验证引用写入 DecisionCase；服务端仍在创建、预览、审核和执行前复核所有权、基线、目标范围、证据与输入是否过期。共享前端引用构造使用服务端 digest；`py_compile`、前端 production build、`git diff --check` 通过。未运行自动测试、HTTP/E2E、运行态 ACL 负向或真实 Action 执行；Object Query 与 M5/E2E-03/04、M0 Gate 仍开放。


补充修订记录：v3.98（2026-11-01）Object Query 新增独立 `what_if_scenario_id` 执行上下文，不复用旧 ScenarioResource 的 `scenario_id`。查询前按情景创建者读取并重建当前用户授权基线，复核 ScenarioContext digest、假设/规则覆盖输入 digest 和 ontology revision；用只读、固定的节点/关系快照执行 Object Query load、精确聚合、对象关系图和双上下文比较。游标、执行摘要与 result manifest 固定情景 ID、基线摘要和输入摘要；Object Query 面板新增情景选择器，选定情景会清除普通 data view，缺失/过期引用会暂停查询。`object_query.schema.json` 已重生成；后端 `py_compile`、前端 production build（2,831 modules，770ms）与 `git diff --check` 通过。未运行自动测试、HTTP/E2E、真实时序查询、ACL 负向或 migration；图关系映射、性能与真实回放数据覆盖仍待运行态验收，M5/E2E-03/04、M0 Gate 和整体目标开放。

补充修订记录：v3.99（2026-11-01）Object Explorer 可将当前对象打开为独立 ObjectRef 页面，链接携带 ontology、类型、对象 ID 和可选 Data View/What-if 上下文；详情页以单对象 `StaticObjectSet` 调用现有 Object Query load，刷新时重新走服务端授权与数据解析，失败/无权对象使用不可区分的提示，ResultManifest 显示授权可见的来源版本；返回链接恢复 Object Explorer 的类型、活动对象、筛选条件和数据上下文。前端 production build（2,833 modules）通过；本地 Vite 真实路由渲染确认 shell/loading/error 状态可见，但 backend 端口 8000 未运行，未验证真实对象页、What-if/Data View 角色矩阵、撤权或浏览器往返；未运行自动测试或 ACL 负向验收。此切片是 M3 源码增量，不关闭 FR-07、E2E-04/05/06、M0 Gate 或整体目标。

补充修订记录：v4.00（2026-11-01）为时序向量索引补上保留生命周期：同步与 Celery 后台建成的 replay revision 统一登记；每日维护任务只清理超过 30 天、每个 replay 最近两版之外、非当前版且未被已保存 What-if Scenario/Run 基线引用的完成索引。清理前按 collection metadata/事实 metadata 发现可识别的旧 Chroma generation；缺少创建时间的旧 collection 从首次登记起保留 30 天。删除操作可重试，并在新增 append-only retention event 表留痕；任务状态增加 `pruned`，重新查询时可重新排队构建。新增 migration 0068 与前端终态提示。后端目标模块 `py_compile`、前端 production build（2,833 modules）和 `git diff --check` 通过；没有运行自动测试、应用 migration、启动 Celery/Chroma 或执行 HTTP/E2E/ACL 负向验收。缺少可识别 replay metadata 的空旧 collection 无法自动归属；M5/E2E-03/04、M0 Gate 和整体目标仍开放。

补充修订记录：v4.01（2026-11-01）新增按 TemporalReplay 隔离的只读向量索引清理审计 API，并在动态演化页展示最近 20 条记录；访问沿用回放的本体所有者/管理员授权，返回范围限定到索引 revision、清理原因、保留天数、保留代数、索引年龄和事实数。此次只完成源码，没有运行 HTTP/ACL 负向或浏览器验收；迁移 0068、Celery/Chroma 与 M5/E2E-03/04、M0 Gate 和整体验收仍未验证。

补充修订记录：v4.02（2026-11-01）新增版本化 `TemporalSourceAdapter`，分离 FactoryNet Observation 与固定映射通用来源的目标定位。通用来源必须与已固定 DatasetVersion 的 source_id、回放 schema revision、对象/属性映射一致；canonical 自关系按固定 revision 再校验。适配器 ID/版本及映射摘要进入 What-if 基线来源血缘和 checkpoint digest，回放映射漂移时重建 fail closed。后端 `py_compile`、前端 production build（2,833 modules）与 `git diff --check` 通过；未运行自动测试、迁移、HTTP/E2E、真实非 FactoryNet fixture 或 ACL 负向验收。M5/E2E-03/04、M0 Gate 与整体目标仍开放。

补充修订记录：v4.03（2026-11-01）将 adapter ID、adapter 版本和 source mapping digest 纳入 ScenarioContext 1.1，checkpoint/context digest 覆盖这些 pin；WhatIfTab 展示适配器版本与映射摘要；保留 v1.0 digest 的兼容读取，旧情景在重建基线时需重新创建以建立 adapter pin。后端语法编译、前端 production build（2,833 modules）和 `git diff --check` 通过；未运行自动测试、HTTP/E2E、迁移或真实来源 fixture。

补充修订记录：v4.04（2026-11-01）为持久 Scenario 增加 `expires_at`/`expired_at` 并在 migration 0069 按既有 TTL 回填截止时间；用户修改 TTL 时同步推进情景截止时间和相关 QueryDataView 保留时间。每 10 分钟的有界 Celery 任务会幂等归档已到期情景，并在独立 retention event 表记录系统到期原因；Scenario 摘要展示到期时间，只读保留信息接口允许授权用户在情景过期后查看事件。ScenarioContext、读取/编辑/合并入口和 queued/running Scenario Run 均检查截止时间/归档状态。源码与迁移已添加，尚未运行数据库迁移、worker、HTTP/E2E、自动测试或共享图数据物理清理；M5 expiry Gate、M5/E2E-03/04、M0 Gate 与整体验收仍开放。

补充修订记录：v4.05（2026-11-01）为到期 QueryDataView 增加可重入图快照清理 worker 和 migration 0070，保留 SQL manifest，并记录无外键、唯一约束的图删除审计；清理前保护活动 Scenario、Study、Run 结果和未过期子视图，`require_ready` 行锁与 worker 锁定同一视图行协调读取和删除，清理 API 按视图权限返回事件。后端 `py_compile`、`git diff --check` 与 Alembic head 读取通过；migration 未应用，未运行自动测试、Celery、FalkorDB、HTTP/E2E 或浏览器验收；M5 expiry/cleanup Gate、M5/E2E-03/04、M0 Gate 与整体验收仍开放。

补充修订记录：v4.06（2026-11-01）为持久 Scenario 增加 revision/Data View/content digest 固定检查点、从检查点创建独立 pinned 分支及审计恢复。恢复生成经过现有 typed edit ACL 的追加修订，并校验投影内容 digest 精确回到检查点；request ID 幂等；大于 500 edits 时明确拒绝恢复并允许从该检查点分支。Workbench 增加检查点列表、保存、分支和恢复入口。新增 migration 0071；仅源码增量，migration 未应用，未运行测试、服务、图数据库、HTTP/E2E、浏览器或 ACL 负向验收；M5 branch/checkpoint/restore Gate、E2E-03/04/07、M0 Gate 和整体验收仍开放。

补充修订记录：v4.07（2026-11-01）Tracking rebase 冲突固定 scenario etag/head、基线、候选修订与实时快照摘要，提供受授权的三方差异读取和逐项决议。对象/字段/关系策略决定差异是否可见及允许选择；决议要求完整选择集合、非空理由与幂等 request ID。写入前复核 scenario/conflict/live 摘要与权限，ChangeSet/ScenarioAudit 记录 actor、理由、策略范围及冲突摘要；过期或越权决议不产生 Scenario revision。Workbench 已加入基线/情景/实时值和决议控件；等待人工处理的冲突不再由定时任务重复覆盖。新增 migration 0072。Python 语法编译、前端 TypeScript/Vite production build、git diff --check 通过，Alembic head 为 0072。迁移尚未应用；没有运行自动测试、HTTP/E2E、ACL 负向、数据库/图服务或浏览器验证。定时 rebase 的 system actor 仍待单独完善，M5/E2E-03/04、M0 Gate 和整体验收仍开放。

补充修订记录：v4.08（2026-11-01）将 Tracking 定时 rebase 的执行 actor 与策略主体分开：worker 以 `actor_kind=system` 留审计，Scenario owner 仅作为已确认仍 active 的授权主体；共享 revision 路径每次仍重新执行对象/字段/关系 ACL，移除未使用的 `system_write` boolean bypass。ScenarioChangeSet 与 ScenarioAudit 现在区分 actor kind，system 的 actor_id 为空；持久记录包含委托策略主体、Scenario/Ontology scope、授权模式及 permission digest。Workspace 只在该 owner 仍是当前用户且权限 digest 未变化时显示 worker 生成的编辑输入。新增 migration 0073 并设置 downgrade guard，避免静默丢弃 system actor 归因。源码静态检查通过；migration 未应用，未运行自动测试、HTTP/E2E、真实 Celery 调度、ACL 负向或数据库迁移。该切片不提供独立平台 service principal/tenant authorization，也不关闭 M4/M5、E2E-03/04、M0 Gate 或整体验收。

补充修订记录：v4.09（2026-11-01）兑现 FR-03 第 7 项的完整事件 envelope hash：文件回放、模拟实时、generic/FactoryNet push 和 FactoryNet 兼容回放统一覆盖来源与固定回放 pin、event ID、operation、对象/episode、sequence、time kind/value/timezone、payload digest 及完整 source_ref；相同 ID 只有规范化 envelope 完全一致才可重放。新增 `payload_hash_version`，既有行由 migration 0074 标记为 v1；v1 事件按已持久化字段逐项比较，v2 同时校验存储摘要和新入站 envelope，未知版本 fail closed。发布快照内容 digest 纳入来源上下文和事件摘要版本。Python 语法编译、Alembic head 与 `git diff --check` 通过；migration 未应用，未运行自动测试、真实来源回放、HTTP/E2E、ACL 负向或并发验收。此增量不关闭完整事件时间/迟到策略、snapshot 原子发布或 M5/E2E-03/04、M0 Gate 与整体验收。

补充修订记录：v4.10（2026-11-01）完成 FR-03 第 11 项源码切片：TemporalReplay 在创建时固定当时的 `base_snapshot_id`；发布将该版本与当前 Ontology schema revision 一并放入数据库条件更新，同一事务内创建快照并切换当前指针，旧 base 竞争失败返回 `SNAPSHOT_STALE`，相同 replay 发布和 push 写入使用 replay 行锁协调。Snapshot v2 manifest/hash 固定 schema revision、DatasetVersion、watermark ordinal/sequence 与 event/node/edge/fact/evidence counts；发布前核对已提交计数及最后事件 watermark，What-if 读取 v2 时重算内容和 manifest 摘要；既有 v1 快照保持兼容读取。新增 migration 0075。目标模块 Python 语法编译、`git diff --check`、Alembic head 检查通过；migration 未应用，未运行自动测试、数据库并发/事务行为、HTTP/E2E 或真实 TemporalReplay 发布。跨进程 worker 的持久 lease 和 M0/M5 完整 Gate 仍开放。

补充修订记录：v4.11（2026-11-01）为 FR-03 第 10 项增加 Temporal Stream 持久 worker token/generation/lease/heartbeat、提交时 lease fencing、带退避的有界事件尝试、派发 lease 与 Celery Beat 过期 worker 回收。新增 `TemporalProjectionAttempt` 台账和 editor 人工重放接口，要求原因与幂等键；普通 start/resume/step 不再清除终态失败事件。动态演化页显示租约、失败事件、尝试历史和基于 SQL facts 与隔离 FalkorDB namespace 的节点/关系数量对比；计数匹配不视为逐事实内容证明。新增 migration 0076；`py_compile`、`git diff --check`、Alembic head 检查通过。当前环境没有 SQLAlchemy，ORM mapper 加载未能执行；migration 未应用，未运行自动测试、Celery/数据库/FalkorDB、HTTP/ACL 负向或浏览器验收。SQL 权威事实驱动的 FalkorDB 全量重建和逐事实内容对账仍未实现，FR-03、M5/E2E-03/04、M0 Gate 与整体验收均未关闭。

补充修订记录：v4.12（2026-11-01）为 FR-03 第 10 项新增已发布 TemporalReplay 的持久图投影重建任务：SQL `TemporalStreamEvent` 与 `TemporalFact` 计算来源修订；Celery 分批重建原隔离 FalkorDB namespace，按重建租约记录写入进度，Beat 回收过期任务；重建排队/运行/失败期间 FactoryNet 图读取回退到 SQL。完成后读取 FalkorDB 节点及关系原始属性，与 SQL 派生投影逐项比较并保存 SHA-256 内容摘要，成功才把已提交事件标记为 projected。动态演化页新增 editor-only 的“按 SQL 重建并核验”操作、任务状态及核验摘要；不更换 namespace，保留已发布快照和 What-if 引用。新增 migration 0077。后端 `py_compile`、`git diff --check` 与 Alembic head 检查通过；migration 未应用，未运行自动测试、ORM mapper、Celery/数据库/FalkorDB、HTTP/ACL 负向或浏览器验收。源码路径仍需在部署依赖下验证 SQLAlchemy mapper、Celery 恢复、FalkorDB 属性序列化与故障恢复；M5/E2E-03/04、M0 Gate、FR-03 完整验收及整体验收未关闭。

补充修订记录：v4.13（2026-11-01）落实 FR-02 第 8 项与 M0.1r 的 Pipeline LLM fallback 状态切片：显式 `llm_structurize`/`auto_extract` 请求默认 fail closed；仅当版本化 Pipeline step 记录 `fallback_policy=rule_based` 时允许整源规则回退。provider 异常只写稳定的非敏感原因码，不写原始异常文本；Run 与 pending-review Curated candidate 保存请求/实际方法、策略、原因和来源版本，fallback Run 终态为 `degraded`，前端和 node status 呈现降级。后端 `py_compile`、`git diff --check` 与前端 TypeScript/Vite production build（2,833 modules）通过。未运行自动测试、数据库迁移、真实 LLM、HTTP/E2E、ACL 负向或浏览器验收；ModelConfig/Prompt pin、`llm_enrich` 与 M0/M2 完整 Gate 仍开放。

补充修订记录：v4.14（2026-11-01）将 `llm_enrich` 纳入 FR-02 第 8 项的显式请求/回退策略：默认 fail closed，只有版本化步骤显式允许才可规则回退；语义增强跨批次原子处理，任一缺模型/调用/空响应/格式错误时丢弃部分 LLM 增强并按策略失败或整源规则回退。Run 分开记录 LLM 成功数与回退规则输出数；pending-review Curated 候选标记 `quality_status=not_evaluated_degraded`，列表与建本体选数/审核/映射 UI 显示质量未判定并要求人工审核。后端 `py_compile`、`git diff --check` 和前端 TypeScript/Vite production build（2,833 modules，1.06 秒）通过；未运行自动测试、迁移、真实 LLM、HTTP/E2E、ACL 负向或浏览器验收。ModelConfig/Prompt pin、attempt ledger、E2E-01 和 M0/M2 完整 Gate 仍开放。

补充修订记录：v4.15（2026-11-01）为 Pipeline Route C 的结构化提取/语义增强把选中的 ModelConfig ID、非秘密配置指纹、静态 Prompt ID/digest 固定进 PipelineRun execution manifest；worker 用同一配置 ID 复核配置/网关漂移，并在提示词摘要不匹配时停止模型调用，不重新选“latest”。新增 ModelInvocation PipelineRun/source DatasetVersion/attempt 序号/ModelConfig 关联字段与 migration 0078；每次调用先持久化 running，再记录 provider 终态、请求/响应 SHA-256 digest、耗时及稳定错误码。请求/响应正文、API endpoint 和 credential 不写入该调用记录，模型制品复现性保持 `unverified`。Run Detail API 与 Transform UI 显示固定摘要和最近调用历史；硬中断或完成写入失败时保留 `running` 作为结果未知信号。`py_compile`、`git diff --check` 和前端 production build（2,833 modules，0.89 秒）通过。未运行自动测试、migration/ORM mapper、真实模型、队列、HTTP/E2E、ACL 负向或浏览器验收；migration 未应用，不能据源码通过宣称 M0.1/M2/E2E-01 已完成。

补充修订记录：v4.16（2026-11-01）为 Pipeline Route C LLM 调用增加数据库不可变生命周期事件链：每次调用事务写入 `started`，provider 返回后追加结果事件，JSON/结构检查拒绝响应时再追加校验失败事件。事件固定 PipelineRun、DatasetVersion、ModelConfig/Prompt、请求/响应 hash、稳定错误码、序号、前序 digest 与 event digest；PostgreSQL/SQLite trigger 和 ORM hook 阻止更新/删除，唯一约束保护每次调用内的顺序，外键阻止删除已留事件的父调用。attempt 序号按 PipelineRun 行锁在 worker 重启后续增，并由唯一部分索引阻止重号。Run Detail 校验摘要链并返回安全事件字段，Transform UI 可展开查看；旧调用标记无事件，不伪造迁移前历史。新增 migration 0079，仅覆盖 Pipeline Route C；Construction/Mapping 与其他 ModelInvocation 调用尚未统一。Python 语法编译、前端 production build、迁移链静态关系和 `git diff --check` 通过；没有运行自动测试、ORM mapper、数据库 migration、模型/worker、HTTP/E2E、ACL 或浏览器验收。

补充修订记录：v4.17（2026-11-01）为 Construction MappingTask 把 Prompt digest、parser/normalizer 版本与 tool 清单加入输入 manifest；来源分析/本体映射两阶段 Prompt 使用固定字符串，并将合同摘要并入任务输入 digest，漂移时失败关闭。新增 `ConstructionMappingAttemptEvent` append-only SHA-256 链，记录 attempt start/result、provider call start/result、response validation 与完成摘要；新增 migration 0080。常规/多模态 MappingTask 页展示链状态和安全事件摘要；缺少旧 pin 的任务 fail closed。后端 `py_compile`、前端 production build（2,833 modules，659ms）、88 个 migration 的单 head 静态检查和 `git diff --check` 通过。migration 未应用；自动测试、ORM mapper、数据库/worker/模型、HTTP/E2E、资源级 ACL 未验收，整体目标仍开放。

补充修订记录：v4.18（2026-11-01）为 ConstructionDraft 增加可空 `created_by` 与 migration 0081；新草案记录创建者，目标本体已设置时，草案和 MappingTask 的读取、修改、预检、生成、重试、取消及构建均按当前本体访问策略复核。无本体的创建模式草案仅创建者可访问；ownerless 旧草案仅管理员可访问。Append 到本体前检查目标访问权。迁移未应用；自动测试、HTTP 角色矩阵、数据集来源级 ACL、ORM mapper 和完整 E2E 未验收。本切片收紧构筑资源入口，不代表数据集其他 API 或 M0.1 ACL Gate 已关闭。

补充修订记录：v4.19（2026-11-01）增加 Dataset/Connection/MultimodalInstallTask creator 字段与 migration 0082；新上传/连接导入/Pipeline 产物/I-BADAS 样例将创建者写入 Dataset，连接、导入任务与多模态资产读取/写入按所有者、显式 `shared_read` 演示数据或当前关联 Ontology ACL 控制。Temporal source/analysis/run/replay 与 temporal graph 路由、Pipeline 定义/版本/Run、通用 ConstructionRun/证据路由加入数据集及本体范围检查；样例版本需属于请求的数据集。后端 284 个 `app/*.py` 源文件静态 compile、Alembic 单 head（0082）和 `git diff --check` 通过；没有运行自动测试、ORM mapper、数据库迁移、HTTP 角色矩阵或 E2E。迁移尚未应用，其他数据/本体入口仍待逐路由审计；本增量不关闭 M0.1 ACL Gate。


补充修订记录：v4.20（2026-11-01）继续收紧旧入口资源授权：AuditTask 的列表按用户可访问本体过滤，详情、重试、取消、finding disposition 与 repair draft 操作均复核关联本体权限；LogicAsset/Run 路由改用统一 `require_ontology_access` 决策，GET `seed=true` 额外要求 editor。与 v4.19 的 Dataset/Connection/Multimodal/Temporal/Pipeline/ConstructionRun 权限增量一并静态复核：backend `app/` 下 284/284 个 Python 文件 compile 通过、Alembic 单 head 为 0082、`git diff --check` 通过。本轮没有运行测试、迁移、数据库、HTTP、浏览器或角色矩阵，仍不能视为 ACL 验收完成。

补充修订记录：v4.21（2026-11-01）为 Reasoning Workbench 补上对象与关系授权投影：输入事实、实例图和证据引用只使用当前可见断言；来源数据集不可访问或缺失来源标识的证据不向普通用户返回，非本体所有者/管理员看不到来源正文与文件/行定位。规则读取和推理图写回限本体所有者/管理员；运行记录按创建者隔离，并保存权限上下文摘要，权限变化后历史详情返回冲突且列表不暴露结论数。前端禁用过期运行及无写回权限时的保存按钮。没有新增 migration；backend `app/` 下 284/284 个 Python 文件 `compileall`、`git diff --check` 通过；未运行自动测试、HTTP 角色矩阵、数据库或业务 E2E，M0.1 ACL Gate 仍开放。

补充修订记录：v4.22（2026-11-01）收紧语义 Schema 的读取和变更入口：未通过当前 Ontology ACL 的请求不会先触发语义元数据补写；影响分析和写入要求 editor 角色及当前本体访问权，并按当前可见语义资源核对变更目标和引用资源。没有细粒度授权策略的 LogicRule 与 source mapping 变更限本体所有者/管理员；source mapping 的读取再复核来源 Dataset ACL。无新增 migration；backend `app/` 下 284/284 个 Python 文件 `compileall`、`git diff --check` 通过；未运行测试、HTTP 角色矩阵、数据库或业务 E2E，M0.1 ACL Gate 仍开放。

补充修订记录：v4.23（2026-11-01）旧版 AgentRun 新建时固定 ontology revision 与权限摘要；列表标注缺少快照或上下文已变化的旧运行，详情、编辑、生成与兼容确认入口不再返回或使用失效结果。AgentToolbox 复用推理结论前要求 ReasoningRun 固定当前本体修订。无新增 migration；只做 Python 静态编译与 `git diff --check`，未运行测试、HTTP/ACL 角色矩阵、模型、数据库或 E2E；M0.1 ACL Gate 保持开放。

补充修订记录：v4.24（2026-11-01）旧版 Decision API 在存在细粒度本体策略时 fail closed；新建 DecisionRecord 时服务端复核每个 ReasoningRun 的当前本体修订和权限摘要（摘要包含可访问证据 Dataset 集合），不再依赖前端禁用过期运行。无新增 migration；只做 Python 静态编译与 `git diff --check`，未运行测试、HTTP/ACL 角色矩阵、数据库或业务 E2E；M0.1 ACL Gate 保持开放。v4.25 曾记录 Extraction 权限 pin 切片；v4.26 已将旧直接写入口及其 worker 退役，当前运行状态以 v4.26 记录为准。
补充修订记录：v4.26（2026-11-01）退役旧版 Extraction 直接写本体接口及 worker：创建接口返回 410，已排队任务由 worker 失败关闭且不调用模型/写入；InfoTab 引导至 Dataset-based Construction 并传递目标 ontology ID。停用绕过 API 的直接写入 CLI/演示脚本，移除其硬编码登录口令。backend 编译、frontend production build 与 `git diff --check` 通过；未运行测试、HTTP/worker、数据库或业务 E2E。现有 E2E 与 `test_data` 仍引用旧 API，须迁移到版本化 Dataset/Construction 流程；独立 reviewer 发布审批仍开放。

补充修订记录：v4.27（2026-11-01）修复常规私有 Dataset 构筑：依据用户选定字段生成显式 `SourceDataset`/`Record` schema、类型与 `HAS_RECORD` 关系，并将数据集版本实例加入 SQL ontology；规则映射不伪装为 LLM 语义推断。Mapping 建议新增 canonical mapping reference，构建时只 materialize 用户明确勾选的候选，且要求保留关系和各实体标识属性；无效选择在 run 入队前拒绝。两处后端文件 `py_compile` 与 `git diff --check` 通过；未运行测试、迁移、服务、HTTP 或 E2E。M2.3 独立 reviewer 审批/发布、旧 E2E 迁移和 M0.1 Gate 仍开放。

补充修订记录：v4.28（2026-11-01）将六领域与三领域 Playwright 场景中的旧 LLM `/execute` 路径改为私有 CSV/Excel Dataset Construction：上传版本化数据集、创建 Draft、固定来源预检、生成并确认所有候选、构建并轮询 ConstructionRun；该路径只验证确定性结构映射，不宣称领域语义推断。登录凭据改从 `PROJECT4_E2E_USERNAME`/`PROJECT4_E2E_PASSWORD` 读取，删除 token 日志。未运行自动测试或浏览器/HTTP 验收；`test_data` 和独立供应链演示脚本仍有旧 `/execute` 引用，M2.3、M0.1 Gate 与整体验收保持开放。

补充修订记录：v4.29（2026-11-01）将 test_three_domains_e2e.py、test_two_paths_per_domain.py、api_run_llm_extraction.py 与 test_simple_llm_path.py 的旧 /execute 路径改为共享 test_data/construction_api_flow.py：每个领域选一个 CSV/TSV/XLSX 样本，上传 private Dataset、生成并确认确定性映射、构建并轮询 ConstructionRun。脚本明确该路径不调用 LLM、不代表领域语义推理；登录改读 PROJECT4_E2E_USERNAME/PROJECT4_E2E_PASSWORD，全库 Ontology 删除改为显式环境变量 opt-in。五个 Python 文件静态编译及 git diff --check 通过；未运行测试、HTTP 或 E2E。其他 test_data 与独立供应链脚本仍留有旧 /execute 引用；M2.3、M0.1 Gate 与整体验收保持开放。


补充修订记录：v4.30（2026-11-01）为常规与多模态 Dataset Construction 增加候选冻结及独立审核闸。提交审核时固定 DatasetVersion/checksum/schema、映射与处理摘要、MappingTask 输入摘要、目标本体基线修订、ModelConfig ID 及候选影响计数；reviewer 必须与提交人不同，审批/拒绝/要求修改写入 append-only 事件链。Build 现在只提交审核；通过后才创建 ConstructionRun/新本体并排队。常规/多模态旧直接 Run 路由被关闭，两个 worker 在执行开始与 canonical 写入前重验审批与基线；UI 提供审核队列/理由和作者状态轮询，演示 helper 要求分开的审核账号。新增 migration 0083；静态 AST 检查 91 个迁移版本、代码图单 head 0083；数据库未应用。15 个相关 Python 文件 `py_compile`、前端 `npm run build`（2,834 modules）及 `git diff --check` 通过；未运行自动测试、ORM mapper、数据库迁移、HTTP/ACL 角色矩阵、Celery/FalkorDB、模型或业务 E2E。影响提示目前是候选计数而非 materialized revision diff；其他语义写入路径和跨 SQL/图存储的故障恢复尚未全覆盖，M2.3、M0.1 ACL Gate 与整体验收保持开放。

补充修订记录：v4.31（2026-11-01）将 Construction 审核影响预览升级为基于固定基线的结构 diff，展示实体类型、属性、关系、规则的新增/变更/移除；安全摘要进入候选 manifest。候选决策与 worker 执行均复核 live schema digest，阻止未发 revision 的结构漂移；常规/多模态 SQL 物化后重算结构 diff 和 schema digest，与候选摘要不一致则拒绝发布。成功时在 revision SQL 事务中追加 append-only `materialized` 事件，记录实际 diff、revision ID、snapshot hash 与摘要核验，并将新 revision 的 parent 指向审核基准。审核页面显示候选差异和实际发布修订。后端 6 个相关 Python 模块 `py_compile`、前端 Vite production build（2,834 modules）及 `git diff --check` 通过；Migration 0083 未应用；未运行自动测试、ORM、数据库迁移、HTTP/ACL 角色矩阵、Celery/FalkorDB、模型或业务 E2E。下游 Query/Action 影响预览、其他语义写入口和跨 SQL/图存储故障恢复仍开放，M2.3、M0.1 Gate 与整体验收未关闭。


补充修订记录：v4.32（2026-11-01）Construction 审核增加下游影响预览。候选 schema diff 的实体/关系变更精确追踪引用实体，规则和动作摘要只显示 ID/名称/影响原因；不返回 function_code、查询定义、实例值或 Object Set 名称。由 projected query metadata digest 变化识别当前可用且绑定旧摘要的 Object Set、最新 Object View 配置和 QueryDataView，并以聚合计数呈现。影响摘要及其 digest 进入候选 manifest；审批及常规/多模态 worker 在继续前重新计算并比对，资源清单变化时 fail closed。三个相关 Python 模块 `py_compile`、前端 TypeScript/Vite production build（2,834 modules）和 `git diff --check` 通过。未运行自动测试、ORM/DB、migration、HTTP/ACL、Celery/FalkorDB、模型或 E2E；0083 未应用。其他语义写入口、恢复/权限传播闭环和跨 SQL/图事务仍开放，M2.3、M0.1 Gate 与整体验收未关闭。

补充修订记录：v4.33（2026-11-01）将 Construction 影响预览扩展到 Scenario。元数据摘要变化后，除 Object Set、最新 Object View 和可用 QueryDataView 外，还统计引用受影响快照的活动 Scenario 及可访问研究/运行产物；预览不返回情景或资源名称、定义和运行载荷。candidate manifest 加入精确查询资源与情景引用集合的哈希摘要，因此即使数量不变而底层 ID/版本替换，审核和 worker 复核仍会发现漂移。三个后端 Python 模块静态编译、前端 TypeScript/Vite production build（2,834 modules）、`git diff --check` 通过；v4.33 五页简报的包、布局、字体和作者 metadata 检查通过并逐页渲染检查。未运行自动测试、ORM/DB、migration、HTTP/ACL、Celery/FalkorDB、模型或 E2E；0083 未应用。Scenario 计数仅覆盖当前 snapshot 引用，其他语义写入口/权限传播及跨 SQL/图存储恢复仍开放，M2.3、M0.1 Gate 与整体验收未关闭。

补充修订记录：v4.34（2026-11-01）Construction 审核影响预览纳入本体修订固定的 What-if ScenarioContext、关联 What-ifRun 与引用这些情景的 DecisionCase。只统计 contract 中的当前 revision ID/hash 匹配情景；关联 DecisionCase 按 what_if_scenarios.ref_id 识别。候选 manifest 固定情景、运行、case 状态/版本和精确引用摘要哈希；页面只显示聚合数量，不展示假设、规则覆盖、运行载荷或决策内容。摘要在审核与 worker 校验时重算。3 个后端 Python 模块静态编译、frontend TypeScript/Vite production build（2,834 modules）与 `git diff --check` 通过；未运行自动测试、ORM、DB/migration、HTTP/ACL、Celery/FalkorDB、模型或 E2E，Migration 0083 未应用。其他语义写入口、Agent 会话与权限传播、SQL/图存储恢复仍未覆盖；M2.3、M0.1 Gate 与整体验收开放。


补充修订记录：v4.35（2026-11-01）Construction 影响预览进一步纳入直接 What-if 引用：查询仍 active 的 AgentContextItem(kind=what_if)，并按 ScenarioContext 所属 OntologyRevision 精确匹配 Scenario ID；统计涉及的 AgentSession、上下文项与 AgentRun。manifest 固定引用版本、权限 digest、session/case 状态摘要与运行身份集合的哈希摘要，审核/worker 重算；UI 仅显示计数，不返回会话标题、提示内容或运行 payload。Python 三模块静态编译、frontend TypeScript/Vite production build（2,834 modules）及 `git diff --check` 通过；未运行自动测试、ORM、DB/migration、HTTP/ACL、Celery/FalkorDB、模型或 E2E，0083 未应用。其他语义写入口、完整权限传播与跨 SQL/图恢复仍开放；M2.3、M0.1 Gate 与整体验收未关闭。


补充修订记录：v4.36（2026-11-01）Mapping Apply 写入口与审核独立性收口。旧 raw-row Apply/build-all 路由关闭；显式 Apply 使用资源 ACL、approved review ID、不可变 DatasetVersion 和幂等 MappingApplyRun；服务与 worker 校验申请/决定身份分离，worker 再核对 Mapping digest、审核与停用状态。旧快捷批准改为发起审核，创建向导只保存 Mapping 草稿。`py_compile`、前端 TypeScript/Vite production build（2,834 modules）和 `git diff --check` 通过；未运行测试、数据库/迁移、HTTP/ACL、worker、图数据库或 E2E，0083 未应用。Mapping 配置尚无独立发布版本及发起/worker actor 全链路，M2.3、M0.1 Gate 和整体验收仍开放。

补充修订记录：v4.37（2026-11-01）新增 Mapping 定义候选版本、独立审核、批准发布与追加事件；Mapping ApplyRun 现通过 provenance 扩展表固定 MappingReview，并记录请求 actor 和最近执行 worker。前端提供映射审核和运行身份摘要。相关 Python 文件 `py_compile`、TypeScript/Vite production build（2,834 modules）与 `git diff --check` 通过；Migration 0084 尚未应用，未运行自动测试、ORM、数据库迁移、HTTP/ACL、Celery/FalkorDB 或业务 E2E。每个 Mapping 与 CuratedReview 的既有唯一 ApplyRun 约束仍保留，worker actor 目前只记录最近一次；旧全域 E2E 仍需迁移。M2.3 其他语义写入口、完整权限传播、跨存储恢复与整体目标继续开放。

补充修订记录：v4.49（2026-11-01）修复 Action 授权快照字段脱敏后的原始属性回流：compiler 输入对象的 properties 只取 redact_record 输出，避免原始图属性中的下划线内部字段绕过 FR-05 投影。变更文件 `backend/app/services/v2/action_authorization.py` 静态编译通过；未运行自动测试、ORM/API、HTTP/ACL、数据库、worker/FalkorDB 或业务 E2E。无新增 migration；M4/E2E-05、M0.1 Gate 与整体验收保持开放。

补充修订记录：v4.50（2026-11-01）FR-05 为 Action 建立默认空的内部字段 allowlist，覆盖共享编译快照、compiled edit 读写校验、DecisionCase 目标属性和历史 preview 序列化；执行/补偿 ActionRun JSON 快照只保存本次涉及且允许的字段，同时以完整 before/after digest 检测对象漂移，按每个属性的变更序列校验快照并仅补偿净变化字段。无新增 migration。`action_authorization.py` 与 `decision_cases.py` 静态编译通过；未运行自动测试、ORM/API、HTTP/ACL、数据库、worker、FalkorDB 或业务 E2E。M4/E2E-05、M0.1 Gate 与整体验收保持开放。

补充修订记录：v4.51（2026-11-01）注册 Scenario ActionRun 获得带执行 attempt、随机 lease token 和过期时间的 fenced claim；同请求只在租约过期、claim digest chain 完整有效且没有已提交 ScenarioChangeSet 时接管，接管后旧 token 无法通过 revision 发布前核验。续租拒绝已过期 token；发现 ChangeSet 已提交但 ActionRun 仍 running 时转入 reconciliation_required。旧版缺少租约的 running 记录保留人工核对，不自动重放。新增 Migration 0090 创建租约字段、约束、索引和 append-only claim event 表；迁移尚未应用。相关 5 个后端 Python 模块和 Migration `py_compile`、`git diff --check` 通过；未运行自动测试、ORM/API import、migration/数据库、HTTP/ACL、worker、FalkorDB 或进程退出故障注入。确定性 Scenario 图 key 在 SQL 提交前写入，仍可能因进程中断遗留孤立图数据；outbox/孤立图恢复、M4/E2E-05、M0.1 Gate 与整体验收继续开放。

补充修订记录：v4.52（2026-11-01）注册 Scenario ActionRun 写图前持久化 graph key/revision 与 digest-chain 资源事件；重新获取 Scenario 与 ActionRun 锁后复核 head/etag、ACL、父视图及 compiled-edit 权限。成功发布时在同一 SQL 事务完成 ScenarioRevision/QueryDataView/ChangeSet/ActionRun 并清除标记。新增 Migration 0091 与每分钟恢复任务；过期且无 QueryDataView 引用、无 ChangeSet、claim chain 有效的 orphan 可幂等删除并记审计；引用视图时保护图；终态歧义和证据缺失保留人工处理。更新 Action 路由续租事务边界以统一锁顺序；attempt=0 的 workbench/supplier-study 原子事务保持不变。相关 6 个后端 Python 文件与 Migration `py_compile`、`git diff --check` 通过；未执行测试、应用迁移、连接数据库、启动 worker/FalkorDB 或故障注入。仅覆盖 `/actions/{action_id}/run`，其他 Scenario 跨存储路径、M4/E2E-05、M0.1 Gate 与整体验收仍开放。

补充修订记录：v4.53（2026-11-01）将 `PUT /{ontology_id}/scenarios/{scenario_id}/actions` 纳入 Scenario 图构建恢复：新 ActionRun 在图写入前持久化首个 claim，运行中的同键请求拒绝并发重放，租约过期且 claim chain 有效时可接管；route 续租后把 claim manifest 传给 `ScenarioService.revision()`，复用 v4.52 的 graph marker、重锁、权限复核和孤儿图 recovery。提交失败时 route 尝试以 fencing token 结束当前 ActionRun。无新增 migration。Action runtime 与 Workbench router 静态编译、`git diff --check` 通过；未运行自动测试、API/ORM import、数据库/migration、HTTP/ACL、worker/FalkorDB 或进程退出故障注入。供应商研究 case 提交及直接 Scenario revision 仍未纳入 durable graph-build claim；M4/E2E-05、M0.1 Gate 与整体验收继续开放。

补充修订记录：v4.54（2026-11-01）将 Supplier Study `PATCH .../cases/{case_id}` 纳入 Scenario graph-build 恢复。新 ActionRun 在写图前固定 claim，幂等同请求可回放或安全接管；ScenarioService 在 marker commit 后依次重锁 Study、Scenario、case，复核 study context、case revision/etag 与 preview digest。case 参数与 definition revision 现在只在图构建成功后、与 Scenario View/Revision/ChangeSet 和 ActionRun 同一个最终 SQL 事务中更新。新增仅限 leased ActionRun 使用的 revision callbacks；rename/archive case 也按 Study → Scenario → case 锁序。无新增 migration；复用 0090/0091。后端静态编译与 `git diff --check` 通过；未运行自动测试、ORM/API、数据库/migration、HTTP/ACL、Celery/FalkorDB、并发竞争或进程退出故障注入。直接 Scenario revision、live merge、Agent/DecisionCase 等路径及 M4/E2E-05、M0.1 Gate 与整体验收仍开放。

补充修订记录：v4.55（2026-11-01）将通用 typed ChangeSet Scenario revision 变为有服务端授权预览摘要、权限摘要和 leased ActionRun claim 的两阶段发布；checkpoint restore、Scenario duplicate、Tracking rebase/冲突解决均接入相同图构建标记与恢复合同。rebase/resolve 的 Scenario 基线变更从图写入前移到最终 SQL 发布事务；所有非租约 `ScenarioService.revision()` 调用由共享发布器接管，其他 ActionRun 必须带有效 fencing claim。该切片尚未把直接 ChangeSet 编辑转为统一 `ActionRequest` compiler。无新增 migration。相关 Python 模块 `py_compile` 与 `git diff --check` 通过；runtime import 因当前隔离 Python 缺 SQLAlchemy 未完成；未运行自动测试、ORM/API、迁移、HTTP/ACL、worker/FalkorDB、并发或故障注入。直接 live merge、Agent/DecisionCase、M4/E2E-05 与 M0.1 Gate 继续开放。

补充修订记录：v4.56（2026-11-01）通用 Scenario revision preview 新增按授权投影生成的逐操作 before/after 差异；属性只显示当前授权字段的值，创建/删除显示对象存在性，关系编辑显示关系存在性。创建编辑若目标对象已存在则先检查对象读取策略，再以 `object_already_exists` 冲突拒绝，隐藏对象不会通过错误码暴露存在性。`scenarios.py`、`action_authorization.py`、Scenario router `py_compile` 及 `git diff --check` 通过；未运行自动测试、ORM/API、数据库迁移、HTTP/ACL、worker/FalkorDB 或并发/崩溃故障注入。无新增 migration；direct ChangeSet UI/审批体验、运行验收、live merge、Agent/DecisionCase 与 M4/E2E-05/M0.1 Gate 仍开放。

补充修订记录：v4.57（2026-11-01）情景 Action Preview 现在把 compiler 使用的固定图快照投影为可见对象/字段/关系变化，并由工作台逐项显示属性前后值、对象存在性和关系状态；隐藏字段不进入差异响应，原 Action preview digest / permission digest 提交合同不变。`logic_actions.py`、相关 Scenario service 与前端静态编译/生产构建通过；未运行自动测试、ORM/API import、HTTP/ACL、数据库、Celery/FalkorDB、并发或故障注入。无新增 migration。任意 ChangeSet 编辑器、独立审批 UX、唯一 live/Scenario/DecisionCase Action Runtime、M4/E2E-05、M0.1 Gate 与整体验收继续开放。

补充修订记录：v4.58（2026-11-01）Scenario Live Merge Preview 按当前授权投影返回逐操作 before/after 差异，preview digest 绑定变化清单；工作台展示候选属性值、对象存在状态与关系状态。创建目标撞到可见主对象时保留 `Object already exists in main Ontology` 合并冲突；授权器先验证对象读取权限，变化清单只含拟写入字段，不复述主对象属性，隐藏对象仍 fail closed，普通创建仍拒绝覆盖。相关 Python `py_compile`、TypeScript/Vite production build（2,834 modules）与 `git diff --check` 通过；未运行自动测试、ORM/API import、数据库/migration、HTTP/ACL、Celery/FalkorDB 或故障注入。无新增 migration；统一 Action Runtime、M4/E2E-05、M0.1 Gate 与整体验收保持开放。

补充修订记录：v4.59（2026-11-01）`main.py::_run_schema_migration()` 在开发环境迁移或 schema 校验失败后不再回退执行 `Base.metadata.create_all()`；统一维持 `not_ready`，不执行启动 seeding/backfill，写请求被 middleware 拒绝且 `/health/ready` 返回 503，生产环境继续启动失败退出。无新增 migration。`main.py` Python 静态编译与 `git diff --check` 通过；未运行自动测试、应用启动、数据库连接/变更、migration 或 HTTP/readiness 验证。M0.1 Gate、运行验收与整体验收继续开放。

补充修订记录：v4.60（2026-11-01）Scenario Live Merge 图写入与人工恢复执行前新增共享 `reauthorize_edit_batch()`：执行器读取当前 FalkorDB 快照后，按当前对象、字段、关系权限重算摘要并与预览/审核 digest 比较；不匹配或缺少摘要即拒绝副作用。恢复时允许已部分写入的可见对象创建冲突，仅在当前操作者仍有访问/写入权限时收敛。更新 Scenario Workbench 调用方；无新增 migration。`action_runtime.py`、`live_edits.py`、`scenario_workbench.py`、`logic_actions.py` 静态编译和 `git diff --check` 通过；未运行自动测试、ORM/API、数据库、FalkorDB、HTTP/ACL、并发或故障注入。M4/E2E-05、统一 Action Runtime、M0.1 Gate 与整体验收保持开放。

补充修订记录：v4.61（2026-11-01）将 Scenario Live Merge 恢复分支的权限复核改为：以当前权限策略重新核对固定合并前快照的 edit batch 与权限摘要，再校验当前图是否处于 edit sequence 可产生的合法前缀状态。此举允许恢复期间对象已被部分删除的合法情况，同时阻断策略/权限变化及非预期图状态；首次执行分支仍按当前图核权。无新增 migration。`live_edits.py`、`action_runtime.py`、`scenario_workbench.py` 静态编译与 `git diff --check` 通过；未运行自动测试、DB/FalkorDB、HTTP/ACL、并发或故障注入。统一 Action Runtime、M4/E2E-05、M0.1 Gate 与整体验收继续开放。

补充修订记录：v4.62（2026-11-01）Construction MappingTask 的模型/执行失败不再持久化异常原文：通过稳定错误码分类并写入安全提示，mapping-task 详情仅返回经过完整 SHA-256 事件链验证的失败码，旧记录也不会向 API 返回原始 `task.error`。派发异常同样改为通用安全提示，并同步更新草案状态。无新增 migration。`construction_drafts.py` 静态编译与 `git diff --check` 通过；未运行自动测试、API/ACL、模型调用或异常 canary 验收。M0.1/M2/E2E-01 隐私与资源 ACL Gate 继续开放。

补充修订记录：v4.63（2026-11-01）ConstructionRun 的共享 `update_run()` 入口将异常/自由文本错误分类后只保存稳定错误码，`serialize_run()` 对所有新旧 ConstructionRun 记录统一返回安全消息和 `error_code`，避免构建 API 将 provider/异常原文直接返回。覆盖常规、多模态、时序构建的共享 Run；没有新增 migration。`construction_service.py` 与 `construction_drafts.py` 静态编译、`git diff --check` 通过；未运行自动测试、ORM/API、HTTP/ACL 或异常 canary 验收。M0.1/M2/E2E-01 隐私 Gate 与整体验收仍开放。
补充修订记录：v4.64（2026-11-01）What-if run 不再将 `WhatIfError` 或通用异常原文写入运行记录；worker 仅保存 allowlist 错误码，`serialize_run()` 将新旧记录映射为安全用户提示和 `error_code`。取消、提交者权限失效、情景所有权变化、排队后输入变化使用固定分类。无新增 migration。`what_if_service.py`、`what_if.py` 静态编译和 `git diff --check` 通过；未运行自动测试、ORM/API、HTTP/ACL 或异常 canary 泄漏验收。M0.1/M5/E2E-03 隐私与资源 ACL Gate 及整体验收仍开放。
补充修订记录：v4.65（2026-11-01）关闭 `temporal_replay_service._sync_construction_run()` 直接把 TemporalReplay 错误原文写入 `ConstructionRun.error` 的绕行路径；该同步现调用共享 `construction_run_error_code()`，只持久化稳定分类码。无新增 migration。`construction_service.py`、`temporal_replay_service.py` 及相关路由静态编译和 `git diff --check` 通过；未运行自动测试、ORM/API、worker、HTTP/ACL 或异常 canary 验收。TemporalReplay 自身错误记录仍需单独审阅；M0.1/M2/E2E-01 隐私 Gate 与整体验收仍开放。

补充修订记录：v4.69（2026-11-01）strict approved MappingApplyRun 路径不再在 SQL commit 前执行无 RowChange/outbox 追踪的 Neo4j 概念节点 best-effort 写入；持久投影由 RowChange 驱动 FalkorDB 概念与实例写入，legacy/non-strict 路径保留原行为。静态编译/import、单一 Alembic code head 与 `git diff --check` 通过；未运行测试、数据库/迁移、HTTP/ACL、Celery/FalkorDB 或 E2E。该增量不构成跨数据库原子性或 exactly-once 证明。

补充修订记录：v4.70（2026-11-01）扩展 0046 Mapping reconciliation 的 downgrade guard：存在 RowChange、实例 tombstone、非空 `source_mapping_id` 或 `Entity.is_source_deleted=true` 时拒绝删除来源历史字段。仅补强既有 migration 的降级保护，不增加 revision、不运行数据库迁移；完整 disposable PostgreSQL/SQLite 链升级与数据事实回退验收仍待完成。

补充修订记录：v4.71（2026-11-01）为 Pipeline migrations 0047/0048 补齐字段级 downgrade guards，防止单独写入的 version/run lineage、request actor/fingerprint、dispatch lease/task/time/error 事实在回退时被丢弃；0048 自动回填的 `legacy` 状态仍允许在其他 dispatch 字段均无事实时删除。无新增 migration revision；真实数据库分支和完整链升级/回退仍待隔离验收。

补充修订记录：v4.72（2026-11-01）扩展 0054 Mapping Row projection downgrade guard，任何非空 lease/current attempt ID 均阻止删除恢复字段；修正 M0.1 对 0065 append-only trigger 与 ORM listeners 的过时描述。无新增 migration revision；各单字段降级拒绝分支和完整 PostgreSQL/SQLite migration 链仍待隔离验收。

补充修订记录：v4.73（2026-11-01）为 0042 Action projection retry 状态和 0043 Curated Review DatasetVersion pin 增加 downgrade fail-closed guard，避免回退删除 retry 证据或审核版本来源。无新增 migration revision；积压限流、DatasetVersion 删除合同与真实数据库分支仍待隔离验收。

补充修订记录：v4.74（2026-11-01）为 CuratedReview 的固定 DatasetVersion 引入 FK RESTRICT 保留策略；删除包含审核记录的 Dataset/Curated Dataset 需显式 cascade 确认，避免默认删除清空审核来源；对象存储清理改在 SQL 删除提交后执行。新增 migration 0092，未应用；数据库行为、并发删除和完整 migration replay 尚未验证。

补充修订记录：v4.75（2026-11-01）将 v4.74 的显式删除合同接入两个 Curated 删除界面：首次删除遇到审核引用时展示审核记录数，并要求第二次确认后才以 `cascade=true` 删除审核及行编辑历史；其他删除冲突保留在界面显示。前端 TypeScript/Vite production build（2,834 modules）通过；未运行自动测试、HTTP/ACL 或数据库验收。Migration 0092 尚未应用，审核引用并发变化与级联顺序仍待数据库验证。

补充修订记录：v4.76（2026-11-01）为 Pipeline 新增持久 Schedule 资源、不可变 revision 快照、固定的 published PipelineVersion、时区 cron 校验、Beat 每分钟触发物化、幂等 ScheduleRun 与 PipelineRun 关联、错过触发 skip/run_once、forbid/allow overlap、待执行取消和触发人/服务身份 lineage；Pipeline Builder 增加基础管理与最近运行记录 UI。DST 明确为跳过不存在的本地时间、重复本地时间只触发首次；Connection 定时同步接口改为 501 并说明未创建任务，不再保存 cron 后返回成功。新增 migration 0093，尚未应用；frontend production build（2,834 modules）、相关 Python `compileall`、Alembic 单 head 和 `git diff --check` 通过。当前 Python 环境缺 SQLAlchemy，未能加载 ORM mapper；未运行自动测试、数据库迁移、HTTP/ACL、Celery/Beat 或运行态验收。service principal 目前是固定标识字符串，尚无独立身份注册/授权；Connection scheduler、运行中断和显式 ScheduleRun retry 仍未实现，M0 Gate 与整体目标开放。
