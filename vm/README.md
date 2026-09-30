# OASIS VM run

The simulation runs on a VM, not in this repo. Only two files cross the boundary.

1. Here: `python scripts/oasis_sim.py seed --max-usd 5 --max-agents 50 --max-steps 10`
   writes `state/oasis_seeds/<run_id>.json`. Copy it to the VM with `oasis_runner.py`.
2. On the VM, once: `bash setup_vm.sh` (Python 3.11 venv, `camel-oasis` from
   https://github.com/camel-ai/oasis). Set `ANTHROPIC_API_KEY` in the VM's environment.
3. On the VM: `python oasis_runner.py seed.json --out result.json --confirm-spend`.
   Without `--confirm-spend` it only prints the budget.
4. Copy `result.json` to `state/oasis_inbox/`, then
   `python scripts/oasis_sim.py ingest state/oasis_inbox/result.json`.

## Not done yet

`drive()` in `oasis_runner.py` raises `NotImplementedError`. It needs to build the OASIS
environment from the seed, step it with the Anthropic model, call `meter.add(...)` after every
model call, and stop when `meter.over`. Try it first on about a $1 budget with a handful of agents.

camel-oasis pins Python 3.10 to 3.11, which is why it is not installed into the trading repo's
Python 3.13 environment.
