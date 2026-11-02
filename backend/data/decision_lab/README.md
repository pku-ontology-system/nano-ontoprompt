# 决策分析演示切片

此目录仅保存小样例和来源清单，不保存三套 benchmark 的完整数据。`manifest.json` 固定了上游文件哈希、选样规则、切片哈希、许可信息与局限。使用前应先通过原有数据目录安装 FactoryNet CNC 和 I-BADAS 的固定来源，再运行：

```bash
docker compose exec -T backend python -m scripts.install_decision_lab_examples
```

需要从上游重新生成样例时运行：

```bash
docker compose exec -T backend python -m scripts.prepare_decision_lab_examples
docker compose exec -T backend python -m scripts.install_decision_lab_examples
```

准备脚本只临时下载并校验 UCI AI4I 的 522,170 字节官方压缩包，留下 29 条分层样例；从已安装的 FactoryNet `cnc_000.parquet` 取 4 个 episode 共 256 点；从已安装 I-BADAS 的 12 组样本提取媒体角色、尺寸与原始路径，不复制媒体文件。安装脚本在数据库中建立三个独立本体、来源证据和九个预置计算 Run，重复执行不应新增同名对象或预置 Run。

来源：[UCI AI4I 2020](https://archive.ics.uci.edu/dataset/601/ai4i%2B2020%2B)（CC BY 4.0）、[FactoryNet](https://huggingface.co/datasets/Forgis/FactoryNet) CNC 子集（沿用 [原始 UMich CNC 来源](https://www.kaggle.com/datasets/shasun/tool-wear-detection-in-cnc-mill)条款，机构条款待核验）、[I-BADAS](https://huggingface.co/datasets/moiaraya/i-badas)（CC BY-NC 4.0）。AI4I 故障标签和 I-BADAS 异常标注仅用于固定切片的回看，不代表可外推的模型评估；FactoryNet 的采样点并非连续事件窗口。
