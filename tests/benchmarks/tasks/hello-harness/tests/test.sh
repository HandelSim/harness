#!/bin/bash
# Verifier: invoked by Harbor after the agent finishes. Runs pytest against
# the agent's environment and writes /logs/verifier/reward.txt (1=pass, 0=fail).
set -euo pipefail

apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 python3-pip python3-pytest >/dev/null

mkdir -p /logs/verifier
if pytest -rA /tests/test_outputs.py; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
