#!/bin/sh
# Диагностика "сеть тормозит" vs "hh.ru троттлит" перед тем, как списывать
# зависший goto_hh()/ThrottledChannelDetected на анти-фрод hh.ru. Минимум
# трафика (HEAD-запросы), не требует storage_state/аккаунта — безопасно
# гонять вручную в любой момент, в т.ч. параллельно с боевой командой.
#
# Логика: если baseline (не hh.ru) хост отвечает быстро, а hh.ru — нет,
# узкое место специфично для hh.ru (DDoS-Guard/анти-фрод троттлинг), а не
# общая деградация сети/VPN на этой машине.
set -eu

# Два baseline-хоста на разных континентах/маршрутах — если тормозит
# только hh.ru, а оба baseline быстрые, вывод увереннее, чем с одним.
BASELINE_HOSTS="https://www.google.com/ https://www.baidu.com/"
HH_HOST="https://hh.ru/"
TIMEOUT="${CHECK_HH_TIMEOUT:-10}"

probe() {
    label="$1"
    url="$2"
    echo "=== ${label} (${url}) ==="
    curl -sS -o /dev/null --max-time "$TIMEOUT" \
        -w "http_code=%{http_code} time_connect=%{time_connect}s time_total=%{time_total}s size=%{size_download}\n" \
        "$url" 2>&1 || echo "curl failed (timeout ${TIMEOUT}s exceeded or connection error)"
}

for host in $BASELINE_HOSTS; do
    probe "baseline" "$host"
done
probe "hh.ru" "$HH_HOST"

echo
echo "Baseline-хосты быстрые + hh.ru медленный/зависает -> узкое место у hh.ru (DDoS-Guard/троттлинг), не общая сеть."
echo "Baseline-хосты тоже медленные -> проблема в сети/маршруте (VPN, провайдер), не специфична для hh.ru."
