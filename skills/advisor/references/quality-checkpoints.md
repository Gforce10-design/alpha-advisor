# Quality supervision across a project

Apply this mode only when the user or an established project workflow explicitly selects persistent advisor quality supervision. Project size, multiple files, or multiple rounds alone do not enable it. The conductor remains responsible for execution. The configured advisor is an independent checkpoint reviewer; it is not a continuously running observer. Explicit model settings come from the advisor role.

Before implementation, define observable quality criteria: correctness plus relevant usability, maintainability, performance, robustness and completeness. Include concrete evidence and thresholds, not “good quality.” Define ordered checkpoints: plan acceptance, first representative implementation, major round completion, release readiness. Combine redundant review/test checkpoints when the evidence is identical. Small routine fixes can use ordinary advisory mode; document why in the task context.

Write `quality-plan.json` in the existing task folder and keep its path in task context. Example (extend with round and release checkpoints):

```json
{"schema_version":1,"task_id":"project-feature","stages":[{"id":"plan","max_calls":2,"criteria":{"value":"Every user requirement maps to an acceptance check","design":"Compare two approaches and justify maintenance tradeoffs"},"evidence_files":["/absolute/task/evidence/plan.md"]},{"id":"first-implementation","max_calls":2,"criteria":{"behavior":"Representative user flow passes its recorded test","maintainability":"No duplicated business rules; failure handling is explicit"},"evidence_files":["/absolute/task/evidence/first/source.txt","/absolute/task/evidence/first/tests.txt"]}]}
```

Use stage-specific evidence snapshots: actual code/diff, rendered-output observations when relevant, tests and known limitations, not only a producer summary. Do not point earlier stages at files that later stages routinely edit. Preserve source paths, revision/hash and capture commands in snapshots. Before accepting the CURRENT checkpoint, verify its evidence still matches the current deliverable. This freshness comparison is the conductor's responsibility; the helper cannot discover omitted files or validate screenshots semantically.

Earlier accepted snapshots represent historical checkpoints. Later changes must be evaluated by the next checkpoint. If requirements or design assumptions change, explicitly update affected earlier criteria/evidence and re-review; do not leave them silently stale. Adding later stages does not alter earlier contracts. Never rename a plan/stage to evade its budget.

At each checkpoint, the conductor executes (paths to scripts are relative to the advisor skill):

```sh
python3 scripts/quality.py review /absolute/task/quality-plan.json plan
# Read the configured advisor's result; independently verify its findings and source freshness.
# Write a note explaining accepted findings, evidence checks and remaining limits.
python3 scripts/quality.py accept /absolute/task/quality-plan.json plan --note /absolute/task/accept-plan.md
python3 scripts/quality.py gate /absolute/task/quality-plan.json plan
```

Only `gate` returning `READY` permits dependent workflow progress under this quality procedure. The workflow owner must run the gate before advancing; this is not an OS-level hook and does not intercept arbitrary shell/deploy commands. READY is not deployment authorization. Apply existing deployment permissions, tests and rollback requirements separately.

`review` returns REVIEWED only for a valid all-criteria PASS; conductor acceptance is still required. REVISE, NEEDS_EVIDENCE, missing/malformed results, missing acceptance, modified evidence/configuration or missing runtime evidence block advancement. Fix findings and review the updated packet; unchanged valid results reuse without another call. Invalid JSON consumes the attempt and permits a bounded retry. Do not rewrite the model output into PASS. If the conductor disagrees, record the objection and provide counter-evidence for re-review; unresolved disagreements or exhausted limits require an owner decision, never fabricated PASS.

Each planned stage has an initial 1–6 attempt limit (including failures), stored in the plan and frozen in the ledger at initialization. Heuristics, not proven optima: 2 for a narrow well-evidenced stage; 3 for a normal multi-file stage; 4 for a broad cross-system, long-backlog, or many-source stage; 5–6 only for exceptional breadth with a written reason before the first call. One call is for an intentionally one-shot review. Summing the plan values gives the project's original maximum calls; review that total before starting. After a valid owner-note extension, the audited effective limit may rise to 12; each grant increases the current effective limit by at most 4. The direct advisor executor still allows 1–20; that hard ceiling is not the quality-stage policy. There is no automatic budget growth, unlimited retry, automatic ledger rotation, or infinite correction loop. Do not edit plan `max_calls` or the ledger limit to masquerade as an extension.

After useful reviews exhaust an undersized initial limit, raise only review capacity:

```sh
python3 scripts/quality.py extend /absolute/task/quality-plan.json plan --max-calls 4 --note /absolute/task/owner-budget-note.md
```

Preconditions: initialized ledger, no pending attempt, nonempty intact note, same task/stage/decision binding, integer N strictly greater than the current effective limit, N − current ≤ 4, N ≤ 12, and every completed attempt already structurally classified against the stage criteria with an intact result identity and intact quality-runner runtime evidence. A changed result file that still parses as valid review JSON is not classified. Each successful review records a deterministic quality-classification marker on the attempt (result identity, stage, criteria contract, verdict). A marker whose stage is not the current stage id is rejected. Extension validates that marker instead of reparsing historical results against later criteria; unmarked legacy rows are validated against the current criteria and backfilled before the grant, leaving a resumable schema-2 ledger if the grant does not follow. The marker is bound into the attempt-prefix digest. The executor binds the ledger task_id to the supplied decision before pending inspection or recovery writes. An exact retry of the last successful extension (same N, task, stage, plan path, and intact note) is idempotent and does not add an entry or change hashes. A same-limit request with a different note or binding, or any older lower target, is an error. The original plan value stays unchanged. The existing advisor ledger is the single atomic state: frozen original limit, executor-visible effective `max_calls`, and ordered hash-chained extension history (sequence, binding, previous/new limits, used-attempt count, timestamp, note path/hash, previous-entry hash, entry hash). Schema-2 ledgers migrate on the first valid extension without discarding attempts. Local hashes detect inconsistent edits; they are not external authenticity proof. A separate extension file is forbidden. Extension alone leaves the stage HOLD; conductor acceptance and `gate` READY remain required. `review`, `accept`, `gate`, `status`, and every advisor reservation/check/report path validate the chain before use, including before pending/recovery inspection. Runner completion publishes the attempt and its runtime evidence in one ledger write.

`review ... --recover` uses existing terminal evidence after an interrupted executor; it does not authorize blind repeat calls.

Use existing advisor-ledger reports in each checkpoint directory for known usage. Record quality findings adopted, final criterion outcomes, rework and elapsed time in the task's existing log. Cost savings and quality gains remain observations to measure, not guaranteed effects of using the configured advisor. GPT web planning/review stays separate; share its evidence where useful without duplicating the full review automatically.

## Resume without another model call

```sh
python3 scripts/quality.py status /absolute/task/quality-plan.json
```

Run this on resumption and after a major round. It reports every checkpoint, the first incomplete stage, acceptance needs, pending execution, original/base limit, effective limit, attempts used, remaining attempts, extension count, and original plus effective project totals. Existing `max_calls` on a stage is the executor-visible effective limit. It verifies prior artifacts and may refresh deterministic brief/decision files; it never launches a provider or resets a ledger. `STATUS` / exit 0 means the report succeeded, not that the project passed. Only `all_ready` describes all listed stages, and the existing `gate` still controls dependent advancement. Missing future checkpoints cannot be inferred from this report.

READY → continue to the next planned checkpoint; AWAITING_ACCEPTANCE → evaluate the saved review and current evidence, then accept; PENDING → inspect the existing execution and recover only with terminal evidence; HOLD → inspect evidence, settings, budget or review findings. Exhausted attempts do not authorize a new ledger. After a 2→4 extension, status shows base 2, effective 4, used 2, remaining 2; another review remains mandatory. A changed plan stage limit is rejected by review, acceptance, gate, extend and status until the original limit is restored. Direct ledger-limit edits, broken or reordered extension chains, changed notes, and binding mismatches fail closed.
