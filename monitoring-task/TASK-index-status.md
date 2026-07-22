# Задание: оповещение о состоянии индексации MCP-флота

Самодостаточное задание, можно делать независимо от `TASK.md` (там —
динамические цели Prometheus и статусы прогонов workflow целиком).

## Контекст

У каждого юнита флота (repo, branch) до двух индексов:

- **graph** (контейнер `<slug>-graph`, 1c-mcp-metacode → Neo4j): первичная
  сборка стартует сама при первом запуске контейнера; при пуше в ветку
  `reindex-on-push.yml` рестартует контейнер и startup-инкремент подхватывает
  дельту. Готовность индекса снаружи не видна.
- **rlm** (контейнер `<slug>-rlm`, rlm-tools-bsl → SQLite): автостарта нет.
  Первичная сборка — шаг `Build rlm-tools-bsl index...` в `apply-fleet.yml`
  (`docker exec ... rlm-bsl-index index build /repos`), обновление — шаг
  `Update rlm-tools-bsl index...` в `reindex-on-push.yml` (`... index update /repos`).

`<slug>` = `<org>-<repo>-<branch>` в lower-case, `_`→`-` (см. `generate_fleet.py`).

## Принимающая сторона — уже готова (репозиторий monitoring)

- pushgateway в docker-сети `monitoring` (создаётся стеком мониторинга),
  адрес изнутри сети — `http://pushgateway:9091`; runner-контейнер надо
  разово подключить к сети: `docker network connect monitoring <container_runner>`
  (или добавить `monitoring` в networks compose-файла runner'а);
- панель «rlm-индексы: часов с последней сборки» на дашборде Grafana «MCP Fleet»;
- алерт **FleetIndexMissing** → Telegram: rlm-контейнер жив (tcp-проба ok),
  но метрики сборки индекса нет более 30 минут — т.е. первичный build не
  отработал или не отчитался;
- алерт **ReindexFailed** → Telegram: упавший прогон reindex-on-push
  (метрика из `TASK.md` §3).

Со стороны мониторинга делать ничего не нужно — только поставлять метрику.

## Контракт метрики

`fleet_index_last_success_timestamp_seconds` — gauge, unix-время последней
успешной сборки/обновления rlm-индекса юнита.

Push в pushgateway, grouping key строго `job=index_build`, `fleet_unit=<slug>`
(label `fleet_unit` обязан совпадать со slug'ом — по нему алерт джойнится с
tcp-пробами):

```
POST $PUSHGATEWAY_URL/metrics/job/index_build/fleet_unit/<slug>
fleet_index_last_success_timestamp_seconds <unix_ts>
```

## Что сделать

### 1. apply-fleet.yml — отметка после первичной сборки

В `env` job'а добавить (если ещё нет):

```yaml
PUSHGATEWAY_URL: ${{ vars.PUSHGATEWAY_URL || 'http://pushgateway:9091' }}
```

В шаге `Build rlm-tools-bsl index for newly created checkouts`, в python-цикле
сразу после успешного `subprocess.run([... "index", "build", path], check=True)`:

```python
subprocess.run(
    ["sh", "-c",
     f"printf 'fleet_index_last_success_timestamp_seconds %d\\n' \"$(date +%s)\" | "
     f"curl -sf --data-binary @- {os.environ['PUSHGATEWAY_URL']}/metrics/job/index_build/fleet_unit/{unit['project_name']}"],
    check=False,   # мониторинг не должен ронять пайплайн
)
```

(`unit['project_name']` в плане генератора — это и есть slug.)

### 2. reindex-on-push.yml — отметка после обновления

В `env` добавить `PUSHGATEWAY_URL` (как выше). В шаге
`Update rlm-tools-bsl index (if the repo has an rlm sidecar)` сразу после
успешного `docker exec "$RLM_CONTAINER" rlm-bsl-index index update "/repos"`:

```bash
UNIT="$(echo "${ORG}-${NAME}-${BRANCH}" | tr '[:upper:]_' '[:lower:]-')"
printf 'fleet_index_last_success_timestamp_seconds %s\n' "$(date +%s)" | \
  curl -sf --data-binary @- "$PUSHGATEWAY_URL/metrics/job/index_build/fleet_unit/$UNIT" || true
```

Важно: шаг сам себя пропускает (`exit 0`), если rlm-сайдкара нет — push должен
стоять ПОСЛЕ `docker exec`, чтобы срабатывать только при реальном успехе update.

### 3. Фаза 2 — ВЫПОЛНЕНО (вариант A)

В 1c-mcp-metacode добавлен анонимный readiness-эндпоинт
`GET /api/console/health/index` (200 = bootstrap-индексация завершена, 503 =
ещё нет; порт тот же, что у MCP/консоли — 6001). Мониторинг подхватывает его
автоматически: `generate_monitoring_targets.py` теперь пишет рядом с
`fleet.json` второй файл `fleet-http.json` с readiness-URL каждого
graph-контейнера, Prometheus проверяет их job'ом `fleet-readiness`
(алерты GraphIndexNotReady >30м / GraphIndexNotReadyLong >2ч, панель
«Готовность graph-индексов» на дашборде MCP Fleet).

Единственное действие: обновить копию `generate_monitoring_targets.py`
в ops-репозитории до текущей версии из репо monitoring (`fleet/`).

## Ограничения

- Ни один push не должен ронять пайплайн: `check=False` / `|| true`.
- Существующая логика workflow не меняется — только вставки.
- Label `fleet_unit` — строго slug из `generate_fleet.py`, без вариаций.

## Критерии приёмки

0. Runner-контейнер подключён к сети `monitoring`; изнутри него
   `curl -s http://pushgateway:9091/-/ready` отвечает OK.
1. После apply-fleet, добавившего юнит с `rlm: true`, в pushgateway появляется
   `fleet_index_last_success_timestamp_seconds{fleet_unit="<slug>"}`
   (проверка: `curl -s http://pushgateway:9091/metrics | grep <slug>` из
   runner-контейнера), панель «rlm-индексы» на дашборде MCP Fleet показывает
   свежесть ~0 часов.
2. После пуша в ветку проекта с rlm-сайдкаром метрика обновляется (timestamp
   растёт), без рестарта rlm-контейнера.
3. Негативный тест FleetIndexMissing: удалить группу из pushgateway
   (`curl -X DELETE http://pushgateway:9091/metrics/job/index_build/fleet_unit/<slug>`
   из runner-контейнера) при живом rlm-контейнере → через ~30 минут алерт в Telegram.
4. Прогон в репо без rlm-сайдкара ничего не пушит и не падает.
5. Недоступный pushgateway не влияет на результат workflow.
