#!/usr/bin/env python3
"""
数据集下载 + 转换脚本（在有网的构建机上运行）。

从 ModelScope 魔搭下载 C-Eval（val 划分，带答案）和 GSM8K（test 划分，带答案），
转换成本工具所需的 .jsonl 格式，写入 app/datasets/，随后即可 docker build 打包进镜像。

用法：
    pip install modelscope
    pip install pandas pyarrow      # 解析 GSM8K 的 parquet 需要
    python scripts/prepare_datasets.py                 # 下载全部（C-Eval + GSM8K）
    python scripts/prepare_datasets.py --only ceval    # 仅 C-Eval
    python scripts/prepare_datasets.py --only gsm8k    # 仅 GSM8K
    python scripts/prepare_datasets.py --limit 200     # 每个数据集只取前 N 条（调试用）

数据集仓库（已核对，2025 年有效）：
    C-Eval : modelscope/ceval-exam  （val 划分带答案）
    GSM8K  : modelscope/gsm8k       （test 划分带答案）

下载策略：用 dataset_snapshot_download 仅下载原始文件，再本地解析 CSV/jsonl/parquet，
不调用 MsDataset.load() 的 as_dataset() 阶段，以规避 modelscope 与 datasets 库的版本
兼容问题（如 'verification_mod' 报错）。若 SDK 快照下载失败，自动退回 modelscope 命令行下载。

输出：
    app/datasets/ceval.jsonl   选择题，含 subject/question/A/B/C/D/answer
    app/datasets/gsm8k.jsonl   数值题，含 question/answer
"""
import os
import sys
import json
import argparse

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "datasets")

# C-Eval 的 52 个学科子集名称
CEVAL_SUBJECTS = [
    "computer_network", "operating_system", "computer_architecture",
    "college_programming", "college_physics", "college_chemistry",
    "advanced_mathematics", "probability_and_statistics", "discrete_mathematics",
    "electrical_engineer", "metrology_engineer", "high_school_mathematics",
    "high_school_physics", "high_school_chemistry", "high_school_biology",
    "middle_school_mathematics", "middle_school_biology", "middle_school_physics",
    "middle_school_chemistry", "veterinary_medicine", "college_economics",
    "business_administration", "marxism", "mao_zedong_thought",
    "education_science", "teacher_qualification", "high_school_politics",
    "high_school_geography", "middle_school_politics", "middle_school_geography",
    "modern_chinese_history", "ideological_and_moral_cultivation",
    "logic", "law", "chinese_language_and_literature", "art_studies",
    "professional_tour_guide", "legal_professional", "high_school_chinese",
    "high_school_history", "middle_school_history", "civil_servant",
    "sports_science", "plant_protection", "basic_medicine", "clinical_medicine",
    "urban_and_rural_planner", "accountant", "fire_engineer",
    "environmental_impact_assessment_engineer", "tax_accountant", "physician",
]

# 学科英文名 -> 中文显示名（用于结果分项展示）
CEVAL_CN = {
    "computer_network": "计算机网络", "operating_system": "操作系统",
    "computer_architecture": "计算机组成", "college_programming": "大学编程",
    "college_physics": "大学物理", "college_chemistry": "大学化学",
    "advanced_mathematics": "高等数学", "probability_and_statistics": "概率统计",
    "discrete_mathematics": "离散数学", "electrical_engineer": "电气工程师",
    "metrology_engineer": "计量工程师", "high_school_mathematics": "高中数学",
    "high_school_physics": "高中物理", "high_school_chemistry": "高中化学",
    "high_school_biology": "高中生物", "middle_school_mathematics": "初中数学",
    "middle_school_biology": "初中生物", "middle_school_physics": "初中物理",
    "middle_school_chemistry": "初中化学", "veterinary_medicine": "兽医学",
    "college_economics": "大学经济学", "business_administration": "工商管理",
    "marxism": "马克思主义", "mao_zedong_thought": "毛泽东思想",
    "education_science": "教育学", "teacher_qualification": "教师资格",
    "high_school_politics": "高中政治", "high_school_geography": "高中地理",
    "middle_school_politics": "初中政治", "middle_school_geography": "初中地理",
    "modern_chinese_history": "近代史", "ideological_and_moral_cultivation": "思想道德修养",
    "logic": "逻辑学", "law": "法学", "chinese_language_and_literature": "中国语言文学",
    "art_studies": "艺术学", "professional_tour_guide": "导游资格",
    "legal_professional": "法律职业", "high_school_chinese": "高中语文",
    "high_school_history": "高中历史", "middle_school_history": "初中历史",
    "civil_servant": "公务员", "sports_science": "体育学",
    "plant_protection": "植物保护", "basic_medicine": "基础医学",
    "clinical_medicine": "临床医学", "urban_and_rural_planner": "城乡规划师",
    "accountant": "注册会计师", "fire_engineer": "消防工程师",
    "environmental_impact_assessment_engineer": "环境影响评价工程师",
    "tax_accountant": "税务师", "physician": "医师资格",
}


def _write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  ✓ 写入 {path}（{len(rows)} 条）")


def _snapshot_download(dataset_id):
    """只下载数据集文件到本地，不触发 as_dataset() 解析（规避 SDK 版本兼容 bug）。

    返回本地目录路径，失败返回 None。
    优先用 SDK 的 dataset_snapshot_download，退而用 modelscope 命令行。
    """
    # 路径 1：SDK 文件快照下载（不解析，绕开 verification_mod 等兼容问题）
    try:
        from modelscope import dataset_snapshot_download
        local = dataset_snapshot_download(dataset_id)
        print(f"  ✓ 文件已下载到：{local}")
        return local
    except Exception as e:
        print(f"  dataset_snapshot_download 失败：{str(e)[:80]}")
    # 路径 2：命令行下载
    import subprocess, tempfile
    tmp = tempfile.mkdtemp(prefix="ms_")
    try:
        subprocess.run(
            ["modelscope", "download", "--dataset", dataset_id, "--local_dir", tmp],
            check=True, capture_output=True, text=True, timeout=900,
        )
        print(f"  ✓ 命令行下载到：{tmp}")
        return tmp
    except Exception as e:
        msg = getattr(e, "stderr", "") or str(e)
        print(f"  命令行下载失败：{str(msg)[:150]}")
        return None


def _read_table(fp):
    """读取单个 CSV / jsonl 文件为 dict 列表。"""
    import csv, json as _json
    rows = []
    if fp.endswith(".csv"):
        with open(fp, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                rows.append(r)
    elif fp.endswith((".jsonl", ".json")):
        with open(fp, encoding="utf-8") as f:
            txt = f.read().strip()
            if fp.endswith(".json") and txt.startswith("["):
                rows = _json.loads(txt)
            else:
                for line in txt.splitlines():
                    if line.strip():
                        rows.append(_json.loads(line))
    return rows


def prepare_ceval(limit=None):
    """C-Eval：下载文件后本地解析 val 划分（带答案）。绕开 as_dataset() 兼容问题。"""
    import glob
    print("下载 C-Eval（ModelScope: modelscope/ceval-exam）...")
    local = _snapshot_download("modelscope/ceval-exam")
    if not local:
        print("  ✗ C-Eval 下载失败，请确认网络与数据集名称。")
        return

    # 优先 val 划分；C-Eval 文件名形如 val/<subject>_val.csv 或 <subject>_val.jsonl
    candidates = []
    for ext in ("csv", "jsonl"):
        candidates += glob.glob(os.path.join(local, "**", f"*val*.{ext}"), recursive=True)
    # 排除 dev/test
    candidates = [c for c in candidates
                  if "_dev" not in os.path.basename(c) and "_test" not in os.path.basename(c)]
    if not candidates:
        print(f"  ✗ 未在下载目录找到 val 文件。请检查目录结构：{local}")
        return

    rows = []
    by_subj = {}
    for fp in sorted(candidates):
        base = os.path.basename(fp)
        subj_key = base.replace("_val.csv", "").replace("_val.jsonl", "") \
                       .replace(".csv", "").replace(".jsonl", "")
        cn = CEVAL_CN.get(subj_key, subj_key)
        cnt = 0
        for r in _read_table(fp):
            ans = (str(r.get("answer", "")) or "").strip().upper()
            if ans not in ("A", "B", "C", "D"):
                continue
            if not all(k in r for k in ("question", "A", "B", "C", "D")):
                continue
            rows.append({"subject": cn, "question": r["question"],
                         "A": r["A"], "B": r["B"], "C": r["C"], "D": r["D"],
                         "answer": ans})
            cnt += 1
            if limit and cnt >= limit:
                break
        if cnt:
            by_subj[cn] = cnt
    for cn, c in by_subj.items():
        print(f"  {cn}: {c} 题")
    if not rows:
        print("  ✗ 解析到 0 条，请把下载目录结构发我排查。")
        return
    _write_jsonl(os.path.join(OUT_DIR, "ceval.jsonl"), rows)


def prepare_gsm8k(limit=None):
    """GSM8K：下载文件后本地解析 test 划分（带答案，#### 后为最终数字）。"""
    import glob
    print("下载 GSM8K（ModelScope: modelscope/gsm8k）...")
    local = _snapshot_download("modelscope/gsm8k")
    if not local:
        print("  ✗ GSM8K 下载失败，请确认网络与数据集名称。")
        return

    def _clean(raw):
        raw = str(raw)
        final = raw.split("####")[-1].strip() if "####" in raw else raw.strip()
        return final.replace(",", "").replace("$", "")

    rows = []
    # 优先 test 的 parquet（GSM8K 主分发格式）
    parquets = glob.glob(os.path.join(local, "**", "*test*.parquet"), recursive=True)
    if parquets:
        try:
            import pandas as pd
            for fp in sorted(parquets):
                df = pd.read_parquet(fp)
                for _, r in df.iterrows():
                    rows.append({"question": str(r["question"]), "answer": _clean(r.get("answer", ""))})
                    if limit and len(rows) >= limit:
                        break
                if limit and len(rows) >= limit:
                    break
        except ImportError:
            print("  解析 parquet 需要 pandas/pyarrow：pip install pandas pyarrow")
        except Exception as e:
            print(f"  parquet 解析失败：{str(e)[:80]}")
    # 退而找 test 的 jsonl
    if not rows:
        for fp in sorted(glob.glob(os.path.join(local, "**", "*test*.jsonl"), recursive=True)):
            for r in _read_table(fp):
                rows.append({"question": r["question"], "answer": _clean(r.get("answer", ""))})
                if limit and len(rows) >= limit:
                    break
            if limit and len(rows) >= limit:
                break
    if not rows:
        print(f"  ✗ 未找到 test 数据文件。请检查下载目录：{local}")
        return
    _write_jsonl(os.path.join(OUT_DIR, "gsm8k.jsonl"), rows)




def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["ceval", "gsm8k"], help="仅处理指定数据集")
    ap.add_argument("--limit", type=int, default=None, help="每个数据集/学科取前 N 条")
    args = ap.parse_args()

    try:
        import modelscope  # noqa
    except ImportError:
        print("缺少依赖，请先安装：pip install modelscope")
        sys.exit(1)

    targets = [args.only] if args.only else ["ceval", "gsm8k"]
    if "ceval" in targets:
        prepare_ceval(args.limit)
    if "gsm8k" in targets:
        prepare_gsm8k(args.limit)
    print("\n完成。现在可以构建镜像：docker compose build")


if __name__ == "__main__":
    main()
