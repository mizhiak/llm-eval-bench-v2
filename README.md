# llm-eval-bench-v2

基于 evalscope 的大模型精度、性能、上下文综合测评台。项目提供一个 FastAPI + 原生前端的 Web UI，用来接入 OpenAI 兼容接口、vLLM、Ollama 等模型服务，并完成精度评测、并发压测、上下文长度扫描、历史任务管理和报告导出。

![精度结果](docs/images/accuracy-results.png)

## 功能亮点

- 模型接入连通性测试：支持 Base URL、API Key、模型名、接口格式、超时和思考模式开关，兼容 OpenAI/vLLM/Ollama/Anthropic 风格响应。
- 被测服务溯源：任务启动时自动关联"被测地址 → 服务容器名/镜像"（配合宿主机定时导出的 docker 端口映射），并抓取目标 `/v1/models` 模型列表；也可手动填写服务标识。历史任务展示完整测试参数（并发、超时、提示词、温度、流式等）。
- 精度评测：对接 evalscope 内置 benchmark，支持数据集选择、学科/子集过滤、few-shot、抽样、自定义 JSONL 数据集。
- 性能压测：支持多并发档位扫描、请求数配置、倍率扩展、流式/非流式、max_tokens/min_tokens、长上下文填充。
- 上下文扫描：按指定上下文长度档位测试吞吐、延迟、成功率等指标。
- 实时任务流：通过 SSE 广播展示队列、阶段、日志、进度和结果（多标签页可同时观看同一任务，断线自动重连）。
- 历史任务：支持查看、续跑、重命名、删除、导出 Excel/PDF、下载 evalscope 原始报告。
- 回收站：删除的任务进入回收站（默认永久保留，可追溯），支持 5 秒撤销、随时恢复或彻底删除。
- 任务对比：最多 4 个任务的精度/性能横向对比矩阵（成绩单页）。
- 结果分析：聚合准确率、学科得分、类别得分、RPS、tok/s、TPOT、ITL、TTFT、逐请求明细，并标记短输出/空响应导致的静默退化。
- 可选鉴权：设置 `EVALBENCH_AUTH_TOKEN` 后所有 API 要求携带令牌（Bearer / X-Auth-Token / ?token= / Cookie，前端自动适配）。
- 离线部署辅助：提供数据集预取、转换和离线构建脚本，方便把 C-Eval、GSM8K 等数据集打包到镜像或缓存中。

## 最新更新

- **UI 全新浅色配色**：中性灰白 + 靛蓝强调 + 青绿数据色 + 墨色主按钮，图表同步换色。
- **安全加固**：SSRF 防护（代理探测端点校验目标地址，默认拦截链路本地/云元数据网段）、可选鉴权中间件、API Key 落盘脱敏、路径穿越校验、`context_lengths` 上限防 OOM。
- **健壮性**：SSE 广播模式（多客户端事件不再瓜分）、断线自动重连、重启级联取消 evalscope 孤儿任务、删除运行中任务防"复活"、evalscope 状态判定修正（completed/success）、0 分结果不再被误判缺失。
- **性能**：逐请求明细的 ITL 改为后端聚合统计（响应从数 MB 降到 KB 级）、日志上限、数据集搜索防抖、预检连通性探测移至线程池。
- **体验**：回收站视图、5 秒撤销删除、图表数据点 tooltip、任务完成自动切结果页签、历史任务查看横幅、label 无障碍补全。
- 新增 `scripts/` 离线辅助脚本：`prepare_datasets.py`、`prefetch_datasets.py`、`build_offline.sh`、`start.sh`。
- 增强模型客户端：补充 Anthropic SSE/usage 解析、`x-api-key` header、流式收集和 TTFT 统计。
- 增强性能结果页：新增 TTFT 曲线、Token 吞吐曲线、退化行高亮、逐请求 prompt 摘要。
- 增强 evalscope 数据集目录：更稳健地读取 benchmark 元数据和子集信息。
- 增强报告导出：预留 `app/fonts/` 字体目录，便于 PDF 中文字体渲染。

## 界面预览

### 历史任务

![历史任务](docs/images/history-panel.png)

### 性能摘要

![性能摘要](docs/images/performance-summary.png)

### 性能明细

![性能明细](docs/images/performance-details.png)

## 项目结构

```text
.
├── app/
│   ├── main.py                # FastAPI 入口、REST API、SSE、静态文件挂载、鉴权/SSRF 防护
│   ├── task_manager.py        # 任务生命周期、队列调度、历史任务持久化、回收站
│   ├── evalscope_client.py    # evalscope service HTTP 客户端
│   ├── evalscope_runner.py    # 精度、性能、上下文扫描编排
│   ├── model_client.py        # OpenAI/vLLM/Ollama 模型调用封装
│   ├── serving.py             # 被测服务溯源（地址→容器名映射、模型列表抓取）
│   ├── custom_dataset.py      # 自定义数据集转换
│   ├── evalscope_catalog.py   # benchmark 目录和子集缓存
│   ├── report.py              # Excel/PDF 报告导出
│   └── fonts/                 # PDF 中文字体预留目录
├── static/
│   ├── index.html             # 单页 UI
│   ├── app.js                 # 前端交互和结果渲染
│   └── style.css              # 浅色测评台样式（设计 token 体系）
├── scripts/
│   ├── build_offline.sh       # 离线镜像/缓存构建辅助
│   ├── prefetch_datasets.py   # 触发 evalscope 数据集缓存
│   ├── prepare_datasets.py    # 下载并转换 C-Eval/GSM8K 等数据
│   ├── docker_services_dump.sh# 宿主机端口→容器映射导出（被测服务溯源用，cron 每分钟）
│   └── start.sh               # 脚本版启动入口
├── docs/images/               # README 截图
├── requirements.txt
└── start.sh                   # 同时启动 evalscope service 和 Web 应用
```

## 快速启动

安装依赖：

```bash
pip install -r requirements.txt
```

启动 evalscope service 和 Web 应用：

```bash
chmod +x start.sh
./start.sh
```

应用默认监听：

- Web/API：`http://0.0.0.0:8000`
- evalscope service：`http://127.0.0.1:9000`

如果部署在 Docker 或服务器网关后，可以把宿主机端口映射到 `8002:8000`，然后通过 `http://<host>:8002` 访问。

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `EVALBENCH_AUTH_TOKEN` | 空（不启用） | 设置后所有 `/api` 请求需携带该令牌；支持 `Authorization: Bearer`、`X-Auth-Token`、`?token=`、Cookie 四种方式，前端首次访问 `http://host/?token=xxx` 后自动携带 |
| `BLOCK_PRIVATE_NETWORKS` | `0` | 设为 `1` 时，连通性探测/模型列表代理额外拦截 RFC1918 私网与回环地址（链路本地/云元数据地址始终拦截） |
| `OUTPUTS_DIR` | `/app/outputs` | evalscope 输出根目录（`docker_services.json` 溯源映射也在此目录） |
| `TRASH_RETENTION_DAYS` | `0` | 回收站自动清理天数；`0` = 永久保留（默认，保证评测历史可追溯） |
| `EVALSCOPE_EVAL_TIMEOUT` | `7200` | 精度评测整体 HTTP 超时（秒） |
| `EVALSCOPE_URL` | `http://127.0.0.1:9000` | evalscope service 地址 |
| `MAX_UPLOAD_BYTES` | `52428800` | 自定义数据集上传大小上限（字节） |
| `MODELSCOPE_CACHE` | `/opt/modelscope_cache` | 数据集缓存目录（离线部署） |

### 被测服务溯源（容器名自动识别）

把 `scripts/docker_services_dump.sh` 部署到宿主机（cron 每分钟执行一次），它会解析 `docker ps` 的端口映射并导出为 `$OUTPUTS_DIR/docker_services.json`。应用会自动把任务填写的 `Base URL` 端口关联到运行被测模型的容器名/镜像，并在任务日志、历史任务详情中展示；同时抓取目标 `/v1/models` 模型列表一并落库。不部署该脚本也不影响评测，仅溯源信息缺失。

## 基本用法

1. 在左侧填写模型服务 `Base URL`、`API Key`、模型名称和接口格式。
2. 点击“测试连通性”，确认模型接口可用。
3. 选择精度评测数据集，或开启性能压测、上下文扫描。
4. 点击“开始前预检”，检查 evalscope、数据集缓存、请求规模和潜在风险。
5. 点击“开始测评”，在实时进程和结果页查看进度。
6. 完成后可在历史任务中导出 Excel、PDF 或 evalscope 原始报告。

## 运行数据

运行时数据默认写入：

- `data/tasks/`：任务配置、状态和历史记录；删除的任务以 `*.json.trash` 保留在回收站
- `data/reports/`：导出的 Excel/PDF
- `custom_datasets/`：上传的自定义数据集
- `/app/outputs/`：evalscope 原始输出目录（含 `docker_services.json` 溯源映射）

这些目录通常不建议提交到 Git，仓库已在 `.gitignore` 中忽略常见运行产物、数据库、JSONL 和环境文件。

## 自定义数据集

前端支持上传 JSONL 数据集。后端会读取并转换为 evalscope 可消费的格式，适合快速验证私有题库、业务问答或多选题集合。

## 离线数据准备

新版提供了 `scripts/` 辅助脚本，用于提前准备或缓存数据集：

```bash
python scripts/prepare_datasets.py --only ceval
python scripts/prepare_datasets.py --only gsm8k
python scripts/prefetch_datasets.py
```

`prepare_datasets.py` 会把下载到的数据转换成本工具可读取的 JSONL 格式；`prefetch_datasets.py` 会通过 evalscope mock 任务触发缓存，适合离线或内网环境部署前使用。

## 说明

本项目侧重内部测评工作台场景，适合在已有模型服务和 evalscope 运行环境中部署。大规模压测前建议先小样本试跑，确认超时、并发、token 预算和数据集缓存状态。
