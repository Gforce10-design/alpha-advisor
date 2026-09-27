---
name: advisor
description: Execute bounded independent advice with automatic evidence capture, completion checks, and reuse. Use when the user explicitly requests an advisor, two same-cause repairs fail, evidence remains unresolved, or an architecture or hard-to-reverse decision needs independent challenge. Use persistent quality checkpoints only when the user or an established project workflow explicitly selects them. Do not use merely because work is multi-file, multi-round, or has a normal implementation plan. Works alongside a separate planning and review workflow.
---

# Independent advisor

Use `scripts/advisor.py` relative to this skill. The conductor prepares a bounded question, runs one command, and evaluates the saved advice. Do not ask the user to assemble JSON or manually dispatch the advisor.

## Project quality supervision

Only when the user or an established project workflow explicitly selects persistent advisor quality supervision, read [quality-checkpoints.md](references/quality-checkpoints.md) and establish its quality plan before implementation. Do not enable this mode merely because a task is multi-file, multi-round, or complex. Run its independent reviews at agreed checkpoints even when the conductor can complete the work without assistance. Require `quality.py gate` READY before dependent advancement; criteria cover result quality, not just absence of errors. Resume the same plan after compaction using `python3 scripts/quality.py status /absolute/task/quality-plan.json`. Read its next_stage, per-stage state and consumed attempts; do not create a fresh plan or ledger. STATUS reports state but never authorizes advancement; use gate for that. This mode reuses the executor below with bounded per-stage budgets. Initial quality-stage limits are 1–6 (heuristics: 2 narrow well-evidenced; 3 normal multi-file; 4 broad cross-system, long-backlog, or many-source; 5–6 exceptional breadth with a written reason before the first call). Audited effective quality limits may later reach 12; each manual extension raises the current effective limit by at most 4. After useful reviews exhaust an undersized limit, raise only the executor-visible effective limit with `python3 scripts/quality.py extend /absolute/task/quality-plan.json STAGE --max-calls N --note /absolute/owner-note.md`. Keep the plan's original `max_calls` unchanged. The existing ledger stores the frozen original, the effective limit, and an ordered hash-chained extension history; consumed attempts stay. An exact retry of the last successful extension (same N, task, stage, plan path, and intact note) returns success without rewriting the ledger. A same-limit request with a different note or binding, or any lower target, is an error. Do not extend while a completed attempt is still an unclassified transport result, or while its result identity or runner runtime evidence is missing or changed; resume `review` so it can mark that row failed first. A rewritten result that still parses as valid review JSON is not classified. Successful reviews store a quality-classification marker on the attempt so later criteria changes do not invalidate historical rows; extension checks that marker plus intact artifacts, and backfills unmarked legacy rows before granting budget. An extension never turns HOLD into READY and grants no acceptance, implementation, or deployment authority. Direct plan or ledger limit edits fail closed. Local hashes are consistency/tamper evidence only, not external authenticity proof.

## Planning checkpoint

Before accepting a consequential implementation plan that meets a consultation trigger below, consult the configured high-capability advisor. The brief must state the decision, acceptance criteria, options, material disagreement, and only the evidence needed to assess them. The advisor evaluates the plan; the conductor remains responsible for selecting and implementing it.

Routine planning, ordinary multi-file implementation, and work already covered by clear acceptance tests do not trigger advice on their own. A selected persistent quality plan supplies its own plan checkpoint; do not add a duplicate direct consultation.

## Settings and roles

`python3 scripts/advisor.py settings` reads conductor defaults from account `config.toml` and advisor settings from account `agents/astra_advisor.toml`. That role is the sole source of advisor model, effort and instructions. Its filename does not fix the model. Saved conductor settings do not switch the current session. Never silently substitute a model or increase effort.

Mirror Claude Code's useful model control without weakening the evidence workflow:

```sh
python3 scripts/advisor.py model
python3 scripts/advisor.py model luna|terra|sol|astra|off
```

The first command reports the current selection. The second atomically changes only the advisor
model and enabled state. For known Codex tiers, an advisor weaker than the conductor returns
`SKIP: advisor_weaker_than_conductor` before creating a ledger or dispatching a model. Unknown
future model identities are reported as capability-order unverified rather than guessed. Model
identity remains separate from the derived capability comparison.

Keep web-chat collaboration (such as Alpha C2C) separate: ChatGPT plans/reviews; the conductor evaluates and implements accepted ideas. Request advice on a specific unresolved decision, not a duplicate full review. Advice does not grant permission for implementation or consequential actions.

## Prepare once per decision

Keep one ledger for the entire task across restarts. Record its absolute path in existing task context. Never create another ledger to bypass its budget. At plan acceptance, consult only when a trigger below applies. Also check advisory need after failed repair/retest and before consequential actions.

Write a nonempty brief containing the question, acceptance criteria, options and material disagreement. Select only relevant evidence files, not the whole conversation. All selected content must be UTF-8 and fit the combined 48,000-byte packet limit.

Create decision JSON with absolute file paths:

```json
{"schema_version":2,"task_id":"task-id","decision_id":"decision-id","brief_file":"/absolute/brief.md","evidence_files":["/absolute/evidence.txt"],"failed_repair_files":[],"same_cause_failed_repairs":0,"explicit_request":false,"unresolved_evidence_conflict":false,"architecture_change":false,"hard_to_reverse_action":false}
```

All referenced files must exist and be nonempty. Failure count must equal distinct failed repair/retest log paths. Count only completed failed repairs of the same cause; authentication, quota and infrastructure failures are not model-quality failures. Inspect evidence before assigning flags.

Consultation triggers: explicit user request; a selected persistent quality checkpoint; at least two same-cause failed repairs; unresolved evidence conflict; architecture change involving service/data boundaries or public/permission contracts; or a hard-to-reverse action. Project size and ordinary plan acceptance are not triggers. These thresholds are operating rules, not proven cost optima.

## Resume after an executor repair

`python3 scripts/advisor.py credentials` reports only the effective credential
home and selection source, without a model call, secret output, or reservation.
Before carrying an old authentication/quota blocker into a resumed task, compare
that diagnostic with the failed launch's `credential_home`. A failure from the
wrong installation account does not establish a limit on the current account.

No automatic retry means no blind retry of unchanged failed conditions. When the
user has authorized skill repair and retesting, a verified executor correction
can receive one deliberate rerun within the existing ledger and remaining
budget. Record the changed execution condition and why it addresses the recorded
failure before invoking `run`; preserve the old failure and call count. Do not
rotate accounts, change the selected model, reset a ledger, or keep retrying an
unchanged quota error. A new process alone is not a repaired condition.

## Compare the current decision before reuse

`REUSE` proves that a submitted packet and its saved result are intact; it does
not prove that the packet still describes the current decision. Before adopting
old advice, compare its question, feasible options, acceptance criteria, and
material evidence with the present task. For a decision governed by component
skills, read the currently applicable sections and include their bounded
excerpts plus source paths/content hashes as selected evidence in the packet.
Do not include whole skills or exceed the packet limit.

Record the comparison. If options, constraints, or governing semantics changed
materially, preserve the old result as historical advice and prepare a genuinely
new bounded decision packet when a consultation trigger applies. Use the same
task ledger and its remaining budget. The executor already fingerprints the
brief and selected evidence files, so updated decision evidence participates in
its normal reuse check; do not bypass it or fabricate a completion receipt.
Unresolved conflict between an old packet's feasible options and the current
contract is an evidence-conflict trigger, not a new model-quality failure.

Cosmetic wording, timestamps, an unchanged failure, or a new process alone do
not justify another consultation. If the old packet lacks a governing section,
assess whether that omission changes its conclusion before adopting it; do not
assume either unconditional reuse or unconditional invalidation. Exhausted
budget still stops additional calls and does not authorize ledger rotation.

## Execute and reuse

```sh
python3 scripts/advisor.py run /absolute/task/decision.json --ledger /absolute/task/advisor-ledger.json --timeout 180
```

`run` automatically initializes the ledger with a three-attempt limit, reserves one call, starts `codex exec`, captures the response and execution evidence, validates completion and records usage. Omit `--ledger` to use `advisor-ledger.json` beside the decision. Timeout accepts 1–900 seconds. A different initial call limit requires `init DECISION --ledger LEDGER --max-calls N` before the first run (1–20 for this direct executor; quality checkpoints initial max 6 / audited effective max 12). Every reservation, including failure/cancellation, consumes budget. Do not add unlimited retry, automatic growth, or ledger rotation.

Credential selection preserves an explicit `ADVISOR_AUTH_CODEX_HOME` override;
otherwise it uses the running session's `CODEX_HOME`, falling back to the skill
installation home only when neither is set. An invalid selected login fails
before reserving a call; it never silently tries another account. The advisor
role and model/effort still come from its configured role file. A quota error
never authorizes account rotation or changing models.

The executor uses current account login with explicit role model/effort, ignores general user configuration, removes API-key overrides, runs in a temporary directory with read-only sandbox, and disables tools, skills discovery, agents and integrations. It does not read the workspace automatically: include necessary evidence in the packet. Custom non-OpenAI providers are rejected rather than silently rerouted.

Interpret the returned action:

- `COMPLETED`: read the result file, independently assess it, and record accepted/rejected recommendations and verification in the task log.
- `REUSE`: the submitted packet has a verified saved result; no model call occurred. Adopt it only after **Compare the current decision before reuse**. Identical decisions reuse; a materially changed decision or explicitly requested further opinion may use a new decision ID within the same ledger and budget.
- `SKIP`: no trigger; continue authorized work directly.
- `BUSY`: an executor or pending attempt already exists; inspect it instead of dispatching another.
- `BUDGET_EXHAUSTED`: stop additional consultations; do not rotate ledgers or raise the limit automatically.
- `FAILED` or `ERROR`: inspect the recorded error. No automatic retries or model fallbacks.
- `RECOVERY_REQUIRED`: inspect the saved launch and process state. Do not assume a stale timestamp proves termination.

Artifacts live in `advisor-runs/<request-id>/` beside the ledger: prompt, launch arguments/PID, raw JSONL, stderr, terminal evidence, response and receipt. Completion requires exit 0, one runtime thread and completed turn, matching final message/output, unchanged decision/settings and artifact hashes. Unexpected tool events are rejected. Model/effort are confirmed launch parameters, not service-side identity attestations.

On timeout the executor terminates and reaps its own child before recording failure. After an interrupted executor, recover only when terminal evidence exists:

```sh
python3 scripts/advisor.py run /absolute/task/decision.json --ledger /absolute/task/advisor-ledger.json --recover
```

Recovery validates saved evidence without a new dispatch. If terminal evidence is unavailable, investigate the recorded process; never clear an unknown/live attempt or call again blindly. Low-level `check/start/complete/fail` commands remain diagnostic interfaces; ordinary usage is `run`. Manually supplied completion receipts alone cannot satisfy executor reuse.

## Measurement and verification

```sh
python3 scripts/advisor.py report --ledger /absolute/task/advisor-ledger.json
python3 -m unittest discover -s scripts -p 'test_*.py'
```

Tests use local fake executables and consume no model tokens. Reports distinguish known advisor usage from unknown usage, and exclude conductor, GPT web and implementation costs. Do not infer whole-workflow savings or launch paid comparison benchmarks automatically.

This is an executable skill, not a mandatory global hook: the conductor must invoke it and classify the decision honestly. Local artifacts and ledger hashes are not tamper-proof provider attestations or external authenticity proof. Locks cover callers sharing one ledger; packet size, wall time and attempt limits are not a hard token-spend cap. Verify advice against actual task evidence before adopting it.
