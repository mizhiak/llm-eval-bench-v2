"""任务管理器：调度精度/性能评测，实时推送进度日志。"""
import os
import time
import uuid
import json
import queue
import logging
import threading
import collections

from app.model_client import ModelClient

logger = logging.getLogger("task_manager")


CUSTOM_DIR = os.path.join(os.path.dirname(__file__), "..", "custom_datasets")
os.makedirs(CUSTOM_DIR, exist_ok=True)


def _load_jsonl(path: str) -> list[dict]:
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _desc_trunc(desc) -> str:
    """从可能为 dict 的 desc 中提取摘要文本。"""
    if isinstance(desc, dict):
        desc = desc.get("full") or desc.get("short") or ""
    if isinstance(desc, str):
        return desc[:120]
    return str(desc)[:120]


def list_datasets() -> list[dict]:
    """返回可用数据集。"""
    from app.evalscope_catalog import catalog_list
    out = []
    for d in catalog_list():
        out.append({
            "name": d["name"], "display": d["display"], "type": d["type"],
            "lang": d.get("lang", "-"), "subjects": d.get("subjects", []),
            "count": d.get("count", 0),
            "default_few_shot": d.get("default_few_shot", 0),
            "builtin": True,
            "desc": _desc_trunc(d.get("desc", "")),
        })
    for fn in sorted(os.listdir(CUSTOM_DIR)):
        if fn.endswith(".jsonl"):
            path = os.path.join(CUSTOM_DIR, fn)
            try:
                items = _load_jsonl(path)
                dtype = _detect_type(items)
                out.append({"name": f"custom:{fn}", "display": f"自定义：{fn}",
                            "count": len(items), "subjects": [], "builtin": False,
                            "type": dtype, "lang": "-",
                            "desc": "用户上传，经 evalscope general_mcq/general_qa 评测"})
            except Exception:
                pass
    return out


def _detect_type(items: list[dict]) -> str:
    if items and any(k in items[0] for k in ("A", "B", "C", "D")):
        return "mc"
    return "qa"


DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "tasks")
os.makedirs(DATA_DIR, exist_ok=True)

# 回收站保留策略：默认 0 = 永久保留（评测历史可长期追溯）；
# 设置为正数 N 时，启动阶段自动清除删除超过 N 天的回收站文件。
TRASH_RETENTION_DAYS = int(os.environ.get("TRASH_RETENTION_DAYS", "0"))


class Task:
    # 每个 SSE 客户端独立订阅队列的最大容量（慢客户端丢最旧事件，不阻塞评测线程）
    SUBSCRIBER_MAXSIZE = 2000

    def __init__(self, task_id: str, config: dict, name: str = ""):
        self.id = task_id
        self.config = config
        self.name = name or self._default_name(config)
        self.status = "pending"  # pending|queued|running|stopping|done|error|stopped
        self.result = {}
        self._stop = False
        self.created = time.time()
        self.finished_at = None
        self.logs: list[dict] = []
        self.sweep_levels: list[dict] = []
        self.last_progress: dict = {}
        self.deleted = False  # 运行中被删除：停止持久化，防止任务“复活”
        # 当前阶段活动的 evalscope task_id：持久化供重启后级联取消孤儿任务
        self.es_task_id: str | None = None
        # SSE 广播：多个标签页同时观看同一任务时，事件广播到每个订阅者
        # （旧实现是单队列，事件被多客户端瓜分，日志会串页）
        self._subs: list[queue.Queue] = []
        self._subs_lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        """为新的 SSE 连接创建独立订阅队列。"""
        q = queue.Queue(maxsize=self.SUBSCRIBER_MAXSIZE)
        with self._subs_lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._subs_lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def _publish(self, event):
        """把事件广播给所有订阅者；None 哨兵表示流结束，也必须送达。"""
        with self._subs_lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                # 慢客户端：丢掉最旧事件再塞入，避免阻塞评测线程
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except queue.Empty:
                    pass

    @staticmethod
    def _default_name(config: dict) -> str:
        model = config.get("model") or "未命名模型"
        ts = time.strftime("%m-%d %H:%M", time.localtime())
        return f"{model} · {ts}"

    def _persist(self):
        if self.deleted:
            return  # 运行中被删除的任务不再落盘
        try:
            path = os.path.join(DATA_DIR, f"{self.id}.json")
            data = {
                "id": self.id, "name": self.name, "status": self.status,
                # api_key 脱敏后再落盘，避免明文密钥留在磁盘（rerun 会重新要求输入）
                "config": _safe_config(self.config), "result": _sanitize_json(self.result),
                "created": self.created, "finished_at": self.finished_at,
                "logs": self.logs[-500:], "sweep_levels": self.sweep_levels,
                "es_task_id": self.es_task_id,
            }
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            logger.exception("任务 %s 持久化失败", self.id)

    def log(self, level: str, msg: str, **extra):
        event = {"type": "log", "level": level, "msg": msg, "ts": time.time()}
        event.update(extra)
        self.logs.append(event)
        if len(self.logs) > 2000:  # 内存日志设上限，长任务不无限增长
            self.logs = self.logs[-1500:]
        self._publish(event)

    def progress(self, **kw):
        self.last_progress = kw
        self._publish({"type": "progress", **kw})

    def emit(self, event: dict):
        if event.get("type") == "sweep_level":
            self.sweep_levels.append(event)
        self._publish(event)

    def finish(self, result: dict):
        self.status = "done"
        self.result = result
        self.finished_at = time.time()
        self._persist()
        self._publish({"type": "done", "result": result})
        self._publish(None)

    def fail(self, err: str):
        self.status = "error"
        self.finished_at = time.time()
        self.logs.append({"type": "log", "level": "error", "msg": err, "ts": time.time()})
        self._persist()
        self._publish({"type": "error", "msg": err})
        self._publish(None)

    def mark_running(self):
        self.status = "running"
        self._persist()

    def stop(self):
        self._stop = True
        # 排队中（尚未启动）的任务直接落 stopped：
        # 旧逻辑统一置 stopping，但 queued 任务永远等不到 evalscope 收尾，会永远卡"停止中"
        if self.status in ("pending", "queued"):
            self.status = "stopped"
            self.finished_at = time.time()
            self.log("warn", "任务在排队中被停止，未实际执行评测。")
            self._persist()
            self._publish({"type": "done", "result": self.result, "stopped": True})
            self._publish(None)
            return
        if self.status == "running":
            self.status = "stopping"
            # 队列移除由 TaskManager.delete/stop 处理
            self.log("warn", "已收到停止请求。正在请求 evalscope 取消任务...")
            self._persist()
            # 级联停止 evalscope 任务
            try:
                from app.evalscope_runner import get_active_eval_task, clear_eval_task
                eval_id = get_active_eval_task(self.id)
                if eval_id:
                    from app import evalscope_client as es
                    es.stop_eval(eval_id)
                    es.stop_perf(eval_id)
                    clear_eval_task(self.id)
            except Exception:
                pass

    def stopped(self):
        return self._stop


def _safe_config(config: dict) -> dict:
    c = dict(config)
    if c.get("api_key"):
        c["api_key"] = "***"
    return c


def _sanitize_json(obj):
    """递归移除 JSON 不支持的 NaN/Infinity 值，替换为 None。"""
    import math
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_json(v) for v in obj]
    return obj


def _result_summary(result: dict, config: dict = None) -> dict:
    """从结果里提取列表页用的精简摘要，含配置关键信息。"""
    if not result:
        return {}
    out = {}
    acc_r = result.get("accuracy") or {}
    if acc_r:
        out["accuracy"] = {k: v.get("accuracy") for k, v in acc_r.items()}
    perf_r = result.get("performance") or {}
    best = perf_r.get("best") if isinstance(perf_r, dict) else None
    if best:
        out["perf_best"] = {"concurrency": best.get("concurrency"), "rps": best.get("rps")}
    ctx_r = result.get("context_scan") or {}
    if ctx_r:
        out["context_scan"] = {"levels": len(ctx_r.get("sweep", []))}
    if config:
        out["datasets"] = config.get("accuracy_datasets", [])
        out["perf_enabled"] = config.get("run_performance", False)
        out["context_scan_enabled"] = bool(config.get("context_lengths", []))
        out["few_shot"] = config.get("few_shot", 0)
        out["sample_limit"] = config.get("sample_limit", 0) or None
        pc = config.get("perf", {})
        if pc:
            out["perf_levels"] = pc.get("levels", [])
            out["context_length"] = pc.get("context_length", 0) or None
        sv = config.get("serving")
        if sv:
            out["serving"] = {
                "container": sv.get("container"),
                "image": sv.get("image"),
                "label": sv.get("label"),
                "models": sv.get("models", []),
            }
    return out


class TaskManager:
    MAX_CONCURRENT = 3  # 最多同时运行的任务数

    def __init__(self):
        self.tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._queue = collections.deque()
        self._running_task_ids: set[str] = set()
        self._queue_lock = threading.Lock()
        self._reap_zombies()

    def _reap_zombies(self):
        """启动时扫描磁盘：把 status=running/stopping 但进程已不在内存的任务标记为
        stopped，并尽力向 evalscope service 级联取消遗留任务；同时清理过期回收站文件。"""
        if not os.path.isdir(DATA_DIR):
            return
        now = time.time()
        for fn in os.listdir(DATA_DIR):
            path = os.path.join(DATA_DIR, fn)
            # 回收站文件：仅在显式设置保留天数时自动清理（默认永久保留，保证可追溯）
            if fn.endswith(".json.trash"):
                try:
                    if TRASH_RETENTION_DAYS > 0 and \
                            time.time() - os.path.getmtime(path) > TRASH_RETENTION_DAYS * 86400:
                        os.remove(path)
                except Exception:
                    pass
                continue
            if not fn.endswith(".json"):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("status") not in ("running", "stopping"):
                    continue
                d["status"] = "stopped"
                d["finished_at"] = d.get("finished_at") or now
                d.setdefault("logs", []).append({
                    "type": "log", "level": "warn",
                    "msg": "服务重启时进程中断，已自动标记为 stopped（实际未完成评测）",
                    "ts": now,
                })
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False)
                os.replace(tmp, path)
                # 级联取消 evalscope 侧遗留任务（若 service 仍存活）
                es_id = d.get("es_task_id")
                if es_id:
                    try:
                        from app import evalscope_client as es
                        es.stop_eval(es_id)
                        es.stop_perf(es_id)
                        logger.warning("重启清理：已尝试取消遗留 evalscope 任务 %s", es_id)
                    except Exception:
                        pass
            except Exception:
                pass

    def create(self, config: dict, name: str = "", task_id: str | None = None) -> Task:
        tid = task_id or uuid.uuid4().hex[:12]
        task = Task(tid, config, name=name)
        with self._lock:
            self.tasks[tid] = task
        task._persist()
        # 入队而非直接启动
        with self._queue_lock:
            self._queue.append(task)
        self._process_queue()
        return task

    def _process_queue(self):
        """如果未达并发上限，从队列取任务启动。"""
        while True:
            with self._queue_lock:
                if len(self._running_task_ids) >= self.MAX_CONCURRENT:
                    return
                if not self._queue:
                    return
                task = self._queue.popleft()
                self._running_task_ids.add(task.id)
            threading.Thread(target=self._run_wrapper, args=(task,), daemon=True).start()

    def _run_wrapper(self, task: Task):
        try:
            self._run(task)
        finally:
            with self._queue_lock:
                self._running_task_ids.discard(task.id)
            self._process_queue()

    def _on_task_complete(self, task: Task):
        """已废弃，保留兼容；实际逻辑在 _run_wrapper finally 中。"""
        pass

    def remove_from_queue(self, task_id: str):
        """从等待队列中移除任务（如果还在排队）。"""
        with self._queue_lock:
            self._queue = collections.deque(
                t for t in self._queue if t.id != task_id
            )

    def get_queue_position(self, task_id: str) -> int | None:
        """返回任务在队列中的位置（0-based），不在队列返回 None。"""
        with self._queue_lock:
            for i, t in enumerate(self._queue):
                if t.id == task_id:
                    return i + 1  # 1-based for display
        return None

    def queue_info(self) -> dict:
        """返回当前队列状态。"""
        # _running_task_ids 统一由 _queue_lock 保护，先取快照再遍历，避免迭代中变更
        with self._queue_lock:
            running_ids = list(self._running_task_ids)
            queue_list = [{"id": t.id, "name": t.name} for t in self._queue]
        running = []
        with self._lock:
            for tid in running_ids:
                t = self.tasks.get(tid)
                if t:
                    running.append({"id": t.id, "name": t.name, "status": t.status})
        return {
            "running": running,
            "queue": queue_list,
            "queue_length": len(queue_list),
            "running_count": len(running),
            "max_concurrent": self.MAX_CONCURRENT,
        }

    def rerun(self, tid: str) -> Task | None:
        detail = self.load_detail(tid)
        if not detail:
            return None
        cfg = detail.get("config", {})
        if cfg.get("api_key") == "***":
            cfg = dict(cfg)
            cfg["api_key"] = ""
        return self.create(cfg, name=detail.get("name", ""), task_id=tid)

    def get(self, tid: str) -> Task | None:
        return self.tasks.get(tid)

    def list_tasks(self) -> list[dict]:
        seen = {}
        for t in self.tasks.values():
            duration = None
            if t.finished_at:
                duration = round(t.finished_at - t.created, 1)
            elif t.status == "running":
                duration = round(time.time() - t.created, 1)
            seen[t.id] = {
                "id": t.id, "name": t.name, "status": t.status,
                "created": t.created, "finished_at": t.finished_at,
                "model": t.config.get("model", ""),
                "base_url": (t.config.get("base_url") or "")[:50],
                "duration": duration,
                "queue_position": self.get_queue_position(t.id),
                "summary": _result_summary(t.result, t.config),
            }
        if os.path.isdir(DATA_DIR):
            for fn in os.listdir(DATA_DIR):
                if not fn.endswith(".json"):
                    continue
                tid = fn[:-5]
                if tid in seen:
                    continue
                try:
                    with open(os.path.join(DATA_DIR, fn), encoding="utf-8") as f:
                        d = json.load(f)
                    created = d.get("created")
                    finished = d.get("finished_at")
                    dur = round(finished - created, 1) if (finished and created) else None
                    status = d.get("status")
                    # 容器重启后，遗留的 running 任务实际已停止
                    if status == "running":
                        status = "stopped"
                    seen[tid] = {
                        "id": d["id"], "name": d.get("name", tid),
                        "status": status, "created": created,
                        "finished_at": finished,
                        "model": d.get("config", {}).get("model", ""),
                        "base_url": (d.get("config", {}).get("base_url") or "")[:50],
                        "duration": dur,
                        "summary": _result_summary(d.get("result", {}), d.get("config")),
                    }
                except Exception:
                    pass
        return sorted(seen.values(), key=lambda x: x.get("created") or 0, reverse=True)

    def _trim_logs(self, logs: list, max_logs: int = 500) -> list:
        """Trim logs to max_logs, preserving important entries."""
        if len(logs) <= max_logs:
            return logs
        important = []
        regular = []
        for l in logs:
            lvl = l.get("level", "") if isinstance(l, dict) else ""
            msg = l.get("msg", "") if isinstance(l, dict) else str(l)
            if lvl in ("success", "error", "warn") or "完成" in msg or "失败" in msg or "开始" in msg:
                important.append(l)
            else:
                regular.append(l)
        result = regular[-(max_logs - len(important)):] + important[-50:]
        return result[-max_logs:]

    def load_detail(self, tid: str) -> dict | None:
        t = self.tasks.get(tid)
        if t:
            duration = None
            if t.finished_at:
                duration = round(t.finished_at - t.created, 1)
            elif t.status == "running":
                duration = round(time.time() - t.created, 1)
            result = t.result
            from app.evalscope_client import renormalize_stored_result
            renormalize_stored_result(result)
            return {
                "id": t.id, "name": t.name, "status": t.status,
                "config": _safe_config(t.config), "result": result,
                "created": t.created, "finished_at": t.finished_at,
                "duration": duration,
                "logs": self._trim_logs(t.logs), "sweep_levels": t.sweep_levels,
                "queue_position": self.get_queue_position(t.id),
            }
        path = os.path.join(DATA_DIR, f"{tid}.json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                created = d.get("created")
                finished = d.get("finished_at")
                dur = round(finished - created, 1) if (finished and created) else None
                d["duration"] = dur
                # 容器重启后，遗留的 running 任务实际已停止
                if d.get("status") == "running":
                    d["status"] = "stopped"
                # 脱敏 api_key
                d["config"] = _safe_config(d.get("config", {}))
                # 从 raw 字段重提 by_subject/by_category（修复旧版数据）
                from app.evalscope_client import renormalize_stored_result
                renormalize_stored_result(d.get("result"))
                d["logs"] = self._trim_logs(d.get("logs") or [])
                return d
            except Exception:
                return None
        return None

    def rename(self, tid: str, name: str) -> bool:
        t = self.tasks.get(tid)
        if t:
            t.name = name
            t._persist()
            return True
        path = os.path.join(DATA_DIR, f"{tid}.json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                d["name"] = name
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False)
                os.replace(tmp, path)  # 原子替换，避免中途崩溃损坏任务文件
                return True
            except Exception:
                return False
        return False

    def delete(self, tid: str) -> bool:
        # 如果任务在队列中，先移除
        with self._queue_lock:
            self._queue = collections.deque(
                t for t in self._queue if t.id != tid
            )
        t = self.tasks.pop(tid, None)
        if t is not None:
            # 运行中的任务：标记 deleted 阻止后续持久化（防“复活”），并级联停止
            t.deleted = True
            try:
                t.stop()
            except Exception:
                logger.exception("停止任务 %s 时出错", tid)
        path = os.path.join(DATA_DIR, f"{tid}.json")
        if os.path.exists(path):
            try:
                # 软删除：改名进回收站而非直接删除，支持 restore 撤销
                os.replace(path, path + ".trash")
            except Exception:
                return False
        return True

    def restore(self, tid: str) -> bool:
        """把软删除的任务恢复回任务列表。"""
        path = os.path.join(DATA_DIR, f"{tid}.json")
        trash = path + ".trash"
        if not os.path.exists(trash):
            return False
        if os.path.exists(path):
            return False  # 同名任务已存在（如已重新创建）
        try:
            os.replace(trash, path)
            return True
        except Exception:
            logger.exception("恢复任务 %s 失败", tid)
            return False

    def list_trashed(self) -> list[dict]:
        """列出回收站中的任务（软删除但未彻底删除），支持追溯与恢复。"""
        out = []
        if not os.path.isdir(DATA_DIR):
            return out
        for fn in sorted(os.listdir(DATA_DIR), reverse=True):
            if not fn.endswith(".json.trash"):
                continue
            path = os.path.join(DATA_DIR, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                out.append({
                    "id": d.get("id", fn[:-11]),
                    "name": d.get("name", d.get("id", fn[:-11])),
                    "status": d.get("status", "unknown"),
                    "model": (d.get("config") or {}).get("model", ""),
                    "created": d.get("created"),
                    "finished_at": d.get("finished_at"),
                    "deleted_at": os.path.getmtime(path),
                })
            except Exception:
                logger.exception("读取回收站文件 %s 失败", fn)
        return out

    def purge(self, tid: str) -> bool:
        """彻底删除回收站中的任务（不可恢复）。"""
        trash = os.path.join(DATA_DIR, f"{tid}.json.trash")
        if not os.path.exists(trash):
            return False
        try:
            os.remove(trash)
            return True
        except Exception:
            logger.exception("彻底删除任务 %s 失败", tid)
            return False

    def _make_client(self, cfg: dict) -> ModelClient:
        return ModelClient(
            base_url=cfg["base_url"], api_key=cfg.get("api_key", ""),
            model=cfg.get("model", ""), api_format=cfg.get("api_format", "openai"),
            timeout=cfg.get("timeout", 120),
            disable_thinking=cfg.get("disable_thinking", False),
        )

    def _run(self, task: Task):
        cfg = task.config
        # 出队后、启动前被停止的任务直接退出，避免被 mark_running 复活
        if task.stopped():
            return
        task.mark_running()
        try:
            task.log("info", f"开始评测，模型：{cfg.get('model','(未指定)')}，"
                             f"接口格式：{cfg.get('api_format')}")
            if cfg.get("eval_warning"):
                task.log("warn", cfg["eval_warning"])

            client = self._make_client(cfg)
            task.log("info", "正在测试模型连通性...")
            conn = client.test_connection()
            if not conn["ok"]:
                task.fail(f"连通性测试失败：{conn['error']}")
                return
            task.log("success", f"连通性正常（延迟 {conn['latency']}s）")

            # 溯源：关联被测服务容器名/镜像 + 抓取目标服务模型列表（best-effort）
            try:
                from app import serving
                sv: dict = {}
                info = serving.resolve_serving(cfg.get("base_url", ""))
                if info:
                    sv.update(info)
                models = serving.probe_models(cfg.get("base_url", ""), cfg.get("api_key", ""))
                if models:
                    sv["models"] = models
                label = (cfg.get("serving_label") or "").strip()
                if label:
                    sv["label"] = label
                if sv:
                    task.config["serving"] = sv
                    task._persist()
                    if info:
                        task.log("info", f"被测服务容器：{info['container']}（{info['image']}）")
                    if models:
                        task.log("info", f"目标服务模型列表：{', '.join(models[:5])}"
                                          f"{'…' if len(models) > 5 else ''}")
            except Exception:
                pass

            from app import evalscope_client as es
            if not es.health():
                task.log("warn", "⚠ evalscope service 未就绪。评测依赖容器内的 "
                                "evalscope service（默认 9000 端口）。请确认其已启动。")

            summary = {"accuracy": {}, "performance": {}}

            # 精度评测
            if cfg.get("accuracy_datasets") and not task.stopped():
                from app.evalscope_runner import run_accuracy_evalscope
                run_accuracy_evalscope(task, cfg, summary)

            # 性能压测
            if cfg.get("run_performance") and not task.stopped():
                from app.evalscope_runner import run_performance_evalscope
                run_performance_evalscope(task, cfg, summary)

            # 上下文长度扫描
            if cfg.get("context_lengths") and not task.stopped():
                from app.evalscope_runner import run_context_scan
                run_context_scan(task, cfg, summary)

            if task.stopped():
                task.status = "stopped"
                task.finished_at = time.time()
                task.result = summary
                task._persist()
                task.emit({"type": "done", "result": summary, "stopped": True})
                task._publish(None)
            else:
                task.finish(summary)
        except Exception as e:
            import traceback
            task.fail(f"{type(e).__name__}: {e}\n{traceback.format_exc()[:500]}")


manager = TaskManager()
