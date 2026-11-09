# 开源项目：本体构建完成后的分析（30 个项目）

> 日期：2026-11-09 · 来源：各项目官方仓库与文档（链接见表）；2026-11-09 联网核实了 7 项（Openllet、LogMap、GraphRAG、DataHub、WIDOCO、MetricFlow、Neo4j GDS）的许可证与成熟度，其余是项目自述和通行认识，没有逐项复核 · 证据等级：第三方开源项目自述 + 本次部分联网核实 + 本项目推断
> 起因：老师建议在现有成果基础上多参考开源项目，重点看“本体建好之后怎么做分析”。
> 配套：同名 CSV（30 个项目）和 `_FUNCTIONS.csv`（功能清单），可直接用 Excel 打开。

## 一句话结论

这 30 个项目里，**大多数的前提是 RDF / OWL + JVM，而我们的本体是属性图（FalkorDB）+ PostgreSQL**，所以建议**借鉴它们的分析方法、自己实现**，不集成项目本身。其中最值得先做的是三件不需要新依赖、演示效果直接的事：**本体体检（F1）、告警富集分析（F2）、图结构分析（F3）**。

## 怎么读这张表

- **证据状态**：许可证一列里带“（本次核实）”的，是 2026-11-09 联网查过的；其余是项目自己的说法或业内常识（CSV 里有单独的“证据状态”列），写进方案前请以仓库的 README 和 LICENSE 为准。
- **效果**只写公开能查到的评价，没有我们自己跑出来的数字；这份调研没有在我们的数据上实测任何一个项目。
- **对应功能**指下面“值得做进系统的功能”里的编号。

## 30 个项目（按用途分组）

### 一、本体质量与约束校验

| 项目 | 干什么用 | 本体数据怎么用、怎么做分析 | 效果与成熟度 | 许可证 | 能否用到我们系统 | 结论 |
|---|---|---|---|---|---|---|
| [ROBOT](https://github.com/ontodev/robot) | OBO 生态的本体命令行工具集，放在发布流程里自动检查和处理本体。 | 读入 OWL 本体：report 按规则查缺标签、缺定义、重复、引用弃用项，分 ERROR/WARN/INFO 三级；reason 调推理机查一致性；verify 用 SPARQL 写“不应出现”的断言；diff 比两个版本；extract 抽取模块。 | OBO Foundry 本体的事实标准工具，大量本体的发布流程在用；检查项可配置。 | BSD-3-Clause（以仓库为准） | 检查清单可直接改写成我们对象类型、属性、关系上的确定性规则；不必引入 JVM。 | **借鉴检查清单，做进“本体体检”** → F1 |
| [OBO Dashboard](https://github.com/OBOFoundry/OBO-Dashboard) | 对一批 OBO 本体定期体检，生成网页看板。 | 对每个本体跑 ROBOT report、OWL 配置检查和 OBO 原则检查（命名、元数据、许可证声明等），汇总成红/黄/绿总评并保留历史。 | OBO Foundry 例行使用；价值在“体检结果→总评→趋势”这个产品形态，不在单条规则。 | 以仓库为准（未核实） | 每发布一个修订就自动体检、给总评、看趋势——我们现在没有。 | **借鉴产品形态** → F1 |
| [pySHACL](https://github.com/RDFLib/pySHACL) | Python 的 SHACL 校验器：用“形状”声明数据应满足的约束，再检查数据。 | 输入 RDF 数据图 + SHACL 形状图（必填属性、基数、数据类型、取值范围、唯一性、SPARQL 约束），输出逐条违规报告；可先做 RDFS/OWL-RL 推理再校验。 | 实现了 W3C SHACL 规范，纯 Python 易嵌入；大图受 RDFLib 性能限制。 | Apache-2.0（以仓库为准） | 约束校验的做法对；但我们的数据在 PostgreSQL / FalkorDB，不是 RDF，引入要先转换。 | **借鉴“形状”概念，自实现** → F4 |
| [LinkML](https://linkml.io/) | 用 YAML 写数据模型（类、字段、约束、枚举），再从同一份模型生成各种产物。 | 一份模型 → 生成 JSON Schema / SHACL / OWL / SQL DDL / Pydantic / 文档，并用同一份模型校验实例数据（linkml-validate）。 | 在生物医学和科研数据社区被广泛使用；约束表达力强。 | CC0-1.0（以仓库为准） | “模型是唯一事实来源，校验、文档、Schema 都从它生成”，正好对应我们“对象类型+属性 → 约束+文档”。 | **借鉴；约束与文档导出以此为参照** → F4、F6 |

### 二、OWL 推理机

| 项目 | 干什么用 | 本体数据怎么用、怎么做分析 | 效果与成熟度 | 许可证 | 能否用到我们系统 | 结论 |
|---|---|---|---|---|---|---|
| [ELK](https://github.com/liveontologies/elk-reasoner) | OWL 2 EL 子集的高性能推理机。 | 对 OWL 本体做类层次分类、可满足性和一致性检查，找出隐含的子类关系和不可满足的类。 | 在 SNOMED CT、GO 等大型 EL 本体上分类很快（论文与推理机评测自述）；只支持 EL 子集。 | Apache-2.0（以仓库为准） | 我们的本体不是形式化 OWL（对象类型+链接+属性），要先导出；收益主要是查“类层次矛盾”。 | **暂缓；若做 OWL 导出，经 ROBOT 调用** |
| [HermiT](http://www.hermit-reasoner.com/) | 完整 OWL 2 DL 推理机。 | 一致性检查、分类、实例检索，支持 DL 的全部表达力。 | 经典、完备；大本体上通常比 EL 专用推理机慢。 | LGPL（以仓库为准） | 表达力对我们过剩，同样要先导出 OWL。 | **不做** |
| [Openllet](https://github.com/Galigator/openllet) | Pellet 的开源延续分支：OWL 2 推理 + 推理解释 + SPARQL。 | 可接 Jena / OWL API，做一致性检查、分类，并解释“为什么推出这个结论”。 | 解释能力是亮点；活跃度偏低（2025-08 快照约 108 星，已过期）。 | 本次未能确认：扫描工具判其 LICENSE.txt 非 OSI 许可；Pellet 原版为 AGPL/商业双许可（本次核实） | 我们的推理验证已有证据链；许可证风险高。 | **不引入** |

### 三、数据访问与查询

| 项目 | 干什么用 | 本体数据怎么用、怎么做分析 | 效果与成熟度 | 许可证 | 能否用到我们系统 | 结论 |
|---|---|---|---|---|---|---|
| [Ontop](https://ontop-vkg.org/) | 虚拟知识图谱（OBDA）：不搬数据，用映射让关系库按本体被查询。 | 写“表/列 → 本体概念/属性”的映射；SPARQL 查询被实时改写成 SQL；还能依据本体约束检查数据一致性。 | 有工业落地案例（官方自述）；Apache-2.0（官方自述）。 | Apache-2.0（官方自述） | 和我们“数据视图 + 映射”相近，但我们把数据复制进 FalkorDB；Ontop 的价值在映射可导出、可验证；需要 JVM 与 RDF 栈。 | **借鉴映射表达；不引入** → F5 |
| [Apache Jena](https://jena.apache.org/) | Java 的 RDF / 语义网框架：存储、SPARQL、SHACL、规则推理、Fuseki 服务。 | 把本体和实例以 RDF 存入 TDB，用 SPARQL 查询，用 SHACL 校验，用 RDFS/OWL 规则推理机补全隐含事实。 | Apache 顶级项目，成熟稳定。 | Apache-2.0 | 前提是 RDF 与 JVM；我们是属性图。 | **不引入** |
| [neosemantics (n10s)](https://github.com/neo4j-labs/neosemantics) | Neo4j 插件，在属性图里导入/导出 RDF 与本体。 | 导入 RDFS/OWL 子集，做 SHACL 校验、子类/子属性轻量推理，再用 Cypher 分析。 | 证明了“在属性图上做 SHACL 校验和轻推理”可行；属于 Neo4j Labs 项目（实验性质）。 | Apache-2.0（以仓库为准） | 我们主图是 FalkorDB；栈里的 Neo4j 不是主存储。 | **不引入；借鉴做法** → F4 |

### 四、本体语义分析

| 项目 | 干什么用 | 本体数据怎么用、怎么做分析 | 效果与成熟度 | 许可证 | 能否用到我们系统 | 结论 |
|---|---|---|---|---|---|---|
| [OAK (Ontology Access Kit)](https://github.com/INCATools/ontology-access-kit) | Python 库 + 命令行，用一套接口读取各种本体后端（OWL、OBO JSON、SQLite、BioPortal 等）。 | 术语搜索、祖先/后代/路径、语义相似度、文本注释（文本→术语）、版本差异、映射、验证。 | Monarch / OBO 生态的基础组件之一；价值是统一接口。 | BSD-3-Clause（以仓库为准） | 其中“相似度、差异”可用在 AI 候选去重和修订对比；其余多面向生物本体。 | **借鉴** → F6、F7 |
| [semantic-sql](https://github.com/INCATools/semantic-sql) | 把本体放进 SQLite / PostgreSQL，并预置视图，用普通 SQL 做本体查询和统计。 | 预计算继承闭包（entailed_edge 等）、标签与定义视图；用 SQL 查“某类的全部后代”“哪些类没有定义”。 | 小而实用，适合层级深的本体。 | CC0（以仓库为准） | 我们的对象类型层级浅，暂无需；以后层级变深，“闭包表”是现成办法。 | **暂缓** |
| [Semsimian](https://github.com/monarch-initiative/semsimian) | Rust 实现的本体语义相似度库（带 Python 绑定）。 | 基于共同祖先计算 Jaccard、信息量（Resnik）、Phenodigm 等，比较两组术语或两个对象的相似程度，常用于病例/表型匹配。 | 作为 OAK 的高性能后端，自述比纯 Python 实现快。 | 以仓库为准（未核实） | 需要“对象被本体术语标注”的语料，我们没有；度量思路可用。 | **借鉴指标，不引入** → F7 |
| [GOATOOLS](https://github.com/tanghaibao/goatools) | 基因本体（GO）富集分析工具。 | 给定一组对象（如差异表达基因）和背景集，检验哪些本体类别被显著“过表达”（Fisher 精确检验 + 多重检验校正），并沿层级汇总、画图。 | 学术界广泛使用（Sci Rep 2018 论文）。 | BSD-2-Clause（以仓库为准） | 思路可原样迁移：这批告警设备是否集中在某类设备 / 某条产线 / 某个供应商？这是“本体 + 数据”最典型的分析，实现只需 scipy。 | **直接做** → F2 |
| [KGTK](https://github.com/usc-isi-i2/kgtk) | USC/ISI 的知识图谱工具包，以 TSV 表示图。 | 导入 Wikidata 等、Kypher 图查询、图统计、中心性、嵌入、导出。 | 能处理 Wikidata 规模（论文自述）。 | MIT（以仓库为准） | 规模远超我们所需；“图统计摘要”的项目可借鉴。 | **借鉴统计项** → F3 |

### 五、嵌入、机器学习与对齐

| 项目 | 干什么用 | 本体数据怎么用、怎么做分析 | 效果与成熟度 | 许可证 | 能否用到我们系统 | 结论 |
|---|---|---|---|---|---|---|
| [PyKEEN](https://github.com/pykeen/pykeen) | 知识图谱嵌入与链接预测库。 | 把三元组训练成向量（TransE、RotatE、ComplEx 等数十种模型），预测缺失的关系，用 MRR、Hits@k 评测，支持超参搜索。 | 在公开基准上可复现多种结果（论文自述）；小图上不稳定。 | MIT（以仓库为准） | 做“补全缺失关系”需要数千条以上三元组；现在的演示数据（22 个实例）远不够。 | **暂缓，等 AI4I / FactoryNet 数据到位** |
| [mOWL](https://github.com/bio-ontology-research-group/mowl) | 面向 OWL 本体的机器学习库（PyTorch）。 | 把本体公理（不只是三元组）嵌入向量空间（EL Embeddings、Box 嵌入、OWL2Vec* 等），用于基因-疾病关联预测、本体补全。 | 论文在生物医学任务上报告有效。 | 以仓库为准（未核实） | 要求形式化 OWL 公理；我们没有。 | **不做** |
| [DeepOnto](https://github.com/KRR-Oxford/DeepOnto) | 深度学习 + 本体工程的 Python 包。 | 基于 OWL API 处理本体；含 BERTMap（本体对齐）、基于大模型提示的子类关系推断（OntoLAMA）和 Bio-ML 对齐基准。 | 在 OAEI Bio-ML 赛道有对比结果（自述）。 | Apache-2.0（以仓库为准） | 对齐能力可用于“AI 候选 ↔ 已有本体 / 第二个本体”的语义匹配；需要 JVM。 | **中期借鉴** → F7 |
| [LogMap](https://github.com/ernestojimenezruiz/logmap-matcher) | 本体匹配（对齐）系统，带逻辑修复。 | 找出两个本体里等价/子类的概念对，并保证对齐结果加进去不会造成逻辑矛盾。 | 多年参加 OAEI 评测，是常见的强基线之一（OAEI 论文）。 | README 标注 Apache-2.0；早期 OAEI 论文写 LGPL-3.0，以仓库 LICENSE 为准（本次核实） | 对齐需求中期才有；“对齐后保证一致”的思路可借鉴。 | **借鉴思路，不引入** → F7 |

### 六、图分析与可视化

| 项目 | 干什么用 | 本体数据怎么用、怎么做分析 | 效果与成熟度 | 许可证 | 能否用到我们系统 | 结论 |
|---|---|---|---|---|---|---|
| [NetworkX](https://networkx.org/) | 纯 Python 图算法库。 | 把数据视图里的对象和关系导成图，算度与中心性、最短路径、连通分量、社区、PageRank。 | 算法齐全、上手最快；纯 Python，边数到数十万后明显变慢。 | BSD-3-Clause | 对“固定数据视图”的子图做关键对象、孤立子图、影响半径分析，成本最低。 | **直接做** → F3 |
| [Neo4j Graph Data Science](https://neo4j.com/docs/graph-data-science/current/introduction/) | Neo4j 的图数据科学库。 | 中心性、社区发现、相似度、路径、节点嵌入、链接预测等数十种算法，在图投影上运行。 | 算法最全、性能好；社区版并行度上限 4 核、模型目录最多 3 个，企业版需授权。 | 按授权分社区版 / 企业版（以官方文档为准）（本次核实） | 主图是 FalkorDB，不直接用；算法清单可作参照。 | **不引入；借鉴算法清单** → F3 |
| [WebVOWL](https://github.com/VisualDataWeb/WebVOWL) | 浏览器里的本体可视化。 | 把 OWL 转成 VOWL JSON，用统一图例画出类、属性、关系；可筛选、缩放。 | 中小本体一目了然，大本体容易杂乱。 | MIT（以仓库为准） | 我们已有关系图（Cytoscape）；可参考统一图例。 | **不引入** |

### 七、血缘与治理

| 项目 | 干什么用 | 本体数据怎么用、怎么做分析 | 效果与成熟度 | 许可证 | 能否用到我们系统 | 结论 |
|---|---|---|---|---|---|---|
| [Apache Atlas](https://atlas.apache.org/) | Hadoop 生态的元数据管理与治理。 | 类型系统、数据血缘、分类标签（沿血缘传播）、业务词汇表；用血缘做“影响分析”。 | 治理功能完整，但部署重（依赖 HBase、Solr、Kafka 等）。 | Apache-2.0 | 我们的规模不需要整套；“分类沿血缘传播”（如敏感级别）可借鉴。 | **不引入；借鉴** → F5 |
| [DataHub](https://github.com/datahub-project/datahub) | 元数据平台：数据集、管道、看板的目录、血缘与治理。 | 通过摄取器收集元数据，画上下游血缘，支持搜索、标签、所有者；用血缘回答“改这个表会影响谁”。 | 血缘覆盖面广；列级血缘取决于摄取方式（官方 OpenLineage 文档提示）。 | Apache-2.0（官方 FAQ）（本次核实） | 太重（需要 Kafka、搜索引擎、数据库）；血缘图 + 影响分析的界面可借鉴。 | **借鉴，不引入** → F5 |
| [OpenMetadata](https://open-metadata.org/) | 统一元数据平台：目录、血缘、数据质量测试、词汇表。 | 把质量测试挂到实体/列上，结果回写目录；血缘到列级。 | 同类里相对轻，数据质量内置。 | Apache-2.0（以仓库为准） | “质量测试挂在实体上、结果显示在目录里”可借鉴到我们的对象类型。 | **借鉴** → F4、F5 |

### 八、大模型 + 知识图谱

| 项目 | 干什么用 | 本体数据怎么用、怎么做分析 | 效果与成熟度 | 许可证 | 能否用到我们系统 | 结论 |
|---|---|---|---|---|---|---|
| [Microsoft GraphRAG](https://github.com/microsoft/graphrag) | 把文本变成知识图谱，再做检索增强生成。 | 从文本抽取实体和关系 → 建图 → 社区检测（Leiden）→ 给每个社区写摘要；“全局问题”查社区摘要，“局部问题”查实体邻域。 | 对“全库总结类”问题强于普通向量检索（微软自述）；索引阶段大模型调用多、成本高；微软研究院 2025-06 称 LazyGraphRAG 的索引成本约为完整 GraphRAG 的 0.1%。 | MIT（聚合站点一致，个别列表缺失，需核对仓库 LICENSE）（本次核实） | 我们的本体已结构化，不需要“文本→图”；“社区摘要 + 子图检索”可借鉴。 | **借鉴，不引入** → F8 |
| [OntoGPT](https://github.com/monarch-initiative/ontogpt) | 用大模型从文本抽取信息，并把名称接地到本体。 | SPIRES：按 LinkML 模板让模型分步抽取，再把名称匹配到本体里的标准 ID。 | 在生物医学文献抽取上有论文与案例（自述）。 | 以仓库为准（未核实） | 可把“检修报告 / 文档 → 对象实例”接到“数据源”，让文本也能进本体。 | **中期借鉴** → F10 |
| [Graphiti (Zep)](https://github.com/getzep/graphiti) | 面向智能体的时序知识图谱引擎。 | 事实带有效期（双时态：事件时间与入库时间）、增量写入、混合检索（语义 + BM25 + 图遍历），可自定义实体类型。 | 智能体记忆场景的热门项目；时序事实管理是亮点。 | Apache-2.0（以仓库为准）；据其文档支持 FalkorDB 后端，使用前核对版本 | 与我们的 FalkorDB + 时序本体契合；“事实有效期”可借鉴到决策记录和告警。 | **中期原型** → F8 |

### 九、语义层与文档

| 项目 | 干什么用 | 本体数据怎么用、怎么做分析 | 效果与成熟度 | 许可证 | 能否用到我们系统 | 结论 |
|---|---|---|---|---|---|---|
| [MetricFlow (dbt Labs)](https://github.com/dbt-labs/metricflow) | dbt 的语义层引擎：在语义模型上统一定义度量与维度。 | 声明度量（如“高风险暴露分钟”）和可切的维度，引擎生成对应 SQL；同一口径被报表、接口、智能体复用。 | 度量口径统一的主流做法；服务层和 API 仍在 dbt 付费平台。 | Apache-2.0（自 2025-10-14 的 v0.209.0 起；此前为 BUSL）（本次核实） | 它面向数仓 SQL；我们的“本体查询”可升级成“度量库”（名称、口径、维度、版本）。 | **借鉴数据结构，不引入** → F9 |
| [WIDOCO](https://github.com/dgarijo/Widoco) | 从 OWL 本体自动生成 HTML 文档。 | 读取类、属性、关系、元数据，生成带目录、图和版本记录的网页文档；可附 OOPS! 的本体缺陷评估。 | 本体发布时常用的文档工具；最近记录版本 v1.4.25（codemeta 记于 2025-01）。 | Apache-2.0（codemeta 记载，需核对 LICENSE）（本次核实） | 从任一修订一键导出“本体文档”（对象类型、关系、属性、示例、与上一修订的差异），评审和汇报直接可用。 | **直接做** → F6 |

## 和我们现状的差距

对照当前系统（侧栏 21 项，见 [ADR 0021](../../adr/0021-flat-task-navigation.md)）：

| 我们已有 | 现状 | 开源项目给出的参照 |
|---|---|---|
| 质量审查 | 大模型（本地 qwen3.5:0.8b）调用工具逐步审查，输出会随模型波动；没有评分、没有趋势 | ROBOT report、OBO Dashboard：确定性规则 + 总评 + 历史趋势 |
| 推理验证 | IF-THEN 规则 + 证据链，引擎可用；但演示本体里没有已发布的逻辑规则，默认运行按钮是灰的 | 推理机（ELK、HermiT）面向 OWL；我们的价值在证据链，不必换引擎 |
| 告警中心、运行规则 | 规则在固定视图上评估、告警去重；没有“告警集中在哪”这类归因分析 | GOATOOLS 的富集分析 |
| 对象浏览器、关系图 | 能看分布、能看关系；没有中心性、孤立子图、影响半径 | NetworkX、KGTK、GDS 的算法清单 |
| 数据模型、数据源映射 | 有来源证据与映射；没有“改这一列影响谁”的血缘视图 | DataHub、Atlas、OpenMetadata 的血缘与影响分析 |
| 数据模型页 | 有“数据检查器”（本次没有深入）；没有按约束批量校验并给出违规清单 | SHACL（pySHACL）、LinkML |
| 本体查询 | 可复用查询；没有口径、维度的度量定义 | MetricFlow 的语义层 |
| AI 本体候选 | 能生成、逐条审阅、发布；候选与已有本体只按名称判重 | OAK / Semsimian 相似度、DeepOnto / LogMap 对齐 |
| 修订记录 | 有修订，没有一键导出的文档与差异 | WIDOCO、OAK diff |

## 值得做进系统的功能（建议顺序）

| 编号 | 优先级 | 功能 | 借鉴的项目 | 用我们的什么数据 | 怎么做 | 工作量（估） | 价值与说明 |
|---|---|---|---|---|---|---|---|
| F1 | P1 | 本体体检与修订趋势 | ROBOT、OBO Dashboard | 对象类型/属性/关系的元数据 + 实例使用统计 | 确定性规则表：缺描述、孤立类型、重名或近似名、关系缺端点、同名属性类型不一致、无实例的类型、未被使用的关系；每个修订出一个总评并看趋势。放在“质量审查”页，和大模型审查并列：规则体检给可重复的事实，大模型审查负责解释和修复建议。 | 3–5 天 | 不依赖大模型（现在的审查用 0.8B 本地模型，结果会飘）；马上能演示。 |
| F2 | P1 | 告警 / 事件富集分析 | GOATOOLS | 告警 + 对象类型 / 属性（设备类型、产线、供应商） | 对一批告警对象做 Fisher 精确检验 + 多重检验校正，回答“告警集中在哪一类”。放在“告警中心”里做一个“集中在哪里”面板。 | 3–4 天 | “本体 + 数据”最直接的分析；实现只需 scipy。 |
| F3 | P1 | 图结构分析：关键对象、孤立子图、影响半径 | NetworkX、KGTK、Neo4j GDS（算法清单） | 固定数据视图的对象与关系 | 把数据视图导成 NetworkX 图（限制规模），出中心性 Top-N、连通分量、k 跳影响半径、社区；结果在关系图上高亮。大图改用 FalkorDB 自带的图算法（具体支持以所装版本为准）。 | 4–6 天 | 不改写路径，只读视图；演示效果直观。 |
| F4 | P2 | 实例约束校验（违规清单） | pySHACL、LinkML、OpenMetadata、n10s | 对象类型属性上的约束（必填、类型、范围、唯一、基数） | 借用 SHACL“形状”的概念，自己实现约束表；在固定视图上批量校验，违规对象可点进对象浏览器；与 AI 候选的“采纳”走同一流程（采纳时顺带生成约束）。 | 1–2 周 | 补上“结构对了，数据对不对”这一环。 |
| F5 | P2 | 血缘与影响分析 | DataHub、Apache Atlas、OpenMetadata、Ontop（映射表达） | 来源表/列 → 映射 → 属性 → 规则/函数 → 告警/决策（现有来源证据） | 把已有的映射与证据引用连成血缘图，回答“改这一列 / 这个属性会影响哪些规则、告警、决策”。 | 1–2 周 | 治理故事的核心；数据我们基本都有，缺的是视图。 |
| F6 | P2 | 本体文档导出与修订差异 | WIDOCO、OAK（diff）、LinkML（文档生成） | 任一修订的对象类型、属性、关系 | 一键导出 Markdown / HTML；与上一修订比较，列出新增、删除、改名。 | 3–5 天 | 评审、汇报、PPT 都能直接用。 |
| F7 | P3 | AI 候选去重与语义匹配 | OAK / Semsimian（相似度）、DeepOnto / LogMap（对齐） | Auto Ontology 候选 + 已有本体 | 用现成的 nomic-embed-text 嵌入加名称/属性重合度，标出“疑似重复”并建议合并；以后再扩到两个本体之间的对齐。 | 约 1 周 | 直接提升 Auto Ontology 候选质量。 |
| F8 | P3 | 基于子图的智能体问答 | GraphRAG（局部检索、社区摘要）、Graphiti（事实有效期） | 固定视图的 k 跳子图 + 证据引用 | 智能体回答前先检索相关子图和证据，再交给模型；不引入 GraphRAG 的全量索引。 | 2 周以上 | 依赖模型质量；先做检索与引用，再谈总结。 |
| F9 | P3 | 度量库 | MetricFlow | “本体查询” | 给本体查询加度量定义（口径、维度、版本），Workshop、接口、智能体共用同一口径。 | 约 2 周 | 可复用的口径统一。 |
| F10 | P3 | 文本 → 实例抽取 | OntoGPT | 检修报告、说明书等文本 | 按现有对象类型模板让模型抽取，再接地到已有对象；走人工审核。 | 3 周以上 | 依赖模型与审核流程，放最后。 |

**建议先做 F1、F2、F3**：不加新依赖，只读固定数据视图，不动写路径，一周左右就能在现有演示数据上出结果；F4–F6 是第二批；F7–F10 依赖模型质量或更多数据，放第三批。工作量是粗估，没有排期依据。

## 暂缓 / 不做

| 项目 | 原因 |
|---|---|
| OWL 推理机三件套（ELK、HermiT、Openllet） | 我们的本体是对象类型 + 链接 + 属性，不是形式化 OWL；要用就得先导出，收益只剩查类层次矛盾。Openllet 的许可证还没确认。 |
| 知识图谱嵌入（PyKEEN、mOWL） | 现在的演示数据只有几十个实例，训不出有意义的结果；等 AI4I / FactoryNet 数据装进来再试。mOWL 还要求形式化公理。 |
| RDF / JVM 栈（Jena、Ontop、n10s） | 引入会多出一套存储与运维；借鉴它们的查询、映射和校验思路即可。 |
| 整套元数据平台（Atlas、DataHub、OpenMetadata） | 运维成本远高于我们的规模；用已有的映射与证据自己画血缘图。 |
| WebVOWL、Neo4j GDS | 已有关系图；主图是 FalkorDB，不是 Neo4j。 |

## 不该照搬 / 存疑

- **效果多为项目自述**：论文和 README 里的数字来自它们自己的数据集，没有在我们的数据上复现；尤其嵌入类（PyKEEN、mOWL）在几十个实例的图上不会有意义。
- **许可证要逐个核对**：Openllet 的许可证没有确认（Pellet 原版是 AGPL/商业双许可）；LogMap 的许可证在不同年份的资料里写法不同（Apache-2.0 / LGPL-3.0）；GraphRAG 个别聚合站点没有列出许可证。以仓库的 LICENSE 文件为准。
- **商业授权边界**：Neo4j GDS 社区版有并行度和模型数量上限；MetricFlow 的服务层和 API 在 dbt 付费平台。
- **GraphRAG 的成本**：全量索引要大量大模型调用；网上流传的具体金额出处有争议，没有采用。
- **FalkorDB 的内置图算法**：我们的主图库自带一批图算法，具体清单与版本是否匹配，使用前要对照所装版本核对（本次没有核实）。

## 核实情况与来源（2026-11-09）

- Openllet：[ecosyste.ms 条目](https://awesome.ecosyste.ms/projects/github.com%2FGaligator%2Fopenllet)、[Pellet（Complexible）](https://github.com/Complexible/pellet)——许可证未能确认，需直接看仓库 LICENSE.txt。
- LogMap：[官方仓库](https://github.com/ernestojimenezruiz/logmap-matcher)、[OAEI 2025 系统论文](https://openaccess.city.ac.uk/id/eprint/37671/1/om2025-oaei-paper7.pdf)——README 标注 Apache-2.0，更早的 OAEI 论文写 LGPL-3.0。
- GraphRAG：[LazyGraphRAG 说明（微软研究院）](https://www.microsoft.com/en-us/research/?p=1106013)——索引成本约为完整 GraphRAG 的 0.1%；许可证以仓库为准。
- DataHub：[官方 FAQ](https://docs.datahub.com/docs/faq)——Apache-2.0。
- WIDOCO：[项目主页](https://dgarijo.github.io/Widoco)——codemeta 记载 Apache-2.0。
- MetricFlow：[开源公告](https://ossie.apache.org/updates/announcing-open-source-metricflow-governed-metrics-for-ai-and-agents/)、[仓库](https://github.com/dbt-labs/metricflow)——v0.209.0（2025-10-14）起改为 Apache-2.0。
- Neo4j GDS：[官方文档：简介](https://neo4j.com/docs/graph-data-science/current/introduction/)、[默认值与限制](https://neo4j.com/docs/graph-data-science/current/production-deployment/defaults-and-limits/)——社区版并行度上限 4 核、模型目录最多 3 个。

