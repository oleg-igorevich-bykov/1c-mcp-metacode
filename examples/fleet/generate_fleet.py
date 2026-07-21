#!/usr/bin/env python3
"""Генератор конфигураций флота MCP-индексаторов из fleet.yml.

Читает fleet.yml (источник правды) и генерирует:
  1. docker-compose.fleet.yml — по сервису 1c-mcp-metacode на каждую (repo, branch),
     плюс один сервис rlm-tools-bsl на репозиторий (если rlm: true) — внутри него
     зарегистрированы все ветки этого репозитория как отдельные rlm-проекты;
  2. gw-routes.yml            — routes-файл gateway: содержимое
     gateway_base_routes (ручные tier-1 маршруты) + fleet-маршруты (по одному на
     graph-сервис и, если включён, ещё один на rlm-сервис репозитория);
  3. projects.json для каждого rlm-сервиса — пишется напрямую в его конфиг-каталог
     на хосте (идемпотентная перезапись при каждом запуске, без пароля — раз
     реестром управляет только генератор, MCP-мутации не нужны).

Образы (1c-mcp-metacode, rlm-tools-bsl) собираются и тегируются вручную заранее —
генератор их не строит, только ссылается на готовые теги из settings.

Также умеет считать план реконсиляции (что добавить/удалить) по разнице
между fleet.yml и фактическими чекаутами в data_root — этим пользуется
apply-fleet workflow.

Запуск:
    python3 generate_fleet.py fleet.yml --out-dir out/          # только генерация
    python3 generate_fleet.py fleet.yml --out-dir out/ --plan   # + план изменений

Зависимости: PyYAML.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

# Env-переменные, которыми выключается тяжёлое на lightweight-ветках (dev).
LIGHTWEIGHT_ENV = {
    "ENABLE_BSL_CODE_EMBEDDING": "false",
    "OBJECT_SUMMARY_ENABLED": "false",
    "LOAD_METADATA_GUIDS": "false",
    "ENABLE_ROUTINE_DESCRIPTION_EMBEDDING": "false",
    "ENABLE_METADATA_DESCRIPTION_EMBEDDING": "false",
}


def slug(repo: str, branch: str) -> str:
    """kgg/do30 + main -> kgg-do30-main (PROJECT_NAME и основа всех имён)."""
    org, name = repo.split("/", 1)
    return f"{org}-{name}-{branch}".lower().replace("_", "-")


def repo_slug(repo: str) -> str:
    """kgg/do30 -> kgg-do30 (без ветки — основа rlm-имён, общих на весь репо)."""
    org, name = repo.split("/", 1)
    return f"{org}-{name}".lower().replace("_", "-")


def container_name(repo: str, branch: str) -> str:
    return f"{slug(repo, branch)}-graph"


def volume_name(repo: str, branch: str) -> str:
    return "fleet_storage_" + slug(repo, branch).replace("-", "_")


def checkout_dir(data_root: str, repo: str, branch: str) -> str:
    org, name = repo.split("/", 1)
    return str(Path(data_root) / org / name / branch)


def repo_dir(data_root: str, repo: str) -> str:
    """Каталог репозитория целиком (родитель всех branch-чекаутов) — то, что
    монтируется в rlm-контейнер как /repos:ro, чтобы внутри были видны все ветки
    разом."""
    org, name = repo.split("/", 1)
    return str(Path(data_root) / org / name)


def rlm_container(repo: str) -> str:
    return f"{repo_slug(repo)}-rlm"


def rlm_config_dir(data_root: str, repo: str) -> str:
    org, name = repo.split("/", 1)
    return str(Path(data_root) / "_rlm" / org / name / "config")


def rlm_cache_dir(data_root: str, repo: str) -> str:
    org, name = repo.split("/", 1)
    return str(Path(data_root) / "_rlm" / org / name / "cache")


def load_fleet(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "projects" not in data or "settings" not in data:
        sys.exit(f"error: {path}: fleet.yml must contain 'settings' and 'projects'")
    for prj in data["projects"]:
        if "/" not in prj.get("repo", ""):
            sys.exit(f"error: bad repo {prj!r}: expected 'owner/repo'")
        if prj.get("rlm") and "rlm_image" not in data["settings"]:
            sys.exit(f"error: {prj['repo']}: rlm=true requires settings.rlm_image")
        for br in prj.get("branches") or []:
            if not br.get("name"):
                sys.exit(f"error: branch without name in {prj['repo']}")
    return data


def iter_units(fleet: dict):
    """Yield (repo, branch_name, lightweight) для каждой единицы флота (graph)."""
    for prj in fleet["projects"]:
        for br in prj.get("branches") or []:
            yield prj["repo"], br["name"], bool(br.get("lightweight"))


def iter_rlm_projects(fleet: dict):
    """Yield (repo, [branch_name, ...]) для репозиториев с rlm: true."""
    for prj in fleet["projects"]:
        if prj.get("rlm"):
            branches = [br["name"] for br in prj.get("branches") or []]
            if branches:
                yield prj["repo"], branches


def rlm_enabled_repos(fleet: dict) -> set[str]:
    return {repo for repo, _branches in iter_rlm_projects(fleet)}


# --------------------------------------------------------------- compose

def build_compose(fleet: dict) -> dict:
    s = fleet["settings"]
    services: dict = {}
    volumes: dict = {}

    for repo, branch, lightweight in iter_units(fleet):
        name = container_name(repo, branch)
        vol = volume_name(repo, branch)
        env = {
            "PROJECT_NAME": slug(repo, branch),
            "PROJECT_LAYOUT": "vanessa",
            "METADATA_SOURCE": "xml",
            "NEO4J_URI": f"bolt://{s['neo4j_container']}:7687",
            # Явный false: общий .env с true не должен сносить проекты при recreate.
            "FULL_METADATA_RELOAD": "false",
        }
        if lightweight:
            env.update(LIGHTWEIGHT_ENV)

        services[name] = {
            "image": s["image"],
            "container_name": name,
            "restart": "unless-stopped",
            # Host-порты не публикуем: gateway ходит по docker-сети на :6001.
            "volumes": [
                f"{checkout_dir(s['data_root'], repo, branch)}:/app/data",
                f"{vol}:/app/storage",
            ],
            "env_file": [".env"],
            "environment": [f"{k}={v}" for k, v in env.items()],
            "networks": [s["network"]],
        }
        volumes[vol] = {"driver": "local"}

    for repo, _branches in iter_rlm_projects(fleet):
        name = rlm_container(repo)
        services[name] = {
            "image": s["rlm_image"],
            "container_name": name,
            "restart": "unless-stopped",
            "volumes": [
                # Весь репозиторий (все ветки разом) — read-only, только чтение исходников.
                f"{repo_dir(s['data_root'], repo)}:/repos:ro",
                # Конфиг (projects.json) — генератор пишет его сам, см. write_rlm_registries.
                f"{rlm_config_dir(s['data_root'], repo)}:/home/rlm/.config/rlm-tools-bsl",
                # Кэш индексов — переживает пересоздание контейнера.
                f"{rlm_cache_dir(s['data_root'], repo)}:/home/rlm/.cache/rlm-tools-bsl",
            ],
            "environment": [
                "RLM_TRANSPORT=streamable-http",
                "RLM_HOST=0.0.0.0",
                "RLM_PORT=9000",
                # Обновление индексов делает reindex-on-push (docker exec ... index update)
                # синхронно с git pull — авто-update при рестарте контейнера не нужен.
            ],
            "networks": [s["network"]],
        }

    return {
        "name": "mcp-fleet",
        "services": services,
        "volumes": volumes,
        "networks": {s["network"]: {"external": True}},
    }


# --------------------------------------------------------------- rlm registry

def write_rlm_registries(fleet: dict) -> list[str]:
    """Пишет projects.json для каждого rlm-сервиса напрямую в его конфиг-каталог
    на хосте. Идемпотентно — перезаписывается полностью на каждом запуске
    генератора, без пароля (см. схему ProjectRegistry: {"projects": [...]})."""
    s = fleet["settings"]
    written = []
    for repo, branches in iter_rlm_projects(fleet):
        cfg_dir = Path(rlm_config_dir(s["data_root"], repo))
        cfg_dir.mkdir(parents=True, exist_ok=True)
        Path(rlm_cache_dir(s["data_root"], repo)).mkdir(parents=True, exist_ok=True)
        payload = {
            "projects": [
                {
                    "name": branch,
                    "path": f"/repos/{branch}",
                    "description": f"{repo}@{branch}",
                }
                for branch in branches
            ]
        }
        target = cfg_dir / "projects.json"
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written.append(str(target))
    return written


# --------------------------------------------------------------- gateway

def build_gateway_routes(fleet: dict, base_routes_path: Path | None) -> dict:
    routes: list = []
    if base_routes_path and base_routes_path.exists():
        base = yaml.safe_load(base_routes_path.read_text(encoding="utf-8")) or {}
        routes.extend(base.get("routes") or [])

    for repo, branch, _light in iter_units(fleet):
        routes.append({
            "prefix": f"/mcp/{slug(repo, branch)}",
            "upstream": f"http://{container_name(repo, branch)}:6001",
            "auth": "gitea",
            "required_repos": [repo],
        })

    for repo, _branches in iter_rlm_projects(fleet):
        routes.append({
            "prefix": f"/mcp/{repo_slug(repo)}-rlm",
            "upstream": f"http://{rlm_container(repo)}:9000",
            "auth": "gitea",
            "required_repos": [repo],
        })
    return {"routes": routes}


# --------------------------------------------------------------- plan

def build_plan(fleet: dict) -> dict:
    """Сравнить желаемое состояние с фактическими чекаутами в data_root.

    Состояние на диске = каталоги data_root/<org>/<repo>/<branch> с .git внутри.
    Возвращает {'create': [...], 'delete': [...]} где каждый элемент — dict
    c repo/branch/dir/container/project_name/volume (+ rlm/rlm_container, если
    у репозитория включён rlm) — всё, что нужно apply-скрипту для клонирования,
    сборки rlm-индекса или зачистки.
    """
    s = fleet["settings"]
    data_root = Path(s["data_root"])
    rlm_repos = rlm_enabled_repos(fleet)

    def _enrich(entry: dict) -> dict:
        if entry["repo"] in rlm_repos:
            entry["rlm"] = True
            entry["rlm_container"] = rlm_container(entry["repo"])
        else:
            entry["rlm"] = False
        return entry

    desired = {}
    for repo, branch, light in iter_units(fleet):
        desired[(repo, branch)] = _enrich({
            "repo": repo,
            "branch": branch,
            "lightweight": light,
            "dir": checkout_dir(s["data_root"], repo, branch),
            "container": container_name(repo, branch),
            "project_name": slug(repo, branch),
            "volume": volume_name(repo, branch),
        })

    actual: set = set()
    if data_root.exists():
        for org_dir in data_root.iterdir():
            if not org_dir.is_dir() or org_dir.name.startswith("_"):
                continue
            for repo_d in org_dir.iterdir():
                if not repo_d.is_dir():
                    continue
                for br_dir in repo_d.iterdir():
                    if br_dir.is_dir() and (br_dir / ".git").exists():
                        actual.add((f"{org_dir.name}/{repo_d.name}", br_dir.name))

    create = [desired[k] for k in sorted(desired.keys() - actual)]
    delete = []
    for repo, branch in sorted(actual - desired.keys()):
        delete.append(_enrich({
            "repo": repo,
            "branch": branch,
            "dir": checkout_dir(s["data_root"], repo, branch),
            "container": container_name(repo, branch),
            "project_name": slug(repo, branch),
            "volume": volume_name(repo, branch),
        }))
    return {"create": create, "delete": delete}


# --------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("fleet", type=Path, help="путь к fleet.yml")
    ap.add_argument("--out-dir", type=Path, default=Path("."),
                    help="куда писать docker-compose.fleet.yml и gw-routes.yml")
    ap.add_argument("--plan", action="store_true",
                    help="дополнительно вывести JSON-план create/delete в stdout")
    ap.add_argument("--no-rlm-registry-write", action="store_true",
                    help="не писать projects.json в data_root (для сухого прогона/тестов)")
    args = ap.parse_args()

    fleet = load_fleet(args.fleet)
    s = fleet["settings"]
    args.out_dir.mkdir(parents=True, exist_ok=True)

    compose = build_compose(fleet)
    compose_path = args.out_dir / "docker-compose.fleet.yml"
    compose_path.write_text(
        "# GENERATED by generate_fleet.py — не редактировать руками, источник: fleet.yml\n"
        + yaml.safe_dump(compose, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    base_path = args.fleet.parent / s.get("gateway_base_routes", "")
    gw = build_gateway_routes(fleet, base_path if s.get("gateway_base_routes") else None)
    gw_path = args.out_dir / "gw-routes.yml"
    gw_path.write_text(
        "# GENERATED by generate_fleet.py — не редактировать руками, источник: fleet.yml\n"
        + yaml.safe_dump(gw, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    print(f"written: {compose_path}", file=sys.stderr)
    print(f"written: {gw_path}", file=sys.stderr)

    if not args.no_rlm_registry_write:
        for p in write_rlm_registries(fleet):
            print(f"written: {p}", file=sys.stderr)

    if args.plan:
        print(json.dumps(build_plan(fleet), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
