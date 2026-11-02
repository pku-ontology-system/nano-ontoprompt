# NVIDIA：Auto Ontology 能否接入 Project 4

> 日期：2026-11-02 · 来源：[NVIDIA/auto-ontology](https://github.com/NVIDIA/auto-ontology)（commit `dd78d06`）、[NVIDIA Agent Knowledge Foundation](https://developer.nvidia.com/topic/ai/agent-knowledge-foundation/)、[NGC：auto-ontology-mcp](https://catalog.ngc.nvidia.com/orgs/nvidia/-/containers/auto-ontology-mcp/-)、[AlphaSignal 报道](https://alphasignal.ai/news/nvidia-s-auto-ontology-stops-ai-agents-from-querying-the-wrong-data)、本机接入试验 · 证据等级：官方文档（NVIDIA 仓库与文档）/ 第三方分析（媒体报道）/ 本项目推断 / 本机试验
> 原件：evidence/nvidia-auto-ontology-2026-11-02/（不进 git，见 ORIGINALS.md）

## 一句话结论

可以接，但只适合接在“数据接入 → 本体候选”这一段。本机用 7B 本地模型在 6 张表的样例上，推断出 4/6 条未声明关系（另有 1 条指错），产出可以转成编辑器的草稿建议。具体做法是：把它当作关系型数据源的候选生成器，它产出的术语、关系和指标经转换后进入我们的候选与审核流程。它替代不了我们的运行时本体（对象、关系、Action、Scenario、决策包），也不应直接连生产库或默认的云端模型。【本项目推断 + 本机试验】

## 对方怎么做的（看到了什么）

- **定位**【官方文档】：Apache-2.0 的 Python 库，从企业元数据生成“语义层/本体”，让 Agent 写的 SQL 对照经过认证的定义和血缘，并在答案里带来源。属于 NVIDIA Agent Knowledge Foundation（同组还有 Nemotron Retriever、cuOpt、cuGraph 等）。官方页面称在 NVIDIA 内部测试中把本体构建时间缩短了 90%。
- **生成方式**【官方文档，已读代码核实】：
  - 先从关系型库（Postgres、Databricks、Snowflake、DuckDB、MySQL、Trino 等）读取库、表、列、主外键和查询统计，写入自己的 Postgres，并把表、列向量化存进 pgvector。
  - 再做“语义编译”，每一步都调用 LLM：业务术语抽取（`term_extractor`）、列描述（`deterministic`）、疑似外键推断（`fk_suggester`）、语义外键匹配（`semantic_fk`：已声明外键走快速路径，否则向量检索 + LLM 选择，必要时探测样本值）。
  - 结果先作为候选，由业务专家审核认证；之后可以按 24 小时周期重新编译。
- **交换格式**【官方文档】：YAML 模型文档（`model_interchange`），分两层：
  - `data_layer`：databases / schemas / tables / columns（含样本值、唯一性）、`foreign_keys`、`joins`；
  - `semantic_layer`：`terms`（术语及其代表的表、列属性）、`semantic_fks`、`sql_attributes`（指标 SQL）、`custom_analyses`。
  - 另支持导出为 Apache Ossie 格式。
- **对外接口**【官方文档】：MCP（OAuth 登录，权限沿用登录用户、每次调用重新检查，MCP 服务本身不持有凭据）、CLI、REST；Web UI 用 Next.js + Better Auth + Prisma，后端用 FastAPI + LangChain，存储用 Postgres + pgvector；支持 Docker Compose 和 Helm。
- **模型**【官方文档，已读代码核实】：默认调用 NVIDIA NIM 托管端点（`nemotron-3-nano-30b-a3b`、`nemotron-3-embed-1b`、rerank 模型）。也可以换成 OpenAI 兼容端点，但只有模型名带 `openai/`、`azure/`、`aws/` 前缀时才走 `ChatOpenAI` 的结构化输出，其余一律走 `ChatNVIDIA`。
- **成熟度**【官方文档】：2026-04-27 创建，约 690 星，不接受外部贡献；评测在单独的 `NVIDIA/auto-ontology-eval`（BIRD 等 text-to-SQL 基准）。

## 和我们现状的差距

| 维度 | Auto Ontology | Project 4（main） |
| --- | --- | --- |
| 本体是什么 | 分析问答用的语义层：术语、列属性、指标 SQL、外键 | 运行时本体：对象、属性、关系、函数、Action、Scenario |
| 数据来源 | 关系型库 | 文件/数据集 → 管线 → 本体实例，FalkorDB 图投影 |
| 怎么生成 | LLM 编译 + 人工认证 | LLM 候选 + 映射审核（Plan A 合同中的 `source_mappings` candidate） |
| 写入 | 没有业务写入，只读问答 | 统一 Action / 决策包：预览、审批、执行、回滚 |
| 权限 | 自带 Better Auth RBAC | 自有 JWT 与对象/字段权限投影 |
| 时间与情景 | 无 | 时序、Scenario、What-if、决策推演 |

【本项目推断】

## 可以借鉴（对应模块 + 优先级）

1. **P1 · 数据接入与本体建模（PRD 阶段 2，G2）**：对 ERP/MES 这类关系型来源，把它当作候选生成器；经转换脚本导入我们的候选审核，只进候选，不自动发布。
2. **P1 · Link Mapping 候选**：外键与语义外键建议可以直接对应关系候选；已声明外键可信度高，LLM 推断的必须人工确认。
3. **P2 · 指标定义**：`sql_attributes`（认证过的指标 SQL）对应我们的版本化 Ontology Query 与受限 Derived Property。
4. **P2 · MCP（PRD 阶段 7）**：“权限沿用登录用户、每次调用重新检查、服务端不持有凭据”的代理模式，可以对照 main 已有的 MCP scopes 设计。
5. **P3 · 纠错回流**：用户认证过的修正回写语义层，持续改进候选质量。

【本项目推断】

## 不该照搬 / 存疑

- 不要嵌入它整套 Web、认证和数据库：会出现两套语义和权限，违背 PRD“同一套身份、权限、版本”。
- 默认的托管 NIM 会把表结构和样本值发到 NVIDIA 云端，违反 PRD §3.12“正式试点不得调用外部模型接口”。正式使用只能接本机模型，或在有 NVIDIA GPU 的服务器上自托管 NIM（Mac 不行，学校算力需审批）。
- 不配 Vault 时，连接凭据以明文存进它的 Postgres。
- 向量列维度写死为 2048（`nemotron-embed-1b` 的维度），换本地嵌入模型需要改造。
- “缩短 90%”只是 NVIDIA 内部测试的说法，未经独立验证。

【官方文档 + 本项目推断】

## 本机接入试验（2026-11-02）

**做法**【本机试验】：在这台 Mac 上搭了一个与演示环境隔离的试验，数据不出本机：

- **代码**：Auto Ontology `dd78d06` 原样运行，不改上游代码，只跑 Python 后端管线（catalog 导入 → 向量化 → 语义编译 → YAML 导出），不起它的 Next.js 前端。
- **存储与来源库**：独立的 pgvector Postgres 容器。来源库是工厂孪生 fixture 的关系型副本，共 6 张表：资源、设备、工单、排程项、排程项—资源桥表、排程依赖。其中只声明了“设备 → 资源”一个外键，另外 6 条关系故意不声明，用来测它能否推断。
- **模型**：本机 Ollama（OpenAI 兼容端点）提供对话模型与嵌入模型 `nomic-embed-text`。
- **运行时补丁**：上游向量列写死 2048 维，本地嵌入模型是 768 维，运行时把向量维度改为 768。这是唯一的补丁，不改上游文件。
- **对照基准**：项目里人工建好的工厂本体，即 4 个业务对象、1 条声明关系、6 条隐含关系。

**结果**：

| 指标 | qwen3.5:0.8b | qwen2.5:7b |
| --- | --- | --- |
| 业务对象覆盖（4 个） | 4/4，但两张桥表也被当成了对象 | 4/4；资源桥表没有被当成对象，依赖表仍被当成对象；设备表被命名为“Asset Maintenance And Sensor Data” |
| 声明外键 | 1/1 | 1/1 |
| 未声明关系推断 | 0/6 | 4/6 正确；另有 1 条指错目标（桥表 `item_id` 指到了依赖表），1 条漏掉 |
| 列描述（列属性） | 0/30 | 10/21 |
| 术语描述 | 只有模板句“Business entity represented by table …” | 有业务含义，如“制造过程中使用的资源和能力”；1 条为空 |
| 指标 SQL / 桥表识别 | 0 / 0 | 0 / 0 |
| 语义编译耗时（6 张表） | 2204 秒 | 159 秒 |
| 转换后的草稿建议 | 6 个对象类型 + 1 条关系 | 5 个对象类型 + 6 条关系 |

**接入方式验证**：写了一个转换脚本，把导出的 YAML 转成 main 已有的“模型建议”草稿格式，即 `POST /model-suggestions` 返回的 `entity_type` / `relationship` 草稿操作（`status=draft_only`）。每条都带来源表/列、样本值、生成工具与模型，并标为未审核。这些草稿交给本体编辑器人工审核，再通过 `semantic-changes` 发布。试验中没有自动写入任何本体。

**观察**【本机试验】：

- 管线可以完全在本机跑通，数据不出站；没有前端时，它读取前端配置表会报错，但会降级继续。
- 质量取决于模型：0.8B 微型模型只产出确定性部分（表 → 术语命名、声明外键），依赖 LLM 的产出全为空，而且因“思考”输出拖到 37 分钟；7B 本地模型在 159 秒内推断出大部分关系、写出可读的描述，但仍有错（1 条关系指错表、命名不自然、一半列没有描述、没有指标 SQL）。这说明它适合当“候选生成器”，产出必须人工审核，正好对应 PRD 里“模型只给候选、人工审核后发布”的要求。
- 结构化输出依赖模型名前缀：本地模型必须以 `openai/…` 别名暴露，否则会走 NVIDIA 专用客户端。

## 后续怎么做

1. **先定结论（ADR）**：建议定为“借鉴设计 + 作为可选的候选来源”，不整体接入。理由见上文差距表和风险；需要两位开发者共同签收。【本项目推断】
2. **如果要继续评估**：
   - 换更强的本地模型，或在有 NVIDIA GPU 的服务器上自托管 NIM 复测；
   - 样本扩大到 UCI AI4I 2020、DataCo 订单表；
   - 按 PRD §3.12 的指标对照人工参考本体打分（对象/属性/关系的精确率与召回率、无依据候选比例、人工修改条数与用时、耗时、出站记录），并与 main 现有 `/model-suggestions` 的本地模型路径对比。
3. **如果决定采用**：
   - 把试验里的转换逻辑做成后端适配器：只读调用 Auto Ontology 的导出接口，或读取 YAML 文件；
   - 产出走现有链路：`/model-suggestions` 草稿 → 编辑器人工审核 → `semantic-changes` 发布；
   - 每条建议保留来源表/列、样本值和生成模型；
   - 关系建议要等对象类型创建后，再解析成 resource id。
4. **安全边界**：
   - 只用只读账号连接来源库；
   - 非公开数据不走默认云端端点；
   - 凭据放 Vault；
   - 不嵌入它的 Web 和认证。
5. **试验产物**：脚本、两次运行导出的 YAML、评分结果和草稿建议 JSON，都在本机 `external/ao-spike/`，不在仓库内；是否入库另行决定。
