# Invoice Flow — Solution Guide

This repository contains a local-first invoice processing prototype that combines deterministic
financial controls with bounded LLM assistance. The original case brief is preserved unchanged in
[`README.md`](README.md); this document explains the implemented solution and the fastest review
path.

## Five-minute review

### 1. Install

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/) on macOS, Linux, or Windows.

```bash
uv sync --all-extras --locked
```

`uv.lock` pins the complete environment. The `tui` extra is optional at runtime, but included by
the command above for the full review experience.

### 2. Run the complete sample corpus offline

```bash
uv run python main.py --invoice_path=data/invoices --llm=offline --ledger=demo-ledger.db --inventory=demo-inventory.db
```

The commands in this guide are single-line and shell-neutral: they run unchanged in bash, zsh,
PowerShell, and `cmd`. The demo databases are created in the current directory (`*.db` is
git-ignored); delete `demo-*.db` to start fresh.

The inventory database is seeded automatically when the requested path does not exist. A fresh
offline run currently finishes with:

```text
paid: 6, needs_review: 8, logged_rejection: 4, duplicate: 2; failed: 0
```

Offline mode performs no model or external network calls. It is the deterministic baseline and the
recommended first review path.

### 3. Inspect the review queue

```bash
uv run python main.py review --list --ledger=demo-ledger.db
```

### 4. Open the reviewer TUI

```bash
uv run python main.py tui --ledger=demo-ledger.db --inventory=demo-inventory.db --llm=offline
```

Use `--llm=grok` instead to enable the online tier in the TUI; it needs the environment variables
from [Run with Grok](#run-with-grok). The TUI browses what is already in the ledger, so run step 2
first against the same database files.


The TUI is a presentation layer over service read models. It does not duplicate validation,
approval, payment, or ledger logic.

## What was built

The pipeline processes TXT, JSON, CSV, XML, and text-layer PDFs through four business stages:

![Invoice Flow architecture](docs/diagrams/architecture.png)

Interactive version: [`docs/diagrams/architecture.html`](docs/diagrams/architecture.html) (open locally
in a browser).

### Authority model

The model never receives direct authority to move money.

| Layer | Responsibility | Authority |
|---|---|---|
| Ingestion | Parse supported documents into typed invoices | Deterministic |
| Extraction Fallback | Fill only missing vendor, invoice number, or total from verbatim document text | Always creates a human-review trigger |
| Validation | Check stock, vendors, prices, arithmetic, identity, currency, and data integrity | Deterministic |
| Assessor | Investigate bounded warnings with four read-only exact-key tools | Cannot bypass rejection or review rules |
| Verifier | Check the Assessor's structured claims and cited evidence | Cannot approve by itself |
| Approval policy | Apply the six-row precedence table and evidence guardrails | Owns the decision |
| Ledger and payment | Prevent duplicates, cap cumulative payment, persist a claim, then call the bank | Deterministic |

### Decision precedence

The first matching row wins:

![Decision precedence](docs/diagrams/decision-precedence.png)

Interactive version: [`docs/diagrams/decision-precedence.html`](docs/diagrams/decision-precedence.html).

1. **Duplicate payment** — no model call.
2. **Rejection rule** — deterministic rejection.
3. **Review trigger** — human review only.
4. **Heightened scrutiny** — amounts above the USD threshold require human review.
5. **Bounded warning** — Assessor and Verifier may approve only after mechanical evidence checks.
6. **Clean invoice** — approved unless the optional escalate-only review raises a concern.

This ordering is deliberate: a persuasive model answer cannot outrank a deterministic financial
control.

## Agentic path

Online mode adds four narrowly scoped roles:

- **Extraction Fallback** fills a small allowlist of missing fields and accepts only values grounded
  in the source text.
- **Assessor** investigates warnings using read-only inventory and ledger tools.
- **Verifier** validates the assessment's structured claims.
- **Advisory / escalate-only reviewer** explains deterministic decisions or escalates a clean case;
  it cannot weaken them.

The orchestration is custom rather than framework-based. That keeps role authority, retries,
evidence checks, and failure behavior explicit in a small prototype.

### Run with Grok

The online tier uses an OpenAI-compatible chat-completions endpoint. Configuration is read from the
process environment; local env files are intentionally not loaded automatically.

Set the variables in the same shell that runs the command.

macOS / Linux (bash, zsh):

```bash
export XAI_API_KEY="..."
export GROK_BASE_URL="https://<provider-base>/v1"
export XAI_MODEL="grok-4.7"  # optional; this is the default
```

Windows (PowerShell):

```powershell
$env:XAI_API_KEY = "..."
$env:GROK_BASE_URL = "https://<provider-base>/v1"
$env:XAI_MODEL = "grok-4.7"  # optional; this is the default
```

Then run a single invoice, or point `--invoice_path` at a directory:

```bash
uv run python main.py --invoice_path=data/invoices/invoice_1010.txt --llm=grok --ledger=demo-ledger.db --inventory=demo-inventory.db --json
```

Open the reviewer TUI against the same databases with the online tier:

```bash
uv run python main.py tui --ledger=demo-ledger.db --inventory=demo-inventory.db --llm=grok
```

If online configuration is incomplete, startup fails explicitly instead of silently switching
tiers.

## Safety properties

- Tool calls use exact keys, parameterized SQLite queries, read-only connections, and a shared
  eight-call budget per invoice.
- Structured answers pass through bounded correction attempts and deterministic evidence checks.
- Cross-vendor, cross-line, wrong-currency, malformed, irrelevant, and self-referential evidence is
  rejected.
- Conflicting invoice-level metadata in a row-oriented CSV makes the document unreadable; lines
  from different vendors are never silently merged.
- Duplicate and revision handling uses explicit invoice identity and revision markers. Review notes
  cannot create a payable revision.
- Revision approval pays only the positive remaining delta. The payment cap includes prior paid and
  pending amounts.
- A payment claim is committed before bank I/O. An uncertain bank result remains
  `payment_pending`; there is no unsafe automatic retry.
- Model attempts, corrections, tool calls, guardrail failures, decisions, reviewer resolutions, and
  payment issues are persisted for audit.
- Optimistic version checks discard and recompute a decision if the identity's ledger history
  changes while a model call is running.

## Representative scenarios

| Invoice | Expected behavior |
|---|---|
| `invoice_1001.txt` | Clean invoice; approved and paid |
| `invoice_1002.txt` | Stock shortage; human review |
| `invoice_1003.txt` | Blocked vendor and zero-stock item; rejected |
| `invoice_1004_revised.json` | Explicit revision; human may approve only the USD 4,050 delta |
| `invoice_1010.txt` | Price warning; offline review or online Assessor/Verifier path |
| `invoice_1011.txt` | Duplicate of a previously paid PDF; no second payment |
| `invoice_1014.xml` | EUR warning with pinned reference-rate evidence |

## Verification

```bash
uv lock --check
uv run ruff check .
uv run pytest -q
```

Current local result:

```text
317 passed, 4 skipped
All checks passed!
```

The four skipped tests require an explicitly configured live Grok endpoint. The deterministic
suite, offline network guard, ingestion formats, approval precedence, evidence guardrails, payment
safety, audit records, CLI, and TUI are exercised locally.

## Project map

| Path | Purpose |
|---|---|
| `invoice_pipeline/ingestion/` | Format-specific parsing and normalization |
| `invoice_pipeline/validation.py` | Deterministic business checks |
| `invoice_pipeline/approval.py` | Decision precedence, evidence guardrails, and critic loop |
| `invoice_pipeline/critic.py` | Case Files and online role adapters |
| `invoice_pipeline/tools.py` | Read-only Assessor tools |
| `invoice_pipeline/extraction.py` | Grounded missing-field fallback |
| `invoice_pipeline/ledger.py` | Durable audit state, identity history, claims, and payment cap |
| `invoice_pipeline/service.py` | Use-case orchestration and presenter read models |
| `invoice_pipeline/cli.py` | Batch and Review Queue CLI |
| `invoice_pipeline/tui.py` | Interactive reviewer experience |
| `tests/` | Deterministic, integration, threat, audit, and presentation coverage |

