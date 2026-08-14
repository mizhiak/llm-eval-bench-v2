"""基于 evalscope 的评测运行逻辑。

evalscope service 的 API 是**同步阻塞**的——一次调用跑完整个评测才返回。
我们的策略：
- 生成 task_id 传给 evalscope，后台线程调 /invoke 阻塞等待
- 主线程通过 evalscope 的 /progress + /log 端点轮询真实进度
- 支持通过 evalscope 的 /stop 端点中途取消

这样职责清晰：evalscope 负责"测得准/专业"，我们负责"调度/可视化/留存"。
"""
import time
import uuid
import json
import os
import threading
import tempfile

from app import evalscope_client as es
# Realistic Chinese seed for perf test prompts. Uses diverse vocabulary and
# sentence structures to produce realistic token distributions.
_REALISTIC_PROMPT_SEED = (
    "人工智能技术近年来取得了突破性进展，深度学习模型在自然语言处理、计算机视觉、语音识别等领域展现出强大的能力。"
    "从早期的感知机到如今的Transformer架构，神经网络的发展经历了多次范式转变。大规模预训练模型如GPT、BERT、LLaMA等，"
    "通过在海量文本数据上进行自监督学习，获得了丰富的语言理解和生成能力。这些模型不仅在学术界引起了广泛关注，"
    "也在工业界得到了广泛应用，包括智能客服、代码生成、内容创作、医疗诊断辅助等多个场景。"
    "在实际部署中，模型推理性能成为关键瓶颈。大语言模型的参数量动辄数十亿甚至数千亿，单次推理需要消耗大量的计算资源和内存带宽。"
    "为了提升服务吞吐量，业界提出了多种优化方案：KV缓存技术通过缓存已计算的键值对来避免重复计算；"
    "连续批处理策略允许将多个请求动态合并处理；量化技术通过降低数值精度来减少计算和存储开销；"
    "投机采样利用小模型预测大模型输出以加速生成；FlashAttention通过优化内存访问模式显著提升注意力计算效率。"
    "在分布式推理方面，张量并行、流水线并行、数据并行等策略各有优劣。张量并行将单个Transformer层的参数切分到多张GPU上，"
    "通信开销较大但延迟低；流水线并行将不同层分配到不同设备，适合超大规模模型但可能产生流水线气泡；"
    "数据并行则通过复制模型实例来处理更多并发请求，实现简单的水平扩展。实际系统往往综合运用多种并行策略。"
    "服务系统的调度策略也至关重要。请求到达率、服务时间分布、排队策略等因素共同决定了系统的整体表现。"
    "常见的调度算法包括先到先服务、最短作业优先、轮询调度等。对于大模型推理服务，还需要考虑抢占式调度、"
    "优先级队列等机制来平衡延迟和吞吐。当并发请求数超过系统容量时，排队延迟会急剧增长，需要通过限流、降级等策略来保障服务质量。"
    "在工程实践中，监控和可观测性同样不可或缺。需要采集请求延迟分布、吞吐量、错误率、GPU利用率、显存占用等关键指标，"
    "通过仪表盘和告警规则及时发现性能瓶颈和异常情况。分布式追踪技术可以帮助分析请求在微服务调用链中的耗时分布。"
    "性能测试是评估系统能力的重要手段。通过构造不同并发量、不同输入输出长度的负载场景，可以绘制系统的吞吐-延迟曲线，"
    "找到最佳工作点和性能拐点。压力测试可以验证系统在极限负载下的稳定性。长稳测试则关注系统长期运行的内存泄漏和性能衰减问题。"
    "容量规划需要综合考虑业务增长预期、硬件成本、能效比等因素，确定合适的基础设施规模和扩展策略。"
    "除了性能之外，模型的安全性、公平性、可解释性等问题也日益受到重视。红队测试、对抗攻击检测、偏见评估等措施"
    "有助于构建负责任的人工智能系统。随着各国AI监管法规的陆续出台，合规性也成为企业部署AI系统时必须考虑的因素。"
    "未来，随着模型架构的持续演进和硬件性能的不断提升，大模型推理服务的效率还将进一步提高。稀疏注意力、混合专家模型、"
    "状态空间模型等新技术路线正在积极探索中，有望在保持模型质量的同时大幅降低推理成本。"
    "在数据库领域，关系型数据库如MySQL和PostgreSQL依然占据主导地位，但NoSQL数据库如MongoDB、Redis、Cassandra等"
    "也在各自的适用场景中发挥着重要作用。图数据库如Neo4j适合处理社交网络和推荐系统等图结构数据。"
    "时序数据库如InfluxDB和TimescaleDB专为物联网监控和金融交易等时间序列场景优化。"
    "分布式系统设计中的CAP定理指出，一致性、可用性和分区容错性三者最多只能同时满足两个。"
    "在实际工程中，BASE理论（基本可用、软状态、最终一致性）为很多互联网系统提供了更实用的设计指导。"
    "微服务架构虽然带来了独立部署和团队自治的好处，但也引入了服务发现、负载均衡、熔断降级、分布式事务等新的复杂性。"
    "容器化技术如Docker和编排系统如Kubernetes已经成为云原生应用的标准基础设施。"
    "网络协议方面，HTTP/3基于QUIC协议，通过减少连接建立时间和改进丢包恢复机制，显著提升了网页加载速度。"
    "WebSocket协议为实时通信场景提供了全双工通道，而gRPC则以其高效的二进制序列化和流式传输能力在微服务通信中广泛使用。"
    "前端开发领域，React、Vue、Angular三大框架各有拥趸。React的虚拟DOM和函数式编程范式深刻影响了现代前端开发方式。"
    "TypeScript通过引入静态类型检查，大幅提升了大型前端项目的可维护性和开发效率。"
    "编程语言方面，Rust以其零成本抽象和内存安全保证在系统编程领域迅速崛起，被用于构建操作系统、浏览器引擎、区块链等底层软件。"
    "Go语言以其简洁的语法、高效的并发模型和快速的编译速度，成为云原生基础设施开发的主流选择。"
    "Python凭借其丰富的科学计算和机器学习生态，持续主导着数据科学和人工智能领域。"
    "计算机科学的基础理论依然重要：数据结构和算法是程序设计的基石，操作系统原理指导着资源管理和调度策略，"
    "计算机网络原理支撑着分布式系统的通信设计，编译原理则帮助理解从高级语言到机器码的转换过程。"
    "软件工程的最佳实践包括代码审查、持续集成和持续部署、自动化测试、基础设施即代码等。"
    "敏捷开发方法如Scrum和Kanban强调迭代交付和持续反馈，而DevOps文化则致力于打破开发和运维之间的壁垒。"
    "安全是软件系统不可忽视的横切关注点。常见的安全威胁包括SQL注入、跨站脚本攻击、跨站请求伪造、服务端请求伪造等。"
    "OWASP Top 10项目持续跟踪最关键的Web应用安全风险。零信任安全模型已经成为现代企业安全架构的主流理念。"
    "数据加密技术如AES、RSA、TLS等为数据传输和存储提供机密性保护，而哈希算法如SHA-256则为数据完整性提供校验。"
    "身份认证和授权机制是实现安全访问控制的基础。OAuth 2.0和OpenID Connect是当前最流行的认证授权协议。"
)


def _build_realistic_prompt(target_tokens):
    if target_tokens <= 0:
        return "请用大约200字介绍一下人工智能的发展历史。"
    # 防御性上限：与 StartConfig.validate_context_lengths 保持一致，防 OOM
    if target_tokens > 1_048_576:
        raise ValueError(f"目标 token 数 {target_tokens} 超过上限 1048576")
    chars_per_token = 1.5
    repeats = max(1, int(target_tokens * chars_per_token / len(_REALISTIC_PROMPT_SEED)) + 1)
    full_text = (_REALISTIC_PROMPT_SEED * repeats)[:int(target_tokens * chars_per_token)]
    return full_text


# 映射 app_task_id → evalscope_task_id，用于 stop 级联
_eval_task_map: dict[str, str] = {}
_eval_map_lock = threading.Lock()

# 精度评测整体 HTTP 超时（秒）。不能传 None——httpx 的 timeout=None 是禁用超时，
# evalscope service 挂起时 worker 线程会永久阻塞。
EVAL_TIMEOUT = int(os.environ.get("EVALSCOPE_EVAL_TIMEOUT", "7200"))
OUTPUTS_DIR = os.environ.get("OUTPUTS_DIR", "/app/outputs")


def register_eval_task(app_task_id: str, es_task_id: str):
    """登记 app 任务与 evalscope 任务的映射，供 stop 级联取消。"""
    with _eval_map_lock:
        _eval_task_map[app_task_id] = es_task_id


def get_active_eval_task(app_task_id: str) -> str | None:
    with _eval_map_lock:
        return _eval_task_map.get(app_task_id)


def clear_eval_task(app_task_id: str):
    with _eval_map_lock:
        _eval_task_map.pop(app_task_id, None)


def run_accuracy_evalscope(task, cfg: dict, summary: dict):
    """用 evalscope 跑精度评测。结果写入 summary['accuracy']。

    内置数据集（ceval/mmlu 等）直接用 evalscope 数据集名。
    自定义数据集（custom:*）转成 evalscope 的 general_mcq/general_qa 格式评测。
    """
    datasets = cfg.get("accuracy_datasets", [])
    from app.evalscope_catalog import catalog_list
    catalog_names = {d["name"] for d in catalog_list()}
    builtin = [d for d in datasets if d in catalog_names]
    custom = [d for d in datasets if d.startswith("custom:")]

    if not builtin and not custom:
        task.log("warn", "未选择任何可评测的数据集，跳过精度评测")
        return

    api_url = _normalize_base_url(cfg.get("base_url", ""))
    model = cfg.get("model", "")
    api_key = cfg.get("api_key", "") or "EMPTY"
    limit = cfg.get("sample_limit", 0) or 0
    few_shot = cfg.get("few_shot", 0) or 0
    max_tokens = cfg.get("acc_max_tokens") or 2048
    temperature = cfg.get("acc_temperature", 0.0)
    acc_concurrency = cfg.get("acc_concurrency", 1) or 1
    acc_stream = cfg.get("acc_stream", True)
    disable_thinking = cfg.get("disable_thinking", False)
    req_timeout = cfg.get("timeout") or None  # request-level timeout

    if not es.health():
        task.log("error", "evalscope service 未就绪（默认 9000 端口）。"
                         "请确认容器内 evalscope service 已启动。")
        raise es.EvalScopeError("evalscope service 不可用")

    # 准备 evalscope 的 datasets 列表与 dataset_args
    es_datasets = list(builtin)
    dataset_args = {}
    # 用户未显式设置 few_shot 时，使用数据集默认值（对标官方榜单）
    user_few_shot = cfg.get("few_shot", 0) or 0
    if user_few_shot == 0:
        from app.evalscope_catalog import catalog_list
        catalog = {d["name"]: d for d in catalog_list()}
        for ds in builtin:
            ds_default = catalog.get(ds, {}).get("default_few_shot", 0)
            if ds_default > 0:
                dataset_args.setdefault(ds, {})["few_shot_num"] = ds_default
    subj_sel = cfg.get("dataset_subjects", {}) or {}
    for ds in builtin:
        subs = subj_sel.get(ds)
        if subs:
            # setdefault 合并，不能整体赋值——否则会冲掉前面写入的
            # few_shot_num（数据集默认 shot 数），导致选学科后静默变 0-shot
            dataset_args.setdefault(ds, {})["subset_list"] = list(subs)
            task.log("info", f"   {ds} 仅评测学科：{', '.join(subs)}")
    if custom:
        from app.custom_dataset import prepare_custom_for_evalscope
        for cname in custom:
            kind, ds_key, da = prepare_custom_for_evalscope(cname, few_shot)
            if ds_key not in es_datasets:
                es_datasets.append(ds_key)
            if ds_key in dataset_args:
                dataset_args[ds_key].setdefault("subset_list", [])
                dataset_args[ds_key]["subset_list"].extend(da["subset_list"])
                dataset_args[ds_key]["local_path"] = da["local_path"]
            else:
                dataset_args[ds_key] = da
            task.log("info", f"   自定义数据集 {cname} → evalscope {ds_key}"
                            f"（subset: {da['subset_list'][0]}）")

    ds_label = "、".join(es_datasets)
    # 计算实际生效的 few_shot
    actual_few_shot = user_few_shot
    for ds in builtin:
        fsn = dataset_args.get(ds, {}).get("few_shot_num")
        if fsn:
            actual_few_shot = int(fsn)
            break
    shot_label = f"{actual_few_shot}-shot" if actual_few_shot > 0 else "0-shot"
    if user_few_shot == 0 and actual_few_shot > 0:
        shot_label += "（数据集默认）"
    task.log("info", f"━━ 精度评测启动（evalscope）━━ 数据集：{ds_label}")
    task.log("info", f"   评测参数：{shot_label} | max_tokens {max_tokens} | "
                     f"temperature {temperature}"
                     + (f" | 抽样 {limit} 题/集" if limit else " | 全量") + " | 后端 evalscope")

    # 生成 evalscope task_id，用于 progress/log/stop
    eval_task_id = uuid.uuid4().hex[:12]
    register_eval_task(task.id, eval_task_id)
    task.es_task_id = eval_task_id  # 持久化，供重启后级联取消

    holder = {"result": None, "error": None, "done": False}

    def _call():
        try:
            holder["result"] = es.run_eval(
                model=model, api_url=api_url, api_key=api_key,
                datasets=es_datasets, limit=limit, few_shot=few_shot,
                max_tokens=max_tokens, temperature=temperature,
                dataset_args=dataset_args or None, task_id=eval_task_id,
                eval_batch_size=acc_concurrency,
                stream=acc_stream,
                disable_thinking=disable_thinking,
                request_timeout=req_timeout,
                timeout=EVAL_TIMEOUT)
        except Exception as e:
            holder["error"] = e
        finally:
            holder["done"] = True

    th = threading.Thread(target=_call, daemon=True)
    th.start()

    # 进度轮询：每 5s 查询 evalscope progress，推送真实百分比
    start = time.time()
    last_log_line = 0
    last_progress_pct = -1
    while not holder["done"]:
        if task.stopped():
            task.log("warn", "收到停止信号，正在请求 evalscope 取消任务...")
            es.stop_eval(eval_task_id)
            # 给 evalscope 一点时间响应
            time.sleep(3)
            break
        time.sleep(5)

        # 拉进度
        try:
            prog = es.get_eval_progress(eval_task_id)
            if prog and prog.get("percent", 0) > last_progress_pct:
                last_progress_pct = prog["percent"]
                completed = prog.get("completed", 0)
                total = prog.get("total", 0)
                stage_info = prog.get("stage", {})
                stage_name = ""
                if isinstance(stage_info, dict):
                    stage_name = stage_info.get("label") or stage_info.get("name") or ""
                task.progress(
                    stage="accuracy", dataset=stage_name or "evalscope",
                    completed=completed, total=total,
                    percent=round(prog["percent"], 1),
                    elapsed=int(time.time() - start),
                )
                pct_str = f"{prog['percent']:.1f}%"
                detail = f" ({completed}/{total})" if total else ""
                if stage_name:
                    detail += f" [{stage_name}]"
                task.log("info", f"   ▸ 精度评测进度：{pct_str}{detail}")
        except Exception:
            pass

        # 拉增量日志
        try:
            log_data = es.get_eval_log(eval_task_id, tail_lines=200)
            if log_data and log_data.get("text"):
                lines = log_data["text"].split("\n")
                current_tail = log_data.get("tail_line", 0)
                new_lines = lines[last_log_line:]
                for line in new_lines:
                    line = line.strip()
                    if line:
                        task.log("info", f"[evalscope] {line}")
                last_log_line = max(last_log_line, current_tail)
        except Exception:
            pass

    th.join(timeout=5)

    # 清理映射
    clear_eval_task(task.id)
    task.es_task_id = None

    if holder["error"]:
        task.log("error", f"evalscope 评测失败：{holder['error']}")
        raise holder["error"]

    raw = holder["result"] or {}
    status = raw.get("status")
    # evalscope invoke 成功时返回 status='completed'（'success' 仅为兼容）。
    # HTTP 500 的错误已在 _post 中 raise_for_status 抛 EvalScopeError，不会走到这里；
    # 这里拦截 HTTP 200 但 status 异常的情况（如 'stopped'/'error'）。
    if status and status not in ("completed", "success") and not task.stopped():
        err = (f"evalscope 返回非成功状态：{status}"
               + (f"：{raw.get('error') or raw.get('message') or ''}" if (raw.get('error') or raw.get('message')) else ""))
        task.log("error", err)
        raise es.EvalScopeError(err)
    norm = es.normalize_eval_result(raw)
    for ds_name, d in norm.items():
        d.setdefault("total", limit or None)
        d["few_shot"] = few_shot
        d["backend"] = "evalscope"
        d["repro"] = {
            "api_url": api_url,
            "few_shot": few_shot,
            "sample_limit": limit,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "dataset_args": dataset_args.get(ds_name) if isinstance(dataset_args, dict) else None,
            "output_dir": raw.get("output_dir") or os.path.join(OUTPUTS_DIR, eval_task_id),
            "eval_task_id": eval_task_id,
        }
        summary["accuracy"][ds_name] = d
        acc_str = f"{d['accuracy']}%" if d.get("accuracy") is not None else "见详情"
        task.log("success", f"   {ds_name}：准确率 {acc_str}（{shot_label}，对标官方榜单口径）")
    task.log("success", f"━━ 精度评测完成（evalscope）━━ 输出目录：{raw.get('output_dir','-')}")


def run_performance_evalscope(task, cfg: dict, summary: dict):
    """用 evalscope perf 跑性能压测。结果写入 summary['performance']。"""
    pc = cfg.get("perf", {})
    model = cfg.get("model", "")
    api_key = cfg.get("api_key", "") or "EMPTY"
    url = _normalize_perf_url(cfg.get("base_url", ""))
    levels = _parse_levels(pc.get("levels", pc.get("sweep_levels", [1, 5, 10, 20])))
    req_per_level = pc.get("requests_per_level", 20)
    max_tokens = pc.get("max_tokens", 512)
    temperature = pc.get("temperature", 0.0)
    stream = pc.get("stream", True)
    context_length = pc.get("context_length", 0) or 0
    min_tokens = pc.get("min_tokens", 0) or 0
    request_timeout = pc.get("timeout") or None
    warmup_requests = pc.get("warmup_requests", 0) or 0
    prompt_text = pc.get("prompt") or cfg.get("perfPrompt") or "请用大约200字介绍一下人工智能的发展历史。"

    if not es.health():
        task.log("error", "evalscope service 未就绪（默认 9000 端口）。")
        raise es.EvalScopeError("evalscope service 不可用")

    scale_multiplier = pc.get("scale_multiplier", 0) or 0
    parallel = levels
    if scale_multiplier > 0:
        number = [level * scale_multiplier for level in levels]
        req_per_level = None  # 各档不同
    else:
        number = [req_per_level] * len(levels)
    total_requests = sum(number)

    if context_length > 0:
        # 用重复字符近似目标 token 数（中文字符约 1.5 tokens/字）
        approx_chars = int(context_length / 1.5)
        prompt_text = _build_realistic_prompt(context_length)
        ctx_label = f" | 长上下文输入 ~{context_length} tokens（近似）"
    else:
        ctx_label = ""

    # 生成 line_by_line 数据集文件，每行一条 prompt
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
    for _ in range(total_requests):
        tmp.write(json.dumps(prompt_text, ensure_ascii=False) + "\n")
    tmp.close()
    dataset_path = tmp.name

    task.log("info", "━━ 性能压测启动（evalscope perf）━━")
    req_desc = f"各档 {dict(zip(levels, number))}" if scale_multiplier > 0 else f"每档 {req_per_level} 请求"
    task.log("info", f"   压测参数：并发档位 {levels} | {req_desc} | "
                     f"max_tokens {max_tokens} | {'流式' if stream else '非流式'}"
                     f" | 超时 {request_timeout}s | 预热 {warmup_requests}次{ctx_label}")
    if min_tokens:
        task.log("info", f"   min_tokens: {min_tokens}")

    # 生成 evalscope task_id
    perf_task_id = uuid.uuid4().hex[:12]
    register_eval_task(task.id, perf_task_id)
    task.es_task_id = perf_task_id  # 持久化，供重启后级联取消

    holder = {"result": None, "error": None, "done": False}

    def _call():
        try:
            holder["result"] = es.run_perf(
                model=model, url=url, api_key=api_key,
                parallel=parallel, number=number, dataset="line_by_line",
                dataset_path=dataset_path,
                max_tokens=max_tokens, min_tokens=min_tokens or None,
                temperature=temperature, stream=stream,
                request_timeout=request_timeout,
                warmup_requests=warmup_requests,
                task_id=perf_task_id)
        except Exception as e:
            holder["error"] = e
        finally:
            holder["done"] = True

    th = threading.Thread(target=_call, daemon=True)
    th.start()
    start = time.time()
    last_progress_pct = -1
    while not holder["done"]:
        if task.stopped():
            task.log("warn", "收到停止信号，正在请求 evalscope 取消任务...")
            es.stop_perf(perf_task_id)
            time.sleep(3)
            break
        time.sleep(5)

        # 拉进度
        try:
            prog = es.get_perf_progress(perf_task_id)
            if prog and prog.get("percent", 0) > last_progress_pct:
                last_progress_pct = prog["percent"]
                completed = prog.get("completed", 0)
                total = prog.get("total", 0)
                task.progress(
                    stage="performance", dataset="evalscope perf",
                    completed=completed, total=total,
                    percent=round(prog["percent"], 1),
                    elapsed=int(time.time() - start),
                )
                task.log("info", f"   ▸ 压测进度：{prog['percent']:.1f}%"
                                f"{f' ({completed}/{total})' if total else ''}")
        except Exception:
            pass

    th.join(timeout=5)

    # 清理临时文件（仅当 evalscope 线程已退出，避免删掉仍在读取的文件）
    try:
        if not th.is_alive():
            os.unlink(dataset_path)
        else:
            task.log("warn", "evalscope 压测尚未完全退出，临时数据集文件延后由系统清理")
    except Exception:
        pass

    # 清理映射
    clear_eval_task(task.id)
    task.es_task_id = None

    if holder["error"]:
        task.log("error", f"evalscope 压测失败：{holder['error']}")
        raise holder["error"]

    raw = holder["result"] or {}
    result = es.normalize_perf_result(raw, max_tokens=max_tokens)
    result["profile"] = {
        "levels": levels,
        "requests_per_level": req_per_level,
        "max_tokens": max_tokens,
        "min_tokens": min_tokens or None,
        "temperature": temperature,
        "stream": stream,
        "context_length": context_length,
        "request_timeout": request_timeout,
        "warmup_requests": warmup_requests,
        "dataset": "line_by_line",
        "prompt_text": prompt_text if len(prompt_text) <= 200 else prompt_text[:200] + "...",
        "url": url,
    }
    result["eval_task_id"] = perf_task_id
    result["output_dir"] = os.path.join(OUTPUTS_DIR, perf_task_id)
    summary["performance"] = result
    best = result.get("best")
    if best:
        rec = result.get("recommend")
        msg = (f"━━ 性能压测完成（evalscope）━━ 吞吐峰值：并发 {best['concurrency']} 时 "
               f"{best.get('rps')} RPS")
        if rec:
            msg += f" | 稳定推荐并发区间 {rec['min']}~{rec['max']}"
        task.log("success", msg)
        for w in result.get("warnings", [])[:3]:
            task.log("warn", w if isinstance(w, str) else f"{w.get('title')}：{w.get('message')}")
    else:
        task.log("warn", "压测完成但未解析到指标，请检查 evalscope 返回格式（见原始结果）")


def run_context_scan(task, cfg: dict, summary: dict):
    """上下文长度扫描：固定并发，扫描多个上下文长度。结果写入 summary['context_scan']。"""
    model = cfg.get("model", "")
    api_key = cfg.get("api_key", "") or "EMPTY"
    url = _normalize_perf_url(cfg.get("base_url", ""))
    ctx_lengths = _parse_context_lengths(cfg.get("context_lengths", []))
    if not ctx_lengths:
        task.log("warn", "未配置上下文长度扫描档位，跳过")
        return

    concurrency = cfg.get("context_concurrency", 8)
    requests = cfg.get("context_requests", 20)
    max_tokens = cfg.get("context_max_tokens", 256)
    stream = cfg.get("context_stream", True)
    temperature = 0.0  # 上下文扫描固定 temperature=0

    # 从性能配置中复用 timeout / warmup / min_tokens
    pc = cfg.get("perf", {})
    request_timeout = pc.get("timeout") or None
    warmup_requests = pc.get("warmup_requests", 0) or 0

    total = len(ctx_lengths)
    task.log("info", f"━━ 上下文扫描启动（{total} 档）━━ 固定并发 {concurrency}")
    task.log("info", f"   长度档位：{ctx_lengths} tokens")

    scan_results = {}

    for idx, ctx_len in enumerate(ctx_lengths):
        if task.stopped():
            task.log("warn", "收到停止信号，中断上下文扫描")
            break

        task.log("info", f"   [{idx + 1}/{total}] 上下文 ~{ctx_len} tokens 压测中...")
        task.progress(stage="context_scan", dataset=f"上下文 {ctx_len}",
                      completed=idx + 1, total=total, percent=round(idx / total * 100, 1))

        # 生成临时数据集：用重复字符近似目标 token 数
        approx_chars = max(1, int(ctx_len / 1.5))
        prompt_line = _build_realistic_prompt(ctx_len)
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
        for _ in range(requests):
            tmp.write(json.dumps(prompt_line, ensure_ascii=False) + "\n")
        tmp.close()

        perf_task_id = uuid.uuid4().hex[:12]
        register_eval_task(task.id, perf_task_id)
        task.es_task_id = perf_task_id  # 持久化，供重启后级联取消

        holder = {"result": None, "error": None, "done": False}

        def _call(_len=ctx_len, _path=tmp.name):
            try:
                holder["result"] = es.run_perf(
                    model=model, url=url, api_key=api_key,
                    parallel=[concurrency], number=[requests],
                    dataset="line_by_line", dataset_path=_path,
                    max_tokens=max_tokens, temperature=temperature,
                    stream=stream,
                    request_timeout=request_timeout,
                    warmup_requests=warmup_requests,
                    task_id=perf_task_id,
                )
            except Exception as e:
                holder["error"] = e
            finally:
                holder["done"] = True

        th = threading.Thread(target=_call, daemon=True)
        th.start()
        while not holder["done"]:
            if task.stopped():
                es.stop_perf(perf_task_id)
                time.sleep(2)
                break
            time.sleep(5)
        th.join(timeout=5)
        clear_eval_task(task.id)
        task.es_task_id = None

        # 清理临时文件（仅当 evalscope 线程已退出，避免删掉仍在读取的文件）
        try:
            if not th.is_alive():
                os.unlink(tmp.name)
            else:
                task.log("warn", "evalscope 压测尚未完全退出，临时数据集文件延后由系统清理")
        except Exception:
            pass

        if holder["error"]:
            task.log("error", f"   上下文 {ctx_len} 压测失败：{holder['error']}")
            scan_results[f"context_{ctx_len}"] = {"context_length": ctx_len, "error": str(holder["error"])}
        else:
            norm = es.normalize_perf_result(holder["result"] or {}, max_tokens=max_tokens)
            sweep = norm.get("sweep", [])
            metric = sweep[0] if sweep else {}
            scan_results[f"context_{ctx_len}"] = {
                "context_length": ctx_len,
                "rps": metric.get("rps"),
                "output_tps": metric.get("output_tps"),
                "ttft_avg": metric.get("ttft_avg"),
                "tpot_avg_ms": metric.get("tpot_avg_ms"),
                "latency_avg": metric.get("latency_avg"),
                "latency_p90": metric.get("latency_p90"),
                "latency_p99": metric.get("latency_p99"),
                "success_rate": metric.get("success_rate"),
            }
            task.log("success", f"   {ctx_len}: RPS={metric.get('rps')}, "
                               f"TTFT={metric.get('ttft_avg')}s, P99={metric.get('latency_p99')}s")

    # 整理输出
    sweep_rows = sorted(scan_results.values(), key=lambda x: x.get("context_length", 0))
    summary["context_scan"] = {
        "sweep": sweep_rows,
        "concurrency": concurrency,
        "requests_per_level": requests,
    }
    task.log("success", f"━━ 上下文扫描完成 ━━ {len(sweep_rows)} 档")


def _normalize_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    return url


def _normalize_perf_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


def _parse_levels(raw) -> list[int]:
    if isinstance(raw, str):
        levels = [int(x.strip()) for x in raw.split(",") if x.strip()]
    elif isinstance(raw, list):
        levels = [int(x) for x in raw]
    elif raw is None:
        levels = [1, 5, 10, 20]
    else:
        levels = [int(raw)]
    levels = [x for x in levels if x > 0]
    return sorted(dict.fromkeys(levels)) or [1, 5, 10, 20]


def _parse_context_lengths(raw) -> list[int]:
    """解析上下文长度档位：逗号分隔字符串或列表。"""
    if isinstance(raw, str):
        return [int(x.strip()) for x in raw.split(",") if x.strip()]
    if isinstance(raw, list):
        return [int(x) for x in raw]
    return []
