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
    result=$(curl -sS -o /dev/null --max-time "$TIMEOUT" \
        -w "http_code=%{http_code} time_connect=%{time_connect}s time_total=%{time_total}s size=%{size_download}\n%{http_code} %{time_total}" \
        "$url" 2>&1) || { echo "curl failed (timeout ${TIMEOUT}s exceeded or connection error)"; echo "transport_fail"; return; }
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
for host in $BASELINE_HOSTS; do
    probe_output=$(probe "baseline" "$host")
    echo "$probe_output" | sed '$d'
    result_line=$(echo "$probe_output" | tail -1)
    if is_slow "$result_line"; then
        baseline_all_fast=false
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
if [ "$baseline_all_fast" = true ] && [ "$hh_is_slow" = true ]; then
    echo "ВЕРДИКТ: baseline-хосты быстрые, hh.ru медленный/зависает -> узкое место у hh.ru (DDoS-Guard/троттлинг), не общая сеть."
elif [ "$baseline_all_fast" = false ]; then
    echo "ВЕРДИКТ: baseline-хосты тоже медленные -> проблема в сети/маршруте (VPN, провайдер), не специфична для hh.ru."
else
    echo "ВЕРДИКТ: inconclusive -> baseline быстрые, hh.ru тоже уложился в порог ${SLOW_THRESHOLD}s. Троттлинга сейчас не наблюдается."
fi
