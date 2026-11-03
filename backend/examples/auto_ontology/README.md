# Auto Ontology 导出样例

`factory_twin.*.yaml` 是 NVIDIA Auto Ontology（commit `dd78d06`）对工厂排程关系库（6 张表，只声明 1 个外键）的真实导出，
对应的 `*.run.json` 是运行信息。来源库由本项目的工厂孪生 fixture 生成，不含外部数据。

- `factory_twin.minimax-m3.yaml`：MiniMax-M3（经 LiteLLM 网关，工具调用），70 秒；识别 4 个对象、声明外键，并推断出 6 条隐含关系中的 5 条，另有 1 条错误推断。
- `factory_twin.qwen2.5-7b.yaml`：本机 Qwen2.5 7B，159 秒；推断出 4/6，另有 1 条指错目标。

平台的“AI 本体候选”页可以直接导入它们，在没有运行器时也能演示审阅和发布。
