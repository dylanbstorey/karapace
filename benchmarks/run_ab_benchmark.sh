#!/bin/bash
set -e

# A/B benchmark: compares current branch against main
# Requires: docker compose, git
# Usage: ./benchmarks/run_ab_benchmark.sh [benchmark_script]
#   benchmark_script defaults to benchmark_schema_registration.py
#   Other options: benchmark_produce_avro.py, benchmark_produce.py

export PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
export KARAPACE_VERSION="${KARAPACE_VERSION:-5.0.3}"
export RUNNER_UID="${RUNNER_UID:-$(id -u)}"
export RUNNER_GID="${RUNNER_GID:-$(id -g)}"
export COVERAGE_FILE="${COVERAGE_FILE:-.coverage.${PYTHON_VERSION}}"

COMPOSE="docker compose -f container/compose.yml --profile benchmark"
BENCHMARK_SCRIPT="${1:-benchmark_schema_registration.py}"
BRANCH=$(git branch --show-current)

# Find all modified source files to stash for baseline
MODIFIED_SRC=$(git diff --name-only -- 'src/')
if [ -z "$MODIFIED_SRC" ]; then
    echo "ERROR: No modified files in src/ — nothing to A/B test."
    exit 1
fi

echo "=================================================="
echo "  A/B Benchmark: $BENCHMARK_SCRIPT"
echo "  Current branch: $BRANCH"
echo "  Modified source files:"
# shellcheck disable=SC2086
printf "    %s\n" $MODIFIED_SRC
echo "=================================================="

create_topics() {
    # Only needed for produce benchmarks
    if [[ $BENCHMARK_SCRIPT == *produce* ]]; then
        echo "Creating benchmark topics..."
        docker compose -f container/compose.yml exec kafka bash -c '
            for i in $(seq 0 99); do
                /opt/kafka/bin/kafka-topics.sh --create --if-not-exists \
                    --topic test-topic-avro-$i --partitions 3 --replication-factor 1 \
                    --bootstrap-server kafka:29092 2>/dev/null
                /opt/kafka/bin/kafka-topics.sh --create --if-not-exists \
                    --topic test-topic-json-$i --partitions 3 --replication-factor 1 \
                    --bootstrap-server kafka:29092 2>/dev/null
            done && echo "Created 200 benchmark topics"'
    fi
}

run_benchmark() {
    local label=$1
    local outfile=$2

    echo ""
    echo "--- $label ---"
    $COMPOSE down -v --remove-orphans 2>&1 | tail -1
    $COMPOSE up -d --build --wait --detach 2>&1 | tail -3
    echo "Waiting for services to stabilize..."
    sleep 12
    create_topics
    echo "Running benchmark..."
    $COMPOSE run --rm -e BENCHMARK_SCRIPT="$BENCHMARK_SCRIPT" benchmark-runner 2>&1 | tee "$outfile"
    $COMPOSE down -v --remove-orphans 2>&1 | tail -1
}

# Phase 1: Baseline — temporarily revert source files to main
echo ""
echo "==> Building baseline from main..."
# shellcheck disable=SC2086
git stash push -m "ab-bench" -- $MODIFIED_SRC
run_benchmark "BASELINE (main)" /tmp/benchmark_baseline.txt

# Phase 2: With fixes — restore our changes
echo ""
echo "==> Building with fixes from $BRANCH..."
git stash pop
run_benchmark "WITH FIXES ($BRANCH)" /tmp/benchmark_fixes.txt

echo ""
echo "=================================================="
echo "  RESULTS COMPARISON: $BENCHMARK_SCRIPT"
echo "=================================================="
echo ""
echo "--- BASELINE (main) ---"
grep -E "(Schema evolution|Schema dedup|Re-registration|Total:|Avg:|First|Last|Avg latency|P95)" /tmp/benchmark_baseline.txt || true
echo ""
echo "--- WITH FIXES ($BRANCH) ---"
grep -E "(Schema evolution|Schema dedup|Re-registration|Total:|Avg:|First|Last|Avg latency|P95)" /tmp/benchmark_fixes.txt || true
echo ""
echo "Full output: /tmp/benchmark_baseline.txt /tmp/benchmark_fixes.txt"
