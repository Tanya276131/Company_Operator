# Operon: a durable AI-employee runtime

Turns a short company request ("Reimburse Rahul's October travel") into **completed, verified,
audited work** across files, documents, internal web apps and APIs, with a human in the loop for
risky actions.

```
Goal → Understand → Plan → Execute → Observe → Adapt → Verify → Complete
```

> Status: **foundation scaffold**. Built and tested: schemas, event-sourced state store with a
> hash-chained audit log, connector SDK + tool registry (idempotency, taint, risk classes),
> sandboxed files connector, portal connector, mock company (portal + docs), workflow loader,
> role-based LLM router (retry, failover, rate limits, record/replay, JSON repair). **Still to
> build** (see DESIGN.md): contract compiler, planner, executor loop, guard + approvals, verifier,
> memory, queue/worker, evals.

## Quick start

Python 3.10+ (tested on 3.12). Commands work in bash and Windows PowerShell; `make` is optional.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows   (macOS/Linux: source .venv/bin/activate)
python -m pip install -r requirements.txt
python -m pip install -e .
python -m pytest                # 24 tests, no network, no API key needed
python scripts/foundation_demo.py
```

Run the mock company's portal by hand (UI at http://127.0.0.1:8000):

```bash
python -m uvicorn mock_company.portal.app:app --port 8000
```

Chaos controls for testing recovery: `POST /admin/chaos {"fail_next": 2, "latency_ms": 500, "ui_variant": "b"}`,
`POST /admin/reset`.

### Using a real LLM

```bash
copy .env.example .env          # macOS/Linux: cp .env.example .env   -> add GEMINI_API_KEY / GROQ_API_KEY
python scripts/llm_smoke.py     # verifies keys, model names and tool calling
```

Model names live in `.env` / `config/models.yaml`, never in code. **Verify the defaults** against each
provider's current model list: names and free-tier availability change.

`OPERON_LLM_MODE`: `auto` (use recording if present, else call and record), `record`, `replay`
(never calls a provider, which is what reviewers without keys use), `off`.
Keep run ids/timestamps out of prompts or the cache will never hit.

## Layout

```
operon/
  schemas.py            all data contracts (TaskContract, Plan, Observation, Event, Certificate...)
  state/store.py        event-sourced SQLite store, hash-chained audit, idempotency cache, resume projection
  connectors/           Connector SDK (base.py), ToolRegistry, files, portal (HTTP), browser (Playwright)
  llm/                  role router, record/replay, LiteLLM backend, fake backend for tests
  workflows.py          workflows-as-data loader
config/models.yaml      role -> provider/model candidates, rate limits, retry policy
workflows/expense.yaml  sources, permitted tools, approval rules, acceptance criteria
mock_company/           Acme portal (REST + HTML + chaos) and docs (policy, SOP, receipts, employees)
scripts/                foundation_demo, llm_smoke, browser_smoke
tests/                  24 tests
```

## Known gaps (honest list)

- `connectors/browser.py` and `llm/litellm_backend.py` are written but **not exercised** in the build
  environment (no browser binaries, no API keys there). Run `scripts/browser_smoke.py` and
  `scripts/llm_smoke.py` first and fix anything provider-specific.
- Receipts are `.txt`; add a PDF/OCR connector when the core loop works.
- The acceptance-criteria `check` grammar in workflow YAML is provisional: the verifier defines it.
