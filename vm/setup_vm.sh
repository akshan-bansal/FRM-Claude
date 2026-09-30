#!/usr/bin/env bash
# Run ON THE VM, once. Builds an isolated Python 3.11 env with camel-oasis from its GitHub repo.
#
# Why a separate env: camel-oasis requires Python >=3.10,<3.12 and pins pandas 2.2.2 (plus about
# fifteen other packages), so it cannot share the trading repo's Python 3.13 environment.
# Nothing in the trading repo imports it; the only contract is the seed/result JSON files.
#
#   bash setup_vm.sh            # create env and install
#   export ANTHROPIC_API_KEY=... # the key lives here on the VM, never in the trading repo
#   python oasis_runner.py seed.json --out result.json            # dry run: prints the budget
set -euo pipefail

PY="${PYTHON:-python3.11}"
ENV_DIR="${OASIS_ENV:-$HOME/oasis-env}"

command -v "$PY" >/dev/null || { echo "need $PY (camel-oasis requires Python 3.10 or 3.11)"; exit 2; }
"$PY" -m venv "$ENV_DIR"
# shellcheck disable=SC1091
source "$ENV_DIR/bin/activate"
python -m pip install --upgrade pip
python -m pip install "git+https://github.com/camel-ai/oasis.git@${OASIS_REF:-main}" anthropic
python -c "import oasis, sys; print('oasis', getattr(oasis, '__version__', '?'), 'on python', sys.version.split()[0])"
echo "ok: activate with: source $ENV_DIR/bin/activate"
