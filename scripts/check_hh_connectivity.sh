#!/bin/sh
# Диагностика "сеть тормозит" vs "hh.ru троттлит" перед тем, как списывать
# зависший goto_hh()/ThrottledChannelDetected на анти-фрод hh.ru. Минимум
# трафика (GET без данных аккаунта), не требует storage_state/аккаунта —
# безопасно гонять вручную в любой момент, в т.ч. параллельно с боевой
# командой.
#
# Логика: если baseline (не hh.ru) хост отвечает быстро, а hh.ru — нет,
# узкое место специфично для hh.ru (DDoS-Guard/анти-фрод троттлинг), а не
# общая деградация сети/VPN на этой машине. Вердикт считается по
# фактическим time_total проб относительно $SLOW_THRESHOLD, а не печатается
# безусловно (#852 code review).
#
# Эмпирическая валидация #853 (2026-09-02, 158 прогонов за сутки): дефолтный
# порог 5с чист в ночном спокойном срезе — hh.ru ходит за 2.6-4.5с
# (p95 4.06с), baseline за 1.3-3.1с, ложных срабатываний нет. Днём хвост
# hh.ru тяжелее: штатные ответы иногда пересекают 5с (p95 5.3-7.1с),
# поэтому для дневного мониторинга используйте
# CHECK_HH_SLOW_THRESHOLD=8..10. Две границы применимости, найденные тем же
# прогоном:
# 1. DDoS-Guard может перевести curl в JS-челлендж (быстрый 403 со страницей
#    челленджа вместо 200 с контентом) — тогда time_total меряет edge DDoS-Guard, а не
#    hh.ru, и скрипт слеп к троттлингу за челленджем. Быстрый 403 у hh.ru сам
#    по себе диагностический сигнал (челлендж активен, эпоха #844).
# 2. Отказ baseline-хоста бывает персистентным (маршрут лежит минутами), а не
#    одиночным флапом — повторные пробы не лечат; лечит требование согласной
#    картины baseline для вердикта "сеть виновата" (см. блок вердикта ниже).
set -eu

# Два baseline-хоста на разных континентах/маршрутах — если тормозит
# только hh.ru, а оба baseline быстрые, вывод увереннее, чем с одним.
BASELINE_HOSTS="https://www.google.com/ https://www.baidu.com/"
HH_HOST="https://hh.ru/"
TIMEOUT="${CHECK_HH_TIMEOUT:-10}"
# Проба медленнее этого порога (секунды) считается "зависшей" для целей
# классификации ниже. Не привязан к анти-фрод эвристикам самого бота —
# это чисто диагностический скрипт для человека.
SLOW_THRESHOLD="${CHECK_HH_SLOW_THRESHOLD:-5}"

# probe: печатает сырые метрики пробы (как раньше) и возвращает через stdout
# последней строкой "transport_fail" (таймаут/сеть недоступна) или
# "<http_code> <time_total>" (транспорт отработал, независимо от статуса) —
# источник для классификации. HTTP-ошибка (4xx/5xx) — это не то же самое,
# что медленный транспорт, и is_slow() ниже судит только по time_total,
# не по http_code (#852 code review: быстрый 403 у baseline-хоста не должен
# читаться как "сеть медленная").
probe() {
    label="$1"
    url="$2"
    echo "=== ${label} (${url}) ==="
    # stderr НЕ сливаем в $result (не 2>&1) — curl -sS всё равно печатает
    # ошибки в stderr при провале, но смешивание с захватываемым stdout
    # сдвигало бы построчный разбор ниже при любом postороннем предупреждении
    # curl (#852 code review). stderr curl'а виден пользователю напрямую,
    # не через переменную.
    result=$(curl -sS -o /dev/null --max-time "$TIMEOUT" \
        -w "http_code=%{http_code} time_connect=%{time_connect}s time_total=%{time_total}s size=%{size_download}\n%{http_code} %{time_total}" \
        "$url") || { echo "curl failed (timeout ${TIMEOUT}s exceeded or connection error)"; echo "transport_fail"; return; }
    metrics_line=$(echo "$result" | sed -n '1p')
    status_line=$(echo "$result" | sed -n '2p')
    echo "$metrics_line"
    echo "$status_line"
}

# is_slow <probe-result-line>: "true" если транспорт не отработал (таймаут/
# ошибка соединения) или time_total >= SLOW_THRESHOLD. HTTP-код ответа
# (даже 4xx/5xx) не делает пробу "медленной" сам по себе — сервер мог
# ответить быстро с кодом ошибки, это отдельный сигнал.
is_slow() {
    line="$1"
    case "$line" in
        transport_fail) return 0 ;;
        *)
            t=$(echo "$line" | cut -d' ' -f2)
            awk -v t="$t" -v thr="$SLOW_THRESHOLD" 'BEGIN { exit !(t+0 >= thr+0) }'
            ;;
    esac
}

baseline_all_fast=true
baseline_all_slow=true
for host in $BASELINE_HOSTS; do
    probe_output=$(probe "baseline" "$host")
    echo "$probe_output" | sed '$d'
    result_line=$(echo "$probe_output" | tail -1)
    if is_slow "$result_line"; then
        baseline_all_fast=false
    else
        baseline_all_slow=false
    fi
done

hh_probe_output=$(probe "hh.ru" "$HH_HOST")
echo "$hh_probe_output" | sed '$d'
hh_result_line=$(echo "$hh_probe_output" | tail -1)
hh_is_slow=false
if is_slow "$hh_result_line"; then
    hh_is_slow=true
fi

echo
# Диагноз "сеть виновата" требует согласной картины от ВСЕХ baseline-хостов:
# один отказавший/медленный baseline при быстром втором — недостаточное
# основание (#853, живой кейс 2026-09-02: google.com transport_fail десятки
# минут подряд при стабильно быстром baidu.com и hh.ru, отвечавшем за ~1с —
# прежняя логика выносила уверенный "проблема в сети" по одному хосту).
if [ "$baseline_all_fast" = true ] && [ "$hh_is_slow" = true ]; then
    echo "ВЕРДИКТ: baseline-хосты быстрые, hh.ru медленный/зависает -> узкое место у hh.ru (DDoS-Guard/троттлинг), не общая сеть."
elif [ "$baseline_all_slow" = true ]; then
    echo "ВЕРДИКТ: baseline-хосты тоже медленные/недостижимы -> проблема в сети/маршруте (VPN, провайдер), не специфична для hh.ru."
else
    if [ "$baseline_all_fast" = true ]; then
        echo "ВЕРДИКТ: inconclusive -> baseline быстрые, hh.ru уложился в порог ${SLOW_THRESHOLD}s. Троттлинга сейчас не наблюдается."
    elif [ "$hh_is_slow" = true ]; then
        echo "ВЕРДИКТ: inconclusive -> данные baseline противоречивы (часть быстрая, часть медленная/недостижима), уверенный диагноз невозможен; hh.ru в этом прогоне медленный/зависает — повторите, когда маршрут baseline восстановится."
    else
        echo "ВЕРДИКТ: inconclusive -> данные baseline противоречивы (часть быстрая, часть медленная/недостижима), уверенный диагноз невозможен; hh.ru уложился в порог ${SLOW_THRESHOLD}s. Повторите, когда маршрут baseline восстановится."
    fi
fi
