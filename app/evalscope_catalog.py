"""数据集目录：从 evalscope 的 REST API 查询真实支持的全部数据集与学科子集。

权威来源是 evalscope service 的 GET /api/v1/eval/benchmarks，返回每个数据集的
完整元信息（name/pretty_name/subset_list/total_samples/few_shot_num/
description.zh/tags/metrics 等）。我们直接用它，不去碰各 adapter 的
SUBJECT_MAPPING（那东西每个 benchmark 格式还不一样）。

读不到时（service 没起/旧版无此端点）回退一份基础清单。
"""
import json
import functools

from app import evalscope_client as es

# 回退清单（service 查询失败时兜底，保证 UI 不空）
_FALLBACK = [
    {"name": "ceval", "display": "C-Eval（中文学科）", "type": "mc", "lang": "zh"},
    {"name": "cmmlu", "display": "CMMLU（中文学科）", "type": "mc", "lang": "zh"},
    {"name": "mmlu", "display": "MMLU（英文学科）", "type": "mc", "lang": "en"},
    {"name": "mmlu_pro", "display": "MMLU-Pro（英文进阶）", "type": "mc", "lang": "en"},
    {"name": "gsm8k", "display": "GSM8K（数学推理）", "type": "qa", "lang": "en"},
]

_ZH_HINT = {"ceval", "cmmlu", "cmnli", "iyb", "iquiz", "chinese_simpleqa"}
_MC_TAGS = {"mcq", "multiple_choice", "choice", "multiplechoice"}


def _parse_benchmark(b: dict) -> dict:
    """把一个 evalscope benchmark 元信息转成我们的数据集项。"""
    name = b.get("name") or b.get("id") or b.get("dataset_name") or ""
    pretty = b.get("pretty_name") or b.get("prettyName")
    desc = b.get("description") or {}
    desc_zh = ""
    if isinstance(desc, dict):
        desc_zh = desc.get("zh") or desc.get("zh-cn") or ""
    elif isinstance(desc, str):
        desc_zh = desc
    display = pretty or name

    subset_list = b.get("subset_list") or b.get("subsets") or b.get("subset") or []
    if isinstance(subset_list, dict):
        subset_list = list(subset_list.keys())
    subset_list = [str(s) for s in subset_list] if subset_list else []

    tags = b.get("tags") or []
    metrics = b.get("metrics") or b.get("metric_list") or []
    blob = (" ".join(str(t) for t in tags) + " " + " ".join(str(m) for m in metrics)).lower()
    if any(t in blob for t in _MC_TAGS) or "accuracy" in blob:
        dtype = "mc"
    elif any(k in blob for k in ("rouge", "bleu", "pass@", "math", "gen")):
        dtype = "qa"
    else:
        dtype = "mc"

    lang = "-"
    if any("zh" in str(t).lower() or "chinese" in str(t).lower() for t in tags):
        lang = "zh"
    elif any("english" in str(t).lower() for t in tags):
        lang = "en"
    elif name in _ZH_HINT:
        lang = "zh"

    return {
        "name": name,
        "display": display,
        "type": dtype,
        "lang": lang,
        "subjects": sorted(subset_list),
        "count": b.get("total_samples") or b.get("num_samples") or 0,
        "default_few_shot": b.get("few_shot_num") or b.get("default_few_shot") or 0,
        "desc": desc_zh,
        "builtin": True,
    }


@functools.lru_cache(maxsize=1)
def _cached_catalog_json() -> str:
    """查询并缓存数据集目录（存成 JSON 字符串便于缓存）。"""
    try:
        raw = es.list_benchmarks()
    except Exception:
        raw = None
    out = []
    if raw:
        for b in raw:
            if isinstance(b, dict):
                item = _parse_benchmark(b)
                if item["name"]:
                    out.append(item)
            elif isinstance(b, str):
                out.append({"name": b, "display": b, "type": "mc", "lang": "-",
                            "subjects": [], "count": 0, "desc": "", "builtin": True})
    if not out:
        out = [dict(x, subjects=[], count=0, desc="", builtin=True) for x in _FALLBACK]
    out.sort(key=lambda d: (0 if d.get("lang") == "zh" else 1, d["name"]))
    return json.dumps(out, ensure_ascii=False)


def catalog_list() -> list:
    """返回数据集目录 list[dict]。"""
    return json.loads(_cached_catalog_json())


def get_subsets(dataset_name: str) -> list:
    """查询单个数据集的 subset（学科）列表。"""
    for d in catalog_list():
        if d["name"] == dataset_name:
            return d.get("subjects", [])
    return []


def refresh():
    """清缓存（数据集有变化或 service 重启后调用）。"""
    _cached_catalog_json.cache_clear()
