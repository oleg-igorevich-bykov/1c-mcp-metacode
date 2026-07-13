# Бенчмарк поиска по коду BSL: сравнение режимов

Это сравнение режимов семантического поиска по телу кода 1С на практике: по результатам прогонов на
контрольном датасете. Описание самой подсистемы, флагов и стратегий — в
[bsl-code-search.md](bsl-code-search.md); здесь — цифры замеров, по которым выбирались рабочие режимы.

## Что сравниваем

Цель эксперимента - сравнить режимы поиска по коду 1С на одном контрольном датасете:

- разные нарезки больших процедур;
- `raw` и `compressed` embedding input;
- `embedding-only` и `hybrid` retrieval;
- `no-embedding` fallback;
- разные embedding-модели.

## Тестовый датасет

В качестве контрольного набора используется отдельная выборка процедур и функций из BSL-кода типовой
конфигурации Бухгалтерия 3.0. Всего в конфигурации найдено 473 271 процедур и функций, из них в
контрольный набор отобрано 25 000, а для проверки составлено 500 вопросов.

### Распределение процедур, функций и вопросов по размеру

Распределение в контрольном наборе примерно повторяет распределение размеров в полной конфигурации. В последних колонках показано, к процедурам какого размера относятся проверочные вопросы.

| Размер процедуры/функции | Полная конфигурация | Доля | Контрольный набор | Доля | Вопросы | Доля |
|---|---:|---:|---:|---:|---:|---:|
| 1-100 | 15 076 | 3.2% | 782 | 3.1% | 12 | 2.4% |
| 101-200 | 93 007 | 19.7% | 4 818 | 19.3% | 77 | 15.4% |
| 201-300 | 71 874 | 15.2% | 3 869 | 15.5% | 42 | 8.4% |
| 301-400 | 44 043 | 9.3% | 2 352 | 9.4% | 25 | 5.0% |
| 401-500 | 37 209 | 7.9% | 2 207 | 8.8% | 21 | 4.2% |
| 501-600 | 25 750 | 5.4% | 1 307 | 5.2% | 22 | 4.4% |
| 601-700 | 16 671 | 3.5% | 863 | 3.5% | 21 | 4.2% |
| 701-800 | 15 217 | 3.2% | 790 | 3.2% | 17 | 3.4% |
| 801-900 | 13 775 | 2.9% | 712 | 2.8% | 11 | 2.2% |
| 901-1000 | 12 182 | 2.6% | 645 | 2.6% | 10 | 2.0% |
| 1001-2000 | 67 383 | 14.2% | 3 507 | 14.0% | 82 | 16.4% |
| 2001-3000 | 26 403 | 5.6% | 1 361 | 5.4% | 54 | 10.8% |
| 3001-4000 | 11 806 | 2.5% | 607 | 2.4% | 26 | 5.2% |
| 4001-5000 | 6 990 | 1.5% | 358 | 1.4% | 28 | 5.6% |
| 5001-6000 | 4 694 | 1.0% | 240 | 1.0% | 14 | 2.8% |
| 6001-7000 | 2 995 | 0.6% | 159 | 0.6% | 3 | 0.6% |
| 7001-8000 | 2 007 | 0.4% | 104 | 0.4% | 2 | 0.4% |
| 8001-9000 | 1 200 | 0.3% | 62 | 0.2% | 9 | 1.8% |
| 9001-10000 | 1 007 | 0.2% | 52 | 0.2% | 9 | 1.8% |
| 10001-15000 | 2 095 | 0.4% | 107 | 0.4% | 6 | 1.2% |
| 15001-20000 | 788 | 0.2% | 40 | 0.2% | 2 | 0.4% |
| 20001-25000 | 341 | 0.1% | 25 | 0.1% | 3 | 0.6% |
| 25001-30000 | 216 | 0.0% | 12 | 0.0% | 0 | 0.0% |
| 30001-50000 | 340 | 0.1% | 15 | 0.1% | 4 | 0.8% |
| 50001+ | 202 | 0.0% | 5 | 0.0% | 0 | 0.0% |

Самой большой оказалась функция `СформироватьВТРасширенныеСведенияОДоходахИВзносах` из общего модуля `УчетСтраховыхВзносов`: ее размер составляет 679 695 символов.

### Как составлялись вопросы

Для контрольного набора сначала отбирались процедуры и функции из конфигурации. Затем ChatGPT 5.5 анализировал код выбранной процедуры или функции и формулировал смысловой вопрос: не по имени метода буквально, а по тому, что делает этот фрагмент кода.

Для коротких процедур и функций ответом считалась вся процедура целиком. Для процедур и функций длиннее 2000 символов дополнительно размечался конкретный диапазон строк внутри процедуры, который отвечает на вопрос.

Если в конфигурации находились эквивалентные реализации с тем же смыслом, вопрос мог получить несколько правильных chunk id. Поэтому в метриках учитывается не только один исходный чанк, но и вручную подтвержденные эквивалентные ответы.

### Один правильный ответ или несколько

В 1С часто встречаются одинаковые или эквивалентные обработчики в разных объектах, формах или модулях. Поэтому один вопрос может иметь несколько правильных чанков.

В контрольном наборе вопросов:

| Тип вопроса | Вопросов | Доля |
|---|---:|---:|
| один правильный чанк | 453 | 90.6% |
| несколько правильных чанков | 47 | 9.4% |

Распределение по количеству правильных чанков:

| Количество правильных чанков | Вопросов |
|---:|---:|
| 1 | 453 |
| 2 | 22 |
| 3 | 9 |
| 4 | 7 |
| 5-9 | 7 |
| 25+ | 2 |

Всего в 500 вопросах указано 650 правильных chunk id.

### Распределение вопросов по типам объектов

| Тип объекта метаданных | Вопросов | Доля |
|---|---:|---:|
| Общие модули | 94 | 18.8% |
| Документы | 90 | 18.0% |
| Отчеты | 89 | 17.8% |
| Обработки | 63 | 12.6% |
| Справочники | 62 | 12.4% |
| Регистры сведений | 50 | 10.0% |
| Общие формы | 30 | 6.0% |
| Прочее | 22 | 4.4% |

Такой набор ближе к реальному поиску по конфигурации: в нем есть формы, общие модули, документы, отчеты, справочники и регистры.

### Примеры вопросов

Примеры формулировок из контрольного набора:

```text
Где при изменении вида дохода исполнительного производства вызывается серверная обработка этого изменения?

Где из дерева выгрузки удаляется узел DTNumber, если у него не заполнен CustomsCode?

Где строится запрос данных для печати уведомлений о прекращении отпуска по уходу за ребенком?

Где остатки задолженности выбираются с детализацией или без нее в зависимости от параметров заполнения?

Где на форме отчета комитенту меняются заголовки и видимость реквизитов в зависимости от удержания вознаграждения?

Где параметры фиксации вторичных данных возвращаются на основе списка фиксируемых реквизитов документа?

Где присоединенные файлы ИСМП заполняются по основанию документа вывода из оборота?

Где по имени поля формы возвращается назначенное действие элемента формы?

Где параметры обновления внешних компонент инициализируются настройками соединения и прокси-сервера?

Где по имени команды определяется индекс созданного документа счета и открывается документ по ссылке?
```

Основная метрика — `coverage@5`: агенту планируется возвращать 5 чанков.

Дополнительные метрики:

```text
coverage@3
answer_hit@5
coverage@10
answer_hit@10
parent@50
embed_text_ratio
```

## Таблица 1. Сравнение режимов поиска

Эта таблица нужна для сравнения pipeline-режимов. Каждая embedding-модель прогоняется по одной и той же матрице режимов: две нарезки, `raw`, `compressed`, `raw<1000 + compressed`, `embedding-only` и `hybrid`.

Первый baseline model — `F2LLM-v2 0.6B`.

Для всех embedding-прогонов используется одинаковый размер batch: `batch_size = 4`.

Модели запускались локально через `llama-server` в режиме embedding. Пример запуска для F2LLM-v2 0.6B:

```powershell
./llama-server.exe `
  -m "F:\LMStudio\.lmstudio\models\mradermacher\F2LLM-v2-0.6B-GGUF\F2LLM-v2-0.6B.Q8_0.gguf" `
  --embedding `
  --pooling last `
  -ngl 999 `
  -c 16384 `
  -b 16384 `
  -ub 16384 `
  -np 1 `
  -fa on `
  --host 127.0.0.1 `
  --port 1234
```

Датасет один для всей таблицы, поэтому не дублируется в строках.

| Model | Split | Compression | Retrieval mode | Selector / rerank | Embed text ratio | coverage@3 | coverage@5 | coverage@10 | answer_hit@3 | answer_hit@5 | answer_hit@10 | parent@50 |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Nomic Embed Code | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.8993 | 0.9277 | 0.9447 | 0.9220 | 0.9460 | 0.9500 | 0.9660 |
| Nomic Embed Code | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.8983 | 0.9209 | 0.9469 | 0.9220 | 0.9400 | 0.9520 | 0.9700 |
| Nomic Embed Code | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.8748 | 0.9013 | 0.9322 | 0.8960 | 0.9200 | 0.9400 | 0.9600 |
| Nomic Embed Code | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.8585 | 0.8724 | 0.8924 | 0.9280 | 0.9420 | 0.9540 | 0.9700 |
| Nomic Embed Code | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.8512 | 0.8631 | 0.8787 | 0.9260 | 0.9380 | 0.9480 | 0.9660 |
| Nomic Embed Code | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.8413 | 0.8542 | 0.8712 | 0.9140 | 0.9260 | 0.9400 | 0.9600 |
| Nomic Embed Code | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.8729 | 0.9011 | 0.9341 | 0.9120 | 0.9420 | 0.9480 | 0.9640 |
| Nomic Embed Code | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.8765 | 0.8966 | 0.9336 | 0.9200 | 0.9380 | 0.9480 | 0.9660 |
| Nomic Embed Code | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.8530 | 0.8822 | 0.9247 | 0.8940 | 0.9200 | 0.9380 | 0.9580 |
| Nomic Embed Code | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.7935 | 0.8092 | 0.8408 | 0.9240 | 0.9400 | 0.9520 | 0.9660 |
| Nomic Embed Code | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.7903 | 0.8007 | 0.8312 | 0.9280 | 0.9380 | 0.9480 | 0.9640 |
| Nomic Embed Code | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.7893 | 0.7985 | 0.8332 | 0.9180 | 0.9260 | 0.9400 | 0.9580 |
| | | | | | | | | | | | | |
| Perplexity pplx-embed-v1 4B | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.9073 | 0.9265 | 0.9510 | 0.9300 | 0.9440 | 0.9560 | 0.9640 |
| Perplexity pplx-embed-v1 4B | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.8789 | 0.9089 | 0.9397 | 0.9020 | 0.9260 | 0.9440 | 0.9560 |
| Perplexity pplx-embed-v1 4B | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.8662 | 0.8795 | 0.9032 | 0.9300 | 0.9440 | 0.9580 | 0.9640 |
| Perplexity pplx-embed-v1 4B | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.8488 | 0.8779 | 0.9135 | 0.8760 | 0.8980 | 0.9200 | 0.9420 |
| Perplexity pplx-embed-v1 4B | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.8388 | 0.8593 | 0.8765 | 0.9100 | 0.9300 | 0.9380 | 0.9560 |
| Perplexity pplx-embed-v1 4B | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.8108 | 0.8232 | 0.8486 | 0.8820 | 0.8940 | 0.9080 | 0.9420 |
| Perplexity pplx-embed-v1 4B | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.8880 | 0.9047 | 0.9441 | 0.9380 | 0.9440 | 0.9580 | 0.9620 |
| Perplexity pplx-embed-v1 4B | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.8594 | 0.8901 | 0.9312 | 0.9060 | 0.9280 | 0.9440 | 0.9560 |
| Perplexity pplx-embed-v1 4B | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.8204 | 0.8503 | 0.9029 | 0.8680 | 0.8900 | 0.9180 | 0.9440 |
| Perplexity pplx-embed-v1 4B | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.8084 | 0.8178 | 0.8559 | 0.9380 | 0.9480 | 0.9580 | 0.9620 |
| Perplexity pplx-embed-v1 4B | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.7796 | 0.7949 | 0.8263 | 0.9160 | 0.9300 | 0.9360 | 0.9560 |
| Perplexity pplx-embed-v1 4B | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.7458 | 0.7652 | 0.8050 | 0.8780 | 0.8980 | 0.9140 | 0.9440 |
| | | | | | | | | | | | | |
| Perplexity pplx-embed-v1 0.6B | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.8545 | 0.8996 | 0.9232 | 0.8800 | 0.9200 | 0.9340 | 0.9500 |
| Perplexity pplx-embed-v1 0.6B | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.8491 | 0.8874 | 0.9131 | 0.8720 | 0.9040 | 0.9200 | 0.9460 |
| Perplexity pplx-embed-v1 0.6B | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.8387 | 0.8660 | 0.9023 | 0.8640 | 0.8880 | 0.9100 | 0.9360 |
| Perplexity pplx-embed-v1 0.6B | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.8022 | 0.8363 | 0.8583 | 0.8660 | 0.9040 | 0.9220 | 0.9500 |
| Perplexity pplx-embed-v1 0.6B | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.7893 | 0.8144 | 0.8412 | 0.8580 | 0.8840 | 0.9080 | 0.9460 |
| Perplexity pplx-embed-v1 0.6B | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.7462 | 0.7735 | 0.8071 | 0.8200 | 0.8460 | 0.8720 | 0.9360 |
| Perplexity pplx-embed-v1 0.6B | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.8282 | 0.8759 | 0.9067 | 0.8740 | 0.9100 | 0.9200 | 0.9440 |
| Perplexity pplx-embed-v1 0.6B | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.8417 | 0.8725 | 0.9140 | 0.8840 | 0.9100 | 0.9300 | 0.9520 |
| Perplexity pplx-embed-v1 0.6B | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.8134 | 0.8455 | 0.8946 | 0.8600 | 0.8840 | 0.9100 | 0.9360 |
| Perplexity pplx-embed-v1 0.6B | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.7521 | 0.7779 | 0.8032 | 0.8780 | 0.9060 | 0.9200 | 0.9520 |
| Perplexity pplx-embed-v1 0.6B | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.7118 | 0.7461 | 0.7862 | 0.8500 | 0.8840 | 0.9080 | 0.9440 |
| Perplexity pplx-embed-v1 0.6B | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.6922 | 0.7279 | 0.7742 | 0.8200 | 0.8540 | 0.8800 | 0.9360 |
| | | | | | | | | | | | | |
| F2LLM-v2 4B | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.8996 | 0.9253 | 0.9447 | 0.9240 | 0.9420 | 0.9500 | 0.9560 |
| F2LLM-v2 4B | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.8956 | 0.9161 | 0.9457 | 0.9180 | 0.9340 | 0.9540 | 0.9620 |
| F2LLM-v2 4B | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.8787 | 0.9018 | 0.9293 | 0.9040 | 0.9220 | 0.9380 | 0.9500 |
| F2LLM-v2 4B | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.8517 | 0.8640 | 0.8877 | 0.9180 | 0.9300 | 0.9500 | 0.9620 |
| F2LLM-v2 4B | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.8463 | 0.8566 | 0.8808 | 0.9240 | 0.9340 | 0.9480 | 0.9560 |
| F2LLM-v2 4B | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.8159 | 0.8403 | 0.8590 | 0.8960 | 0.9180 | 0.9260 | 0.9500 |
| F2LLM-v2 4B | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.8741 | 0.9004 | 0.9384 | 0.9140 | 0.9360 | 0.9520 | 0.9560 |
| F2LLM-v2 4B | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.8771 | 0.8946 | 0.9381 | 0.9240 | 0.9360 | 0.9540 | 0.9620 |
| F2LLM-v2 4B | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.8466 | 0.8851 | 0.9254 | 0.8900 | 0.9220 | 0.9400 | 0.9480 |
| F2LLM-v2 4B | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.7934 | 0.8044 | 0.8411 | 0.9220 | 0.9360 | 0.9520 | 0.9620 |
| F2LLM-v2 4B | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.7878 | 0.8027 | 0.8326 | 0.9180 | 0.9320 | 0.9400 | 0.9560 |
| F2LLM-v2 4B | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.7569 | 0.7814 | 0.8118 | 0.8920 | 0.9180 | 0.9260 | 0.9480 |
| | | | | | | | | | | | | |
| F2LLM-v2 1.7B | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.9034 | 0.9248 | 0.9442 | 0.9240 | 0.9400 | 0.9520 | 0.9600 |
| F2LLM-v2 1.7B | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.8831 | 0.9012 | 0.9313 | 0.9040 | 0.9180 | 0.9360 | 0.9520 |
| F2LLM-v2 1.7B | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.8641 | 0.8739 | 0.8915 | 0.9300 | 0.9420 | 0.9500 | 0.9600 |
| F2LLM-v2 1.7B | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.8344 | 0.8687 | 0.9066 | 0.8580 | 0.8880 | 0.9140 | 0.9340 |
| F2LLM-v2 1.7B | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.8148 | 0.8349 | 0.8637 | 0.8860 | 0.9060 | 0.9240 | 0.9520 |
| F2LLM-v2 1.7B | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.7743 | 0.7957 | 0.8290 | 0.8440 | 0.8660 | 0.8900 | 0.9340 |
| F2LLM-v2 1.7B | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.8817 | 0.9020 | 0.9330 | 0.9280 | 0.9400 | 0.9500 | 0.9640 |
| F2LLM-v2 1.7B | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.8576 | 0.8864 | 0.9199 | 0.9020 | 0.9180 | 0.9300 | 0.9520 |
| F2LLM-v2 1.7B | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.8130 | 0.8455 | 0.9032 | 0.8540 | 0.8800 | 0.9140 | 0.9340 |
| F2LLM-v2 1.7B | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.8025 | 0.8156 | 0.8400 | 0.9320 | 0.9460 | 0.9540 | 0.9640 |
| F2LLM-v2 1.7B | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.7734 | 0.7847 | 0.8267 | 0.9000 | 0.9100 | 0.9320 | 0.9520 |
| F2LLM-v2 1.7B | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.7155 | 0.7418 | 0.7883 | 0.8460 | 0.8700 | 0.8900 | 0.9340 |
| | | | | | | | | | | | | |
| F2LLM-v2 0.6B | 3600/720/min480 | raw | hybrid + rerank | vector_plus_field top50 -> Cohere rerank-4-pro + window | 1.000 | 0.9258 | 0.9376 | 0.9552 | 0.9520 | 0.9580 | 0.9600 | 0.9680 |
| F2LLM-v2 0.6B | 3600/720/min480 | raw | hybrid + rerank | vector_plus_field top50 -> Cohere rerank-4-fast + window | 1.000 | 0.9121 | 0.9342 | 0.9572 | 0.9360 | 0.9520 | 0.9620 | 0.9680 |
| F2LLM-v2 0.6B | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.8852 | 0.9035 | 0.9263 | 0.9060 | 0.9180 | 0.9360 | 0.9680 |
| F2LLM-v2 0.6B | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.8591 | 0.8887 | 0.9179 | 0.8820 | 0.9040 | 0.9260 | 0.9460 |
| F2LLM-v2 0.6B | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.8462 | 0.8773 | 0.9019 | 0.8740 | 0.8960 | 0.9140 | 0.9400 |
| F2LLM-v2 0.6B | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.8386 | 0.8507 | 0.8773 | 0.9020 | 0.9160 | 0.9360 | 0.9680 |
| F2LLM-v2 0.6B | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.7981 | 0.8088 | 0.8376 | 0.8780 | 0.8900 | 0.9080 | 0.9460 |
| F2LLM-v2 0.6B | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.7541 | 0.7801 | 0.8180 | 0.8340 | 0.8600 | 0.8860 | 0.9400 |
| F2LLM-v2 0.6B | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.8636 | 0.8805 | 0.9166 | 0.9080 | 0.9200 | 0.9380 | 0.9640 |
| F2LLM-v2 0.6B | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.8377 | 0.8680 | 0.9119 | 0.8800 | 0.9020 | 0.9240 | 0.9440 |
| F2LLM-v2 0.6B | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.8135 | 0.8524 | 0.8922 | 0.8620 | 0.8900 | 0.9080 | 0.9360 |
| F2LLM-v2 0.6B | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.7664 | 0.7888 | 0.8220 | 0.8940 | 0.9180 | 0.9360 | 0.9640 |
| F2LLM-v2 0.6B | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.7435 | 0.7598 | 0.7914 | 0.8780 | 0.8960 | 0.9160 | 0.9440 |
| F2LLM-v2 0.6B | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.7019 | 0.7336 | 0.7778 | 0.8320 | 0.8620 | 0.8940 | 0.9360 |
| | | | | | | | | | | | | |
| F2LLM-v2 330M | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.8559 | 0.8836 | 0.9230 | 0.8760 | 0.9020 | 0.9340 | 0.9540 |
| F2LLM-v2 330M | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.8390 | 0.8781 | 0.9207 | 0.8600 | 0.8940 | 0.9300 | 0.9480 |
| F2LLM-v2 330M | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.8421 | 0.8764 | 0.8977 | 0.8700 | 0.8960 | 0.9120 | 0.9360 |
| F2LLM-v2 330M | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.7922 | 0.8277 | 0.8604 | 0.8500 | 0.8880 | 0.9180 | 0.9540 |
| F2LLM-v2 330M | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.7785 | 0.8110 | 0.8440 | 0.8500 | 0.8840 | 0.9120 | 0.9480 |
| F2LLM-v2 330M | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.7208 | 0.7657 | 0.8164 | 0.7940 | 0.8400 | 0.8840 | 0.9360 |
| F2LLM-v2 330M | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.8249 | 0.8645 | 0.9071 | 0.8720 | 0.9060 | 0.9300 | 0.9560 |
| F2LLM-v2 330M | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.8185 | 0.8573 | 0.9116 | 0.8580 | 0.8880 | 0.9260 | 0.9480 |
| F2LLM-v2 330M | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.8142 | 0.8470 | 0.8897 | 0.8580 | 0.8840 | 0.9080 | 0.9380 |
| F2LLM-v2 330M | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.7395 | 0.7665 | 0.8051 | 0.8600 | 0.8900 | 0.9200 | 0.9560 |
| F2LLM-v2 330M | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.7231 | 0.7589 | 0.7924 | 0.8440 | 0.8900 | 0.9120 | 0.9480 |
| F2LLM-v2 330M | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.6659 | 0.7160 | 0.7596 | 0.7880 | 0.8400 | 0.8800 | 0.9380 |
| | | | | | | | | | | | | |
| F2LLM-v2 160M | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.8256 | 0.8576 | 0.8884 | 0.8500 | 0.8780 | 0.9020 | 0.9200 |
| F2LLM-v2 160M | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.8007 | 0.8429 | 0.8864 | 0.8200 | 0.8580 | 0.9000 | 0.9260 |
| F2LLM-v2 160M | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.7847 | 0.8311 | 0.8705 | 0.8040 | 0.8480 | 0.8860 | 0.9180 |
| F2LLM-v2 160M | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.7022 | 0.7483 | 0.7946 | 0.7560 | 0.8080 | 0.8540 | 0.9260 |
| F2LLM-v2 160M | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.6937 | 0.7333 | 0.7825 | 0.7500 | 0.7940 | 0.8400 | 0.9200 |
| F2LLM-v2 160M | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.6932 | 0.7289 | 0.7699 | 0.7500 | 0.7880 | 0.8300 | 0.9180 |
| F2LLM-v2 160M | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.8030 | 0.8284 | 0.8743 | 0.8400 | 0.8640 | 0.8980 | 0.9160 |
| F2LLM-v2 160M | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.7663 | 0.8140 | 0.8695 | 0.7980 | 0.8480 | 0.8920 | 0.9220 |
| F2LLM-v2 160M | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.7567 | 0.8025 | 0.8514 | 0.8080 | 0.8520 | 0.8880 | 0.9160 |
| F2LLM-v2 160M | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.6511 | 0.6890 | 0.7426 | 0.7540 | 0.8040 | 0.8520 | 0.9220 |
| F2LLM-v2 160M | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.6404 | 0.6820 | 0.7169 | 0.7520 | 0.8000 | 0.8340 | 0.9160 |
| F2LLM-v2 160M | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.6418 | 0.6750 | 0.7220 | 0.7500 | 0.7960 | 0.8360 | 0.9160 |
| | | | | | | | | | | | | |
| F2LLM-v2 80M | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.7559 | 0.8023 | 0.8287 | 0.7800 | 0.8260 | 0.8480 | 0.8800 |
| F2LLM-v2 80M | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.6883 | 0.7585 | 0.8056 | 0.7040 | 0.7740 | 0.8200 | 0.8740 |
| F2LLM-v2 80M | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.7042 | 0.7531 | 0.8100 | 0.7220 | 0.7700 | 0.8260 | 0.8620 |
| F2LLM-v2 80M | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.5709 | 0.6259 | 0.6827 | 0.6160 | 0.6760 | 0.7300 | 0.8620 |
| F2LLM-v2 80M | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.5571 | 0.6227 | 0.6971 | 0.6080 | 0.6740 | 0.7520 | 0.8740 |
| F2LLM-v2 80M | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.5537 | 0.6190 | 0.6938 | 0.5980 | 0.6680 | 0.7420 | 0.8800 |
| F2LLM-v2 80M | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.7236 | 0.7677 | 0.8152 | 0.7680 | 0.8080 | 0.8380 | 0.8740 |
| F2LLM-v2 80M | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.6668 | 0.7267 | 0.7909 | 0.7080 | 0.7700 | 0.8300 | 0.8680 |
| F2LLM-v2 80M | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.6488 | 0.7137 | 0.7858 | 0.6820 | 0.7460 | 0.8100 | 0.8660 |
| F2LLM-v2 80M | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.5214 | 0.5803 | 0.6377 | 0.6160 | 0.6860 | 0.7440 | 0.8680 |
| F2LLM-v2 80M | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.5020 | 0.5777 | 0.6519 | 0.5880 | 0.6720 | 0.7480 | 0.8740 |
| F2LLM-v2 80M | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.5077 | 0.5681 | 0.6435 | 0.5980 | 0.6660 | 0.7480 | 0.8660 |
| | | | | | | | | | | | | |
| Qwen3 Embedding 8B | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.9059 | 0.9234 | 0.9433 | 0.9280 | 0.9420 | 0.9540 | 0.9640 |
| Qwen3 Embedding 8B | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.8767 | 0.9046 | 0.9297 | 0.9000 | 0.9220 | 0.9360 | 0.9500 |
| Qwen3 Embedding 8B | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.8741 | 0.8822 | 0.8959 | 0.9360 | 0.9440 | 0.9540 | 0.9640 |
| Qwen3 Embedding 8B | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.8502 | 0.8641 | 0.9005 | 0.8760 | 0.8860 | 0.9120 | 0.9360 |
| Qwen3 Embedding 8B | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.8263 | 0.8458 | 0.8593 | 0.9020 | 0.9220 | 0.9300 | 0.9500 |
| Qwen3 Embedding 8B | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.7729 | 0.7921 | 0.8154 | 0.8480 | 0.8680 | 0.8880 | 0.9360 |
| Qwen3 Embedding 8B | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.8852 | 0.8993 | 0.9337 | 0.9340 | 0.9420 | 0.9580 | 0.9640 |
| Qwen3 Embedding 8B | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.8623 | 0.8865 | 0.9270 | 0.9020 | 0.9220 | 0.9400 | 0.9520 |
| Qwen3 Embedding 8B | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.8161 | 0.8476 | 0.8943 | 0.8600 | 0.8860 | 0.9100 | 0.9340 |
| Qwen3 Embedding 8B | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.8158 | 0.8223 | 0.8418 | 0.9440 | 0.9500 | 0.9580 | 0.9640 |
| Qwen3 Embedding 8B | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.7799 | 0.7900 | 0.8161 | 0.9060 | 0.9160 | 0.9280 | 0.9520 |
| Qwen3 Embedding 8B | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.7157 | 0.7364 | 0.7688 | 0.8500 | 0.8700 | 0.8900 | 0.9340 |
| | | | | | | | | | | | | |
| Qwen3 Embedding 4B | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.9091 | 0.9288 | 0.9519 | 0.9340 | 0.9460 | 0.9580 | 0.9640 |
| Qwen3 Embedding 4B | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.8996 | 0.9151 | 0.9381 | 0.9240 | 0.9320 | 0.9420 | 0.9600 |
| Qwen3 Embedding 4B | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.8621 | 0.8926 | 0.9281 | 0.8900 | 0.9120 | 0.9400 | 0.9640 |
| Qwen3 Embedding 4B | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.8627 | 0.8763 | 0.8970 | 0.9320 | 0.9460 | 0.9580 | 0.9640 |
| Qwen3 Embedding 4B | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.8392 | 0.8492 | 0.8688 | 0.9120 | 0.9220 | 0.9340 | 0.9600 |
| Qwen3 Embedding 4B | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.8127 | 0.8304 | 0.8654 | 0.8840 | 0.9020 | 0.9320 | 0.9640 |
| Qwen3 Embedding 4B | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.8846 | 0.9077 | 0.9438 | 0.9340 | 0.9500 | 0.9600 | 0.9640 |
| Qwen3 Embedding 4B | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.8838 | 0.9007 | 0.9349 | 0.9260 | 0.9360 | 0.9440 | 0.9600 |
| Qwen3 Embedding 4B | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.8407 | 0.8724 | 0.9213 | 0.8820 | 0.9080 | 0.9340 | 0.9640 |
| Qwen3 Embedding 4B | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.8099 | 0.8181 | 0.8423 | 0.9380 | 0.9460 | 0.9560 | 0.9640 |
| Qwen3 Embedding 4B | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.7797 | 0.7903 | 0.8257 | 0.9120 | 0.9220 | 0.9340 | 0.9600 |
| Qwen3 Embedding 4B | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.7584 | 0.7672 | 0.8222 | 0.8880 | 0.8980 | 0.9300 | 0.9640 |
| | | | | | | | | | | | | |
| Qwen3 Embedding 0.6B | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.8630 | 0.8978 | 0.9338 | 0.8820 | 0.9160 | 0.9400 | 0.9620 |
| Qwen3 Embedding 0.6B | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.8450 | 0.8854 | 0.9206 | 0.8680 | 0.9020 | 0.9260 | 0.9480 |
| Qwen3 Embedding 0.6B | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.8132 | 0.8531 | 0.8890 | 0.8380 | 0.8720 | 0.8980 | 0.9380 |
| Qwen3 Embedding 0.6B | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.8032 | 0.8402 | 0.8747 | 0.8660 | 0.9040 | 0.9300 | 0.9620 |
| Qwen3 Embedding 0.6B | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.7816 | 0.8206 | 0.8529 | 0.8520 | 0.8940 | 0.9200 | 0.9480 |
| Qwen3 Embedding 0.6B | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.7262 | 0.7744 | 0.8166 | 0.8000 | 0.8500 | 0.8860 | 0.9380 |
| Qwen3 Embedding 0.6B | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.8382 | 0.8735 | 0.9231 | 0.8840 | 0.9140 | 0.9400 | 0.9620 |
| Qwen3 Embedding 0.6B | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.8037 | 0.8550 | 0.9172 | 0.8460 | 0.8920 | 0.9300 | 0.9480 |
| Qwen3 Embedding 0.6B | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.7860 | 0.8227 | 0.8757 | 0.8300 | 0.8620 | 0.8900 | 0.9280 |
| Qwen3 Embedding 0.6B | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.7346 | 0.7722 | 0.8248 | 0.8580 | 0.8980 | 0.9300 | 0.9620 |
| Qwen3 Embedding 0.6B | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.7183 | 0.7582 | 0.8160 | 0.8420 | 0.8820 | 0.9160 | 0.9480 |
| Qwen3 Embedding 0.6B | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.6671 | 0.7114 | 0.7742 | 0.7920 | 0.8400 | 0.8800 | 0.9280 |
| | | | | | | | | | | | | |
| Harrier-OSS-v1 0.6B | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.8927 | 0.9208 | 0.9416 | 0.9140 | 0.9380 | 0.9480 | 0.9620 |
| Harrier-OSS-v1 0.6B | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.8444 | 0.8647 | 0.8870 | 0.9100 | 0.9320 | 0.9460 | 0.9620 |
| Harrier-OSS-v1 0.6B | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.7850 | 0.8298 | 0.8718 | 0.8100 | 0.8520 | 0.8860 | 0.9080 |
| Harrier-OSS-v1 0.6B | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.7654 | 0.8092 | 0.8376 | 0.7840 | 0.8220 | 0.8480 | 0.8860 |
| Harrier-OSS-v1 0.6B | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.6330 | 0.6817 | 0.7187 | 0.7080 | 0.7580 | 0.7940 | 0.9080 |
| Harrier-OSS-v1 0.6B | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.6534 | 0.6786 | 0.7236 | 0.7100 | 0.7400 | 0.7840 | 0.8860 |
| Harrier-OSS-v1 0.6B | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.8678 | 0.8955 | 0.9297 | 0.9160 | 0.9360 | 0.9480 | 0.9580 |
| Harrier-OSS-v1 0.6B | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.7904 | 0.8144 | 0.8523 | 0.8240 | 0.8480 | 0.8760 | 0.9100 |
| Harrier-OSS-v1 0.6B | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.7658 | 0.8120 | 0.8636 | 0.8100 | 0.8480 | 0.8780 | 0.9100 |
| Harrier-OSS-v1 0.6B | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.7820 | 0.8042 | 0.8186 | 0.9140 | 0.9360 | 0.9420 | 0.9580 |
| Harrier-OSS-v1 0.6B | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.6515 | 0.6606 | 0.6995 | 0.7740 | 0.7920 | 0.8360 | 0.9100 |
| Harrier-OSS-v1 0.6B | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.5821 | 0.6364 | 0.6847 | 0.7160 | 0.7760 | 0.8160 | 0.9100 |
| | | | | | | | | | | | | |
| Harrier-OSS-v1 270M | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.8124 | 0.8421 | 0.8873 | 0.8320 | 0.8600 | 0.9000 | 0.9300 |
| Harrier-OSS-v1 270M | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.7207 | 0.7504 | 0.7685 | 0.7520 | 0.7760 | 0.7860 | 0.8180 |
| Harrier-OSS-v1 270M | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.7050 | 0.7432 | 0.7892 | 0.7600 | 0.8020 | 0.8480 | 0.9300 |
| Harrier-OSS-v1 270M | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.6892 | 0.7255 | 0.7754 | 0.7060 | 0.7380 | 0.7880 | 0.8200 |
| Harrier-OSS-v1 270M | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.5774 | 0.6151 | 0.6626 | 0.6340 | 0.6740 | 0.7220 | 0.8200 |
| Harrier-OSS-v1 270M | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.4734 | 0.5337 | 0.6001 | 0.5340 | 0.5980 | 0.6720 | 0.8180 |
| Harrier-OSS-v1 270M | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.7872 | 0.8150 | 0.8676 | 0.8380 | 0.8620 | 0.9060 | 0.9400 |
| Harrier-OSS-v1 270M | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.6932 | 0.7345 | 0.7929 | 0.7320 | 0.7660 | 0.8220 | 0.8620 |
| Harrier-OSS-v1 270M | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.6708 | 0.7151 | 0.7569 | 0.7200 | 0.7580 | 0.7800 | 0.8120 |
| Harrier-OSS-v1 270M | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.6519 | 0.6999 | 0.7500 | 0.7500 | 0.8060 | 0.8620 | 0.9400 |
| Harrier-OSS-v1 270M | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.5594 | 0.6042 | 0.6563 | 0.6520 | 0.7020 | 0.7480 | 0.8620 |
| Harrier-OSS-v1 270M | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.4279 | 0.5031 | 0.5706 | 0.5440 | 0.6240 | 0.6800 | 0.8120 |
| | | | | | | | | | | | | |
| Jina Embeddings v5 Text Small Retrieval | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.8907 | 0.9182 | 0.9413 | 0.9120 | 0.9360 | 0.9480 | 0.9580 |
| Jina Embeddings v5 Text Small Retrieval | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.8621 | 0.8993 | 0.9297 | 0.8840 | 0.9160 | 0.9400 | 0.9480 |
| Jina Embeddings v5 Text Small Retrieval | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.8273 | 0.8681 | 0.8997 | 0.8560 | 0.8920 | 0.9120 | 0.9380 |
| Jina Embeddings v5 Text Small Retrieval | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.8496 | 0.8669 | 0.8815 | 0.9120 | 0.9300 | 0.9400 | 0.9580 |
| Jina Embeddings v5 Text Small Retrieval | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.8001 | 0.8290 | 0.8550 | 0.8720 | 0.9060 | 0.9260 | 0.9480 |
| Jina Embeddings v5 Text Small Retrieval | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.7534 | 0.7957 | 0.8275 | 0.8260 | 0.8700 | 0.8960 | 0.9380 |
| Jina Embeddings v5 Text Small Retrieval | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.8627 | 0.8906 | 0.9299 | 0.9060 | 0.9300 | 0.9460 | 0.9580 |
| Jina Embeddings v5 Text Small Retrieval | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.8281 | 0.8666 | 0.9142 | 0.8660 | 0.9000 | 0.9340 | 0.9480 |
| Jina Embeddings v5 Text Small Retrieval | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.7978 | 0.8292 | 0.8937 | 0.8360 | 0.8640 | 0.9080 | 0.9380 |
| Jina Embeddings v5 Text Small Retrieval | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.7778 | 0.7965 | 0.8201 | 0.9060 | 0.9260 | 0.9360 | 0.9580 |
| Jina Embeddings v5 Text Small Retrieval | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.7548 | 0.7775 | 0.8168 | 0.8760 | 0.9080 | 0.9260 | 0.9480 |
| Jina Embeddings v5 Text Small Retrieval | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.6905 | 0.7302 | 0.7799 | 0.8140 | 0.8600 | 0.8900 | 0.9380 |
| | | | | | | | | | | | | |
| Jina Embeddings v4 Text Code | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.8601 | 0.8955 | 0.9207 | 0.8760 | 0.9100 | 0.9280 | 0.9520 |
| Jina Embeddings v4 Text Code | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.8379 | 0.8721 | 0.9107 | 0.8620 | 0.8900 | 0.9200 | 0.9420 |
| Jina Embeddings v4 Text Code | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.7890 | 0.8074 | 0.8333 | 0.8640 | 0.8860 | 0.9080 | 0.9520 |
| Jina Embeddings v4 Text Code | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.7665 | 0.7933 | 0.8320 | 0.8420 | 0.8740 | 0.9080 | 0.9420 |
| Jina Embeddings v4 Text Code | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.6598 | 0.6901 | 0.7238 | 0.6740 | 0.7020 | 0.7360 | 0.7780 |
| Jina Embeddings v4 Text Code | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.5543 | 0.5895 | 0.6189 | 0.6080 | 0.6460 | 0.6820 | 0.7780 |
| Jina Embeddings v4 Text Code | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.8285 | 0.8726 | 0.9140 | 0.8720 | 0.9060 | 0.9260 | 0.9480 |
| Jina Embeddings v4 Text Code | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.8138 | 0.8512 | 0.9108 | 0.8560 | 0.8860 | 0.9240 | 0.9440 |
| Jina Embeddings v4 Text Code | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.7410 | 0.7648 | 0.7990 | 0.8720 | 0.8940 | 0.9140 | 0.9480 |
| Jina Embeddings v4 Text Code | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.7019 | 0.7425 | 0.7807 | 0.7480 | 0.8040 | 0.8320 | 0.8680 |
| Jina Embeddings v4 Text Code | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.7192 | 0.7420 | 0.7877 | 0.8560 | 0.8760 | 0.9120 | 0.9440 |
| Jina Embeddings v4 Text Code | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.5798 | 0.6091 | 0.6438 | 0.7000 | 0.7360 | 0.7800 | 0.8680 |
| | | | | | | | | | | | | |
| BGE-M3 Q8 | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.8254 | 0.8663 | 0.9041 | 0.8500 | 0.8860 | 0.9140 | 0.9360 |
| BGE-M3 Q8 | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.8025 | 0.8475 | 0.8941 | 0.8280 | 0.8680 | 0.9020 | 0.9280 |
| BGE-M3 Q8 | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.7687 | 0.8270 | 0.8784 | 0.7940 | 0.8440 | 0.8860 | 0.9140 |
| BGE-M3 Q8 | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.7479 | 0.7873 | 0.8348 | 0.8160 | 0.8560 | 0.9000 | 0.9360 |
| BGE-M3 Q8 | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.7229 | 0.7663 | 0.8098 | 0.7900 | 0.8340 | 0.8740 | 0.9280 |
| BGE-M3 Q8 | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.7020 | 0.7526 | 0.8022 | 0.7640 | 0.8160 | 0.8600 | 0.9140 |
| BGE-M3 Q8 | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.8084 | 0.8422 | 0.8899 | 0.8560 | 0.8820 | 0.9080 | 0.9340 |
| BGE-M3 Q8 | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.7785 | 0.8219 | 0.8842 | 0.8240 | 0.8620 | 0.9000 | 0.9220 |
| BGE-M3 Q8 | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.7481 | 0.7914 | 0.8652 | 0.7940 | 0.8340 | 0.8920 | 0.9120 |
| BGE-M3 Q8 | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.6829 | 0.7307 | 0.7778 | 0.8120 | 0.8600 | 0.8980 | 0.9340 |
| BGE-M3 Q8 | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.6638 | 0.7117 | 0.7482 | 0.7980 | 0.8480 | 0.8780 | 0.9220 |
| BGE-M3 Q8 | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.6478 | 0.7061 | 0.7539 | 0.7680 | 0.8340 | 0.8720 | 0.9120 |
| | | | | | | | | | | | | |
| Jina Embeddings v5 Text Nano Retrieval | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.8085 | 0.8539 | 0.8938 | 0.8340 | 0.8700 | 0.9020 | 0.9380 |
| Jina Embeddings v5 Text Nano Retrieval | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.8205 | 0.8521 | 0.8835 | 0.8480 | 0.8720 | 0.8960 | 0.9300 |
| Jina Embeddings v5 Text Nano Retrieval | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.7967 | 0.8405 | 0.8974 | 0.8180 | 0.8580 | 0.9060 | 0.9420 |
| Jina Embeddings v5 Text Nano Retrieval | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.7234 | 0.7609 | 0.8058 | 0.7840 | 0.8220 | 0.8640 | 0.9380 |
| Jina Embeddings v5 Text Nano Retrieval | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.7155 | 0.7458 | 0.8119 | 0.7760 | 0.8080 | 0.8660 | 0.9420 |
| Jina Embeddings v5 Text Nano Retrieval | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.6671 | 0.7267 | 0.7861 | 0.7300 | 0.7900 | 0.8440 | 0.9300 |
| Jina Embeddings v5 Text Nano Retrieval | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.7949 | 0.8256 | 0.8735 | 0.8400 | 0.8660 | 0.8920 | 0.9260 |
| Jina Embeddings v5 Text Nano Retrieval | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.7774 | 0.8192 | 0.8731 | 0.8240 | 0.8620 | 0.9000 | 0.9340 |
| Jina Embeddings v5 Text Nano Retrieval | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.7506 | 0.8134 | 0.8852 | 0.7960 | 0.8540 | 0.9060 | 0.9380 |
| Jina Embeddings v5 Text Nano Retrieval | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.6665 | 0.6953 | 0.7487 | 0.7840 | 0.8140 | 0.8540 | 0.9340 |
| Jina Embeddings v5 Text Nano Retrieval | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.6563 | 0.6844 | 0.7551 | 0.7700 | 0.8060 | 0.8600 | 0.9380 |
| Jina Embeddings v5 Text Nano Retrieval | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.6007 | 0.6659 | 0.7327 | 0.7220 | 0.7960 | 0.8440 | 0.9260 |
| | | | | | | | | | | | | |
| Jina Code 1.5B | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.8041 | 0.8219 | 0.8579 | 0.8260 | 0.8400 | 0.8700 | 0.9040 |
| Jina Code 1.5B | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.7750 | 0.8164 | 0.8535 | 0.7940 | 0.8340 | 0.8680 | 0.9120 |
| Jina Code 1.5B | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.7815 | 0.8110 | 0.8718 | 0.8000 | 0.8280 | 0.8840 | 0.9240 |
| Jina Code 1.5B | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.6604 | 0.7322 | 0.7814 | 0.7180 | 0.7940 | 0.8340 | 0.9240 |
| Jina Code 1.5B | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.6761 | 0.7320 | 0.7734 | 0.7260 | 0.7860 | 0.8260 | 0.9120 |
| Jina Code 1.5B | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.6302 | 0.6846 | 0.7391 | 0.6880 | 0.7480 | 0.7940 | 0.9040 |
| Jina Code 1.5B | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.7818 | 0.8113 | 0.8569 | 0.8300 | 0.8520 | 0.8760 | 0.9080 |
| Jina Code 1.5B | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.7521 | 0.8001 | 0.8698 | 0.7920 | 0.8360 | 0.8920 | 0.9200 |
| Jina Code 1.5B | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.7514 | 0.7918 | 0.8401 | 0.7940 | 0.8340 | 0.8720 | 0.9180 |
| Jina Code 1.5B | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.6198 | 0.6790 | 0.7393 | 0.7300 | 0.7960 | 0.8440 | 0.9200 |
| Jina Code 1.5B | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.6356 | 0.6753 | 0.7183 | 0.7360 | 0.7840 | 0.8300 | 0.9180 |
| Jina Code 1.5B | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.5911 | 0.6286 | 0.6973 | 0.6980 | 0.7420 | 0.8020 | 0.9080 |
| | | | | | | | | | | | | |
| OpenAI text-embedding-3-small | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.7763 | 0.8110 | 0.8492 | 0.8040 | 0.8320 | 0.8640 | 0.8920 |
| OpenAI text-embedding-3-small | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.7297 | 0.7747 | 0.8226 | 0.7480 | 0.7880 | 0.8320 | 0.8840 |
| OpenAI text-embedding-3-small | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.7048 | 0.7508 | 0.8092 | 0.7200 | 0.7620 | 0.8220 | 0.8720 |
| OpenAI text-embedding-3-small | 3600/720/min480 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.427 | 0.6327 | 0.6775 | 0.7381 | 0.6960 | 0.7460 | 0.8040 | 0.8920 |
| OpenAI text-embedding-3-small | 3600/720/min480 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.546 | 0.6150 | 0.6722 | 0.7221 | 0.6780 | 0.7400 | 0.7920 | 0.8840 |
| OpenAI text-embedding-3-small | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.5853 | 0.6460 | 0.7176 | 0.6280 | 0.6920 | 0.7760 | 0.8720 |
| OpenAI text-embedding-3-small | 2200/440/min300 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.461 | 0.7535 | 0.7878 | 0.8381 | 0.7920 | 0.8240 | 0.8520 | 0.8900 |
| OpenAI text-embedding-3-small | 2200/440/min300 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.589 | 0.6943 | 0.7480 | 0.8082 | 0.7360 | 0.7840 | 0.8240 | 0.8780 |
| OpenAI text-embedding-3-small | 2200/440/min300 | raw | hybrid | vector_plus_field + large_top1_window 2-2-1 keep1 | 1.000 | 0.6896 | 0.7275 | 0.7912 | 0.7320 | 0.7680 | 0.8300 | 0.8860 |
| OpenAI text-embedding-3-small | 2200/440/min300 | lexdedup cap1 | embedding-only | vector top50 + gated window | 0.461 | 0.6038 | 0.6345 | 0.7060 | 0.7160 | 0.7500 | 0.8060 | 0.8900 |
| OpenAI text-embedding-3-small | 2200/440/min300 | raw<1000 + lexdedup cap1 | embedding-only | vector top50 + gated window | 0.589 | 0.5657 | 0.6285 | 0.6932 | 0.6760 | 0.7420 | 0.7920 | 0.8780 |
| OpenAI text-embedding-3-small | 2200/440/min300 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.5565 | 0.6212 | 0.6752 | 0.6440 | 0.7220 | 0.7820 | 0.8860 |
| | | | | | | | | | | | | |
| none | 3600/720/min480 | raw | no-embedding | RLM intent-routing + large_top1_window 3-1-1 | n/a | 0.6723 | 0.7204 | 0.7837 | 0.6880 | 0.7420 | 0.8100 | 0.8840 |
| none | 2200/440/min300 | raw | no-embedding | RLM intent-routing + always_window 3-1-1 | n/a | 0.6419 | 0.6996 | 0.7746 | 0.6660 | 0.7300 | 0.8120 | 0.8820 |

## Таблица 2. Сравнение embedding-моделей

Сравнение embedding-моделей вынесено в отдельную таблицу, чтобы отделить качество самих моделей от влияния сжатия и `rerank`.

Фиксированный pipeline для честного сравнения моделей:

```text
Split: 3600/720/min480
Compression: raw
Retrieval mode: embedding-only
Vector candidates: flat top50
Selector: gated window
```

| Model | Context tokens | Vector dim | Prompt mode | Split | Compression | Retrieval mode | coverage@3 | coverage@5 | coverage@10 | answer_hit@5 | answer_hit@10 | parent@50 |
|---|---:|---:|---|---|---|---|---:|---:|---:|---:|---:|---:|
| Qwen3 Embedding 8B Q8 | 40960 | 4096 | qwen3-code-search | 3600/720/min480 | raw | embedding-only | 0.8741 | 0.8822 | 0.8959 | 0.9440 | 0.9540 | 0.9640 |
| Perplexity pplx-embed-v1 4B | 32768 | 2560 | none | 3600/720/min480 | raw | embedding-only | 0.8662 | 0.8795 | 0.9032 | 0.9440 | 0.9580 | 0.9640 |
| Qwen3 Embedding 4B Q8 | 40960 | 2560 | qwen3-code-search | 3600/720/min480 | raw | embedding-only | 0.8627 | 0.8763 | 0.8970 | 0.9460 | 0.9580 | 0.9640 |
| F2LLM-v2 1.7B Q8 | 40960 | 2048 | f2llm-code-search | 3600/720/min480 | raw | embedding-only | 0.8641 | 0.8739 | 0.8915 | 0.9420 | 0.9500 | 0.9600 |
| Nomic Embed Code Q8 | 32768 | 3584 | nomic-code-search | 3600/720/min480 | raw | embedding-only | 0.8585 | 0.8724 | 0.8924 | 0.9420 | 0.9540 | 0.9700 |
| Jina Embeddings v5 Text Small Retrieval Q8 | 40960 | 1024 | jina-v5-retrieval | 3600/720/min480 | raw | embedding-only | 0.8496 | 0.8669 | 0.8815 | 0.9300 | 0.9400 | 0.9580 |
| Harrier-OSS-v1 0.6B Q8 | 32768 | 1024 | harrier-code-search | 3600/720/min480 | raw | embedding-only | 0.8444 | 0.8647 | 0.8870 | 0.9320 | 0.9460 | 0.9620 |
| F2LLM-v2 4B Q8 | 40960 | 2560 | f2llm-code-search | 3600/720/min480 | raw | embedding-only | 0.8517 | 0.8640 | 0.8877 | 0.9300 | 0.9500 | 0.9620 |
| F2LLM-v2 0.6B Q8 | 40960 | 1024 | f2llm-code-search | 3600/720/min480 | raw | embedding-only | 0.8386 | 0.8507 | 0.8773 | 0.9160 | 0.9360 | 0.9680 |
| Qwen3 Embedding 0.6B Q8 | 32768 | 1024 | qwen3-code-search | 3600/720/min480 | raw | embedding-only | 0.8032 | 0.8402 | 0.8747 | 0.9040 | 0.9300 | 0.9620 |
| Perplexity pplx-embed-v1 0.6B | 32768 | 1024 | none | 3600/720/min480 | raw | embedding-only | 0.8022 | 0.8363 | 0.8583 | 0.9040 | 0.9220 | 0.9500 |
| F2LLM-v2 330M Q8 | 40960 | 896 | f2llm-code-search | 3600/720/min480 | raw | embedding-only | 0.7922 | 0.8277 | 0.8604 | 0.8880 | 0.9180 | 0.9540 |
| Jina Embeddings v5 Text Nano Retrieval Q8 | 8192 | 768 | jina-v5-retrieval | 3600/720/min480 | raw | embedding-only | 0.7234 | 0.7609 | 0.8058 | 0.8220 | 0.8640 | 0.9380 |
| BGE-M3 Q8 | 8192 | 1024 | bge-m3-dense | 3600/720/min480 | raw | embedding-only | 0.7020 | 0.7526 | 0.8022 | 0.8160 | 0.8600 | 0.9140 |
| Harrier-OSS-v1 270M Q8 | 32768 | 640 | harrier-code-search | 3600/720/min480 | raw | embedding-only | 0.7050 | 0.7432 | 0.7892 | 0.8020 | 0.8480 | 0.9300 |
| Jina Code 1.5B Q8 | 32768 | 1536 | jina-code-nl2code | 3600/720/min480 | raw | embedding-only | 0.6761 | 0.7320 | 0.7734 | 0.7860 | 0.8260 | 0.9120 |
| F2LLM-v2 160M Q8 | 40960 | 640 | f2llm-code-search | 3600/720/min480 | raw | embedding-only | 0.6932 | 0.7289 | 0.7699 | 0.7880 | 0.8300 | 0.9180 |
| OpenAI text-embedding-3-small | 8191 | 1536 | none | 3600/720/min480 | raw | embedding-only | 0.5853 | 0.6460 | 0.7176 | 0.6920 | 0.7760 | 0.8720 |
| F2LLM-v2 80M Q8 | 40960 | 320 | f2llm-code-search | 3600/720/min480 | raw | embedding-only | 0.5709 | 0.6259 | 0.6827 | 0.6760 | 0.7300 | 0.8620 |
| Jina Embeddings v4 Text Code Q8 | 128000 | 2048 | jina-v4-code | 3600/720/min480 | raw | embedding-only | 0.5543 | 0.5895 | 0.6189 | 0.6460 | 0.6820 | 0.7780 |

## Таблица 3. Лучшие итоговые режимы

В итоговую таблицу вошли не все экспериментальные комбинации, а только лучшие результаты в каждой категории.

| Category | Model | Split | Compression | Retrieval mode | Selector / rerank | Embed text ratio | coverage@5 | coverage@10 | answer_hit@5 | answer_hit@10 | Comment |
|---|---|---|---|---|---|---:|---:|---:|---:|---:|---|
| Best rerank over hybrid | F2LLM-v2 0.6B | 3600/720/min480 | raw | hybrid + rerank | vector_plus_field top50 -> Cohere rerank-4-pro + window | 1.000 | 0.9376 | 0.9552 | 0.9580 | 0.9600 | лучший rerank по coverage@5; Cohere rerank-4-fast близок: 0.9342 |
| Best raw | Qwen3 Embedding 4B | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 1.000 | 0.9288 | 0.9519 | 0.9460 | 0.9580 | лучший raw-режим по coverage@5 |
| Best compressed | Nomic Embed Code | 3600/720/min480 | raw<1000 + lexdedup cap1 | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 0.546 | 0.9277 | 0.9447 | 0.9460 | 0.9500 | лучший compressed-режим по coverage@5 |
| Best size/quality tradeoff | F2LLM-v2 4B | 3600/720/min480 | lexdedup cap1 | hybrid | unified_top5_gated + large_top1_window 2-2-1 keep1 | 0.427 | 0.9018 | 0.9293 | 0.9220 | 0.9380 | 42.7% raw-текста при coverage@5 выше 0.90 |
| Best embedding-only | Qwen3 Embedding 8B | 3600/720/min480 | raw | embedding-only | vector top50 + gated window | 1.000 | 0.8822 | 0.8959 | 0.9440 | 0.9540 | лучший embedding-only режим по coverage@5 |
| Best no-embedding | none | 3600/720/min480 | raw | no-embedding | RLM intent-routing + large_top1_window 3-1-1 | n/a | 0.7204 | 0.7837 | 0.7420 | 0.8100 | лучший no-embedding режим по coverage@5 |

## Таблица 4. Проверка на полном индексе конфигурации

В основной таблице качество измеряется на контрольном индексе: после нарезки 25 000 исходных процедур и функций получилось 27 737 retrieval units. Отдельно проверили тот же набор из 500 вопросов на полном индексе Бухгалтерии 3.0: 528 155 retrieval units, полученных из 474 176 исходных единиц полного индекса. Это более жесткая проверка: кандидатный пул содержит весь код конфигурации, а слой эквивалентности размечен только для контрольного набора, поэтому результат можно читать как нижнюю оценку.

Строки `drop` — это не отдельный прогон, а разница метрик между контрольным и полным индексом для той же конфигурации (полный минус контрольный); отрицательные значения показывают, насколько проседает качество при переходе на полный индекс.

| Index scope | Retrieval units | Model | Split | Compression | Retrieval mode | Selector / rerank | source_top_k | coverage@3 | coverage@5 | coverage@10 | answer_hit@5 | answer_hit@10 | parent@50 | Eval time | Notes |
|---|---:|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| control index | 27 737 | F2LLM-v2 0.6B Q8 | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 50 | 0.8852 | 0.9035 | 0.9263 | 0.9180 | 0.9360 | 0.9680 | n/a | основной контрольный результат |
| full BUH30 index | 528 155 | F2LLM-v2 0.6B Q8 | 3600/720/min480 | raw | hybrid | vector_plus_field + always_window 2-2-1 keep1 | 50 | 0.6623 | 0.7102 | 0.7773 | 0.7320 | 0.8020 | 0.8960 | 4489.8s | streaming SQLite evaluation; lower-bound метрика из-за неполной разметки эквивалентов |
| drop | - | F2LLM-v2 0.6B Q8 | 3600/720/min480 | raw | hybrid | same | 50 | -0.2229 | -0.1933 | -0.1490 | -0.1860 | -0.1340 | -0.0720 | n/a | просадка при переходе с контрольного индекса на полный индекс |
| | | | | | | | | | | | | | | | |
| control index | 27 737 | none | 3600/720/min480 | raw | no-embedding | RLM intent-routing + large_top1_window 3-1-1 | 50 | 0.6723 | 0.7204 | 0.7837 | 0.7420 | 0.8100 | 0.8840 | n/a | контрольный no-embedding fallback |
| full BUH30 index | 528 155 | none | 3600/720/min480 | raw | no-embedding | RLM intent-routing + large_top1_window 3-1-1 | 50 | 0.3675 | 0.4323 | 0.5247 | 0.4420 | 0.5380 | 0.7280 | 5638.5s | full-id mapped evaluation |
| drop | - | none | 3600/720/min480 | raw | no-embedding | same | 50 | -0.3048 | -0.2881 | -0.2590 | -0.3000 | -0.2720 | -0.1560 | n/a | просадка no-embedding при переходе с контрольного индекса на полный индекс |

## Описание режимов

### Split

**`3600/720/min480`** — основная нарезка больших процедур.

Алгоритм:

```text
1. Маленькие процедуры и функции остаются целиком.
2. Большие процедуры и функции режутся на последовательные фрагменты.
3. Целевой размер фрагмента - около 3600 символов.
4. Следующий фрагмент начинается с перекрытием около 720 символов относительно предыдущего.
5. Если безопасная граница по AST сдвигает место разреза, перекрытие не должно становиться меньше 480 символов.
6. Разрез старается попадать на безопасную границу: конец выражения, блока, строки или AST-узла.
7. У каждого фрагмента сохраняются идентификатор исходной процедуры, номер фрагмента, диапазон строк и диапазон символов.
```

То есть поиск идет по фрагментам, но метрики считаются против исходной процедуры и проверенных диапазонов ответа.

**`2200/440/min300`** — более мелкая нарезка по той же схеме:

```text
target part size: около 2200 символов
target overlap: около 440 символов
minimum overlap after AST-safe adjustment: 300 символов
```

Она возвращает более короткие фрагменты, но сильнее дробит большие процедуры.

### Compression

**`raw`** (в приложении — `none`). В embedding отправляется исходный текст retrieval unit.

**`lexdedup cap1`** (в приложении — `lexdedup_terms_cap1_lines_normprefix`). Compressed baseline. Повторяющиеся lexical terms внутри chunk оставляются один раз, порядок группируется по первой исходной строке.

**`raw<1000 + lexdedup cap1`** (в приложении — `rawbelow1000_lexdedup_terms_cap1_lines_normprefix`). Короткие чанки меньше 1000 символов остаются raw, остальные сжимаются как `lexdedup cap1`. По бенчмарку — лучший режим среди сжатых; в приложении доступен как отдельная стратегия компрессии.

### Retrieval Mode

**`embedding-only`** — только векторный поиск. Нужен как чистая проверка качества embedding-модели и compression input.

**`hybrid`** — векторные кандидаты пересортировываются с учетом lexical / BM25 / metadata / field signals.

**`no-embedding`** — fallback без embedding: SQLite RLM / FTS / BM25-подобный поиск и parent/window selector.

### Compressed Rule

Для compressed-режимов важно:

```text
compressed text используется только для embedding.
raw text используется для lexical rerank, BM25, field-BM25 и returned context.
```

Иначе `hybrid` начинает оценивать уже сжатый текст, а это не то, что будет в рабочей системе.

## Как читать результаты

Главная строка для качества — `coverage@5`.

Главная строка для стоимости — `embed_text_ratio`.

### Метрики

**`coverage@K`** — доля проверенного answer range, покрытая первыми K возвращенными чанками. Для маленьких процедур обычно совпадает с попаданием правильного chunk. Для больших процедур это основная метрика: можно найти правильную процедуру, но вернуть split-part, который не покрывает нужные строки.

**`answer_hit@K`** — доля вопросов, где среди первых K возвращенных чанков есть хотя бы часть правильного answer range. Это бинарная метрика: попали или не попали. Она полезна как дополнительная проверка, но для больших процедур слабее `coverage@K`, потому что не показывает, насколько хорошо покрыт нужный диапазон строк.

**`parent@50`** — доля вопросов, где правильная процедура/функция попала в первые 50 parent-кандидатов. Для больших процедур parent - это вся исходная процедура, а не отдельный split-part.

**`embed_text_ratio`** — отношение количества символов, отправленных в embedding, к raw-тексту. `1.000` означает без сжатия, `0.500` означает примерно половину raw-размера.

Хороший compressed-режим должен отвечать двум условиям:

```text
coverage@5 проседает умеренно
embed_text_ratio заметно меньше 1.0
```

Если `coverage@5` падает сильно, compression не подходит как основной индекс, даже если экономит много символов.

## Соответствие режимов настройкам приложения

Экспериментальные ярлыки в таблицах выше соответствуют реальным настройкам приложения:

| В таблицах | Настройка приложения |
|---|---|
| Split `3600/720/min480` | `BSL_CODE_SPLIT_STRATEGY=ast_safe_sliding_3600_720_min480` (по умолчанию) |
| Split `2200/440/min300` | `BSL_CODE_SPLIT_STRATEGY=ast_safe_sliding_2200_440_min300` |
| Compression `raw` | `BSL_CODE_COMPRESSION_STRATEGY=none` (по умолчанию) |
| Compression `lexdedup cap1` | `BSL_CODE_COMPRESSION_STRATEGY=lexdedup_terms_cap1_lines_normprefix` |
| Compression `raw<1000 + lexdedup cap1` | `BSL_CODE_COMPRESSION_STRATEGY=rawbelow1000_lexdedup_terms_cap1_lines_normprefix` |
| Retrieval `embedding-only` | векторная ветка (`ENABLE_BSL_CODE_EMBEDDING=true`) |
| Retrieval `hybrid` | гибрид: вектор + RLM |
| Retrieval `no-embedding` | RLM / лексическая ветка (`ENABLE_BSL_CODE_EMBEDDING=false`) |
| `hybrid + rerank` | `BSL_CODE_RERANK_ENABLED=true` (модель — общие `RERANK_*`) |

Приложение дополнительно поддерживает два режима компрессии, которые в этой серии замеров **не
участвовали**: `lexdedup_cap1_nochainparts_lines_normprefix` и
`rawbelow1000_lexdedup_cap1_nochainparts_lines_normprefix` (цепочки `Объект.Свойство` сохраняются целиком).

Дефолт приложения — `none` (raw): по бенчмарку это лучший режим по качеству. Сжатые режимы (включая
`rawbelow1000_lexdedup_terms_cap1_lines_normprefix` — лучший среди сжатых) нужны там, где важнее экономить
объём текста, отправляемого в embedding-модель, ценой умеренной просадки качества.

## Выводы

Поиск без эмбеддингов можно использовать как недорогой резервный вариант, но по качеству он заметно уступает семантическому поиску. На контрольном индексе `hybrid`-режим достигает `coverage@5 = 0.9035`, а `no-embedding` — `0.7204`. На полном индексе разрыв увеличивается: `0.7102` против `0.4323`. Значит, эмбеддинги лучше справляются со смысловыми запросами и устойчивее работают при росте объема кода.


