# Задание для разработчика fleet: подключить readiness-цели graph-индексов

## Контекст

В 1c-mcp-metacode появился анонимный readiness-эндпоинт graph-индекса:
`GET /api/console/health/index` на порту MCP/консоли (6001). Ответ 200 —
bootstrap-индексация завершена, 503 — ещё идёт. Данных не отдаёт, только статус.

Стек мониторинга уже готов принимать эти проверки: job `fleet-readiness` в
Prometheus читает file_sd-файл `fleet-http.json` (лежит рядом с `fleet.json`
в `MONITORING_TARGETS_DIR`) и проверяет каждый URL blackbox'ом. Алерты
`GraphIndexNotReady` (>30 мин, warning) и `GraphIndexNotReadyLong` (>2 ч,
critical → Telegram), панель «Готовность graph-индексов» на дашборде MCP Fleet.

Файл `fleet-http.json` генерирует обновлённый `generate_monitoring_targets.py`:
теперь он пишет ДВА файла за один вызов — `fleet.json` (tcp-живость, как
раньше) и `fleet-http.json` (readiness-URL каждого graph-контейнера:
`http://<slug>-graph:6001/api/console/health/index`). Интерфейс вызова не
менялся: `--out .../fleet.json`, второй файл кладётся рядом автоматически.

## Что сделать

1. Заменить в ops-репозитории (infra-mcp-fleet) файл
   `generate_monitoring_targets.py` на актуальную версию из репозитория
   monitoring, каталог `fleet/` (источник правды по этому скрипту — репо
   monitoring; в ops-репо живёт копия).
2. Убедиться, что шаг `Generate Prometheus targets` в `apply-fleet.yml`
   ничего менять не требует (вызов тот же) и что `generate_monitoring_targets.py`
   есть в `on.push.paths` workflow — чтобы обновление самого скрипта тоже
   триггерило прогон.
3. Прогнать apply-fleet (пуш в main или workflow_dispatch).

Больше ничего: fleet.yml, generate_fleet.py и compose флота не трогаются.

## Критерии приёмки

1. Локальная проверка:
   `python3 generate_monitoring_targets.py fleet.yml --out /tmp/t/fleet.json`
   создаёт ДВА файла; в `/tmp/t/fleet-http.json` — по одной записи на каждую
   (repo, branch) из fleet.yml с target
   `http://<slug>-graph:6001/api/console/health/index` и label
   `fleet_unit=<slug>`, `kind=graph-index`.
2. После прогона apply-fleet в `MONITORING_TARGETS_DIR` лежат оба файла,
   `fleet-http.json` перезаписан (см. mtime).
3. В Prometheus (`http://хост:9090/targets`) job `fleet-readiness` показывает
   все graph-юниты; у веток с готовым индексом target UP, у индексирующихся —
   DOWN с ответом 503 (это ожидаемо, алерт даёт 30-минутную фору).
4. Панель «Готовность graph-индексов» на дашборде MCP Fleet показывает
   READY / INDEXING по каждой ветке.
5. Добавление тестовой ветки в fleet.yml → после apply-fleet она автоматически
   появляется и в `fleet-readiness` (без правок конфигов мониторинга);
   удаление из fleet.yml — исчезает.
