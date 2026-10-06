# Design

## State machine
`RECEIVED → CONTRACTED → PLANNED → EXECUTING ⇄ (REPAIRING | AWAITING_APPROVAL) → VERIFYING → COMPLETED | FAILED | ESCALATED`

Run state is never stored directly: `state/store.py::project()` folds the event log into `RunState`.
That gives crash-resume (a step left `RUNNING` is an *interrupted* step; re-run it with its idempotency
key), replayable traces, and an audit log that is the source of truth.

## Event log (payload conventions)
| Event | Payload |
|---|---|
| RUN_CREATED | `{request, workflow?}` |
| CONTRACT_COMPILED | `{contract: TaskContract}` |
| PLAN_CREATED / PLAN_REVISED | `{plan: Plan}` (a revision must carry over DONE step statuses) |
| STEP_STARTED | `{step_id}` |
| TOOL_CALLED | `{step_id, tool, args, idempotency_key, dry_run}` |
| OBSERVATION | `{observation: Observation}` |
| APPROVAL_REQUESTED / RESOLVED | `{approval: ApprovalRequest}` / `{approval_id, decision, edited_args?, reviewer, comment}` |
| REPAIR_ATTEMPTED | `{step_id, strategy, detail}` |
| VERIFICATION_RESULT | `{certificate: CompletionCertificate}` |
| RUN_COMPLETED / FAILED / ESCALATED | `{summary}` / `{reason}` / `{reason}` |
| LESSON_PROPOSED, NOTE | free-form |

## Decisions already made in the foundation
- **Only the verifier completes a run.** The executor cannot emit RUN_COMPLETED; the certificate is typed `issued_by="verifier"`.
- **Idempotency keys are `<run_id>:<step_id>`**, enforced in `ToolRegistry.call` for WRITE/IRREVERSIBLE tools and, where the target system supports it, also sent as `client_ref` (the portal dedups on it).
- **Taint:** tools that return external text (`read_text`, browser `snapshot`) are `untrusted`; `ToolResult.tainted` must propagate into the prompt as *data*, and tainted content must never be able to authorise a privileged action (see the injection fixture `receipts/priya_2026-09-28_meal.txt`).
- **Errors are classified** (`ErrorKind`) so the repair ladder can choose: TRANSIENT → retry; VALIDATION → fix args; NOT_FOUND → re-plan; CONFLICT/PERMISSION → escalate or ask.
- **Different models for planner vs verifier** (config/models.yaml) to decorrelate errors.

## Roadmap (mapped to the schedule)
**Tier 0, by end of Oct 7**
- [ ] `core/contract.py`: request + company memory → `TaskContract` (use `LLMRouter.complete_json`)
- [ ] `core/planner.py`: SOP skeleton → `Plan` DAG; local repair → partial replan → full replan
- [ ] `core/executor.py`: the loop; every transition is an event; resume from `interrupted_steps()`
- [ ] `core/guard.py`: evaluate `Workflow.approval_rules`, risk tiers, taint rules, allow-lists
- [ ] approvals inbox (CLI first): emit APPROVAL_REQUESTED, block, resume on APPROVAL_RESOLVED
- [ ] `core/verifier.py`: run `acceptance_criteria` against the portal; issue `CompletionCertificate`
- [ ] second workflow (e.g. new-hire provisioning) to prove connectors/workflows are reusable
- [ ] `evals/`: scenarios + metrics (success, false-completion, recovery, interventions, steps)

**Tier 1, by end of Oct 8:** company memory (hybrid retrieval + lessons), SQLite job queue with leases
and retries, trace viewer, chaos suite, ablations (no verifier / no guard / no memory).

**Tier 2 (only if green):** OpenAPI → connector auto-generation, MCP adapter, scheduler, vision fallback.

## Eval scenarios to write (start here)
happy path · missing receipt · over-limit hotel (cap at 6,000) · duplicate claim · receipt older than 60 days ·
amount > 25,000 (approval) · portal 503×2 then recovers · renamed UI ids (browser) · prompt-injection receipt ·
human rejects submission · crash mid-run then resume (no double claim).
