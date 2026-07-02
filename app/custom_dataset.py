"""把用户上传的自定义 jsonl 转成 evalscope 能吃的本地数据集格式。

evalscope 自定义数据集格式（见官方文档）：
- 选择题 general_mcq：目录下 {subset}_val.jsonl（评测）+ 可选 {subset}_dev.jsonl（few-shot）
  字段：id, question, A, B, C, D, answer
- 问答题 general_qa：目录下 {subset}.jsonl
  字段：query, response

我们的上传格式：
- 选择题：{question, A, B, C, D, answer[, subject]}  → 正好匹配 general_mcq
- 问答题：{question, answer}                          → 映射成 general_qa 的 {query, response}

转换后通过 dataset_args 的 local_path + subset_list 传给 evalscope。
"""
import os
import json

CUSTOM_DIR = os.path.join(os.path.dirname(__file__), "..", "custom_datasets")
# evalscope 读取的本地数据集根目录（转换产物放这里）
ES_LOCAL_ROOT = os.path.join(os.path.dirname(__file__), "..", "data", "es_custom")
MCQ_DIR = os.path.join(ES_LOCAL_ROOT, "mcq")
QA_DIR = os.path.join(ES_LOCAL_ROOT, "qa")


def _load_jsonl(path):
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _is_mcq(items):
    return bool(items) and any(k in items[0] for k in ("A", "B", "C", "D"))


def prepare_custom_for_evalscope(custom_name: str, few_shot: int = 0):
    """把 custom:xxx.jsonl 转成 evalscope 本地数据集。

    返回 (kind, dataset_key, dataset_args_for_this_ds)
    - kind: 'mcq' 或 'qa'
    - dataset_key: 'general_mcq' 或 'general_qa'
    - dataset_args_for_this_ds: {'local_path':..., 'subset_list':[subset]}
    """
    fn = custom_name.split(":", 1)[1]
    src = os.path.join(CUSTOM_DIR, fn)
    items = _load_jsonl(src)
    subset = os.path.splitext(fn)[0].replace(":", "_").replace("/", "_")

    if _is_mcq(items):
        os.makedirs(MCQ_DIR, exist_ok=True)
        # 评测集 {subset}_val.jsonl
        val_path = os.path.join(MCQ_DIR, f"{subset}_val.jsonl")
        with open(val_path, "w", encoding="utf-8") as f:
            for i, it in enumerate(items):
                row = {"id": str(it.get("id", i)),
                       "question": it.get("question", ""),
                       "answer": str(it.get("answer", "")).strip()}
                for opt in ["A", "B", "C", "D", "E", "F"]:
                    if opt in it and it[opt] not in (None, ""):
                        row[opt] = it[opt]
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        # few-shot：若需要且数据足够，用末尾几条作 dev 集
        if few_shot and len(items) > few_shot:
            dev_path = os.path.join(MCQ_DIR, f"{subset}_dev.jsonl")
            with open(dev_path, "w", encoding="utf-8") as f:
                for i, it in enumerate(items[-few_shot:]):
                    row = {"id": str(it.get("id", i)),
                           "question": it.get("question", ""),
                           "answer": str(it.get("answer", "")).strip()}
                    for opt in ["A", "B", "C", "D", "E", "F"]:
                        if opt in it and it[opt] not in (None, ""):
                            row[opt] = it[opt]
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return ("mcq", "general_mcq",
                {"local_path": os.path.abspath(MCQ_DIR), "subset_list": [subset]})
    else:
        # 问答题 → general_qa，字段映射 question→query, answer→response
        os.makedirs(QA_DIR, exist_ok=True)
        qa_path = os.path.join(QA_DIR, f"{subset}.jsonl")
        with open(qa_path, "w", encoding="utf-8") as f:
            for it in items:
                row = {"query": it.get("question") or it.get("query", ""),
                       "response": str(it.get("answer") or it.get("response", ""))}
                if it.get("system"):
                    row["system"] = it["system"]
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return ("qa", "general_qa",
                {"local_path": os.path.abspath(QA_DIR), "subset_list": [subset]})
