"""FastAPI 主应用：配置接口、任务启动、SSE 进度流、数据集管理。"""
import os
import json
import queue
import re
import shutil
from typing import Any

from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from app.model_client import ModelClient
from app.task_manager import manager, list_datasets, CUSTOM_DIR, DATA_DIR, _load_jsonl

app = FastAPI(title="大模型性能与精度测试工具")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class ConnTest(BaseModel):
    base_url: str
    api_key: str = ""
    model: str = ""
    api_format: str = "openai"
    timeout: float = 30
    disable_thinking: bool = False


class PerfConfig(BaseModel):
    levels: list[int] = Field(default_factory=lambda: [1, 5, 10, 20])
    requests_per_level: int = Field(default=20, ge=1, le=100000)
    max_tokens: int = Field(default=256, ge=1, le=131072)
    min_tokens: int = Field(default=0, ge=0, le=131072)
    stream: bool = True
    prompt: str = ""
    context_length: int = Field(default=0, ge=0, le=1048576)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    system: str = ""
    timeout: float = Field(default=300, ge=5, le=7200)
    warmup_requests: int = Field(default=0, ge=0, le=100)
    scale_multiplier: int = Field(default=0, ge=0, le=1000)

    @model_validator(mode="before")
    @classmethod
    def normalize_levels(cls, data: Any):
        if not isinstance(data, dict):
            return data
        raw = data.get("levels", data.get("sweep_levels"))
        if isinstance(raw, str):
            levels = [int(x.strip()) for x in raw.split(",") if x.strip()]
        elif isinstance(raw, list):
            levels = [int(x) for x in raw]
        elif raw is None:
            levels = [1, 5, 10, 20]
        else:
            levels = [int(raw)]
        if not levels:
            raise ValueError("并发档位不能为空")
        if len(levels) > 32:
            raise ValueError("并发档位最多支持 32 档")
        if any(x < 1 or x > 4096 for x in levels):
            raise ValueError("并发档位必须在 1~4096 之间")
        data["levels"] = sorted(dict.fromkeys(levels))
        data.pop("sweep_levels", None)
        return data


class StartConfig(BaseModel):
    base_url: str
    api_key: str = ""
    model: str = ""
    api_format: str = "openai"
    timeout: float = Field(default=120, ge=1, le=7200)
    disable_thinking: bool = False
    task_name: str = ""
    accuracy_datasets: list[str] = Field(default_factory=list)
    dataset_subjects: dict[str, list[str]] = Field(default_factory=dict)
    sample_limit: int = Field(default=0, ge=0)
    few_shot: int = Field(default=0, ge=0, le=50)
    acc_concurrency: int = Field(default=4, ge=1, le=128)
    acc_max_tokens: int = Field(default=0, ge=0, le=131072)
    acc_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    acc_system: str = ""
    acc_template: str = ""
    max_retries: int = Field(default=2, ge=0, le=10)
    acc_stream: bool = False
    run_performance: bool = False
    perf: PerfConfig = Field(default_factory=PerfConfig)
    # 上下文长度扫描
    context_lengths: list[int] = Field(default_factory=list)
    context_concurrency: int = Field(default=8, ge=1, le=256)
    context_requests: int = Field(default=20, ge=1, le=10000)
    context_max_tokens: int = Field(default=256, ge=1, le=131072)
    context_stream: bool = True

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v: str):
        v = (v or "").strip().rstrip("/")
        if not v:
            raise ValueError("缺少 base_url")
        if not re.match(r"^https?://", v):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return v

    @field_validator("api_format")
    @classmethod
    def validate_api_format(cls, v: str):
        allowed = {"openai", "vllm", "ollama", "raw_completions"}
        if v not in allowed:
            raise ValueError(f"接口格式仅支持：{', '.join(sorted(allowed))}")
        return v

    @model_validator(mode="after")
    def validate_test_selection(self):
        if not self.accuracy_datasets and not self.run_performance and not self.context_lengths:
            raise ValueError("请至少选择一项测试")
        if self.api_format not in {"openai", "vllm"}:
            if self.accuracy_datasets or self.run_performance or self.context_lengths:
                raise ValueError("正式评测/压测仅支持 OpenAI 兼容接口；Ollama 原生和 Completions 仅用于连通性测试")
        return self


# ===================== 路由 =====================

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/health")
def health():
    from app import evalscope_client as es
    es_ok = es.health()
    return {"status": "ok", "evalscope": "online" if es_ok else "offline",
            "evalscope_url": es.EVALSCOPE_URL}


@app.get("/api/datasets")
def get_datasets():
    return {"datasets": list_datasets()}


@app.get("/api/datasets/{name}")
def dataset_detail(name: str):
    from app.evalscope_catalog import catalog_list
    for d in catalog_list():
        if d["name"] == name:
            return {"name": name, "display": d.get("display", name),
                    "description": d.get("desc", ""),
                    "type": d.get("type"), "lang": d.get("lang", "-"),
                    "subjects": d.get("subjects", []), "count": d.get("count", 0),
                    "default_few_shot": d.get("default_few_shot", 0)}
    for fn in os.listdir(CUSTOM_DIR):
        if fn.endswith(".jsonl") and name == f"custom:{fn}":
            path = os.path.join(CUSTOM_DIR, fn)
            try:
                items = _load_jsonl(path)
                return {"name": name, "display": f"自定义：{fn}",
                        "description": f"用户上传数据集，{len(items)} 条样本。",
                        "type": "mc" if any(k in (items[0] or {}) for k in ("A","B","C","D")) else "qa",
                        "lang": "-", "subjects": [], "count": len(items)}
            except Exception:
                pass
    raise HTTPException(404, "数据集不存在")


@app.get("/api/queue")
def queue_status():
    return manager.queue_info()


@app.post("/api/test_connection")
def test_connection(cfg: ConnTest):
    try:
        client = ModelClient(base_url=cfg.base_url, api_key=cfg.api_key,
                             model=cfg.model, api_format=cfg.api_format,
                             timeout=cfg.timeout or 30,
                             disable_thinking=cfg.disable_thinking)
        res = client.chat('你好，请回复"连接正常"四个字。', max_tokens=512)
        return {"ok": res["ok"], "error": res.get("error"),
                "latency": round(res.get("latency", 0), 3),
                "sample": (res.get("text") or "")[:100],
                "endpoint": client._endpoint()}
    except Exception as e:
        import traceback
        return {"ok": False,
                "error": f"{type(e).__name__}: {str(e)[:300]}",
                "trace": traceback.format_exc()[-400:],
                "latency": 0, "sample": ""}


@app.post("/api/preflight")
async def preflight(req: Request):
    body = await req.json()
    issues = []

    def add(level: str, title: str, message: str, **extra):
        issues.append({"level": level, "title": title, "message": message, **extra})

    base_url = (body.get("base_url") or "").strip().rstrip("/")
    api_format = body.get("api_format") or "openai"
    datasets = body.get("accuracy_datasets") or []
    run_perf = bool(body.get("run_performance"))
    ctx_lengths = body.get("context_lengths") or []
    timeout = body.get("timeout") or 30

    if not base_url:
        add("error", "接口地址缺失", "请填写被测模型的 Base URL。")
    elif not re.match(r"^https?://", base_url):
        add("error", "接口地址格式不正确", "Base URL 必须以 http:// 或 https:// 开头。")
    else:
        add("ok", "接口地址格式", f"Base URL: {base_url}")

    if api_format in {"openai", "vllm"}:
        add("ok", "正式评测接口口径", "当前接口格式可进入 evalscope 正式评测。")
    else:
        add("error", "正式评测接口口径", "evalscope 正式评测/压测需要 OpenAI 兼容接口")

    if not datasets and not run_perf and not ctx_lengths:
        add("error", "测试项缺失", "请至少选择一个精度数据集、性能压测或上下文扫描。")

    try:
        from app import evalscope_client as es
        es_ok = es.health()
        add("ok" if es_ok else "error", "evalscope service",
            f"{'在线' if es_ok else '离线'}：{es.EVALSCOPE_URL}")
    except Exception as e:
        add("error", "evalscope service", f"检查失败：{type(e).__name__}: {str(e)[:160]}")

    _check_datasets(add, datasets, body.get("dataset_subjects") or {})
    perf_info = _check_perf(add, body.get("perf") or {}, run_perf)
    _check_context_scan(add, ctx_lengths,
                        body.get("context_requests", 20),
                        body.get("context_max_tokens", 256))
    _check_disk(add)
    _check_dataset_cache(add)
    _check_model_connection(add, base_url, api_format, body, timeout)

    errors = sum(1 for x in issues if x["level"] == "error")
    warnings = sum(1 for x in issues if x["level"] == "warn")
    return {"ok": errors == 0, "errors": errors, "warnings": warnings,
            "issues": issues, "estimate": _build_estimate(datasets, body, perf_info)}


@app.post("/api/upload_dataset")
async def upload_dataset(file: UploadFile = File(...)):
    if not file.filename.endswith((".jsonl", ".json")):
        raise HTTPException(400, "仅支持 .jsonl 文件")
    fn = os.path.basename(file.filename.replace("\\", "/"))
    fn = SAFE_NAME_RE.sub("_", fn).strip("._")
    if not fn:
        raise HTTPException(400, "文件名无效")
    if not fn.endswith(".jsonl"):
        fn += ".jsonl"
    dest = os.path.join(CUSTOM_DIR, fn)
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"文件过大，最大支持 {MAX_UPLOAD_BYTES // 1024 // 1024}MB")
    with open(dest, "wb") as f:
        f.write(content)
    try:
        items = _load_jsonl(dest)
        if not items:
            raise ValueError("文件为空")
        _validate_custom_dataset(items)
    except HTTPException:
        raise
    except Exception as e:
        if os.path.exists(dest):
            os.remove(dest)
        raise HTTPException(400, f"解析失败：{e}")
    return {"ok": True, "name": f"custom:{fn}", "count": len(items)}


@app.post("/api/start")
async def start(req: Request):
    try:
        parsed = StartConfig.model_validate(await req.json())
    except Exception as e:
        raise HTTPException(400, str(e))
    cfg = parsed.model_dump()
    name = (cfg.pop("task_name", "") or "").strip()
    task = manager.create(cfg, name=name)
    qi = manager.queue_info()
    return {"task_id": task.id, "name": task.name,
            "queue_position": manager.get_queue_position(task.id),
            "queue_length": qi["queue_length"]}


@app.get("/api/tasks")
def list_tasks():
    return {"tasks": manager.list_tasks()}


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str):
    d = manager.load_detail(task_id)
    if not d:
        raise HTTPException(404, "任务不存在")
    return d


@app.post("/api/tasks/{task_id}/rename")
async def rename_task(task_id: str, req: Request):
    body = await req.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "名称不能为空")
    if not manager.rename(task_id, name):
        raise HTTPException(404, "任务不存在")
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    manager.delete(task_id)
    return {"ok": True}


@app.get("/api/tasks/{task_id}/export/{fmt}")
def export_report(task_id: str, fmt: str):
    detail = manager.load_detail(task_id)
    if not detail:
        raise HTTPException(404, "任务不存在")

    # Evalscope 原始报告
    if fmt == "evalscope":
        cfg = detail.get("config", {})
        model_name = cfg.get("model", "")
        output_dir = ""
        result = detail.get("result", {})
        # 优先从精度结果找 output_dir（evalscope eval 才生成 HTML 报告）
        acc = result.get("accuracy", {}) or {}
        for ds_data in acc.values():
            repro = ds_data.get("repro", {}) if isinstance(ds_data, dict) else {}
            od = repro.get("output_dir", "") or ds_data.get("raw", {}).get("output_dir", "")
            if od and os.path.isdir(od):
                output_dir = od
                break
        # 纯性能任务：evalscope perf 不生成 HTML 报告，告知用户
        if not output_dir:
            has_accuracy = bool(acc)
            if not has_accuracy:
                raise HTTPException(400,
                    "纯性能压测任务不生成 evalscope HTML 报告。"
                    "evalscope 原始报告仅适用于精度评测任务。"
                    "性能结果请使用 Excel/PDF 导出。")
            output_dir = _find_output_dir(model_name) or ""
        if not output_dir or not os.path.isdir(output_dir):
            raise HTTPException(404, "找不到该任务的 evalscope 输出目录，可能已被清理")
        report_path = os.path.join(output_dir, "reports", "report.html")
        if not os.path.exists(report_path):
            reports_dir = os.path.join(output_dir, "reports")
            if os.path.isdir(reports_dir):
                for root, dirs, files in os.walk(reports_dir):
                    for fn in files:
                        if fn.endswith(".html"):
                            report_path = os.path.join(root, fn)
                            break
        if not os.path.exists(report_path):
            raise HTTPException(404, "原始 evalscope HTML 报告不存在（可能已被清理）")
        fname = f"evalscope_report_{task_id}.html"
        return FileResponse(report_path, media_type="text/html; charset=utf-8", filename=fname)

    if not detail.get("result"):
        raise HTTPException(400, "该任务没有可导出的结果")
    from app import report
    try:
        if fmt == "excel":
            path = report.export_excel(detail)
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif fmt == "pdf":
            path = report.export_pdf(detail)
            media = "application/pdf"
        else:
            raise HTTPException(400, "格式只支持 excel、pdf 或 evalscope")
    except Exception as e:
        import traceback
        raise HTTPException(500, f"导出失败：{type(e).__name__}: {e}")
    fname = os.path.basename(path)
    return FileResponse(path, media_type=media, filename=fname)


@app.post("/api/tasks/{task_id}/stop")
def stop_task(task_id: str):
    t = manager.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在或已不在运行")
    # 如果排在队列中，直接从队列移除
    manager.remove_from_queue(task_id)
    t.stop()
    return {"ok": True}


@app.post("/api/tasks/{task_id}/rerun")
async def rerun_task(task_id: str, req: Request):
    body = {}
    try:
        body = await req.json()
    except Exception:
        pass
    detail = manager.load_detail(task_id)
    if not detail:
        raise HTTPException(404, "任务不存在")
    cfg = dict(detail.get("config", {}))
    if cfg.get("api_key") in (None, "", "***"):
        cfg["api_key"] = body.get("api_key", "")
    task = manager.create(cfg, name=detail.get("name", ""), task_id=task_id)
    return {"task_id": task.id, "name": task.name}


@app.get("/api/stream/{task_id}")
def stream(task_id: str):
    task = manager.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")

    def gen():
        while True:
            try:
                event = task.q.get(timeout=30)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if event is None:
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/stop/{task_id}")
def stop(task_id: str):
    task = manager.get(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    task.stop()
    return {"ok": True}


@app.get("/api/tasks/{task_id}/samples/{dataset}")
def task_samples(task_id: str, dataset: str, filter: str = "", page: int = 1, page_size: int = 50):
    """返回数据集的逐题答题详情，从 evalscope review 文件中读取。

    filter: 空=全部, wrong=仅错题
    返回 {samples: [...], total: int, page: int, page_size: int, dataset: str}
    """
    detail = manager.load_detail(task_id)
    if not detail:
        raise HTTPException(404, "任务不存在")
    cfg = detail.get("config", {})
    model_name = cfg.get("model", "")
    result = detail.get("result", {})
    acc = result.get("accuracy", {})
    ds_data = acc.get(dataset)
    if not ds_data:
        raise HTTPException(404, f"数据集 {dataset} 不在该任务结果中")

    raw = ds_data.get("raw", {})
    repro = ds_data.get("repro", {})
    # Try multiple sources for output_dir
    output_dir = repro.get("output_dir", "") or raw.get("output_dir", "")
    if not output_dir or not os.path.isdir(output_dir):
        output_dir = _find_output_dir(model_name) or ""

    if not output_dir or not os.path.isdir(output_dir):
        raise HTTPException(404, "该任务的 evalscope 输出目录不存在，无法提取答题详情")

    samples = _parse_review_files(output_dir, model_name, dataset, filter)

    total = len(samples)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "samples": samples[start:end],
        "total": total,
        "page": page,
        "page_size": page_size,
        "dataset": dataset,
        "has_more": end < total,
    }


@app.get("/api/tasks/{task_id}/perf-requests")
def task_perf_requests(task_id: str, eval_task_id: str = "", level: int = 0):
    """读取 evalscope perf 的 benchmark_data.db，返回逐请求明细。
    level=0 返回所有并发档位；指定 level 仅返回该档位。
    """
    import sqlite3, glob as _glob

    if not eval_task_id:
        # 尝试从任务配置推断
        detail = manager.load_detail(task_id)
        if not detail:
            raise HTTPException(404, "任务不存在")
        perf = (detail.get("result") or {}).get("performance") or {}
        eval_task_id = perf.get("eval_task_id", "")

    if not eval_task_id:
        raise HTTPException(400, "缺少 eval_task_id")

    outputs_root = "/app/outputs"
    perf_dir = os.path.join(outputs_root, eval_task_id, "perf")
    if not os.path.isdir(perf_dir):
        raise HTTPException(404, f"evalscope 输出目录不存在：{perf_dir}")

    # 查找所有 parallel_X_number_Y 子目录
    pattern = os.path.join(perf_dir, "parallel_*_number_*")
    sub_dirs = sorted(_glob.glob(pattern))

    if level > 0:
        sub_dirs = [d for d in sub_dirs if os.path.basename(d).startswith(f"parallel_{level}_")]

    all_data: dict[str, list[dict]] = {}
    for d in sub_dirs:
        db_path = os.path.join(d, "benchmark_data.db")
        if not os.path.isfile(db_path):
            continue
        try:
            db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cols = [c[1] for c in db.execute("PRAGMA table_info(result)").fetchall()]
            rows = db.execute("SELECT * FROM result ORDER BY start_time").fetchall()
            db.close()

            level_name = os.path.basename(d)
            records = []
            for row in rows:
                rec = dict(zip(cols, row))
                # 解析 request JSON 获取 prompt 摘要
                prompt_text = ""
                try:
                    req = json.loads(rec.get("request", "{}"))
                    msgs = req.get("messages", [])
                    if msgs:
                        content = msgs[-1].get("content", "")
                        prompt_text = content[:200] if isinstance(content, str) else str(content)[:200]
                except Exception:
                    pass
                # 解析 inter_token_latencies (可能是 JSON 字符串)
                itl = rec.get("inter_token_latencies")
                if isinstance(itl, str):
                    try:
                        itl = json.loads(itl)
                    except Exception:
                        itl = []
                itl_list = itl if isinstance(itl, list) else []
                records.append({
                    "prompt_preview": prompt_text,
                    "prompt_tokens": rec.get("prompt_tokens"),
                    "completion_tokens": rec.get("completion_tokens"),
                    "latency": rec.get("latency"),
                    "ttft": rec.get("first_chunk_latency"),
                    "tpot": rec.get("time_per_output_token"),
                    "inter_token_latencies": itl_list,
                    "itl_count": len(itl_list),
                    "success": bool(rec.get("success")),
                    "start_time": rec.get("start_time"),
                })
            all_data[level_name] = records
        except Exception as e:
            all_data[os.path.basename(d)] = [{"error": str(e)}]

    return {
        "eval_task_id": eval_task_id,
        "levels": all_data,
        "level_count": len(all_data),
        "total_requests": sum(len(v) for v in all_data.values()),
    }


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ===================== 辅助函数 =====================

def _find_output_dir(model_name: str) -> str | None:
    """在 /app/outputs 中搜索包含指定模型 review 文件的输出目录。"""
    outputs_root = "/app/outputs"
    if not os.path.isdir(outputs_root):
        return None
    # 优先精确匹配 model_name
    for task_dir in sorted(os.listdir(outputs_root), reverse=True):
        reviews_dir = os.path.join(outputs_root, task_dir, "reviews")
        if not os.path.isdir(reviews_dir):
            continue
        for sub in os.listdir(reviews_dir):
            if os.path.isdir(os.path.join(reviews_dir, sub)) and sub == model_name:
                # Found exact model match - verify it has actual data
                for fn in os.listdir(os.path.join(reviews_dir, sub)):
                    if fn.endswith(".jsonl"):
                        return os.path.join(outputs_root, task_dir)
    # Fallback: any directory with review JSONL files (for model name variations)
    for task_dir in sorted(os.listdir(outputs_root), reverse=True):
        reviews_dir = os.path.join(outputs_root, task_dir, "reviews")
        if not os.path.isdir(reviews_dir):
            continue
        for sub in os.listdir(reviews_dir):
            sub_path = os.path.join(reviews_dir, sub)
            if not os.path.isdir(sub_path):
                continue
            # Check if any part of model name matches
            if model_name and (model_name in sub or sub in model_name):
                for fn in os.listdir(sub_path):
                    if fn.endswith(".jsonl"):
                        return os.path.join(outputs_root, task_dir)
    return None


def _parse_review_files(output_dir: str, model_name: str, dataset: str, filter_wrong: str) -> list[dict]:
    """从 evalscope review 文件中解析逐题答题详情。"""
    import re as _re
    reviews_root = os.path.join(output_dir, "reviews")
    if not os.path.isdir(reviews_root):
        return []

    # Find the model subdirectory
    model_dir = None
    for sub in os.listdir(reviews_root):
        sub_path = os.path.join(reviews_root, sub)
        if os.path.isdir(sub_path):
            model_dir = sub_path
            break
    if not model_dir:
        return []

    # Collect JSONL files matching the dataset name
    jsonl_files = []
    for fn in sorted(os.listdir(model_dir)):
        if fn.endswith(".jsonl") and fn.startswith(dataset):
            jsonl_files.append(os.path.join(model_dir, fn))

    samples = []
    for jf in jsonl_files:
        subset_name = os.path.basename(jf)[len(dataset):].lstrip("_").replace(".jsonl", "") or dataset
        try:
            with open(jf, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sample = _extract_sample(row, subset_name)
                    if not sample:
                        continue
                    if filter_wrong == "wrong" and sample.get("is_correct"):
                        continue
                    samples.append(sample)
        except Exception:
            continue

    return samples


def _extract_sample(row: dict, subset_name: str) -> dict | None:
    """从单条 review 记录提取答题详情。"""
    import re as _re
    target = row.get("target", "")
    score = row.get("sample_score") if "sample_score" in row else row.get("score")

    messages = row.get("messages") or []
    # Last user message is the actual question (earlier ones are few-shot examples)
    question_text = ""
    options = {}
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                question_text, options = _parse_question_options(content)
                break

    # Assistant message: extract reasoning and text answer
    reasoning = ""
    answer_text = ""
    for msg in messages:
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, list):
                for part in content:
                    if part.get("type") == "reasoning":
                        reasoning = part.get("reasoning", "")
                    elif part.get("type") == "text":
                        answer_text = part.get("text", "")
            elif isinstance(content, str):
                answer_text = content
            break

    if not question_text:
        return None

    # Extract model's predicted choice from answer text
    prediction = row.get("prediction", "")
    if not prediction and answer_text:
        m = _re.search(r"答案[：:]\s*([A-F])", answer_text)
        if m:
            prediction = m.group(1)

    is_correct = False
    if score is not None:
        try:
            is_correct = float(score) >= 1.0
        except (ValueError, TypeError):
            pass
    if not is_correct and target and prediction:
        is_correct = str(target).strip().upper() == str(prediction).strip().upper()

    options_list = [{"key": k, "text": v} for k, v in options.items()]

    return {
        "index": row.get("index", 0),
        "subset": subset_name,
        "question": question_text[:800],
        "options": options_list,
        "target": target,
        "prediction": prediction,
        "is_correct": is_correct,
        "reasoning": reasoning[:3000] if reasoning else "",
        "answer_text": answer_text[:2000] if answer_text else "",
    }


def _parse_question_options(content: str) -> tuple[str, dict]:
    """从用户消息中分离题目文本和选项。"""
    import re as _re
    # Find the actual question (last question in the content, after few-shot examples)
    # Pattern: "问题：..." or just find the last occurrence
    parts = content.split("\n\n")
    question_text = ""
    options = {}

    # Try to find the last "问题：" block
    question_blocks = list(_re.finditer(r"问题[：:]\s*(.+?)(?=\n选项[：:]|\n[A-F][\.\s]|\Z)", content, _re.DOTALL))
    if question_blocks:
        question_text = question_blocks[-1].group(1).strip()[:800]

    # Parse options from the last block
    option_blocks = list(_re.finditer(r"选项[：:]\s*\n(.+?)(?=\n\n|\n解析|\n答案|\Z)", content, _re.DOTALL))
    if option_blocks:
        opt_text = option_blocks[-1].group(1)
        for m in _re.finditer(r"([A-F])[\.\s]\s*(.+?)(?=\n[A-F][\.\s]|\Z)", opt_text, _re.DOTALL):
            options[m.group(1)] = m.group(2).strip()[:200]

    if not question_text:
        # Fallback: use the entire last chunk as question
        question_text = content[-500:].strip()

    return question_text, options


def _validate_custom_dataset(items: list[dict]):
    for i, row in enumerate(items, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {i} 行不是 JSON object")
        question = row.get("question") or row.get("query")
        answer = row.get("answer") or row.get("response")
        if not str(question or "").strip():
            raise ValueError(f"第 {i} 行缺少 question/query")
        if not str(answer or "").strip():
            raise ValueError(f"第 {i} 行缺少 answer/response")
        opts = [k for k in ("A", "B", "C", "D") if str(row.get(k, "")).strip()]
        if opts:
            missing = [k for k in ("A", "B", "C", "D") if not str(row.get(k, "")).strip()]
            if missing:
                raise ValueError(f"第 {i} 行选择题选项不完整，缺少 {','.join(missing)}")
            if str(answer).strip().upper() not in {"A", "B", "C", "D", "E", "F"}:
                raise ValueError(f"第 {i} 行选择题 answer 应为选项字母")


def _check_datasets(add, selected: list[str], subject_map: dict[str, list[str]]):
    datasets = list_datasets()
    by_name = {d["name"]: d for d in datasets}
    if not selected:
        return
    missing = [d for d in selected if d not in by_name]
    if missing:
        add("error", "精度数据集不可用", f"以下数据集不在当前目录中：{', '.join(missing)}")
    else:
        total = 0
        known = 0
        for name in selected:
            count = by_name[name].get("count") or 0
            if count:
                total += int(count)
                known += 1
        if known:
            add("ok", "精度数据集", f"已选择 {len(selected)} 个数据集，已知样本约 {total} 条。")
        else:
            add("ok", "精度数据集", f"已选择 {len(selected)} 个数据集；样本数由 evalscope 运行时读取。")
    for ds, subjects in subject_map.items():
        if not subjects:
            continue
        item = by_name.get(ds)
        if not item:
            continue
        available = set(item.get("subjects") or [])
        if available:
            invalid = [s for s in subjects if s not in available]
            if invalid:
                add("error", "学科子集不可用", f"{ds} 中不存在：{', '.join(invalid)}")
            else:
                add("ok", "学科子集", f"{ds} 仅评测 {len(subjects)} 个子集。")


def _check_perf(add, raw_perf: dict, run_perf: bool) -> dict:
    if not run_perf:
        return {}
    try:
        perf = PerfConfig.model_validate(raw_perf)
    except Exception as e:
        add("error", "性能压测配置", str(e))
        return {}
    total_requests = len(perf.levels) * perf.requests_per_level
    peak = max(perf.levels)
    token_budget = total_requests * (perf.max_tokens + (perf.context_length or 512))
    add("ok", "性能压测配置",
        f"{len(perf.levels)} 个并发档位，峰值并发 {peak}，总请求 {total_requests}。")
    if total_requests > 5000:
        add("warn", "压测规模较大", f"本次压测总请求 {total_requests}，可能耗时较长并明显占用模型服务。")
    if peak >= 256:
        add("warn", "峰值并发较高", f"最高并发 {peak}，请确认被测服务和网关限流配置。")
    if token_budget > 2_000_000:
        add("warn", "Token 消耗较高", f"粗略估算请求 token 规模超过 {token_budget:,}，建议先小样本试跑。")
    return {"levels": perf.levels, "requests_per_level": perf.requests_per_level,
            "total_requests": total_requests, "token_budget": token_budget}


def _check_context_scan(add, ctx_lengths: list, ctx_requests: int = 20, ctx_max_tokens: int = 256):
    if not ctx_lengths:
        return
    if len(ctx_lengths) > 20:
        add("error", "上下文扫描", "最多支持 20 个长度档位。")
    elif len(ctx_lengths) > 10:
        add("warn", "上下文扫描",
            f"共 {len(ctx_lengths)} 个档位可能耗时较长，建议先跑 2-3 个验证模型能力。")
    else:
        total_req = len(ctx_lengths) * ctx_requests
        add("ok", "上下文扫描",
            f"{len(ctx_lengths)} 个长度档位：{ctx_lengths} tokens · {total_req} 请求")


def _check_disk(add):
    path = os.path.abspath(DATA_DIR)
    os.makedirs(path, exist_ok=True)
    try:
        usage = shutil.disk_usage(path)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 1:
            add("error", "磁盘空间不足", f"数据目录剩余 {free_gb:.1f} GB，报告和日志可能写入失败。")
        elif free_gb < 5:
            add("warn", "磁盘空间偏低", f"数据目录剩余 {free_gb:.1f} GB，长任务建议预留更多空间。")
        else:
            add("ok", "磁盘空间", f"数据目录剩余 {free_gb:.1f} GB。")
    except Exception as e:
        add("warn", "磁盘空间", f"无法检查：{type(e).__name__}: {str(e)[:120]}")


def _check_dataset_cache(add):
    cache = os.environ.get("MODELSCOPE_CACHE", "/opt/modelscope_cache")
    if os.path.exists(cache):
        add("ok", "数据集缓存目录", f"已找到 {cache}。")
    else:
        add("warn", "数据集缓存目录", f"未找到 {cache}；若在离线环境运行，请确认镜像内已预置数据集。")


def _check_model_connection(add, base_url: str, api_format: str, body: dict, timeout):
    if not base_url or not re.match(r"^https?://", base_url):
        add("warn", "模型连通性", "接口地址无效，跳过连通性探测。")
        return
    try:
        client = ModelClient(base_url=base_url, api_key=body.get("api_key", ""),
                             model=body.get("model", ""), api_format=api_format,
                             timeout=min(float(timeout or 30), 30),
                             disable_thinking=body.get("disable_thinking", False))
        res = client.test_connection()
        if res.get("ok"):
            add("ok", "模型连通性", f"连接正常，延迟 {res.get('latency')}s。")
        else:
            add("error", "模型连通性", res.get("error") or "请求失败。")
    except Exception as e:
        add("error", "模型连通性", f"{type(e).__name__}: {str(e)[:180]}")


def _build_estimate(datasets: list[str], body: dict, perf_info: dict) -> dict:
    ds_map = {d["name"]: d for d in list_datasets()}
    sample_limit = int(body.get("sample_limit") or 0)
    acc_known = 0
    acc_unknown = 0
    for name in datasets:
        count = int(ds_map.get(name, {}).get("count") or 0)
        if count:
            acc_known += min(count, sample_limit) if sample_limit else count
        else:
            acc_unknown += 1
    ctx_count = len(body.get("context_lengths") or [])
    return {
        "accuracy_known_samples": acc_known,
        "accuracy_unknown_datasets": acc_unknown,
        "perf_requests": perf_info.get("total_requests", 0),
        "perf_token_budget": perf_info.get("token_budget", 0),
        "context_scan_levels": ctx_count,
    }
