# Fleet: обслуживание нескольких репозиториев 1С-конфигураций

Справочные файлы для служебного ops-репозитория (например `infra/mcp-fleet`), который
управляет флотом MCP-серверов для 1С: по паре контейнеров `1c-mcp-metacode` (main+dev) на
каждый git-репозиторий конфигурации плюс опциональный сайдкар `rlm-tools-bsl`, общий Neo4j,
общий MCP-gateway с авторизацией через Gitea.

Оба образа (`1c-mcp-metacode`, `rlm-tools-bsl`) собираются и тегируются вручную заранее —
генератор ничего не билдит, только ссылается на готовые теги (`settings.image`,
`settings.rlm_image` в fleet.yml).

## Архитектура

Один git-репозиторий конфигурации = N единиц флота 1c-mcp-metacode (по одной на
индексируемую ветку) + опционально один сайдкар rlm-tools-bsl на весь репозиторий сразу
(внутри зарегистрированы все его ветки как отдельные rlm-проекты). Все контейнеры пишут в
общий Neo4j / читают общие чекауты (данные проектов разделены по `project_name` и по
container-scope), наружу торчит только gateway.

Зачем два разных MCP на одном репозитории: `1c-mcp-metacode` даёт граф конфигурации (объекты,
формы, права, поиск по коду с гибридным RLM+вектор), `rlm-tools-bsl` — индекс методов/графа
вызовов и `git grep` по сырым XML/BSL, токен-эффективные хелперы для навигации. Один не
заменяет другой, оба смотрят в одни и те же чекауты.

Конвенции (фиксированы генератором):

| Что | Формат | Пример |
|---|---|---|
| Контейнер graph / upstream | `<org>-<repo>-<branch>-graph:6001` | `kgg-do30-main-graph:6001` |
| Контейнер rlm / upstream | `<org>-<repo>-rlm:9000` (один на репо) | `kgg-do30-rlm:9000` |
| PROJECT_NAME (graph) | `<org>-<repo>-<branch>` | `kgg-do30-dev` |
| Gateway prefix (graph) | `/mcp/<org>-<repo>-<branch>` | `/mcp/kgg-do30-dev` |
| Gateway prefix (rlm) | `/mcp/<org>-<repo>-rlm` | `/mcp/kgg-do30-rlm` |
| Чекаут | `<data_root>/<org>/<repo>/<branch>` | `/opt/mcp-fleet/data/kgg/do30/dev` |
| Storage volume (graph) | `fleet_storage_<org>_<repo>_<branch>` | `fleet_storage_kgg_do30_dev` |
| rlm-конфиг/кэш | `<data_root>/_rlm/<org>/<repo>/{config,cache}` | `.../\_rlm/kgg/do30/config/projects.json` |

Каждая единица (ветка graph, репозиторий rlm) получает свой gateway-маршрут с
`required_repos: [<org>/<repo>]` — права на main, dev и rlm идентичны правам на сам
репозиторий в Gitea, отдельная модель прав не заводится. Выбор main/dev/rlm — выбором
эндпоинта на стороне агента.

dev-ветки помечаются `lightweight: true`: без эмбеддингов кода, сводок объектов и
GUID-обогащения (у dev нет актуального `ConfigDumpInfo.xml` — это нормально, обогащение
опционально). Поиск по коду (`search_bsl_code`) при этом работает — лексический RLM-движок
не требует эмбеддингов. rlm-tools-bsl не завязан на эмбеддинги вообще (чистый Python+SQLite),
поэтому для него `lightweight` неактуален — один сайдкар обслуживает main и dev одинаково
полно.

### rlm-tools-bsl: реестр проектов без пароля

Один rlm-контейнер на репозиторий, `/repos` монтируется read-only на каталог репозитория
целиком (обе ветки видны разом: `/repos/main`, `/repos/dev`). Генератор сам пишет
`projects.json` в конфиг-каталог сервиса — без пароля (мутирующие операции через MCP при
этом заблокированы самим rlm-tools-bsl, что и требуется: реестром управляет только
генератор, а не агент во время сессии). `projects.json` перезаписывается целиком на каждом
`apply-fleet` — источник правды тот же fleet.yml, ручные правки через MCP не нужны и не
переживут следующий apply.

## Состав ops-репозитория

```text
fleet.yml               # источник правды (см. fleet.example.yml)
gw-routes.base.yml      # ручные tier-1 маршруты gateway
generate_fleet.py       # генератор compose + gateway-роутов + план реконсиляции
.gitea/workflows/
  apply-fleet.yml       # применение fleet.yml к хосту (см. apply-fleet.yml здесь)
```

## Сценарии

**Provision (новый репозиторий / миграция).** Добавить запись в `fleet.yml` (с `rlm: true`,
если нужен сайдкар), закоммитить в main ops-репо. `apply-fleet` сам: склонирует чекауты
(полный клон, checkout ветки), наложит GUID-оверлей, сгенерирует конфиги и `projects.json`,
поднимет контейнеры (`docker compose up -d --remove-orphans`), для новых веток с rlm=true
соберёт первичный индекс методов (`docker exec ... rlm-bsl-index index build`), перезапустит
gateway. Первичная индексация графа стартует автоматически при первом старте контейнера
1c-mcp-metacode в пустой project-scope.

**Reindex (принят PR в main/dev).** Workflow `reindex-on-push.yml` в самом проектном
репозитории (см. `examples/gitea-actions/`): обновляет чекаут своей ветки, делает
`docker restart` graph-контейнера — это запускает встроенный startup-инкремент немедленно
(не full reload; MCP доступен во время дозагрузки, применяется только дельта) — и, если у
репозитория есть rlm-сайдкар, `docker exec ... rlm-bsl-index index update` (инкрементально
по mtime+size, с git-ускорением; без рестарта rlm-контейнера).

**Decommission (репозиторий удалён/выведен).** Убрать запись из `fleet.yml`, закоммитить.
`apply-fleet` уберёт маршруты (рестарт gateway — доступ закрыт сразу), удалит контейнеры
(`--remove-orphans`, включая rlm-сайдкар, если для репо не осталось ни одной ветки),
вычистит данные проекта из Neo4j (`python main.py --clear-project <name>` one-off
контейнером), удалит чекаут и storage-volume. `projects.json` rlm-сервиса перегенерируется
на каждом apply — убранная ветка сама пропадает из реестра.

Расхождения «на диске есть, в fleet.yml нет» (и наоборот) генератор считает планом
реконсиляции (`--plan`) по фактическим чекаутам в `data_root` — поэтому apply идемпотентен:
его можно перезапускать в любой момент (`workflow_dispatch`).

## GUID-оверлей (ConfigDumpInfo.xml)

`ConfigDumpInfo.xml` в `.gitignore` проектных репозиториев. Автоматика, делающая
ежесуточную выгрузку main из боевой базы, кладёт эти файлы в orphan-ветку `guid-dump`
проектного репо (force-push, структура `cf/ConfigDumpInfo.xml`,
`cfe/<Name>/ConfigDumpInfo.xml`). Provision- и reindex-workflow накладывают их поверх
чекаута main. Для dev оверлей не делается (`LOAD_METADATA_GUIDS=false`).

## Подготовка хоста (один раз)

```bash
docker network create mcp-net
# Neo4j и gateway — отдельный compose, оба в сети mcp-net

# Образы — собираются вручную и тегируются под то, что указано в fleet.yml
# (settings.image / settings.rlm_image). Генератор их не билдит.
docker build -t roctup/1c-mcp-metacode:latest /path/to/1c-mcp-metacode
docker build -t rlm-tools-bsl:latest /path/to/rlm-tools-bsl

mkdir -p /opt/mcp-fleet/data
# /opt/mcp-fleet/.env — NEO4J_PASSWORD и общие настройки (см. .env.example)
# self-hosted Gitea runner с label metacode-host на этом хосте
# секрет FLEET_GIT_TOKEN в ops-репо: чтение всех индексируемых репозиториев
```

Обновление образов новой версией — вне зоны ответственности fleet-автоматики: пересобрали,
перетегировали тем же тегом, что в fleet.yml → следующий `apply-fleet`
(`docker compose up -d`) или ручной `docker compose pull && up -d --force-recreate`
подхватит новую версию по существующему тегу.

## Ограничения

- `concurrency:` в workflow требует Gitea >= 1.26; на старших версиях уберите блок и
  оберните шаги в `flock`.
- Ресурсы: одновременные инкременты нескольких веток — это конкуренция за CPU хоста и
  запись в Neo4j; при необходимости добавьте `cpus:`/`mem_limit:` сервисам в генераторе
  и снизьте `PROCESS_WORKERS` для dev-контейнеров через их environment.
- Первичная полная индексация — самая тяжёлая операция; не провижиньте много репозиториев
  одновременно (workflow и так сериализован через `concurrency`).
- При полном удалении репозитория из fleet.yml каталог `data_root/_rlm/<org>/<repo>/`
  (config+cache rlm-сервиса) на диске не подчищается автоматически — не критично, но
  при желании добавьте это явным шагом в decommission.
