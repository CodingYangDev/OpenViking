# DAG experience memory

Experience templates stay at `viking://user/{user}/memories/experiences/{name}.md`.
The `content` field contains the complete DAG construction source. Python is never executed
with `exec` or `eval`; a restricted interpreter validates and builds the runtime DAG.
Existing text experiences are not converted or accepted as DAG templates.

## Extraction and updates

Agent Evolution first persists one factual Trajectory for each `(Case, Session archive)`.
Its single `content` field is stored and consumed as text. The prompt recommends `# Session Context`
with relevant clauses from the execution-time system messages, followed by `# Trajectory` and
typed numbered items. Headings, their order, numbering, nonempty steps and a one-to-one source-step
count are not runtime requirements; there is no 256-step cap. The server does not reparse content,
renumber it or assign role labels by position. The existing extraction call selects rules based on
the user goal and the full supplied conversation, including requested actions that were skipped
or refused. It preserves conditions and exceptions, not unrelated persona/style instructions;
an empty context section is valid when no relevant rule is supplied. Training control messages
are excluded. Relevant rule selection is prompt-guided; paragraphs are not required by a runtime
validator to match contiguous substrings of the source text.
No separate memory-schema field or extra LLM call is added. The model is asked to preserve the
order, source labels and important facts of user messages, assistant messages, Experience Reminders
and tool records. Source input types remain explicit; the server owns Case name, archive
link, outcome, compact evaluation and Experience execution paths. Client-recorded reminder delivery is never
interpreted as business success. Relevant exact amounts and IDs stay in the steps; only the final
Experience generalizes facts into conditional reminders.

```text
Session + Case + OutcomeEvaluation
  -> Phase 1: AgentTrajectoryContextProvider + ExtractLoop
  -> model emits one sdk.create_trajectories(... Markdown content ...) + sdk.commit()
  -> server validates and normalizes the fixed add-only operation
  -> MemoryUpdater applies content, metadata and provenance links
  -> immutable Trajectory under trajectories/{case_name}/{archive_identity_hash}.md
  -> mark Trajectory experience_update.state=pending
  -> Session commit completes and returns trajectory_uris
  -> Phase 2: POST /api/v1/agent-evolution/experiences/learn
  -> claim pending Trajectories under per-record locks and group them by canonical Case
  -> if this Case's current batch has no success, add at most its latest historical success
  -> ExperienceGradientEstimator: compare evidence and emit one complete DAG per Case/batch
  -> optional proposal Gate
  -> deterministic plan construction + existing locked policy apply
  -> mark each claimed Trajectory applied, no_op, or failed
```

The stable identity is a hash of Case name and archive URI, so concurrent retries cannot create
multiple records or overwrite the first persisted observation. Lookup is by exact Case directory,
not semantic search. Trajectories do not participate in online Experience autorecall. Each
Trajectory links to its immutable source Session and canonical Case; each Experience links to the
Trajectories used to synthesize it. A compact source-session index supports Gate lookup, but the
link chain is the provenance source of truth. Commit diffs expose newly persisted Trajectories,
including when DAG synthesis returns no-op. Old procedure-style Trajectories are not migrated.

Step preprocessing uses up to 16 KiB per item within a 64,000-character conversation budget with
explicit omission notices. System messages are supplied separately for rule selection, not counted
as steps. Step summaries are requested to stay within 800 characters each; this is prompt guidance,
not a runtime content check. The existing ExtractLoop still enforces one Trajectory operation and
the fixed memory type and storage target. Persistence, pending scans, historical lookup and
Experience inputs no longer decode a mandatory heading/step format. Optional Gate evidence uses
bounded text chunks without interpreting them as steps or changing the stored content. Runtime execution and external
evaluation remain server-owned metadata rather than model-generated claims. Synthesis sends the
complete combined `content` plus compact evaluation and execution metadata, so each run keeps its
own rules rather than inheriting another run's policy. Each selected Trajectory occupies its own
`user` message: compact YAML metadata followed by the original Markdown content, not a JSON-escaped
string. A separate `user` message holds Case/batch metadata, and another holds the existing
Experience as plain source text (or the read error). The final user message requests the update.
All records still share one model request; this is not one extraction call per Trajectory.
The existing read/SDK-binding path runs before this presentation step, so
`experience.content.overwrite(...)` still refers to the actually loaded memory. No assistant/tool
prefetch pair is sent to the model on this Experience path; ordinary memory extraction keeps its
native tool messages. Instruction normalization does not rewrite quoted Trajectory evidence.
The 240,000-character comparison budget counts the rendered Case/Trajectory message text,
including metadata and headings (not the separately supplied existing Experience or system
instructions). It always retains current Trajectories. If any current Trajectory succeeded,
historical lookup is skipped entirely. Otherwise, at most the latest eligible same-Case historical
success is selected and admitted while space remains; historical failures, partial, unfinished
and unknown outcomes are never used as witnesses. If no historical success exists, the batch
proceeds without one. Oversized current batches fail rather than silently dropping or splitting
Trajectories.

The comparison prompt distinguishes successful runs, failed runs and unknown/partial runs. Without
a successful run it preserves passed subcriteria and can return no-op. A grading literal alone is
not a universal rule; conflicts with user intent are retained as uncertainties. A successful run is
not proof of a whole SOP. The update focuses on evidenced differences in scope, timing or decisions.
Concrete entity IDs from a Session remain Trajectory evidence and must not appear in generated
reminders. The proposal boundary deterministically turns a candidate that copies such an ID into a
no-op; amounts and policy thresholds are not treated as IDs by this check.
Compact evaluator criteria are stored with each Trajectory as evidence for synthesis. Concrete
runtime results such as calculated totals remain evidence rather than literal requirements in the
Experience; the synthesized reminder should preserve the grounded scope, timing, and decision rule.

Concurrent Sessions still extract and persist Trajectories independently, but Session commit no
longer generates an Experience. The Phase 2 caller provides an explicit batch of Trajectory URIs,
or asks the server to scan pending records. Each invocation produces at most one DAG per Case.
`pending -> processing -> applied|no_op|failed` is server-owned metadata; processing leases permit
recovery after interruption. The benchmark invokes Phase 2 once after all commit tasks in its
epoch finish and before evaluation. Online callers may invoke it after a quiet window or another
external batch boundary. Inside an invocation, up to ten Case groups execute concurrently by default
(`StreamingPolicyTrainerConfig.max_concurrent_cases`). Each admitted Case uses the explicit
`StreamingPolicyTrainer.train_gradients` entry, sharing the existing plan/apply implementation
without starting a time-window timer or splitting that Case's observations by the streaming
chunk-size limit. Model planning runs outside the policy-directory lock; fresh-version
validation and apply remain locked. Case-link/snapshot finalizers are serialized. Queued Cases
remain pending until admitted, and overlapping learning invocations remain serialized. On
cancellation, active workers are joined; unfinished processing records retain their expiring
leases for recovery. Optional proposal Gate configuration is retained and defaults to
disabled. Case links use the same policy lock boundary when their backlinks are updated. The
coordinator is process-local in this version: per-record file locks protect writes, but distributed
claim arbitration across multiple OpenViking server instances is not supported.

### Explicit training phases and progress

`OfflinePolicyOptimizationPipeline` orchestrates the two remote-training phases explicitly:

1. `SessionCommitPolicyTrainer.train_rollouts` commits each rollout batch and waits for its
   Trajectories. It does **not** invoke Experience learning.
2. After **all** loader batches in an epoch finish, `RemoteExperienceLearner.learn` receives their
   results and learns once from the deduplicated Trajectory URIs of successful commits. No URI
   means no learning request; the benchmark never falls back to scanning unrelated pending data.
3. Epoch reporting and evaluation follow the learning phase. `train_from_rollouts` uses the same
   explicit two-step sequence. Local `BatchPolicyTrainer` use without an Experience learner is
   unchanged.

Epoch reports expose `session_commit_cost_seconds` and `experience_learn_cost_seconds`, alongside
commit errors and Experience-learning results/errors. The total epoch cost still includes rollout.

With progress output enabled, each completed Session-commit batch prints one aggregate line:
`[TRAIN_COMMIT] completed=240 with_traj=192 without_traj=48 errors=0`.
`completed` counts successful non-skipped commits; `with_traj` and `without_traj` partition those
commits by whether they returned any nonempty Trajectory URI. Multiple URIs count as one producing
commit. Failed and skipped commits are excluded from both output counts; `errors` counts failed
commit records, not distinct error messages. No-output commits are not reclassified as failures.
The same `commit_summary` is stored in batch results and aggregated across commit batches in the
two-phase learner and epoch report. Existing retry/skip behavior and error lists are unchanged.

Both training phases use lenient extraction handling. When `strict_extract_errors=false`,
Session memory/Trajectory extraction failures (including `ExperienceTrainingError`) are recorded
in Trace and return an empty extraction result. If the other commit steps succeed, the commit
completes normally and retains its raw Session archive; it does not fabricate a Trajectory.
Explicit strict callers still receive the exception. Cancellation is never swallowed.

Experience-learning Case failures remain `failed` in the server-owned Trajectory state and API
result, preserving diagnostics and `retry_failed` behavior. The training adapter treats these
attempts as **skipped**, not applied: they advance processed progress without a red failure count
and do not populate training `errors`. Console summaries report `skipped`; model/parse exception
stacks are trace-only within the extraction call tree, without silencing unrelated concurrent
requests. Request/task failures similarly retain their task ID and incomplete/diagnostic metadata
without becoming training errors; interrupted waits never force completion of the progress bar.

The learning API defaults to `wait=true` and returns the final result. With `wait=false`, it returns
`{"task_id": "..."}` immediately after task creation. The existing `GET /api/v1/tasks/{task_id}`
endpoint provides owner-scoped task status, final results and live `meta.progress`:

```json
{"total": 30, "completed": 12, "running": 10, "applied": 8, "no_op": 3, "failed": 1}
```

The counts refer to **Cases**, not Trajectories. They are initialized after filtering pending
records and applying `max_cases`; completion is published only after a Case's terminal Trajectory
states have been persisted. Both no-op and failed Cases count as completed work. Queued learning
requests have no known total until admitted; no artificial progress is emitted.

The benchmark submits once with `wait=false` and the API's existing `max_cases=1000` limit,
polls the task (every two seconds by default), and renders the shared progress bar from these counts.
This does not add automatic pagination for epochs above the API's Case limit. Transient polling `ReadError`s use the existing
capped backoff without resubmitting learning. The learning wait remains bounded to 1800 seconds;
a cancelled/timed-out waiter does not cancel server work or force the bar to 100%. The recorded
task ID permits subsequent status queries. Service shutdown cancels and joins in-process learning
workers before closing storage. This is not an automatic restart/resume scheduler.

The explicit Phase 2 endpoint accepts exact Trajectory URIs, Case names, or both:

```http
POST /api/v1/agent-evolution/experiences/learn
Content-Type: application/json

{
  "trajectory_uris": ["viking://user/default/memories/trajectories/case/hash.md"],
  "case_names": ["case"],
  "retry_failed": false,
  "max_cases": 100,
  "lease_seconds": 900
}
```

When both filters are supplied, the server takes their union so a caller can pass the exact
Trajectories it observed while also recovering pending records from an interrupted commit. Calls
are idempotent: `applied` and `no_op` records are skipped; expired `processing` leases may be
reclaimed; `failed` records require `retry_failed=true`. Historical records without the
server-owned `experience_update` state are never re-consumed as pending work. Only records marked
with the current `ordered_markdown_v2` format participate in comparison; older formats are ignored
and are not automatically migrated.

Updates read the old source and may apply exact local patches or a full replacement. The
resulting complete program begins with `dag = workflow(...)`, declares observable trigger conditions with `check(...)`
and reminder leaves with `tell(...)`, and adds relationships through object references. New
Experience programs do not encode the agent's complete action path. Node variable names stay stable
for unchanged behavior. The compiler generates internal IDs.

The interpreter permits literals, lists, dictionaries, variable assignments and the listed
constructors/methods. Imports, loops, arbitrary calls, attribute reads and computation are
rejected. Programs and serialized graphs are limited to 256 KiB; graphs have at most 256 nodes.

Validation runs after the complete program. The final graph must be nonempty, acyclic, have distinct slot names and positive
IDs, and have matching branch targets/predecessors. Single-node templates are valid.
Invalid extraction output gets one repair attempt. The optimizer uses the same construction
and validation path. Storage validates again before accepting replacements or related deletes.
The source body bypasses Markdown content templates and linkification; relations remain in
`MEMORY_FIELDS` metadata.

### Materialized content validity gate

Experience `content` uses `merge_op: patch`: exact local `edit`/`drop` operations and complete
`overwrite` replacements are supported. The persisted result must remain a complete DAG.

The common path is **schema field merge → type-owned content gate → persistence**.
`session/memory/validation/content.py` registers type-owned validators; the Experience
implementation in `session/memory/experience/compiler.py` validates and canonicalizes full source.
ExtractLoop and MemoryUpdater dispatch the gate without knowing DAG syntax or special-casing Experiences.
This deterministic validity gate is mandatory and independent of the optional model-based
`ExperienceImprovementGate`; it does not call a model.

Create-only collision checks live in `session/memory/validation/create.py`. These mandatory
checks are shared by extraction and persistence and are repeated under the write-set lease.
Optional training quality gates live in `session/train/gates/`; `ExperienceImprovementGate`
implements the `PolicyProposalGate` (proposal filtering) and `PolicyUpdateGate` (plan filtering)
protocols declared in `session/train/interfaces.py`. Proposal validation does not replace the
mandatory create/content checks at persistence time.

Extraction previews reuse the existing operation materializer and the same gate, allowing the
normal repair opportunity on invalid content. A valid patch remains a patch in the resolved
operation: preview must not turn it into a stale full replacement. If repair remains invalid,
invalid upserts are omitted and destructive follow-up operations are suppressed.

For gated writes, the updater acquires/reuses the complete write-set lease, reads the latest
content, stages sequential patches to each URI, and validates every candidate before any write
or dependent delete. It then writes the already validated bytes under that lease. Unrelated
concurrent changes survive; stale/ambiguous patch anchors and invalid resulting graphs fail
without falling back to prefetched content. Exact patch application is enabled for gated types;
ordinary ungated memory types retain their existing merge/error behavior. Training proposals
also use this materializer to obtain full before/after candidates, retaining the existing base-version
and commit-time stale-plan checks. Gate failures cannot be disabled with a quality-gate setting.

## Server-side execution

Use the existing search endpoint with `mode="advice"` and send the current conversation as
`state`. No server-side Session is created, loaded, or required:

```http
POST /api/v1/search/search
Content-Type: application/json

{
  "mode": "advice",
  "query": "Which rules should I review for the current request?",
  "state": [
    {"role": "user", "content": "Please check order 123."},
    {"role": "tool", "name": "query_order", "tool_call_id": "call_1", "content": "Order 123 is shipped."}
  ],
  "experience_names": []
}
```

The `list` and `context` search modes keep their existing behavior. Advice mode accepts only
`query`, `state`, `experience_names`, `limit`, `score_threshold`, and `telemetry`; `session_id`,
arbitrary target scopes, and unrelated search/assembly options are rejected rather than ignored.
Authentication and Experience access are scoped by the request context. The former Session
advice endpoint is removed, not retained as an alias. The Python SDK exposes advice through
`search(..., options={"mode": "advice", "state": ...})`; pass `limit=3` for the usual advice limit.

The server searches the current user's Experience directory for up to 10 vector candidates,
keeping the requested vector `score_threshold` (default 0.15). It evaluates Experience relevance
and the candidates' latest DAG conditions together against the current context. Relevant
Experiences are ranked by their relevance scores, with at most `limit` retained (default 3).
`matched_experience_uris` and `experiences` describe those selected templates in relevance order,
not every vector candidate. Current `instructions` come only from actions reached by their DAGs.
An explicit `experience_names` request bypasses semantic selection and evaluates only the named
DAGs' nodes, as before. Detailed per-call evaluation results remain in `experiences` for observability. There is no
persisted evaluation revision, active-candidate cache, delivery state, or server-side advice dedup.
`state` is one list of current session messages, preserving roles, order, tool names, call IDs,
arguments and results. There is no runtime `context`/`evidence` pair, generated evidence ID, or
256-record limit. The API rejects the old fields and limits compact serialized state to 131,072
characters. Both evaluation paths select recent messages within `max_state_chars` (default
32,768), including JSON overhead and escaping. One oversized latest message is clipped with an
explicit marker; roles, tool names and call IDs are not clipped. A tool call alone is not proof
of execution. Jev's existing full-request token budget and question batching remain in place.

The DAG decision provider is selectable. `jev` uses the reusable System One service:

```json
{
  "jev": {
    "api_url": "https://api.typesafe.ai/v1/systemone",
    "api_key": "...",
    "model": "jev-latest",
    "max_input_tokens": 28000,
    "timeout": 30,
    "verify_ssl": true
  },
  "agent_evolution": {
    "dag_decider": {
      "provider": "jev",
      "relevance_threshold": 0.7,
      "noul_true_threshold": 0.7,
      "noul_false_threshold": 0.3,
      "choice_confidence_threshold": 0.5
    }
  }
}
```

`vlm` uses the account's configured language model instead and does not require a `jev` block:

```json
{
  "agent_evolution": {
    "dag_decider": {
      "provider": "vlm",
      "relevance_threshold": 0.7,
      "noul_true_threshold": 0.7,
      "noul_false_threshold": 0.3,
      "choice_confidence_threshold": 0.5,
      "max_state_chars": 32768
    }
  }
}
```

Both providers implement the same `evaluate(state, questions)` interface. Semantic recall adds
one Score relevance question per candidate to the existing non-Tell node questions, submitting
both in one logical batch. The shared `state` remains the current conversation, not previous
answers; candidate descriptions, conditions and reminders belong in questions, not in the
observed facts. Relevance measures relative priority for current unresolved needs, not the
probability of a yes/no relevance statement. Its five ordered Score criteria range from unrelated
(0) through background (1), indirect/later help (2), direct secondary help (3), to the most direct
current guidance (4). Fractional scores are preserved and divided by four to expose [0, 1]
relevance scores. Candidates can be compared together; genuinely equal priorities may tie.

Relevance and node truth are independent: a relevant Experience cannot make its conditions
true, and a true condition cannot make an irrelevant Experience selected. A finite relevance
score in [0, 1] must reach `relevance_threshold` (default 0.7); selected Experiences are ordered
by score, preserving vector order on ties. Missing or malformed relevance answers do not fall
back to vector-only selection. Relevance scores and selected URIs are recorded in the decision
trace, never persisted as an Experience invalid/completed list. Every request re-evaluates them.
Changing this relevance cutoff does not change Noul node truth thresholds, Choice confidence,
or the separate vector `score_threshold`. Lowering it cannot recover a candidate below the
Top-N rank cutoff; it only admits lower-scoring candidates into that ranked set.

Node questions retain their original Noul/Choice types, and only the selected Experiences'
node answers are applied. The deterministic DAG executor, not the model, then selects the next
actions. Conditions for unselected candidates are evaluated but discarded, avoiding a second
serial model call. Named-target evaluation and training replay still ask only node questions.
Client-side delivered-advice deduplication is unchanged.

With `jev`, questions are kept in one request while the conservative estimated input is within
`max_input_tokens`; larger sets are bisected without duplicating or truncating questions. An HTTP
413, or a 422 explicitly reporting a context-length/input-token overflow, triggers the
same split. Boolean nodes become Noul questions and conditional branches become Choice questions.
Jev never generates business values: identifiers and tool-result objects stay in the session
messages. Missing, malformed or uncertain answers leave nodes pending. Transient Jev failures do not
invalidate the Experience and never fall back to slow generative slot filling when the Jev
provider is selected. With `vlm`, the same typed question contract is sent to the account model;
malformed or incomplete responses report errors for this request without banning later attempts.

The in-repository HTTP client also provides:

```python
result = await client.search(
    query="What should I verify before handling this order?",
    limit=3,
    options={
        "mode": "advice",
        "state": [{"role": "user", "content": "Please check order 123."}],
    },
)
```

The caller supplies an explicit `query` for retrieval and the current session messages as
`state` for DAG decisions. Jev receives this state directly alongside the generated `questions`. The server does not derive the query from conversation history. The server never
executes the business tools described by `CallTool`.

Memory retrieval is control-plane behavior and is never represented as a DAG node. In
particular, `call("search_advice")`, `call("search_exp")`, `call("search_experience")`, and
`call("read_experience")` are rejected by the compiler.

### Advice applicability and presentation

Each `instructions` entry returned by Experience search carries structured `conditions` for
its reached predecessor path. Each entry has `description`, `operator` (`is_true`, `is_false`, `equals`, or `not_in`),
and `values` for choice labels. All entries are required together, matching DAG predecessor
AND semantics. A condition's original text, including any internal AND/OR, is not rewritten.
False branches negate the whole predicate; choice branches retain the selected label. An
explicit default branch lists the excluded named choices, not an unknown or missing answer.

Only ancestors of the reached advice are included, with shared ancestors listed once.
Unrelated/inactive branches are not included. Prior Tell nodes are traversed for ancestry but
never rendered as a condition that another reminder was delivered. Legacy information/tool
prerequisites retain their completion meaning. A root advice has no additional conditions.
This description is derived deterministically from the existing DAG evaluation, with no new
model call and no claim that the applicability text is independently verified ground truth.

The server API remains structured JSON. The Tau2 client's model-facing message is Markdown:

```markdown
[Experience Reminder]

### 建议 1

**适用条件**
- 用户正在询问即将出行航班及其总费用。

**建议**
核对统计范围，纳入当前有效、尚未出发的相关预订。
```

Each advice has its own section; multiple conditions are explicitly conjunctive. The client
preserves the original instruction Markdown and keeps query text, scores, Experience names,
slot names and other machine metadata out of the presentation. The marker remains unchanged
so advice is still distinguished from user/tool evidence in decisions and trajectory extraction.

Formatting and condition changes do not alter delivery identity: it remains
`(Experience URI, stable slot name, instruction text)`. Markdown is mapped back to those pending
identities only after actual context insertion. A provider with a delivery callback owns this
identity-based deduplication; the agent loop must not collapse distinct advice merely because
their display strings are identical. Providers without that callback retain text deduplication.
Updated server and Tau2 client code must be loaded together for the new response field.

### Tau2 integration

The Tau2 bridge no longer calls `ensure_session()` for advice. Its `runtime_id` (prefixed
`tau2_advice_`) is only a client-local rollout correlation key and a suffix in the local bot
conversation key. `dag_runtime.runtime_id` replaces the misleading server `session_id` field.
Local delivered-advice persistence is unchanged; normal `tau2_train_*` session commits still
create sessions for trajectory extraction. Existing empty sessions and cached rollout artifacts
are not migrated or deleted. Server, Python SDK, and Tau2 processes must reload together.

The Tau2 VikingBot executor loads no Skills and exposes no manual advice tool. Train and
test automatically recall advice for the opening user request and each real user reply,
before the next model decision. Replies are registered after their actual delivery into
model context; internal prompts, ordinary tool results, advice messages, errors, and user
simulator terminal markers do not trigger recall. Multiple queued user replies are processed
sequentially, recording each actual reminder insertion in the client session before the next recall.
A proposed batch containing a write, transfer-to-human, or done tool adds a pre-execution checkpoint.
If it yields new advice, no tool in that batch executes and the model decides again with the advice.
The discarded proposal is not appended to chat history, avoiding unmatched tool-call messages.
If there is no new advice, the batch executes normally; read-only batches skip the extra recall.

The runtime composes a bounded query from the latest user message, the preceding delivered
assistant message, and the original request. There is no query-rewriting model call or fixed
Case-name targeting. It sends one filtered, bounded copy of the current conversation as
`state` to server-side search with `mode="advice"`. It retains policy, historical advice and
ordinary tool-call/result associations, excluding only synthetic reflection prompts and private
reasoning. Plaintext assistant messages carry their known `delivered_to_user` flag. No model
summarizes or extracts another representation. Embedded image data is redacted before submission.

Only nonempty advice is inserted as a `[Experience Reminder]` message; empty results and
temporary `unavailable` responses do not block execution. Advice contains applicable review
instructions, never complete DAG programs. The evaluator is instructed not to treat historical
advice as authoritative policy, user confirmation, or proof of completed actions. Only actual insertion permits
client-side delivery recording. A unique client conversation and local reminder deduplication
remain in place per rollout; no acknowledgement is sent to the server. `advice_recalls` records each query/status/count/duration in rollout
metadata, including the user-message or before-action trigger. Injected messages retain their
tool-history positions. `dag_runtime` keeps evaluations and separate `delivered_advice` facts;
client delivery does not manufacture completed Ask/Call nodes. Existing retrieval thresholds and DAG rules are unchanged.

When a rollout is committed, the opening CaseSpec contains only static Case input and rubric data.
The final OutcomeEvaluation control message carries two sibling fields: `evaluation` and
`experience_execution`. The latter is the final runtime snapshot keyed by Experience URI:
current evaluation state, slot values, evaluated/current nodes, actions,
completed condition nodes, action outcomes, and client-recorded `delivered_advice`. Server
node state is not a delivery receipt; `delivered_advice` identifies actual context insertions. Node arrays use stable `slot_name` values, which are the Python variable
names. These observations are passed directly to the reflection stage. They are not independently
verified business outcomes; `evaluation` remains the authoritative external result. Missing
evaluation is represented as unknown, never converted into a passing result.

`agent_evolution.experience_gate_enabled` remains disabled by default. The existing optional
proposal Gate is invoked on the synthesized candidate before apply; it never receives observation
JSON as a DAG. Its legacy replay requires completion, so reminder-only DAGs cannot establish that
through model slot filling alone. This refactor does not treat replayed reminder
emission as business success or introduce a new Gate policy.

Compilation, fixed-target checks, deletion restrictions, source provenance and locked reload/apply
remain mandatory. Structural validation is not evidence that a candidate will improve future runs.

Accepted Experiences store forward `derived_from` links to Trajectories. Those Trajectories link to
their Session archives; `source_sessions` retains only a compact lookup index of source identity and
outcome. Raw evidence and evaluator feedback are not duplicated into Experience metadata. Archive
contents are never rewritten to add memory backlinks. Snapshot messages use
`OpenViking-Experience-Session-Map`; submitter memory diffs, Case relations and Gate
diagnostics are scoped to the originating Session and Case.

Optional skill extraction runs separately and does not generate Trajectory memories. An explicit
account-level Agent Evolution `false` disables Case/Experience learning. Tau2's no-memory mode
also disables automatic Experience recall. Training errors propagate to the owning commit task;
Gate rejections remain explicit diagnostics in the memory diff.

## Semantics and state

- A source may fan out to many nodes with `source.then(a, b)`. Multiple predecessor edges
  form an AND join: all predecessors must be reached in this evaluation.
- Non-choice conditions and evidence-backed AskUser/CallTool nodes use booleans. TellAgent
  nodes emit advice; models cannot mark them delivered. A Tell does not act as a cross-request
  gate for subsequent advice. The server may return the same applicable advice on every call.
- IfElse and Conditional branches are evaluated from current facts, including false/unknown
  transitions. A null branch target ends only this evaluation's path, not future eligibility.
- The server reads the current template each time. It does not read/write `.experience_instances`,
  retain old candidates, pin a template per session, accept delivered-ID filters, or track receipt
  state. Session existence and user ownership checks remain enforced. Evaluation does not take
  state locks; ordinary Experience memory writes retain their existing concurrency protection.
- Only the client deduplicates advice. Its local SessionManager persists
  `metadata.delivered_experience_advice` after actual context insertion, using the Experience URI,
  stable slot name and instruction text. Numeric node IDs can change with template order and
  are not durable delivery identity. Different or updated advice remains eligible.
- Constructing a note is not delivery. A client write failure is reported and does not undo the
  already inserted note or its in-memory dedup; saving is attempted again on later delivery/close.
  Invalid persisted delivery state disables advice for that rollout rather than silently clearing it.
- Every fresh Tau2 rollout has a unique client conversation key, including across experiments.
  Reopening the same client session preserves dedup facts; a separate conversation is independent.
- The delivery ACK route, SDK methods, retry queue and `delivery_id` are removed. Deploy the
  client and server together. Existing server delivery snapshots are left untouched but unused;
  no old-server-to-client delivery-state migration is performed.
- Graph/slot validation remains enabled. Runtime decisions no longer emit synthetic evidence
  references. Training replay retains its existing evidence format and budget. Delivery is not business
  compliance or success; training must distinguish client `delivered_advice` from server evaluation.
- Automatic conversion of legacy text and tool execution remain outside this release.

## Checks

```bash
.venv/bin/pytest tests/session/memory/test_experience_dag.py tests/service/test_experience_runtime.py -q --no-cov
```

These checks include the Python extraction protocol, repair, graph materialization, file
serialization and server runtime in one flow, using a mock VLM and storage. They also cover
invalid programs/graphs, branches, default-instance validation, stateless reevaluation, client restart recovery, isolation,
and concurrent state updates. They do not measure live-model slot-filling accuracy.
