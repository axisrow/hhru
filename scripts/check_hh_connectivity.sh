#!/bin/sh
# Диагностика "сеть тормозит" vs "hh.ru троттлит" перед тем, как списывать
# зависший goto_hh()/ThrottledChannelDetected на анти-фрод hh.ru. Это только
# read-only GET без данных аккаунта; storage_state/аккаунт не нужны.
#
# По умолчанию проба идёт напрямую: HTTP_PROXY, HTTPS_PROXY, ALL_PROXY и
# NO_PROXY не влияют на результат (curl --noproxy '*'). Чтобы явно измерить
# прокси-канал, передайте --via-proxy; тогда curl использует эти переменные.
# CHECK_HH_TIMEOUT и CHECK_HH_SLOW_THRESHOLD влияют на классификацию, CURL_BIN
# позволяет выбрать curl для локального теста.
set -eu

BASELINE_HOSTS="https://www.baidu.com/ https://yandex.ru/ https://www.cloudflare.com/"
HH_HOST="https://hh.ru/"
TIMEOUT="${CHECK_HH_TIMEOUT:-10}"
SLOW_THRESHOLD="${CHECK_HH_SLOW_THRESHOLD:-5}"
CURL_BIN="${CURL_BIN:-curl}"

case "${1:-}" in
    "") PROBE_CHANNEL="direct"; CURL_ROUTE="--noproxy '*'" ;;
    --via-proxy) PROBE_CHANNEL="via-proxy"; CURL_ROUTE="environment proxy" ;;
    *) echo "usage: $0 [--via-proxy]" >&2; exit 2 ;;
esac

echo "КАНАЛ ПРОБЫ: ${PROBE_CHANNEL} (${CURL_ROUTE})"
if [ "$PROBE_CHANNEL" = "direct" ]; then
    echo "ВЛИЯЮЩИЕ ENV: CHECK_HH_TIMEOUT=${TIMEOUT}, CHECK_HH_SLOW_THRESHOLD=${SLOW_THRESHOLD}; HTTP(S)_PROXY/ALL_PROXY игнорируются"
else
    echo "ВЛИЯЮЩИЕ ENV: CHECK_HH_TIMEOUT=${TIMEOUT}, CHECK_HH_SLOW_THRESHOLD=${SLOW_THRESHOLD}, HTTP(S)_PROXY/ALL_PROXY"
fi

# Последняя строка результата: transport_fail либо "<http_code> <time_total>".
# HTTP-ошибка сама по себе не считается медленным транспортом: это отдельный
# сигнал, как и быстрый 403 от DDoS-Guard.
probe() {
    label="$1"
    url="$2"
    echo "=== ${label} (${url}) ==="
    if [ "$PROBE_CHANNEL" = "direct" ]; then
        result=$("$CURL_BIN" --noproxy '*' -sS -o /dev/null --max-time "$TIMEOUT" \
            -w "http_code=%{http_code} time_connect=%{time_connect}s time_total=%{time_total}s size=%{size_download}\n%{http_code} %{time_total}" \
            "$url") || { echo "curl failed (timeout ${TIMEOUT}s exceeded or connection error)"; echo "transport_fail"; return; }
    else
        result=$("$CURL_BIN" -sS -o /dev/null --max-time "$TIMEOUT" \
            -w "http_code=%{http_code} time_connect=%{time_connect}s time_total=%{time_total}s size=%{size_download}\n%{http_code} %{time_total}" \
            "$url") || { echo "curl failed (timeout ${TIMEOUT}s exceeded or connection error)"; echo "transport_fail"; return; }
    fi
    echo "$result" | sed -n '1p'
    echo "$result" | sed -n '2p'
}

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

baseline_total=0
baseline_fast=0
baseline_slow=0
for host in $BASELINE_HOSTS; do
    probe_output=$(probe "baseline" "$host")
    echo "$probe_output" | sed '$d'
    result_line=$(echo "$probe_output" | tail -1)
    baseline_total=$((baseline_total + 1))
    if is_slow "$result_line"; then
        baseline_slow=$((baseline_slow + 1))
    else
        baseline_fast=$((baseline_fast + 1))
    fi
done

hh_probe_output=$(probe "hh.ru" "$HH_HOST")
echo "$hh_probe_output" | sed '$d'
hh_result_line=$(echo "$hh_probe_output" | tail -1)
hh_is_slow=false
if is_slow "$hh_result_line"; then hh_is_slow=true; fi

if [ "$hh_result_line" = "transport_fail" ]; then
    hh_route="недостижим"
else
    hh_route="достижим"
fi
if [ "$hh_is_slow" = true ]; then
    hh_speed="медленный/зависает"
else
    hh_speed="уложился в ${SLOW_THRESHOLD}s"
fi
echo
echo "МАРШРУТ ДО HH.RU: ${hh_route}"
echo "СКОРОСТЬ HH.RU: ${hh_speed}"
echo "BASELINE: быстро ${baseline_fast}/${baseline_total}, медленно/недостижимо ${baseline_slow}/${baseline_total}"

# Большинство, а не любой один baseline, определяет общий маршрут. При
# ничьей (в т.ч. при двух разных результатах) общий маршрут не классифицируем.
baseline_majority_fast=false
baseline_majority_slow=false
if [ $((baseline_fast * 2)) -gt "$baseline_total" ]; then baseline_majority_fast=true; fi
if [ $((baseline_slow * 2)) -gt "$baseline_total" ]; then baseline_majority_slow=true; fi

if [ "$baseline_majority_fast" = true ] && [ "$hh_is_slow" = true ]; then
    echo "ВЕРДИКТ: большинство baseline быстрые, hh.ru медленный/зависает -> узкое место на маршруте hh.ru (DDoS-Guard/троттлинг), не общая сеть."
elif [ "$baseline_majority_slow" = true ] && [ "$hh_is_slow" = true ]; then
    echo "ВЕРДИКТ: большинство baseline тоже медленные/недостижимы -> общий сетевой маршрут/VPN, не специфично для hh.ru."
elif [ "$baseline_majority_fast" = true ]; then
    echo "ВЕРДИКТ: большинство baseline быстрые, hh.ru ${hh_speed} -> специфичной медленной пробы hh.ru не наблюдается."
else
    echo "ВЕРДИКТ: inconclusive -> у baseline нет согласного большинства; маршрут и скорость hh.ru показаны отдельно."
fi
