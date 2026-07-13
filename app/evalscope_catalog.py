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

# 补充清单：evalscope 已支持但默认 API 不返回的 benchmark
# 这些都有对应的 adapter，可直接用于评测
_SUPPLEMENTARY = [
    # === 代码能力 ===
    {"name": "humaneval", "display": "HumanEval（代码生成）",
     "type": "qa", "lang": "en", "subjects": [], "count": 164,
     "desc": "164 道手写编程题，pass@k 评测"},
    {"name": "humaneval_plus", "display": "HumanEval+（增强代码测试）",
     "type": "qa", "lang": "en", "subjects": [], "count": 164,
     "desc": "HumanEval 增强版，每题 80 个测试用例"},
    {"name": "mbpp", "display": "MBPP（Python 编程）",
     "type": "qa", "lang": "en", "subjects": [], "count": 500,
     "desc": "500 道入门级 Python 编程题"},
    {"name": "mbpp_plus", "display": "MBPP+（增强 Python 测试）",
     "type": "qa", "lang": "en", "subjects": [], "count": 500,
     "desc": "MBPP 增强版，更多测试用例"},
    {"name": "live_code_bench", "display": "LiveCodeBench（最新代码题）",
     "type": "qa", "lang": "en", "subjects": [], "count": 0,
     "desc": "从 LeetCode/AtCoder/CodeForces 收集的最新编程题"},
    {"name": "multiple_humaneval", "display": "MultiPL-E（多语言代码）",
     "type": "qa", "lang": "en", "subjects": [], "count": 0,
     "desc": "HumanEval 翻译为 C++/Java/JS/Go/Rust 等多语言版本"},
    # === 推理能力 ===
    {"name": "bbh", "display": "BBH（BIG-Bench Hard 推理）",
     "type": "qa", "lang": "en", "subjects": [], "count": 6511,
     "desc": "23 个具挑战性的 BIG-Bench 任务，测试多步推理"},
    {"name": "hellaswag", "display": "HellaSwag（常识推理）",
     "type": "mc", "lang": "en", "subjects": [], "count": 10042,
     "desc": "10042 道完形填空，测试常识推理"},
    {"name": "winogrande", "display": "WinoGrande（代词消歧）",
     "type": "mc", "lang": "en", "subjects": [], "count": 1267,
     "desc": "1267 道代词消歧题，测试常识推理"},
    {"name": "truthful_qa", "display": "TruthfulQA（真实性评测）",
     "type": "mc", "lang": "en", "subjects": [], "count": 817,
     "desc": "817 道测试模型是否输出虚假信息的题目"},
    {"name": "drop", "display": "DROP（阅读理解+算术）",
     "type": "qa", "lang": "en", "subjects": [], "count": 9536,
     "desc": "需要数值推理和阅读理解的 QA 数据集"},
    # === 数学 ===
    {"name": "competition_math", "display": "Competition MATH（竞赛数学）",
     "type": "qa", "lang": "en", "subjects": [], "count": 5000,
     "desc": "5000 道 AMC/AIME 等竞赛级数学题（含完整解答）"},
    {"name": "minerva_math", "display": "Minerva-Math（数学推理）",
     "type": "qa", "lang": "en", "subjects": [], "count": 0,
     "desc": "使用 Minerva 格式的 MATH 数据集"},
    {"name": "olympiad_bench", "display": "OlympiadBench（奥赛数学物理）",
     "type": "qa", "lang": "en", "subjects": [], "count": 0,
     "desc": "国际奥林匹克数学/物理竞赛题"},
    # === 知识 ===
    {"name": "trivia_qa", "display": "TriviaQA（百科问答）",
     "type": "qa", "lang": "en", "subjects": [], "count": 0,
     "desc": "大规模百科知识问答"},
    {"name": "simple_qa", "display": "SimpleQA（事实准确性）",
     "type": "qa", "lang": "en", "subjects": [], "count": 4326,
     "desc": "4326 道简单事实问题，测试模型幻觉率"},
    {"name": "chinese_simpleqa", "display": "Chinese SimpleQA（中文事实准确性）",
     "type": "qa", "lang": "zh", "subjects": [], "count": 0,
     "desc": "中文版 SimpleQA，测试中文事实准确性"},
    # === 长文本 ===
    {"name": "longbench_v2", "display": "LongBench v2（长文本理解）",
     "type": "qa", "lang": "en", "subjects": [], "count": 0,
     "desc": "超长文本理解与推理"},
    {"name": "needle_haystack", "display": "Needle-in-Haystack（大海捞针）",
     "type": "qa", "lang": "en", "subjects": [], "count": 0,
     "desc": "长文本中检索特定信息的压力测试"},
    # === 生物医学 ===
    {"name": "pubmedqa", "display": "PubMedQA（生物医学QA）",
     "type": "mc", "lang": "en", "subjects": [], "count": 1000,
     "desc": "1000 道基于 PubMed 摘要的生物医学问答"},
    # === 多语言 ===
    {"name": "mmlu_redux", "display": "MMLU-Redux（MMLU 修正版）",
     "type": "mc", "lang": "en", "subjects": [], "count": 0,
     "desc": "MMLU 错误修正版，修正了原版的标注错误"},
    {"name": "mmlu_pro", "display": "MMLU-Pro（专业版）",
     "type": "mc", "lang": "en", "subjects": [], "count": 12032,
     "desc": "MMLU 进阶版，去除了简单和低质量题目"},
]

_ZH_HINT = {"ceval", "cmmlu", "cmnli", "iyb", "iquiz", "chinese_simpleqa"}
_MC_TAGS = {"mcq", "multiple_choice", "choice", "multiplechoice"}

# 确保 supplementary 不会与 API 返回的重复
_SUPP_NAMES = {d["name"] for d in _SUPPLEMENTARY}


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

    # 合并补充清单（去重：以 API 返回的为准，不重复添加）
    existing = {d["name"] for d in out}
    for s in _SUPPLEMENTARY:
        if s["name"] not in existing:
            entry = dict(s)
            entry.setdefault("default_few_shot", 0)
            entry.setdefault("builtin", True)
            entry.setdefault("desc", "")
            out.append(entry)

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
