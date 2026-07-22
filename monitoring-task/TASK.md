# Задание: встроить мониторинг в GitOps-цикл MCP-флота

## Контекст

Флот контейнеров `<org>-<repo>-<branch>-graph` / `-rlm` управляется декларативно:
`fleet.yml` в ops-репозитории — источник правды, workflow `apply-fleet.yml`
приводит хост в соответствие, `reindex-on-push.yml` в проектных репо обновляет
чекауты и индексы при пушах. Референсные версии обоих workflow лежат в
`examples/fleet/apply-fleet.yml` и `examples/gitea-actions/reindex-on-push.yml`.

Рядом развёрнут стек мониторинга (Prometheus + Alertmanager + blackbox +
pushgateway). Prometheus уже настроен:

- job `fleet` читает цели из `file_sd`-файла `targets/fleet.json` и проверяет их
  blackbox'ом (tcp_connect); файл подхватывается на лету, рестарт не нужен;
- алерты `FleetApplyFailed` (`fleet_apply_success == 0`), `ReindexFailed`
  (`fleet_reindex_success == 0`) и дашборд свежести индексов
  (`fleet_index_last_success_timestamp_seconds`) ждут метрики из pushgateway.

Задача — сделать так, чтобы workflows поставляли эти данные. Менять стек
мониторинга НЕ нужно, только ops-репо и workflows.

## Что сделать

### 1. Генератор целей

Файл `generate_monitoring_targets.py` (лежит рядом с этим заданием, готовый)
положить в корень ops-репозитория рядом с `generate_fleet.py`.

Он читает `fleet.yml` и атомарно пишет file_sd JSON: по tcp-цели
`<slug>-graph:6001` на каждую (repo, branch), `<slug>-rlm:9000` при `rlm: true`,
плюс `settings.neo4j_container:7687`. Конвенции имён скопированы из
`generate_fleet.py` — если тот будет рефакториться, лучше импортировать
`slug`/`iter_units` оттуда вместо дублирования (опционально, по ситуации).

### 2. apply-fleet.yml (ops-репо)

1. В `on.push.paths` добавить `generate_monitoring_targets.py`.
2. В `env` job'а добавить:
   ```yaml
   MONITORING_TARGETS_DIR: ${{ vars.MONITORING_TARGETS_DIR || '/mnt/data/monitoring/targets' }}
   PUSHGATEWAY_URL: ${{ vars.PUSHGATEWAY_URL || 'http://pushgateway:9091' }}
   ```
   Метрики шлются НАПРЯМУЮ в контейнер pushgateway по docker-сети `monitoring`
   (стек мониторинга создаёт её сам). Разовая настройка: подключить контейнер
   runner'а к этой сети —
   ```
   docker network connect monitoring <container_runner>
   ```
   (или добавить `monitoring` в networks compose-файла runner'а). Хост-порты
   pushgateway для этого не нужны.
3. Сразу ПОСЛЕ шага `Generate configs and compute plan` добавить шаг:
   ```yaml
   - name: Generate Prometheus targets
     run: |
       set -euo pipefail
       python3 generate_monitoring_targets.py fleet.yml --out "$MONITORING_TARGETS_DIR/fleet.json"
   ```
4. Отметки свежести rlm-индексов (`fleet_index_last_success_timestamp_seconds`) —
   ОТДЕЛЬНОЕ задание `TASK-index-status.md` (там же контракт метрик и точный код).
   Если оба задания делаются одним заходом — берите код оттуда.
5. В КОНЕЦ job добавить два шага:
   ```yaml
   - name: Report apply status to pushgateway
     if: always()
     run: |
       STATUS=$([ "${{ job.status }}" = "success" ] && echo 1 || echo 0)
       cat <<M | curl -sf --data-binary @- "$PUSHGATEWAY_URL/metrics/job/apply_fleet" || true
       # TYPE fleet_apply_success gauge
       fleet_apply_success $STATUS
       # TYPE fleet_apply_last_run_timestamp_seconds gauge
       fleet_apply_last_run_timestamp_seconds $(date +%s)
       M

   - name: Telegram on failure
     if: failure()
     run: |
       curl -sf "https://api.telegram.org/bot${{ secrets.TG_BOT_TOKEN }}/sendMessage" \
         -d chat_id="${{ secrets.TG_CHAT_ID }}" -d parse_mode=HTML \
         -d text="🔴 <b>apply-fleet</b> упал: ${{ gitea.server_url }}/${{ gitea.repository }}/actions/runs/${{ gitea.run_number }}" || true
   ```

### 3. reindex-on-push.yml (проектные репо)

В `env` добавить `PUSHGATEWAY_URL` (как выше). В КОНЕЦ job добавить:

```yaml
- name: Report reindex status to pushgateway
  if: always()
  run: |
    ORG="${REPO%%/*}"; NAME="${REPO##*/}"
    UNIT="$(echo "${ORG}-${NAME}-${BRANCH}" | tr '[:upper:]_' '[:lower:]-')"
    STATUS=$([ "${{ job.status }}" = "success" ] && echo 1 || echo 0)
    cat <<M | curl -sf --data-binary @- "$PUSHGATEWAY_URL/metrics/job/reindex/fleet_unit/$UNIT" || true
    # TYPE fleet_reindex_success gauge
    fleet_reindex_success $STATUS
    # TYPE fleet_reindex_last_run_timestamp_seconds gauge
    fleet_reindex_last_run_timestamp_seconds $(date +%s)
    M

- name: Telegram on failure
  if: failure()
  run: |
    curl -sf "https://api.telegram.org/bot${{ secrets.TG_BOT_TOKEN }}/sendMessage" \
      -d chat_id="${{ secrets.TG_CHAT_ID }}" -d parse_mode=HTML \
      -d text="🔴 <b>reindex</b> упал: <b>${{ gitea.repository }}@${{ gitea.ref_name }}</b>%0A${{ gitea.server_url }}/${{ gitea.repository }}/actions/runs/${{ gitea.run_number }}" || true
```

Приём с `tr '[:upper:]_' '[:lower:]-'` уже используется в существующих шагах
этого workflow — UNIT должен совпадать со slug'ом из `generate_fleet.py`.

### 4. Переменные и секреты (уровень ОРГАНИЗАЦИИ Gitea)

- Variables: `MONITORING_TARGETS_DIR` (= путь, смонтированный в контейнер
  prometheus как `/etc/prometheus-targets`; создать каталог на хосте заранее),
  `PUSHGATEWAY_URL` (если отличается от `http://localhost:9091`).
- Secrets: `TG_BOT_TOKEN`, `TG_CHAT_ID` — тот же бот, что в Alertmanager.

## Ограничения

- Существующая логика workflows не меняется — только добавления.
- Ни один шаг мониторинга не должен ронять пайплайн: пуши метрик — с `|| true`
  / `check=False`, статус-шаг — `if: always()`, Telegram — `if: failure()`.
- Стиль — как в существующих файлах (bash `set -euo pipefail` там, где шаг
  обязателен; комментарии по-русски).

## Критерии приёмки

1. `python3 generate_monitoring_targets.py examples/fleet/fleet.example.yml --out /tmp/fleet.json`
   отрабатывает и даёт 6 групп целей: `kgg-do30-main-graph:6001`,
   `kgg-do30-main-rlm:9000`, `kgg-do30-dev-graph:6001`, `kgg-do30-dev-rlm:9000`,
   `kgg-other-project-main-graph:6001`, `1c-neo4j:7687`.
2. Оба workflow остаются валидным YAML (проверить парсером).
3. Пуш `fleet.yml` в main ops-репо перегенерирует `targets/fleet.json`.
4. Упавший прогон даёт `fleet_apply_success 0` / `fleet_reindex_success 0`
   в pushgateway и сообщение в Telegram; успешный — `1` и тишину.
5. Падение пушей в pushgateway/Telegram (недоступен и т.п.) не влияет на
   результат workflow.
