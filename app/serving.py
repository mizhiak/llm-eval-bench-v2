"""被测模型服务溯源。

宿主机侧的 docker_services_dump.sh（cron 每分钟）把 docker 端口→容器映射导出到
共享卷（/app/outputs/docker_services.json）。本模块据此把任务填写的 base_url
关联到"运行被测模型的 docker 容器名/镜像"，并抓取目标 /v1/models 列表，
用于任务溯源展示。
"""
import json
import os
import time
import urllib.parse

REGISTRY_PATH = os.path.join(
    os.environ.get("OUTPUTS_DIR", "/app/outputs"), "docker_services.json")

_cache: dict = {"mtime": None, "data": None}


def load_registry() -> dict:
    """读取 docker 服务映射（按文件 mtime 缓存，避免每请求读盘）。"""
    try:
        mtime = os.path.getmtime(REGISTRY_PATH)
    except OSError:
        return {}
    if _cache["data"] is not None and _cache["mtime"] == mtime:
        return _cache["data"]
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    _cache["mtime"] = mtime
    _cache["data"] = data
    return data


def resolve_serving(base_url: str) -> dict | None:
    """按 base_url 的端口匹配宿主机 docker 容器，返回溯源信息或 None。"""
    if not base_url:
        return None
    try:
        parts = urllib.parse.urlsplit(base_url)
        port = parts.port
        if not port:
            port = 443 if parts.scheme == "https" else 80
    except Exception:
        return None

    data = load_registry()
    cands = (data.get("ports") or {}).get(str(port)) or []
    seen: dict = {}
    for c in cands:
        seen.setdefault(c.get("container"), c)  # 同一容器 IPv4/IPv6 双发布去重
    uniq = list(seen.values())
    if not uniq:
        return None
    return {
        "container": uniq[0].get("container"),
        "image": uniq[0].get("image", ""),
        "status": uniq[0].get("status", ""),
        "port": port,
        "ambiguous": len(uniq) > 1,
        "registry_ts": data.get("ts"),
    }


def probe_models(base_url: str, api_key: str = "", timeout: float = 10) -> list[str]:
    """抓取目标服务的 /v1/models 模型列表（best-effort，失败返回空列表）。"""
    if not base_url:
        return []
    url = base_url.strip().rstrip("/")
    if url.endswith("/chat/completions"):
        url = url[: -len("/chat/completions")]
    elif url.endswith("/completions"):
        url = url[: -len("/completions")]
    if not url.endswith("/models"):
        url += "/models"
    try:
        import httpx
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        r = httpx.get(url, headers=headers, timeout=timeout)
        if r.status_code != 200:
            return []
        data = r.json()
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        return [str(i) for i in ids][:20]
    except Exception:
        return []
