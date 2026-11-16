# 本体生成：开源项目调研（数据源、普通/时序/动态本体、展现、高并发）

> 日期：2026-11-16 · 作者：Jie Zhang
> 证据等级：第三方（开源项目源码，只读，未运行）；判断性结论标"本项目推断"
> 配套产出：`exports/本体生成模块PRD_v0.1_JieZhang.docx`（及同名 PDF、`_素材/` 目录，不进 git）

## 为什么做这次调研

上周老师提出：平台接入数据中台（易老师团队的通用平台），数据治理交给中台，本平台专注本体生成。老师对本体生成提了四点要求：

1. 数据源，要能接数据中台。
2. 普通、时序、动态本体怎么生成，其中动态本体必须做。
3. 展现形式：除了实体关系图，要有微软 Ontology Playground 的观感、Semantica 的炫酷效果，再加第三、四、五种。
4. 高并发，参考 Semantica 的做法。

本笔记记录调研依据，PRD 记录方案。

## 读了哪些项目

| 项目 | 许可证 | commit（日期） | 看什么 |
|---|---|---|---|
| microsoft/Ontology-Playground | MIT | `7bf7acb`（2026-10-22） | 类型图的观感与交互 |
| semantica-agi/semantica | MIT | `066505c`（2026-11-16） | Explorer 可视化；并发实现 |
| getzep/graphiti | Apache-2.0 | `869c1b2`（2026-11-15） | 双时间线事实；单图并发写入 |
| aws/context-ontology-accelerator | Apache-2.0 | `c91e1b0`（2026-11-12） | 从表生成本体；校验；任务 API |
| HelixDB/helix-foundry | Apache-2.0 | `a758e3a`（2026-11-14） | 连接器、变更捕获、装载与来源 |
| deeplethe/utopia | Apache-2.0 | `a0a800f`（2026-11-16） | 双时态图；结构演化；冲突检测 |
| ruc-datalab/EvoOntology | MIT | `4c33f9b`（2026-11-06） | 本体版本演化与门控发布 |

另读了三篇文章：

- 微信公众号"老刘说 NLP"2026-10-30 的《本体 ontology 开源盛世总结》：其中"微软"指 Ontology-Playground，"Semantic"指 semantica。
- 阿里云开发者社区 1428917：图数据库选型。
- CSDN 162175304：Palantir 本体的五要素。

## 主要结论

### 1. Semantica 的"高并发"只是进程内线程池（源码实测）

- 抽取器内部用线程池，默认 5 个线程：`semantica/semantic_extract/config.py` 中的 `resolve_max_workers`。
- 流水线步骤按依赖分层，同层并行：`semantica/pipeline/parallelism_manager.py`。
- `semantica/worker.py` 的主循环只有 `while self.running: … time.sleep(5)`，没有任务队列。
- README 中的性能数字来自历史 CHANGELOG，不是并发压测结果。

**结论**：Semantica 的可视化和流水线设计可以借鉴，但并发方案要参考其他项目。

### 2. 并发做得更扎实的是 Graphiti、helix-foundry、utopia 和 AWS（源码实测）

- **Graphiti**：
  - 模型调用用信号量限流，`graphiti_core/helpers.py` 中 `DEFAULT_SEMAPHORE_LIMIT = 20`。
  - 同一张图的并发写入设计见 `spec/concurrent-group-ingestion.md`：准入（串行，分配序号）→ 分析（窗口内并行，不写图）→ 提交（按序号串行，只修复冲突条目）→ 补充。
- **helix-foundry 与 utopia**：都用 PostgreSQL 表加 `FOR UPDATE SKIP LOCKED` 做任务队列。
  - helix-foundry：`backend/store/foundry_graph_jobs.go`、`connectors.go`
  - utopia：`crates/utopia-store/src/jobs.rs`、`migrations/0001_core.sql`
- **AWS 加速器**：
  - 标准对齐的线程池宽 24。
  - 共享的模型客户端连接池设为 32，比线程数大，避免线程抢不到连接，见 `packages/ontology-engine/src/coa_ontology/inducer/services/grounding.py`。

**本项目推断**：平台已经依赖 PostgreSQL，任务表加 SKIP LOCKED 可以直接满足队列需求，不必引入 Redis 或 Kafka；同一本体内的有序提交照搬 Graphiti 的四阶段。

### 3. 动态本体应分两层（本项目推断，依据如下）

- **数据层**（实例随数据增量更新）：
  - helix-foundry 的变更批次（JSONL，带起止序号）和"装载"概念（保留每个值的来源）；
  - Graphiti 和 utopia 的双时间线：Graphiti 的边有 `valid_at` / `invalid_at` 和 `created_at` / `expired_at`；旧事实失效但不删除。
- **结构层**（本体本身改版）：
  - utopia 先统计新术语，经人确认后再加入本体；
  - EvoOntology 的门控版本：候选版本在配对评估中优于父版本才发布。
- **平台差距**：我们的时序事实只有有效时间（`valid_from_ordinal` / `valid_to_ordinal`），缺记录时间。

### 4. 普通本体生成可参考 AWS 加速器的两种策略

- **`table_to_ontology`**：用向量把表和列匹配到 Schema.org、Dublin Core、PROV、FOAF、FIBO 等标准术语。
- **`rigor_ontology`（RIGOR，arXiv 2506.01232）**：按外键拓扑顺序逐表生成，一个模型生成，另一个模型评审。
- **校验分三级**：SHACL 结构约束、HermiT 逻辑一致性、OOPS! 建模质量。

### 5. 五种展现视图

| 视图 | 参考 |
|---|---|
| 类型总览 | Ontology Playground：Cytoscape + fcose 布局；数据绑定面板；按跳数展开；视图上限 180 个实体；首屏 ≤ 60 个实体，主线程单任务 ≤ 200 ms |
| 实例星图 | Semantica Explorer：React + Sigma.js（WebGL）+ ForceAtlas2；自我网络、距离模式、时间轴 |
| 时间演化 | Graphiti 的双时间线、EvoOntology 的版本对比 |
| 来源映射 | helix-foundry 的装载与来源 |
| 矩阵与分层 | 类型多时使用的热力矩阵和旭日图（本项目设计） |

## 需要老师确认

- "动态本体"指数据层、结构层，还是两层都要。
- 数据中台能提供哪些接口：元数据、数据读取、变更通知、敏感等级。待卢同学的文档。
- 高并发的目标规模。
- Semantica 是只借鉴设计，还是要作为组件集成。

## 未做的事

- 七个项目都只读了源码，没有运行，也没有做性能对比。性能数字留到验证阶段，用我们自己的数据压测。
- 克隆目录在会话临时目录中，不进 git；需要时按上表的 commit 重新克隆。
