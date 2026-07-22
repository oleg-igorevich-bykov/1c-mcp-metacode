#!/usr/bin/env python3
"""Генератор Prometheus file_sd целей из fleet.yml (мониторинг MCP-флота).

Кладётся в ops-репозиторий рядом с generate_fleet.py. Вызывается шагом
apply-fleet workflow после генерации конфигов:

    python3 generate_monitoring_targets.py fleet.yml \
        --out /mnt/data/monitoring/targets/fleet.json

Prometheus читает этот файл через file_sd_configs (job "fleet") и подхватывает
изменения на лету — ни рестартов, ни правок prometheus.yml при изменении флота.
Мониторится ЖЕЛАЕМОЕ состояние из fleet.yml: юнит в списке, но tcp-порт не
отвечает (контейнер не поднялся/удалён руками) -> алерт FleetUnitDown.

Цели (blackbox tcp_connect):
  <slug>-graph:6001  kind=graph   на каждую (repo, branch)
  <slug>-rlm:9000    kind=rlm     если у проекта rlm: true
  <neo4j>:7687       kind=neo4j   общая зависимость всего флота

Конвенции имён скопированы из generate_fleet.py (slug, container_name,
rlm_container) — при изменении там менять и здесь.

Зависимости: PyYAML.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

GRAPH_MCP_PORT = 6001
RLM_PORT = 9000
NEO4J_BOLT_PORT = 7687


def slug(repo: str, branch: str) -> str:
    org, name = repo.split("/", 1)
    return f"{org}-{name}-{branch}".lower().replace("_", "-")


def iter_units(fleet: dict):
    for prj in fleet.get("projects") or []:
        rlm = bool(prj.get("rlm"))
        for br in prj.get("branches") or []:
            yield prj["repo"], br["name"], bool(br.get("lightweight")), rlm


def build_targets(fleet: dict) -> list[dict]:
    out: list[dict] = []
    for repo, branch, lightweight, rlm in iter_units(fleet):
        s = slug(repo, branch)
        base = {
            "repo": repo,
            "branch": branch,
            "fleet_unit": s,
            "tier": "critical",
        }
        out.append({
            "targets": [f"{s}-graph:{GRAPH_MCP_PORT}"],
            "labels": {**base, "kind": "graph",
                       "lightweight": "true" if lightweight else "false"},
        })
        if rlm:
            out.append({
                "targets": [f"{s}-rlm:{RLM_PORT}"],
                "labels": {**base, "kind": "rlm"},
            })

    neo4j = (fleet.get("settings") or {}).get("neo4j_container")
    if neo4j:
        out.append({
            "targets": [f"{neo4j}:{NEO4J_BOLT_PORT}"],
            "labels": {"kind": "neo4j", "tier": "critical"},
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("fleet", type=Path, help="путь к fleet.yml")
    ap.add_argument("--out", type=Path, required=True,
                    help="куда писать file_sd JSON (например .../targets/fleet.json)")
    args = ap.parse_args()

    fleet = yaml.safe_load(args.fleet.read_text(encoding="utf-8")) or {}
    if "projects" not in fleet:
        sys.exit(f"error: {args.fleet}: no 'projects' key")

    targets = build_targets(fleet)

    # Атомарная запись: Prometheus следит за файлом, не должен увидеть половину.
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=args.out.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(targets, f, ensure_ascii=False, indent=2)
    os.replace(tmp, args.out)
    print(f"written: {args.out} ({len(targets)} target groups)", file=sys.stderr)


if __name__ == "__main__":
    main()
