# Задание: автовосстановление структуры флота после перезапуска хоста

## Контекст

Сейчас после перезагрузки хоста происходит только ЧАСТЬ восстановления —
важно различать два уровня:

1. **Уже работает само, ничего делать не надо.** `generate_fleet.py` ставит
   каждому graph-/rlm-сервису `restart: unless-stopped`, у монитор-стека
   (Prometheus, Grafana, Alertmanager, экспортёры) та же политика. Если
   docker-демон запускается при загрузке ОС (`systemctl is-enabled docker`
   должен быть `enabled`) и контейнеры не были остановлены руками перед
   выключением — Docker поднимет их сам, чекауты (bind-mount) и данные
   (volumes, Neo4j) на месте.
2. **Само НЕ восстанавливается — то, что нужно сделать в этом задании.**
   Рестарт Docker — это не GitOps-реконсиляция. Если, пока хост был выключен,
   `fleet.yml` менялся (добавили/убрали ветку), это расхождение не
   исправится само: не склонируется новый чекаут, не обновится
   `gw-routes.yml`, не рестартует gateway, не соберётся первичный rlm-индекс,
   не снесётся удалённый проект. А мониторинг при этом продолжит опрашивать
   СТАРЫЙ набор целей (`targets/fleet*.json` не перегенерируется на рестарте
   контейнера — это статические файлы) — то есть новые ветки не появятся в
   Prometheus, а удалённые будут висеть как ложные `FleetUnitDown`, пока
   кто-то не запустит apply-fleet руками.

Задача — при старте хоста автоматически прогонять полную реконсиляцию,
как будто кто-то только что запушил `fleet.yml`.

## Решение: триггерить существующий apply-fleet, а не дублировать его логику

`apply-fleet.yml` уже поддерживает `workflow_dispatch: {}` — значит его можно
запустить через Gitea API, не меняя сам workflow. Это лучше, чем писать
отдельный bootstrap-скрипт с копией той же логики: секреты (`FLEET_GIT_TOKEN`)
и код реконсиляции остаются в одном месте.

### 1. Systemd-юнит на хосте флота

`/etc/systemd/system/fleet-boot-reconcile.service`:

```ini
[Unit]
Description=Trigger apply-fleet reconciliation after boot
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
EnvironmentFile=/etc/fleet-boot-reconcile.env
ExecStart=/usr/local/bin/fleet-boot-reconcile.sh
# Не блокирует остальную загрузку и не валит boot, если Gitea/runner
# ещё не готовы к моменту первой попытки — retry внутри скрипта.
TimeoutStartSec=900
```

Включить: `systemctl daemon-reload && systemctl enable fleet-boot-reconcile.service`.

### 2. Скрипт с retry (Gitea/runner поднимаются не мгновенно)

`/usr/local/bin/fleet-boot-reconcile.sh`:

```bash
#!/bin/bash
set -euo pipefail
# GITEA_URL, OPS_REPO (owner/repo), DISPATCH_TOKEN, BRANCH — из EnvironmentFile
BRANCH="${BRANCH:-main}"

for i in $(seq 1 30); do
  if curl -sf -o /dev/null "${GITEA_URL}/api/v1/version"; then
    break
  fi
  echo "Gitea ещё не отвечает, попытка $i/30..."
  sleep 10
done

curl -sf -X POST \
  -H "Authorization: token ${DISPATCH_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"ref\":\"${BRANCH}\"}" \
  "${GITEA_URL}/api/v1/repos/${OPS_REPO}/actions/workflows/apply-fleet.yml/dispatches"

echo "apply-fleet: workflow_dispatch отправлен (ref=${BRANCH})"
```

`chmod 700 /usr/local/bin/fleet-boot-reconcile.sh` (это единственный скрипт,
которому нужен токен).

### 3. Секрет — ОТДЕЛЬНЫЙ токен, не FLEET_GIT_TOKEN

`/etc/fleet-boot-reconcile.env` (`chmod 600`, владелец root):

```
GITEA_URL=http://gitea:3000
OPS_REPO=infra/mcp-fleet
DISPATCH_TOKEN=...
BRANCH=main
```

`DISPATCH_TOKEN` — отдельный Gitea API-токен с правом `write:repository`
(запуск Actions) на ops-репозиторий. НЕ переиспользуйте `FLEET_GIT_TOKEN`
(у него доступ на чтение всех индексируемых репозиториев — избыточно и
опасно держать на хосте в файле для boot-скрипта; здесь нужно только право
дёрнуть dispatch одного репозитория).

## Что это даёт мониторингу

`apply-fleet.yml` уже (после ваших правок из `TASK.md`) перегенерирует
`targets/fleet.json` и `targets/fleet-http.json` при каждом прогоне — то есть
после реконсиляции по этому триггеру Prometheus сам увидит актуальный список
веток (file_sd подхватывает файл без рестарта) в течение ~30 секунд.
Дополнительных действий на стороне мониторинга не требуется.

## Ограничения

- Скрипт только УВЕДОМЛЯЕТ Gitea — саму реконсиляцию по-прежнему выполняет
  runner (нужен рабочий self-hosted runner с доступом к docker CLI хоста).
  Если runner тоже не поднялся — dispatch останется в очереди Actions до его
  появления, ничего не потеряется.
- `apply-fleet` идемпотентен (create/delete по разнице с диском) — безопасно
  дёргать его даже при нулевом дрейфе, лишней работы почти не будет.
- Не подменяет push-триггер: обычный поток (правка `fleet.yml` → пуш →
  apply-fleet) как работал, так и работает; это только добавляет ту же
  реконсиляцию как реакцию на перезагрузку хоста.

## Критерии приёмки

1. `systemctl status fleet-boot-reconcile.service` после ребута — `inactive
   (dead)` с `Result: success` (oneshot успешно отработал).
2. В Gitea → ops-репо → Actions видно прогон apply-fleet, стартовавший вскоре
   после старта хоста (workflow_dispatch, не push).
3. Тест дрейфа: добавить тестовую ветку в `fleet.yml`, выключить хост НЕ
   дожидаясь обычного push-триггера (либо смоделировать: остановить runner,
   запушить, перезагрузить хост) → после старта хоста контейнер новой ветки
   поднимается сам, без ручного запуска workflow.
4. `docker exec prometheus wget -qO- http://prometheus:9090/api/v1/targets`
   (или UI `/targets`) показывает новую ветку в job `fleet` в течение минуты
   после завершения прогона.
5. `/etc/fleet-boot-reconcile.env` недоступен на чтение никому, кроме root
   (`600`), токен в нём не совпадает с `FLEET_GIT_TOKEN`.
