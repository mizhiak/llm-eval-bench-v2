#!/bin/bash
# 导出 docker 端口→容器映射，供 llm-eval-bench 应用关联"被测地址→服务容器"
# 输出到共享卷：/soft/llm-eval-bench-v2/outputs/docker_services.json
# 由 bbgadm crontab 每分钟执行一次
set -e
OUT=/soft/llm-eval-bench-v2/outputs/docker_services.json
TMP=$(mktemp "$(dirname "$OUT")/.docker_services.XXXXXX")
DPS=$(mktemp)

docker ps --format '{{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}' > "$DPS"

python3 - "$OUT" "$TMP" "$DPS" <<'PYEOF'
import json, re, sys, time, os

out, tmp, dps = sys.argv[1], sys.argv[2], sys.argv[3]
ports = {}
with open(dps, encoding="utf-8") as f:
    for line in f:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name, image = parts[0], parts[1]
        port_str = parts[2] if len(parts) > 2 else ""
        status = parts[3] if len(parts) > 3 else ""
        # 匹配 "0.0.0.0:18090->18090/tcp" 或 ":::28002->8000/tcp" 等
        for m in re.finditer(r"(?:\d+\.\d+\.\d+\.\d+|\[::\]|::)?:?(\d+)->(\d+)/(tcp|udp)", port_str):
            host_port = int(m.group(1))
            ports.setdefault(str(host_port), []).append({
                "container": name, "image": image, "status": status[:60],
            })
        # 未发布端口但 EXPOSE 的容器
        for m in re.finditer(r"^\s*(\d+)/(tcp|udp)", port_str):
            ports.setdefault(m.group(1), []).append({
                "container": name, "image": image, "status": status[:60],
            })

doc = {"ts": time.time(), "ports": ports}
json.dump(doc, open(tmp, "w"), ensure_ascii=False)
os.replace(tmp, out)
print(f"docker_services.json updated: {len(ports)} ports, ts={time.time():.0f}")
PYEOF
rm -f "$DPS"
