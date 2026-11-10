# 分析功能的演示数据

数据健康、实体消歧、血缘三个功能各自照一个开源项目做，演示数据就取自那个项目自带的演示数据。文件是 2026-11-09 经用户同意后从下面的地址下载的，没有改过；程序加载时核对 SHA-256，对不上就拒绝使用（`app/services/v2/analysis/demos.py`）。

| 文件 | 来自 | 许可证 | 大小 | SHA-256 | 用在 |
|---|---|---|---|---|---|
| `fake_1000.csv` | Splink 的演示数据仓库 [splink_datasets](https://github.com/moj-analytical-services/splink_datasets) `data/fake_1000.csv` | MIT，见 `LICENSE.splink-datasets` | 55,983 B | `66f9b8f9…2e2a` | 实体消歧默认演示；流水线演示 |
| `historical_figures_with_errors_50k.parquet` | 同一仓库 `data/historical_figures_with_errors_50k.parquet` | MIT | 1,633,737 B | `b8b74840…ce43` | 实体消歧效果最好的演示（Splink 官方用它展示去重） |
| `yellow_tripdata_sample_2019-01.csv` | Great Expectations 教程仓库 [gx_tutorials](https://github.com/great-expectations/gx_tutorials) `data/yellow_tripdata_sample_2019-01.csv` | 仓库 Apache-2.0，见 `LICENSE.gx-tutorials`；数据是纽约市 TLC 的公开出租车行程数据 | 954,456 B | `8e9d0d31…df18` | 数据健康默认演示 |
| `marquez_metadata.template.json` | Marquez [`docker/metadata.template.json`](https://github.com/MarquezProject/marquez/blob/main/docker/metadata.template.json)，`./docker/up.sh --seed` 用的就是它 | Apache-2.0，见 `LICENSE.marquez` | 73,559 B | `f40f076f…0073` | 血缘默认演示（26 条 OpenLineage 事件、13 个作业、13 个数据集） |

完整 SHA-256 写在 `demos.py` 的 `CATALOG` 里。Marquez 的文件里有 `{{RUN_START_TIME}}`、`{{RUN_END_TIME_AFTER_n_MINUTES}}` 占位符，载入时按它自己的 `seed.sh` 的做法替换成时间（这里取“12 分钟前开始”，让图上的运行时间落在最近）。

## 三个功能和这些项目的关系

- **数据健康**照 [Great Expectations](https://github.com/great-expectations/great_expectations)：期望的名字、`mostly` 容差、结果的字段都按它的文档，只重写了 pandas 上的执行部分；没有拷贝它的源码。
- **实体消歧**照 [Splink](https://github.com/moj-analytical-services/splink)：Fellegi–Sunter 模型，u 用随机配对估计、先验用确定性规则加假定召回率估计、m 用 EM 估计，加词频校正；底层同样用 DuckDB。没有拷贝它的源码。
- **血缘**照 [OpenLineage](https://github.com/OpenLineage/OpenLineage) 和 [Marquez](https://github.com/MarquezProject/marquez)：接收 OpenLineage 运行事件，由事件推出作业和数据集的图、状态、模式和数据质量。没有拷贝它们的源码；演示数据就是 Marquez 的 seed 文件本身。

## 效果（测试里固定的数字）

- `fake_1000`：精确率 ≥ 0.99，召回率 ≥ 0.88。
- 5 万条历史人物：精确率 ≥ 0.93，召回率 ≥ 0.78，最大的一簇不超过 100 条；耗时约 11 秒。
- 出租车样例：23 条期望里 8 条不通过，每条的“不符合的行数”都固定在测试里（`backend/tests/test_analysis_demos.py`）。

这些数字是这套重写在这些数据上跑出来的，不是 Splink 或 Great Expectations 官方的结果；没有拿原项目对拍过。

## 在平台本体上分析

这三份文件只是“开源项目的示例文件”模式的数据。要看分析在平台自己的本体上怎么做，到“数据源”页导入五份不同来源、不同类型的演示数据集（TPC-H 供应链、纽约出租车事件流、Brick 楼宇时序、Abt-Buy 商品合并、豆叶病害照片），或者导入自己的文件；导入后的本体在数据健康、实体消歧、数据血缘、流水线演示里直接可选。数据文件在 `backend/examples/data_import/`，来源和许可见该目录的 `LICENSES.md`；做法见 [ADR 0024](../../../docs/adr/0024-generic-data-import.md)。
