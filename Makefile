.PHONY: help sync lint format type test backtest paper status risk kill clean

help:
	@echo "Targets:"
	@echo "  sync       Install/refresh dependencies via uv"
	@echo "  lint       Run ruff check"
	@echo "  format     Run ruff format"
	@echo "  type       Run mypy --strict"
	@echo "  test       Run pytest with coverage"
	@echo "  status     Live-only: read Questrade accounts"
	@echo "  backtest   Example: make backtest STRAT=ema_crossover SYM=AAPL"
	@echo "  paper      Example: make paper STRAT=ema_crossover SYMS=AAPL,XIC.TO"
	@echo "  risk       Print current risk report"
	@echo "  kill       Trip kill switch: make kill REASON=manual"
	@echo "  clean      Remove caches, reports, and journals"

sync:
	uv sync

lint:
	uv run ruff check src tests

format:
	uv run ruff format src tests

type:
	uv run mypy src

test:
	uv run pytest

status:
	uv run trading status

backtest:
	uv run trading backtest --strategy $(STRAT) --symbol $(SYM) --years $(or $(YEARS),3)

paper:
	uv run trading paper --strategy $(STRAT) --symbols $(SYMS) --iterations $(or $(N),30)

risk:
	uv run trading risk-report

kill:
	uv run trading kill --reason "$(or $(REASON),manual)"

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	rm -rf data/cache reports logs
	rm -f state/orders.jsonl state/rejected.jsonl state/fills.jsonl state/paper_fills.jsonl state/paper_orders.jsonl state/paper_equity.csv state/equity.csv
