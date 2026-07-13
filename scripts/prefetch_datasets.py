"""联网 build 阶段预下载 evalscope 数据集，打进镜像供离线使用。

evalscope 评测时需要数据集，离线环境无法现下，必须在联网 build 时预取。
通过 modelscope 数据源下载（国内快），缓存到 MODELSCOPE_CACHE。

实现要点：
- 用 `evalscope eval --datasets <name> --limit 1 --eval-type mock_llm` 触发
  evalscope 自己的 adapter 加载路径。这保证缓存与运行时完全一致——
  直接调 MsDataset.load 会被 subset 命名/数据布局差异坑（cmmlu 在 modelscope
  是单 config 大 parquet，按 subset 名load 会 KeyError）。
- mock_llm 不需要真实模型，1 条样本就能触发完整数据集加载+缓存。
- trust_remote_code 由 evalscope 内部处理。
"""
import os
import subprocess
import sys

DATASETS = ["ceval", "cmmlu", "mmlu", "gsm8k"]


def prefetch():
    os.environ.setdefault("EVALSCOPE_DATASET_HUB", "modelscope")
    os.environ.setdefault("MODELSCOPE_CACHE", "/opt/modelscope_cache")
    print(f"[prefetch] 数据源：{os.environ.get('EVALSCOPE_DATASET_HUB')}")
    print(f"[prefetch] 缓存目录：{os.environ.get('MODELSCOPE_CACHE')}")

    work_dir = "/tmp/prefetch_work"
    os.makedirs(work_dir, exist_ok=True)

    ok, fail = [], []
    for ds in DATASETS:
        print(f"\n[prefetch] === 触发 {ds} 加载 ===")
        cmd = [
            "evalscope", "eval",
            "--model", "dummy",
            "--model-id", "dummy",
            "--datasets", ds,
            "--limit", "1",
            "--eval-type", "mock_llm",
            "--dataset-hub", "modelscope",
            "--dataset-dir", os.environ["MODELSCOPE_CACHE"],
            "--work-dir", work_dir,
            "--no-timestamp",
        ]
        print(f"[prefetch] cmd: {' '.join(cmd)}")
        try:
            # 实时输出，不捕获到内存
            proc = subprocess.run(cmd, check=False)
            if proc.returncode == 0:
                ok.append(ds)
                print(f"[prefetch] ✓ {ds} 完成")
            else:
                fail.append(ds)
                print(f"[prefetch] ✗ {ds} 失败：evalscope exit={proc.returncode}")
        except Exception as e:
            fail.append(ds)
            print(f"[prefetch] ✗ {ds} 异常：{type(e).__name__}: {e}")

    print(f"\n[prefetch] 成功 {len(ok)}：{ok}")
    if fail:
        print(f"[prefetch] 失败 {len(fail)}：{fail}")
        print("[prefetch] ⚠ 失败的数据集离线时不可用，请检查网络后重试 build。")
    if fail and not ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(prefetch())
