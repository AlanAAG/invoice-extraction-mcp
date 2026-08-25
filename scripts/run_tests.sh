#!/usr/bin/env bash
# Run every suite. No server, no network, no infrastructure required.
#   ./scripts/run_tests.sh
set -uo pipefail
cd "$(dirname "$0")/.."

SUITES=(test_samples test_adversarial test_detection
        test_content test_robustness test_multipage)
fail=0

for t in "${SUITES[@]}"; do
  printf "%-20s " "$t"
  if out=$(python3 "tests/$t.py" 2>&1); then
    echo "$(echo "$out" | tail -1)"
  else
    echo "FAILED"
    echo "$out" | tail -20
    fail=1
  fi
done

echo
if [ "$fail" -eq 0 ]; then
  echo "All suites green."
else
  echo "Some suites failed."
fi
exit "$fail"
