#!/usr/bin/env bash
# Verify a clone has every file it needs. Run from the repo root:
#   bash scripts/check_repo.sh
cd "$(dirname "$0")/.."
missing=0
required=(
  requirements.txt requirements-dev.txt Dockerfile docker-compose.yml
  .env.example .gitignore .dockerignore .devcontainer/devcontainer.json
  README.md TESTING.md HANDOVER.md BUILDING_AN_MCP_SERVER.md neon_schema.sql
  profiles/sap_b1_supplier_statement.yaml
  scripts/run_tests.sh scripts/smoke_test.py
  src/__init__.py src/layout.py src/profile.py src/content.py
  src/pipeline.py src/server.py
  tests/test_samples.py tests/test_adversarial.py tests/test_detection.py
  tests/test_content.py tests/test_robustness.py tests/test_multipage.py
  tests/e2e_http.py tests/fixtures/README.md
)
for f in "${required[@]}"; do
  if [ ! -f "$f" ]; then echo "MISSING  $f"; missing=1; fi
done
# src/__init__.py is legitimately empty; everything else must have content.
for f in "${required[@]}"; do
  if [ -f "$f" ] && [ ! -s "$f" ] && [ "$f" != "src/__init__.py" ]; then
    echo "EMPTY    $f"; missing=1
  fi
done
if [ "$missing" -eq 0 ]; then
  echo "Repo complete — ${#required[@]} files present."
else
  echo "Repo incomplete. src/__init__.py may be empty (that is correct);"
  echo "anything else listed above needs to be added."
fi
exit "$missing"
