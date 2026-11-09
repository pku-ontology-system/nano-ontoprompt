# 对照：Palantir 的功能，和做过类似功能的开源项目

> 日期：2026-11-09 · 来源：GitHub 仓库元数据（2026-11-09 用 GitHub API 查的许可证、星标、最近更新、演示数据文件大小）、各项目官方文档、联网检索（聚合站的说法已单独标注）· 证据等级：第三方开源项目 + 本项目推断
> 起因：要挑 3 个“照开源项目抄”的模块，用项目自带的演示数据展示。Palantir 应用名（Data Lineage、Data Health、Machinery 等）和“对应关系”是本项目的归纳，这次没有逐页核对 Palantir 官方文档。
> 前一份调研：[本体构建后的分析（30 个项目）](OSS_ONTOLOGY_ANALYSIS_SURVEY_2026_11_09.md)；Palantir 官方资料见 [../official/](../official/)。

## 一句话结论

可以直接抄、又有自带演示数据的有三类：**数据血缘（Marquez + OpenLineage，Apache-2.0）、实体消歧（Splink，MIT）、数据健康（Great Expectations，Apache-2.0；或 Pandera，MIT）**，我们现在都没有。最贴近“本体里的对象和事件”的是对象中心流程挖掘（Palantir Machinery 一类），但 PM4Py 是 AGPL-3.0、OCPA 是 GPL-3.0，**代码不能直接抄**，只能按 OCEL 2.0 标准自己写。

## 候选（按推荐度排）

| Palantir 里的功能 | 开源项目 | 许可证 · 星标 · 最近更新 | 做什么，自带什么演示数据 | 我们现状 | 能不能直接抄 |
|---|---|---|---|---|---|
| 数据血缘（Data Lineage） | [OpenLineage](https://github.com/OpenLineage/OpenLineage)（标准）+ [Marquez](https://github.com/MarquezProject/marquez)（参考实现与界面） | Apache-2.0 · 2.8k / 2.3k★ · 11-09 / 11-04 | 统一的血缘事件（作业、数据集、运行）和血缘图界面；Marquez 的 `seed` 命令载入 `docker/metadata.template.json`（73 KB，26 条 OpenLineage 事件、13 个作业、13 个数据集，一家虚构外卖公司的血缘）；OpenLineage 的事件 JSON Schema 在 `spec/` | 没有血缘视图；来源证据和映射都有，但没连成图 | **能**：事件模型和图界面都可抄；顺带支持“改这一列会影响谁”的影响分析 |
| 实体消歧（对象归并、去重） | [Splink](https://github.com/moj-analytical-services/splink) | MIT · 2.6k★ · 11-09 | 概率式记录匹配（Fellegi–Sunter + EM），SQL 后端（DuckDB）。自带演示数据：`fake_1000.csv`（56 KB，1000 行，250 个虚构人物，另有 99 KB 人工标注）、`historical_figures_with_errors_50k.parquet`（1.6 MB，5 万行带错误的历史人物，来自 Wikidata） | 没有；Auto Ontology 候选只按名称判重 | **能**：MIT；可以当库用，也可以只抄原理自己实现。备选：[dedupe](https://github.com/dedupeio/dedupe)（MIT，4.5k★，2025-07 后没更新）、[FollowTheMoney](https://github.com/opensanctions/followthemoney) + [nomenklatura](https://github.com/opensanctions/nomenklatura)（MIT，调查类实体数据模型和跨来源归并） |
| 数据健康（Data Health） | [Great Expectations](https://github.com/great-expectations/great_expectations)（GitHub 现在重定向到 fivetran/great_expectations）；[Pandera](https://github.com/unionai-oss/pandera) | Apache-2.0 · 11.9k★ · 11-09；MIT · 4.5k★ · 11-06 | 用声明式“期望”检查数据（非空、唯一、取值范围、正则、引用完整性等，第三方列表称 300 多种），产出校验报告。演示数据：仓库测试集里有纽约出租车样例（如 `yellow_tripdata_sample_2018-01.csv`，约 0.9 MB，按月份分文件）和 `Titanic.csv` | 质量审查是大模型逐步审查，没有可重复的规则库 | **能**：期望库和报告结构可抄。Soda Core 的许可证 GitHub 没识别出来（检索结果称 Apache-2.0），先不选 |
| 流程挖掘（Machinery，基于对象和事件） | [PM4Py](https://github.com/process-intelligence-solutions/pm4py)、[OCPA](https://github.com/ocpm/ocpa)；标准是 OCEL 2.0 | **AGPL-3.0** · 1.1k★；**GPL-3.0** · 49★（2025-05 后没更新） | 从“对象 + 事件”日志里挖流程图、瓶颈、耗时。OCEL 2.0 标准论文 CC BY 4.0；演示日志在 Zenodo（订单到现金、采购到付款、医院等，各条记录的许可证没核实） | 有“运营流程模型”（状态机），没有从事件日志挖流程 | **代码不能抄**（AGPL/GPL 会传染）；按 OCEL 2.0 规范自己写，数据可以用 |
| 本体导出到标准（OWL / SHACL） | [foundry-ontology-open](https://github.com/cloudbadal007/foundry-ontology-open) | MIT · 116★ · 2026-07-19 | 用 Python 复刻 Foundry 的三层本体（语义 / 动力 / 动态），外加 OWL、SHACL 导出和 MCP 服务；自带 `energy_asset_maintenance` 示例（以上是项目自述） | 语义层、动力层都有；缺 OWL / SHACL 导出——导出后才能用 ROBOT、pySHACL 这类标准工具检查我们自己的本体 | **能**：MIT；项目很小，代码质量要自己审 |
| 异常检测 / 预测性维护（Quiver 时序、ML） | [PyOD](https://github.com/yzhao062/pyod) | BSD-2-Clause · 10k★ · 10-28 | 数十种异常检测算法；用 NASA C-MAPSS（已在本机，不用下载）演示 | 传感闭环只有阈值规则 | **能**；要新增依赖。Merlion 已归档（2026-03），不选 |
| 分析看板（Contour） | [Apache Superset](https://github.com/apache/superset) | Apache-2.0 · 75k★ · 11-09 | 自带示例数据集的数据探索平台 | 对象浏览器有图表 | 体量太大，不适合抄，只借鉴交互 |
| 应用搭建（Workshop） | [Appsmith](https://github.com/appsmithorg/appsmith) | Apache-2.0 · 41k★ · 11-09 | 内部工具搭建平台 | 已有 Workshop | 不抄 |
| 数字孪生（Vertex） | [Eclipse Ditto](https://github.com/eclipse-ditto/ditto) | EPL-2.0 · 947★ · 11-09 | 设备孪生的状态同步与消息，不是推演引擎 | 已有情景研究 | 不适合 |

## 许可证提醒

- **AGPL / GPL（PM4Py、OCPA）**：把代码抄进来，我们的代码也要按同样许可证公开。只能读规范（OCEL 2.0）后自己实现。
- **MIT / Apache-2.0 / BSD**：可以抄，保留版权声明和许可证文本；Apache-2.0 还要保留 NOTICE。抄进来的部分放进项目的第三方声明，界面上注明出处。
- **演示数据的许可证要单独看**：Splink 的演示数据在单独的仓库里，仓库有 LICENSE（我没读内容）；GO、OCEL 等数据多为 CC BY 4.0，需署名。

## 没选的整套“开源 Palantir”

- **OpenFoundry（DioCrafts）**：聚合站称 AGPL-3.0、约 231★，但这次在 GitHub 上查不到这个仓库（404），不当作抄的对象。
- **Marsovius**：只找到官网，没有仓库、许可证和使用证据。
- **[jupyter-naas/abi](https://github.com/jupyter-naas/abi)**：MIT，177★，今天仍在更新，自称 “Open AI Operating System”；内容我没细看，需要时再单独评估。

## 存疑

- 星标和更新日期是 2026-11-09 当天的快照。
- “哪个功能对应 Palantir 的哪个应用”是本项目的归纳，不是官方说法。
- 各项目的演示数据这次只查了文件名和大小，没有下载、没有打开；下载前要单独征得同意。
- 更正（2026-11-09 晚）：上一版写成“Marquez 的 seed 是程序生成的”是错的。`SeedCommand.java` 只是把一个事件 JSON 文件发给服务，演示数据就是 `docker/metadata.template.json`。
