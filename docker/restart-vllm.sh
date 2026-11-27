#!/usr/bin/env bash
# Перезапуск vLLM в правильном порядке: сначала генератор, потом embedding.
#
# Почему порядок обязателен. Обе модели живут на одной карте (RTX 4090,
# 24 ГБ). Генератору нужно 0.87 от 23.51 ГиБ = 20.45 ГиБ, из которых
# 19.85 — веса и активации, а 0.6 — KV-кэш под контекст 12288. Когда
# embedding уже работает, он держит 2.5 ГиБ плюс ~1 ГиБ CUDA-оверхеда,
# и генератору остаётся 20.19 ГиБ — на 0.26 меньше нужного. vLLM падает:
#
#   ValueError: Free memory on device cuda:0 (20.19/23.51 GiB) on startup
#   is less than desired GPU memory utilization (0.87, 20.45 GiB)
#
# При `restart: always` это превращается в краш-луп: Docker поднимает
# контейнер, тот падает, Docker поднимает снова. Проверено.
#
# Ужать доли не получается: при 0.85 у генератора остаётся 0.1 ГиБ
# KV-кэша вместо нужных 0.44, а embedding меньше 0.11 не хватает на
# контекст 4096 (самый длинный parent — 1464 токена).
#
# Использование:
#   ./restart-vllm.sh              # перезапустить оба
#   ./restart-vllm.sh --wait 600   # ждать healthy до 600 с (по умолчанию 300)

set -euo pipefail

COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WAIT_SECONDS=300

while [[ $# -gt 0 ]]; do
    case "$1" in
        --wait) WAIT_SECONDS="$2"; shift 2 ;;
        *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
done

compose() {
    docker compose -f "$COMPOSE_DIR/docker-compose.yml" "$@"
}

wait_healthy() {
    local name="$1" deadline=$(( SECONDS + WAIT_SECONDS ))
    while (( SECONDS < deadline )); do
        local status
        status="$(docker inspect --format='{{.State.Health.Status}}' "$name" 2>/dev/null || echo missing)"
        if [[ "$status" == "healthy" ]]; then
            echo "  $name: healthy"
            return 0
        fi
        if [[ "$status" == "exited" || "$status" == "missing" ]]; then
            echo "  $name: $status — модель не поднялась" >&2
            docker logs --tail 20 "$name" >&2 || true
            return 1
        fi
        sleep 5
    done
    echo "  $name: не стал healthy за ${WAIT_SECONDS}s" >&2
    return 1
}

echo "1/3 останавливаю embedding — он держит память, нужную генератору"
compose stop vllm-embedding >/dev/null 2>&1 || true

echo "2/3 поднимаю генератор"
compose up -d --force-recreate vllm-generate >/dev/null
wait_healthy vllm-generate

echo "3/3 поднимаю embedding"
compose up -d --force-recreate vllm-embedding >/dev/null
wait_healthy vllm-embedding

echo "готово: обе модели healthy"
