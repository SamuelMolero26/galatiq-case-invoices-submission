# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Galatiq case submission: a CLI invoice-processing pipeline (ingestion → validation → approval → payment) for mock invoices in `data/invoices/` (read-only corpus). Python ≥3.14, package `invoice_pipeline/`, entry `main.py`. Grok (xAI) is the LLM; everything must also run fully offline.

## Commands

System `python3` has no dev tools; use `uv`.

```bash
uv run --with pytest python -m pytest -q -m 'not slow'          # fast suite (no network/key)
uv run --with pytest python -m pytest tests/test_x.py::test_name # single test
uvx ruff check && uvx ruff format --check                        # lint gate (line 100, E,F,I,UP,B)
uv run python main.py --invoice_path=data/invoices/              # process file or dir
uv run python main.py review --list                              # Needs Review queue
```

Global flags: `--llm {grok,offline}`, `--ledger` (default `ledger.db`), `--inventory` (default `inventory.db`, seeded if missing), `--json`. Grok is auto-selected only when `XAI_API_KEY` and `GROK_BASE_URL` are both set (see `.env.example`); otherwise offline. An explicitly requested but incomplete tier fails at bootstrap before any file is read.

`slow` marks live-provider tests; they must skip without configuration. Every PR ends with ruff check, ruff format --check, and its focused fast tests green.

## Architecture

Flow (`service.py`):
1. `ingestion.ingest()` never raises. Dispatches TXT / text-layer PDF (`text.py`, `pdf.py`) and JSON/CSV/XML (`structured.py`), then `normalize.py`. Text parsing reports `missing_required` and keeps `raw_text`; a PDF without a text layer or no recovered items becomes an Unreadable Document (no OCR, by design).
2. Unreadable → `approval.decide_unreadable()`. Otherwise `record_arrival()`: `validation.validate()` produces Findings, then a read–decide–write loop: `ledger.read_identity/classify/vendor_history` → `critic.build_case_file()` → `approval.decide(case_file, agents)` → `ledger.record_if_unchanged()` (optimistic version check, up to `MAX_DECIDE_ATTEMPTS`).
3. An Approved insert claims payment in the same transaction; `pay_claimed()` then calls the bank. Claim-first: the claim survives any payment failure, so nothing is paid twice.

Decision precedence in `approval.decide()`, first matching row wins: Duplicate → Rejection Rule → Review Trigger → Warnings over $10K (heightened scrutiny) → Warnings (critic bounds; otherwise Unreviewed Warnings) → no findings (escalate-only review). Only `decide()` constructs a `Decision`.

LLM layer:
- `llm.py`: `TierConfig`/`select_tier()`, a urllib chat-completions adapter, and `ask()`, the correction wrapper (`FORMAT_TRIES = 3`; a validator returns `CorrectableError`/`FinalError`). `role_call()` turns the result into a `RoleCall` audit record.
- `critic.py`: `online_agents(tier)` / `offline_agents()` build the `Agents` callables (`model.py`).
- `tools.py`: read-only, schema-validated lookup tools (`MAX_TOOL_CALLS = 8`).

Every role is fail-closed: offline, a timeout, or exhausted tries can never produce an approval. The model audit is stored in the Decision JSON inside `arrivals.record` (`ledger.py`).

## Invariants

- Money is `Decimal`; floats are rejected (`model._no_float`).
- `model.SEVERITY` is the only Finding-code → severity source. Thresholds live in `approval.py` (`PRICE_TOLERANCE`, `CRITIC_PRICE_CEILING`, `HEIGHTENED_SCRUTINY_USD`).
- LLM output is advisory or must pass deterministic bounds/guardrails. It never moves money directly, and extracted fields force human review (`LLM_EXTRACTED`).
- `TierConfig.api_key` is excluded from serialization; credentials must never reach events, logs, or the ledger.

## Workflow

- Strict TDD: observe RED before GREEN.
- Delivery is stacked PRs per slice (`feat/...` branches, each based on the previous). The user merges to `main`.
- `tests/test_golden.py` is legacy: it imports a removed `tests/golden` package. Ignore it; don't restore deleted tests.
