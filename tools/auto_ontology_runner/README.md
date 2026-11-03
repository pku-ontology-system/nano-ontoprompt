# Auto Ontology 本机运行器

让平台在“本体候选”页一键运行 [NVIDIA Auto Ontology](https://github.com/NVIDIA/auto-ontology)（Apache-2.0），
读取一个已登记的关系库，生成术语、列属性和外键候选。运行器只暴露一个很小的 HTTP 接口；平台拿不到来源库的连接串或模型密钥。

```
平台后端 ──HTTP（bearer token）──► 运行器 ──子进程──► pipeline.py（Auto Ontology 自带 venv）
                                      │                    ├─ 来源库（只读）
                                      │                    ├─ 每次运行一个新建的存储库（pgvector），结束即删
                                      │                    ├─ 向量化：本机 Ollama（nomic-embed-text）
                                      │                    └─ 对话模型：LiteLLM 网关 / 本机 Ollama
```

## 准备

1. Auto Ontology 代码（本项目固定在 commit `dd78d06`）：`git clone https://github.com/NVIDIA/auto-ontology`，在其目录里 `uv sync`，得到 `.venv`。
2. 一个带 pgvector 的 Postgres 作存储（运行器会为每次运行新建、删除一个数据库）：`pgvector/pgvector:pg17`。
3. Ollama 里要有向量模型：`ollama pull nomic-embed-text`（768 维）。本机模型还要把对话模型复制成带 `openai/` 前缀的别名，
   例如 `ollama cp qwen2.5:7b openai/qwen2.5:7b`——Auto Ontology 只在模型名带 `openai/` 前缀时才走 OpenAI 兼容客户端。
4. 云端模型走仓库根目录的 LiteLLM 网关（`litellm_config.yaml` 里的 `openai/MiniMax-M3`，密钥 `MINIMAX_API_KEY` 只放在网关里）。

## 配置

- `config.example.json` 复制成 `config.json`：写 Auto Ontology 目录、存储库地址、来源库和模型档案。
- `.env.local`（不进 git，权限 600）放密钥，变量名在 `config.json` 里引用：
  - `AUTO_ONTOLOGY_RUNNER_TOKEN`：平台调用运行器用的 token，16 位以上；
  - `LITELLM_API_KEY`：网关的 key；
  - `AO_STORE_PASSWORD`：存储库密码；
  - `AO_SOURCE_FACTORY_TWIN`：来源库连接串（建议只读账号）。
- 模型档案的 `locality` 为 `cloud` 时，平台会提示将发送的内容并要求确认；来源库的 `privacy_level` 为 `private` 时，运行器会拒绝云端模型。
- 模型档案的 `structured_output: function_calling`：有的云端模型在长提示下不遵守 `response_format`（会输出散文或多余字段），
  改用工具调用后遵守得更好。Auto Ontology 只按模型名（Claude 系）选择工具调用，所以由 `pipeline.py` 在运行时指定，不改上游文件。

## 启动

```bash
<auto-ontology>/.venv/bin/python -I tools/auto_ontology_runner/runner.py \
  --config tools/auto_ontology_runner/config.json --env tools/auto_ontology_runner/.env.local
```

默认监听 `127.0.0.1:28765`。容器里的平台后端通过 `http://host.docker.internal:28765` 访问，需要把同一个 token 设为 `AUTO_ONTOLOGY_RUNNER_TOKEN`。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | Auto Ontology 版本、存储库和向量模型是否可用、当前运行 |
| GET | `/models` | 模型档案（本机 / 云端、是否可用、将发送的内容） |
| GET | `/sources` | 来源库（名称、隐私级别、表和列名；不含连接串） |
| POST | `/runs` | `{source_id, model_id}` 启动一次运行；同一时间只允许一次（409） |
| GET | `/runs/{id}` | 状态、各阶段耗时、表进度、模型调用次数、日志末尾 |
| GET | `/runs/{id}/export` | 完成后的 YAML 导出和运行信息 |
| POST | `/runs/{id}:cancel` | 取消 |

所有接口都需要 `Authorization: Bearer <token>`。

## 边界

- 日志写入前去掉连接串口令、Bearer 令牌和长随机串。
- 运行器重启时，未完成的运行记为失败。
- 默认超时 30 分钟；`runs/` 只保留最近 30 次。
- 上游的向量列写死 2048 维，运行时改成向量模型的维度（768）；这是唯一的补丁，不改 Auto Ontology 的文件。
