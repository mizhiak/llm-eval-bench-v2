"""evalscope service 客户端：通过 HTTP 调用 evalscope 的评测/压测能力。

evalscope 以 service 模式（Flask）独立运行，暴露 REST API：
- POST /api/v1/eval/invoke  精度评测（底层走 OpenCompass/native backend，对标官方榜单）
- POST /api/v1/perf/invoke  性能压测（就是 evalscope perf，含 TPOT/TTFT/Gen tok/s 等完整指标）

本客户端封装这些调用，并把结果转换成我们 UI/报告所需的结构。
真正的评测逻辑全部由 evalscope 完成，我们只做配置生成、调度、结果转换。
"""
import os
import json
import time
import uuid
import httpx

# evalscope service 地址（同容器内，默认 9000 端口）
EVALSCOPE_URL = os.environ.get("EVALSCOPE_URL", "http://127.0.0.1:9000")


class EvalScopeError(Exception):
    pass


def _post(path: str, payload: dict, timeout: float = 7200, task_id: str = None) -> dict:
    """调用 evalscope service，长超时（完整评测可能很久）。"""
    url = EVALSCOPE_URL.rstrip("/") + path
    headers = {}
    if task_id:
        headers["EvalScope-Task-Id"] = task_id
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.text[:500]
        except Exception:
            pass
        raise EvalScopeError(f"evalscope HTTP {e.response.status_code}: {body}")
    except httpx.ConnectError:
        raise EvalScopeError(
            "无法连接 evalscope service。请确认容器内 evalscope service 已启动"
            "（默认 9000 端口）。")
    except Exception as e:
        raise EvalScopeError(f"{type(e).__name__}: {str(e)[:300]}")


def _get(path: str, timeout: float = 15) -> dict:
    """GET 请求 evalscope service。"""
    url = EVALSCOPE_URL.rstrip("/") + path
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.json()
    except Exception:
        return {}


def health() -> bool:
    """检查 evalscope service 是否在线。端点为 GET /health。"""
    try:
        with httpx.Client(timeout=5) as client:
            r = client.get(EVALSCOPE_URL.rstrip("/") + "/health")
            if r.status_code == 200:
                data = r.json()
                return data.get("status") == "ok"
            return False
    except Exception:
        return False


def list_benchmarks() -> list:
    """查询 evalscope 支持的全部数据集（权威来源）。

    端点 GET /api/v1/eval/benchmarks 返回每个数据集的完整元信息：
    name/pretty_name/tags/metrics/subset_list/total_samples/few_shot_num/
    description.zh 等。
    """
    url = EVALSCOPE_URL.rstrip("/") + "/api/v1/eval/benchmarks"
    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(url)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        raise EvalScopeError(f"查询数据集列表失败：{type(e).__name__}: {str(e)[:200]}")
    # 返回结构：{"text":[...], "multimodal":[...]} 按类别分组
    if isinstance(data, dict):
        result = []
        for v in data.values():
            if isinstance(v, list):
                result.extend(v)
        return result
    return data if isinstance(data, list) else []


# ============ evalscope 任务控制 ============

def stop_eval(task_id: str) -> bool:
    """停止 evalscope 正在运行的精度评测任务（best-effort）。"""
    try:
        r = _get(f"/api/v1/eval/stop?task_id={task_id}", timeout=10)
        return bool(r)
    except Exception:
        return False


def stop_perf(task_id: str) -> bool:
    """停止 evalscope 正在运行的性能压测任务（best-effort）。"""
    try:
        r = _get(f"/api/v1/perf/stop?task_id={task_id}", timeout=10)
        return bool(r)
    except Exception:
        return False


def get_eval_progress(task_id: str) -> dict | None:
    """查询 evalscope 精度评测实时进度。
    返回 {"percent": 37.0, "total_count": 14042, "processed_count": 5200, "stage": {...}}
    或 None（progress 文件尚未生成）。
    """
    try:
        data = _get(f"/api/v1/eval/progress?task_id={task_id}", timeout=8)
        if data and data.get("percent") is not None:
            return {
                "percent": data.get("percent", 0),
                "total": data.get("total_count") or 0,
                "completed": data.get("processed_count") or 0,
                "stage": data.get("stage") or {},
                "status": data.get("status", ""),
            }
        return None
    except Exception:
        return None


def get_eval_log(task_id: str, tail_lines: int = 100) -> dict | None:
    """查询 evalscope 精度评测日志（增量）。"""
    try:
        data = _get(f"/api/v1/eval/log?task_id={task_id}&page={tail_lines}", timeout=10)
        if data and data.get("text"):
            return data
        return None
    except Exception:
        return None


def get_perf_progress(task_id: str) -> dict | None:
    """查询 evalscope 性能压测实时进度。"""
    try:
        data = _get(f"/api/v1/perf/progress?task_id={task_id}", timeout=8)
        if data and data.get("percent") is not None:
            return {
                "percent": data.get("percent", 0),
                "total": data.get("total_count") or 0,
                "completed": data.get("processed_count") or 0,
                "stage": data.get("stage") or {},
                "status": data.get("status", ""),
            }
        return None
    except Exception:
        return None


def get_perf_log(task_id: str, tail_lines: int = 100) -> dict | None:
    """查询 evalscope 性能压测日志（增量）。"""
    try:
        data = _get(f"/api/v1/perf/log?task_id={task_id}&page={tail_lines}", timeout=10)
        if data and data.get("text"):
            return data
        return None
    except Exception:
        return None


# ============ 精度评测 ============

def run_eval(model: str, api_url: str, api_key: str, datasets: list[str],
             limit: int = 0, few_shot: int = 0, max_tokens: int = 2048,
             temperature: float = 0.0, dataset_args: dict | None = None,
             timeout: float = 7200, task_id: str = None,
             eval_batch_size: int = 1, stream: bool = True,
             disable_thinking: bool = False,
             request_timeout: float = None) -> dict:
    """调用 evalscope 精度评测。

    datasets: evalscope 数据集名，如 ['ceval','mmlu','gsm8k','cmmlu']
    few_shot: 0=zero-shot；>0 走 few-shot（evalscope 用标准 dev 集做示例，对标榜单）
    task_id: 如果提供，传给 evalscope 用于 progress/log/stop 关联
    返回 evalscope 的评测结果 JSON。
    """
    gen_cfg = {"max_tokens": max_tokens, "temperature": temperature, "stream": stream}
    payload = {
        "model": model,
        "api_url": api_url,
        "api_key": api_key or "EMPTY",
        "eval_type": "openai_api",
        "datasets": datasets,
        "generation_config": gen_cfg,
    }
    if disable_thinking:
        gen_cfg["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    if limit and limit > 0:
        payload["limit"] = limit
    if eval_batch_size and eval_batch_size > 1:
        payload["eval_batch_size"] = eval_batch_size
    # few-shot 与子集通过 dataset_args 传给 evalscope
    da = dict(dataset_args or {})
    if few_shot and few_shot > 0:
        # evalscope 用 few_shot_num 控制 shot 数
        for ds in datasets:
            da.setdefault(ds, {})["few_shot_num"] = few_shot
    if da:
        payload["dataset_args"] = da
    if request_timeout is not None:
        payload["timeout"] = request_timeout
    tid = task_id or uuid.uuid4().hex[:12]
    return _post("/api/v1/eval/invoke", payload, timeout=timeout, task_id=tid)


# ============ 性能压测 ============

def run_perf(model: str, url: str, api_key: str, parallel, number,
             dataset: str = "random", max_tokens: int = 512, min_tokens: int = None,
             prefix_length: int = 0, prompt_length: int = 512,
             tokenizer_path: str = None, dataset_path: str = None,
             temperature: float = 0.0,
             stream: bool = True, timeout: float = 7200,
             task_id: str = None, request_timeout: float = None,
             warmup_requests: int = 0) -> dict:
    """调用 evalscope perf 性能压测。

    parallel/number: 可为 int 或 list。传 list（如 parallel=[1,5,10,20]、
    number=[20,20,20,20]）时 evalscope 会逐档扫描，对应我们 UI 的并发扫描。
    url: 必须是完整端点 URL，如 http://host:8000/v1/chat/completions
    dataset: 'random'（合成可控长度，适合压测）或 'line_by_line' 等。
    dataset_path: line_by_line 数据集的本地文件路径。
    task_id: 如果提供，传给 evalscope 用于 progress/log/stop 关联
    request_timeout: 单请求超时（秒），区别于全局 timeout
    warmup_requests: 正式压测前的预热请求数
    返回 evalscope 压测结果（results 按 parallel_X_number_Y 组织各档）。
    """
    payload = {
        "model": model,
        "url": url,
        "api": "openai",
        "api_key": api_key or "EMPTY",
        "parallel": parallel,
        "number": number,
        "dataset": dataset,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    if min_tokens is not None and min_tokens > 0:
        payload["min_tokens"] = min_tokens
    if request_timeout is not None:
        payload["request_timeout"] = request_timeout
    if warmup_requests > 0:
        payload["warmup_num"] = warmup_requests  # evalscope field: warmup_num (>=1=absolute, <1=ratio)
    if dataset == "random":
        # random 数据集需指定 token 长度范围与 tokenizer
        payload["prefix_length"] = prefix_length
        payload["min_prompt_length"] = prompt_length
        payload["max_prompt_length"] = prompt_length
    if tokenizer_path:
        payload["tokenizer_path"] = tokenizer_path
    if dataset_path:
        payload["dataset_path"] = dataset_path
    tid = task_id or uuid.uuid4().hex[:12]
    return _post("/api/v1/perf/invoke", payload, timeout=timeout, task_id=tid)


# ============ 上下文长度扫描 ============

def run_context_scan(model: str, url: str, api_key: str, concurrency: int,
                     context_lengths: list[int], requests: int = 20,
                     max_tokens: int = 256, temperature: float = 0.0,
                     stream: bool = True, timeout: float = 7200,
                     task_id: str = None) -> dict:
    """上下文长度扫描：对每个 context_length 跑一轮 perf，收集 per-length 延迟分布。

    不同于普通 perf（并发扫描），这里固定并发、扫描上下文长度。
    结果结构类似 perf sweep，但 key 为 context_4K / context_8K 等。
    """
    all_results = {}
    tid = task_id or uuid.uuid4().hex[:12]
    for ctx_len in context_lengths:
        perf_tid = f"{tid}_ctx{ctx_len}"
        try:
            result = run_perf(
                model=model, url=url, api_key=api_key,
                parallel=[concurrency], number=[requests],
                dataset="random", max_tokens=max_tokens,
                prompt_length=ctx_len, temperature=temperature,
                stream=stream, timeout=timeout, task_id=perf_tid,
            )
            all_results[f"context_{ctx_len}"] = {
                "context_length": ctx_len,
                "result": result,
            }
        except Exception as e:
            all_results[f"context_{ctx_len}"] = {
                "context_length": ctx_len,
                "error": str(e),
            }
    return all_results


def normalize_context_scan(raw: dict) -> dict:
    """把上下文扫描结果转成前端图表数据。"""
    rows = []
    for key, block in raw.items():
        ctx_len = block.get("context_length", 0)
        result = block.get("result") or {}
        error = block.get("error")
        if error:
            rows.append({"context_length": ctx_len, "error": error})
            continue
        # 从单档 perf 结果提取第一个（也是唯一一个）并发档的指标
        perf_norm = normalize_perf_result(result)
        sweep = perf_norm.get("sweep", [])
        if sweep:
            r = sweep[0]
            rows.append({
                "context_length": ctx_len,
                "rps": r.get("rps"),
                "output_tps": r.get("output_tps"),
                "ttft_avg": r.get("ttft_avg"),
                "tpot_avg_ms": r.get("tpot_avg_ms"),
                "latency_avg": r.get("latency_avg"),
                "latency_p90": r.get("latency_p90"),
                "latency_p99": r.get("latency_p99"),
                "success_rate": r.get("success_rate"),
                "avg_in_tokens": r.get("avg_in_tokens"),
                "avg_out_tokens": r.get("avg_out_tokens"),
            })
    rows.sort(key=lambda r: r.get("context_length", 0))
    return {"sweep": rows, "raw": raw}


# ============ 结果转换：evalscope 格式 → 我们 UI/报告格式 ============

def normalize_eval_result(raw: dict) -> dict:
    """把 evalscope 评测结果转换成我们的精度结果结构。

    evalscope 真实结构（1.8.1）：
      { dataset_name: { score, num, metrics: [
          { name, score, macro_score, num, categories: [
            { name, score, macro_score, num, subsets: [
              { name, score, num, is_aggregate }
            ]}
          ]}
        ], analysis, dataset_description, perf_metrics }}
    """
    out = {}
    report = raw.get("result") or raw.get("report") or raw
    if not isinstance(report, dict):
        return out
    for ds_name, ds_data in report.items():
        if not isinstance(ds_data, dict):
            continue
        score = (ds_data.get("score") or ds_data.get("accuracy")
                 or ds_data.get("AverageAccuracy"))

        # 从 metrics[].categories[].subsets[] 提取学科/类别分数
        by_subject = {}
        by_category = {}
        metrics = ds_data.get("metrics") or []
        if isinstance(metrics, list):
            for m in metrics:
                if not isinstance(m, dict):
                    continue
                for cat in m.get("categories") or []:
                    if not isinstance(cat, dict):
                        continue
                    cat_name = cat.get("name")
                    if isinstance(cat_name, list):
                        cat_name = "/".join(str(x) for x in cat_name)
                    cat_key = str(cat_name) if cat_name else "unknown"
                    cat_score = cat.get("score")
                    cat_macro = cat.get("macro_score")

                    cat_subsets = {}
                    for sub in cat.get("subsets") or []:
                        if not isinstance(sub, dict):
                            continue
                        sub_name = sub.get("name")
                        sub_score = sub.get("score")
                        if sub_name and sub_score is not None:
                            ss = _pct(sub_score)
                            by_subject[str(sub_name)] = ss
                            cat_subsets[str(sub_name)] = {
                                "score": ss, "num": sub.get("num"),
                            }

                    by_category[cat_key] = {
                        "score": _pct(cat_score) if cat_score is not None else None,
                        "macro_score": _pct(cat_macro) if cat_macro is not None else None,
                        "num": cat.get("num"),
                        "subsets": cat_subsets,
                    }

        acc_pct = _pct(score) if score is not None else None

        perf_summary = None
        pm = ds_data.get("perf_metrics")
        if isinstance(pm, dict):
            perf_summary = pm.get("summary")

        out[ds_name] = {
            "accuracy": acc_pct,
            "num": ds_data.get("num"),
            "by_subject": by_subject,
            "by_category": by_category,
            "analysis": ds_data.get("analysis", ""),
            "dataset_description": ds_data.get("dataset_description", ""),
            "perf_metrics": perf_summary,
            "raw": ds_data,
        }
    return out


def _pct(v) -> float:
    """把 0-1 的分数转成百分比；已是百分比则原样。"""
    f = float(v)
    return round(f * 100, 2) if f <= 1 else round(f, 2)


def renormalize_stored_result(result: dict) -> dict:
    """对已持久化的旧结果做增量修复：从 raw 字段重新提取 by_subject/by_category。

    旧版 normalize_eval_result 不会解析 metrics[].categories[].subsets[]，
    导致 by_subject 为空。此函数按新版逻辑重提，避免旧任务数据缺失。
    """
    if not result or not isinstance(result, dict):
        return result
    accuracy = result.get("accuracy")
    if not isinstance(accuracy, dict):
        return result
    for ds_name, ds_data in accuracy.items():
        if not isinstance(ds_data, dict):
            continue
        raw = ds_data.get("raw")
        if not isinstance(raw, dict):
            continue
        # 只在旧数据缺失时才从 raw 重提
        need_fix = not ds_data.get("by_subject") and not ds_data.get("by_category")
        if not need_fix:
            continue
        by_subject = {}
        by_category = {}
        metrics = raw.get("metrics") or []
        if isinstance(metrics, list):
            for m in metrics:
                if not isinstance(m, dict):
                    continue
                for cat in m.get("categories") or []:
                    if not isinstance(cat, dict):
                        continue
                    cat_name = cat.get("name")
                    if isinstance(cat_name, list):
                        cat_name = "/".join(str(x) for x in cat_name)
                    cat_key = str(cat_name) if cat_name else "unknown"
                    cat_score = cat.get("score")
                    cat_macro = cat.get("macro_score")

                    cat_subsets = {}
                    for sub in cat.get("subsets") or []:
                        if not isinstance(sub, dict):
                            continue
                        sub_name = sub.get("name")
                        sub_score = sub.get("score")
                        if sub_name and sub_score is not None:
                            ss = _pct(sub_score)
                            by_subject[str(sub_name)] = ss
                            cat_subsets[str(sub_name)] = {
                                "score": ss, "num": sub.get("num"),
                            }

                    by_category[cat_key] = {
                        "score": _pct(cat_score) if cat_score is not None else None,
                        "macro_score": _pct(cat_macro) if cat_macro is not None else None,
                        "num": cat.get("num"),
                        "subsets": cat_subsets,
                    }

        ds_data["by_subject"] = by_subject
        ds_data["by_category"] = by_category
        ds_data["num"] = ds_data.get("num") or raw.get("num")
        ds_data["analysis"] = ds_data.get("analysis") or raw.get("analysis", "")
        ds_data["dataset_description"] = ds_data.get("dataset_description") or raw.get("dataset_description", "")
        pm = raw.get("perf_metrics")
        if isinstance(pm, dict) and not ds_data.get("perf_metrics"):
            ds_data["perf_metrics"] = pm.get("summary")
    return result


def normalize_perf_result(raw: dict, max_tokens: int = None) -> dict:
    """把 evalscope perf 结果转换成我们的 sweep 结构（曲线+明细+推荐）。

    evalscope perf 真实响应格式：
    {
      "status": "success",
      "results": {
        "parallel_10_number_100": {"metrics": {...}, "percentiles": {...}},
        "parallel_20_number_100": {...},
        ...
      }
    }
    每个 key 对应一个并发档，metrics 含吞吐/延迟等，percentiles 含分位数。
    """
    rows = []
    results = raw.get("results") or raw.get("result") or {}
    if not isinstance(results, dict):
        return {"sweep": [], "best": None, "recommend": None, "raw": raw}

    for key, block in results.items():
        if not isinstance(block, dict):
            continue
        metrics = block.get("metrics", {}) or {}
        pct = block.get("percentiles", {}) or {}
        pct_map = _norm_percentiles(pct)
        # 从 key "parallel_10_number_100" 提取并发数
        conc = None
        try:
            parts = key.split("_")
            if "parallel" in parts:
                conc = int(parts[parts.index("parallel") + 1])
        except Exception:
            conc = metrics.get("Concurrency") or metrics.get("parallel")

        success = _num(_pick(metrics, "Success Requests", "Succeed requests",
                              "success", "Number of succeed"))
        total = _num(_pick(metrics, "Total Requests", "Total requests", "number", "total"))
        row = {
            "concurrency": conc,
            "rps": _num(_pick(metrics, "RPS", "Request throughput (req/s)",
                       "Req Throughput (req/s)", "Request Rate (req/s)", "rps", "throughput")),
            "output_tps": _num(_pick(metrics, "Output token throughput (tok/s)",
                               "Output Throughput (tok/s)", "Gen. toks/s",
                               "output_throughput", "gen_tps")),
            "total_tps": _num(_pick(metrics, "Total token throughput (tok/s)",
                              "Total Throughput (tok/s)")),
            "input_tps": _num(_pick(metrics, "Input Throughput (tok/s)",
                              "Input throughput (tok/s)", "input_throughput")),
            "tpot_avg_ms": _ms(_pick(metrics, "Average time per output token (s)",
                                    "TPOT (ms)", "TPOT", "tpot")),
            "itl_avg_ms": _ms(_pick(metrics, "ITL (ms)", "ITL", "itl",
                                   "Inter-token Latency (ms)")),
            "ttft_avg": _ttft(_pick(metrics, "Average time to first token (s)",
                             "TTFT (ms)", "TTFT", "ttft")),
            "latency_avg": _num(_pick(metrics, "Average latency (s)", "Avg Latency (s)",
                                "avg_latency", "latency")),
            "latency_p50": _num(pct_map.get("50%") or _pick(metrics, "P50 latency (s)")),
            "latency_p90": _num(pct_map.get("90%") or _pick(metrics, "P90 latency (s)")),
            "latency_p99": _num(pct_map.get("99%") or _pick(metrics, "P99 latency (s)")),
            "latency_max": _num(pct_map.get("max") or _pick(metrics, "Max latency (s)")),
            "ttft_p50": _num(pct_map.get("TTFT_P50") or pct_map.get("ttft_p50") or pct_map.get("TTFT_50%")),
            "ttft_p99": _num(pct_map.get("TTFT_P99") or pct_map.get("ttft_p99") or pct_map.get("TTFT_99%")),
            "ttft_max": _num(pct_map.get("TTFT_max") or pct_map.get("ttft_max")),
            "avg_in_tokens": _num(_pick(metrics, "Average input tokens per request",
                                  "Avg Input Tokens", "avg_in_tokens", "input_tokens")),
            "avg_out_tokens": _num(_pick(metrics, "Average output tokens per request",
                                   "Avg Output Tokens", "avg_out_tokens", "output_tokens")),
            "test_duration": _num(_pick(metrics, "Test Duration (s)", "test_duration", "duration")),
            "success": success,
            "total": total,
        }
        row["success_rate"] = round(success / total * 100, 2) if total else None
        row["error_rate"] = round(100 - row["success_rate"], 2) if row["success_rate"] is not None else None
        if row["concurrency"] is not None:
            rows.append(row)

    # 退化检测：success_rate 高但 avg_out_tokens 严重低于预期
    # 从正常档位推断预期输出长度（取 avg_out_tokens 最大值作为 baseline）
    expected_tokens = max_tokens
    if not expected_tokens and rows:
        expected_tokens = max((r.get("avg_out_tokens") or 0) for r in rows)
    for r in rows:
        avg_out = r.get("avg_out_tokens") or 0
        r["degraded"] = False
        r["degraded_pct"] = 0.0
        if expected_tokens and expected_tokens > 0:
            ratio = avg_out / expected_tokens
            if ratio < 0.5 and (r.get("success_rate") or 0) > 95:
                r["degraded"] = True
                r["degraded_pct"] = round((1 - ratio) * 100, 1)
                if ratio > 0:
                    effective = int((r.get("total") or 0) * ratio)
                    r["effective_requests"] = effective
                    r["degraded_requests"] = (r.get("total") or 0) - effective

    rows.sort(key=lambda r: r["concurrency"] or 0)
    best = max(rows, key=lambda r: r.get("rps") or 0) if rows else None
    lowest_lat = min(rows, key=lambda r: r.get("latency_avg") or 9e9) if rows else None
    rec, warnings = _recommend_perf(rows, best)
    degraded_levels = [r for r in rows if r.get("degraded")]
    if degraded_levels:
        concs = ",".join(str(r["concurrency"]) for r in degraded_levels)
        warnings.append(
            f"⚠ 并发 {concs} 档存在静默退化：成功率显示 100% 但平均输出 token "
            f"远低于预期（{expected_tokens}），大量请求返回空响应。"
            f"建议以退化前的最高并发档作为有效上限。"
        )
    return {"sweep": rows, "best": best, "lowest_latency": lowest_lat,
            "recommend": rec, "warnings": warnings,
            "raw": _sanitize_for_json(raw)}


def _norm_percentiles(pct):
    """标准化 percentiles：可能是 dict {P50: x, 90%: y} 或 list [{Percentiles: '50%', ...}]。"""
    if isinstance(pct, dict) and pct:
        # Handle serialized format: {"rows": [{...}, ...]}
        if "rows" in pct and isinstance(pct["rows"], list):
            pct = pct["rows"]
        else:
            return pct
    if isinstance(pct, list):
        out = {}
        for item in pct:
            if not isinstance(item, dict):
                continue
            k = item.get("Percentiles") or item.get("percentile") or item.get("pct") or ""
            if k:
                # 提取 Latency/TTFT/ITL/TPOT 等
                for field in item:
                    if field == "Percentiles" or field == "percentile" or field == "pct":
                        continue
                    out_key = f"{field}_{k}" if k not in ("50%", "90%", "99%") else k
                    if k in ("50%", "90%", "99%", "max"):
                        # 对于常用分位，按 metric 生成独立 key
                        short = field.replace(" (s)", "").replace(" (ms)", "").replace(" ", "_")
                        if short != field:
                            out[f"{short}_{k}"] = item[field]
                        else:
                            out[f"{short}"] = item[field]
                    out[k] = item.get("Latency (s)") or item.get("latency") or item.get(field)
                # Also add direct metric keys
                latency = item.get("Latency (s)") or item.get("latency")
                ttft = item.get("TTFT (ms)") or item.get("TTFT") or item.get("ttft")
                if latency is not None and k in ("50%", "90%", "99%", "max"):
                    out[k] = latency
                if ttft is not None and k in ("50%", "90%", "99%", "max"):
                    out[f"TTFT_{k}"] = ttft
        return out
    return {}


def _sanitize_for_json(obj):
    """递归移除 NaN/Inf 值，确保 JSON 可序列化。"""
    import math
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    return obj


def _pick(d: dict, *keys):
    """从 dict 里按多个候选 key 取第一个非空值（兼容 evalscope 不同版本字段名）。"""
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _num(v):
    """把 evalscope 可能返回的数字/字符串统一成 float，缺失返回 None。"""
    import math
    if v is None or v == "":
        return None
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, 4)
    except Exception:
        return None


def _ms(v):
    """秒转毫秒（TPOT 等以秒返回时）。已是毫秒级则原样。"""
    if v is None:
        return None
    try:
        v = float(v)
        # 小于 5 视为秒，转毫秒；否则认为已是毫秒
        return round(v * 1000, 2) if v < 5 else round(v, 2)
    except Exception:
        return None


def _ttft(v):
    """TTFT 统一为秒。evalscope 不同版本可能返回秒或毫秒。"""
    import math
    if v is None:
        return None
    try:
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return None
        # > 100 视为毫秒，转换到秒
        if v > 100:
            return round(v / 1000, 4)
        return round(v, 4)
    except Exception:
        return None


def _recommend_perf(rows: list[dict], best: dict | None):
    """按吞吐、成功率和 P99 延迟共同给出更保守的生产推荐。"""
    warnings = []
    if not rows or not best:
        return None, warnings

    peak = best.get("rps") or 0
    p99_values = [r.get("latency_p99") for r in rows if r.get("latency_p99") is not None]
    base_p99 = min(p99_values) if p99_values else None

    stable = []
    for r in rows:
        success_rate = r.get("success_rate")
        p99 = r.get("latency_p99")
        enough_throughput = (r.get("rps") or 0) >= peak * 0.9
        success_ok = success_rate is None or success_rate >= 99
        p99_ok = base_p99 is None or p99 is None or p99 <= base_p99 * 2.5
        if enough_throughput and success_ok and p99_ok:
            stable.append(r)

    failed_rows = [r for r in rows if r.get("success_rate") is not None and r["success_rate"] < 100]
    if failed_rows:
        first = failed_rows[0]
        warnings.append({
            "level": "warn",
            "title": "存在失败请求",
            "message": f"并发 {first['concurrency']} 起成功率低于 100%，建议关注限流、超时或模型服务错误。"
        })
    if base_p99 is not None:
        bad_p99 = [r for r in rows if r.get("latency_p99") is not None and r["latency_p99"] > base_p99 * 3]
        if bad_p99:
            first = bad_p99[0]
            warnings.append({
                "level": "warn",
                "title": "P99 延迟劣化",
                "message": f"并发 {first['concurrency']} 的 P99 已超过最低 P99 的 3 倍，生产并发不建议继续上探。"
            })

    if stable:
        peak_row = max(stable, key=lambda r: r.get("rps") or 0)
        return {
            "min": min(r["concurrency"] for r in stable),
            "max": max(r["concurrency"] for r in stable),
            "peak": peak_row["concurrency"],
            "basis": "吞吐达到峰值 90% 以上，且成功率/P99 未明显劣化",
        }, warnings

    warnings.append({
        "level": "warn",
        "title": "未找到稳定推荐区间",
        "message": "所有高吞吐档位都存在成功率或 P99 风险，建议降低并发或扩大压测档位重新测试。"
    })
    return {"min": best["concurrency"], "max": best["concurrency"],
            "peak": best["concurrency"], "basis": "仅按最高吞吐兜底"}, warnings
