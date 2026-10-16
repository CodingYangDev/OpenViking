# Tau2 Train/Eval Pipeline

Tau2 training/evaluation uses the generic OpenViking session/train batch
pipeline. In day-to-day runs, use
`benchmark/tau2/train/restart_vikingbot_train_eval.sh` as the main entrypoint:
it restarts the required services, points them at the same slot/config, waits for
health checks, and then launches the batch runner.

`benchmark/tau2/train/run_batch_train_eval.sh` is only the lower-level Tau2
wrapper. Use it when you have already started OpenViking and the Tau2 rollout
service yourself.

## 1. Main entrypoint: restart VikingBot train/eval

```bash
bash benchmark/tau2/train/restart_vikingbot_train_eval.sh
```

For a single isolated DAG-experience smoke run (training + same-task evaluation), use:

```bash
bash benchmark/tau2/train/run_dag_experience_smoke.sh
```

It defaults to slot `7` and task index `0`; override them with `--slot` and
`--task-index`. The wrapper keeps the run isolated, bounds rollout concurrency,
and enables Agent Evolution through the normal launcher.

What the launcher does:

1. prepares the OpenViking config/data directory for the selected slot;
2. restarts OpenViking and the VikingBot API;
3. waits for `http://127.0.0.1:<ov-port>/bot/v1/health`;
4. restarts the Tau2 rollout service with `--rollout-backend vikingbot`;
5. waits for `http://127.0.0.1:<tau2-port>/health`;
6. runs `benchmark/tau2/train/run_batch_train_eval.sh` with the matching
   `--config`, `--server-url`, `--benchmark-service-url`, and result directory.

Every fresh VikingBot rollout (baseline, train, per-epoch evaluation, and final evaluation)
uses exactly the tools from `Tau2BenchToolProvider.list_openai_tools()`. The setup removes
all inherited VikingBot tools before registering that allowlist, so removed tools are neither
shown to the model nor executable through its registry. Tau2 business tools, `calculate`,
`communicate_with_user`, and `done` remain available when supplied by the environment; generic
web/file/shell/messaging/subagent/image tools do not. Other VikingBot uses are unchanged.
The existing `keep_default_tools` option controls automatic advice in this executor, not whether
to retain generic tools. Both values use the same Tau2-only tool boundary.

When comparing this boundary with older runs, start a fresh Tau2 rollout service and use
`--no-reuse-train-rollout-cache --force-baseline-recompute`. Existing cached rollouts and baseline
scores do not retroactively adopt the new tool list. No cache is deleted by this change.

With advice enabled (`keep_default_tools=true`), train and test automatically recall advice
once for the initial user request and each subsequent real user reply, before the next model
decision. No Skills are loaded and no `ask_for_advice` tool is exposed to the agent. Internal
clock/reflection prompts, other tool results, injected advice, and simulator termination/error
markers do not trigger recall. Before a proposed tool batch containing a write operation,
`transfer_to_human_agents`, or `done`, it checks advice again using the latest state and proposed
calls. If new advice is inserted, the entire batch is left unexecuted and the model replans.
Without new advice, execution proceeds normally. Read-only batches do not make this extra request.

The runtime uses the latest user message, preceding customer-facing assistant message, and
original request as query context (bounded to 16,384 characters); it makes no query-rewriting
LLM call. Both splits use semantic lookup, not fixed Case-name targeting. The real conversation
and tool messages are sent as one `state` list for DAG decisions, without a duplicate
`context`/`evidence` pair. Message roles, order, tool names, arguments and results are retained;
policy and historical advice are retained; internal reflection prompts and private reasoning are excluded.
Historical advice is not authoritative policy or evidence that a condition/action has completed.
The state has a serialized-character budget rather than a 256-evidence-record cap. Only nonempty advice is injected;
empty results or unavailable retrieval do not block the rollout. After actual insertion, the
client persists delivery facts in its existing SessionManager JSONL metadata before processing
the next queued user turn. There is no server delivery ACK or `delivery_id`.

The server reevaluates only the current request's retrieved templates. It neither stores nor
accepts advice-deduplication state, and it does not retain historical active candidates. Client
advice identity uses `(experience_uri, slot_name, instruction)` rather than a mutable numeric
node ID; reordering a DAG does not repeat advice, while changed advice remains eligible.
Each rollout uses an independent client conversation key, preventing delivery facts from one
experiment or trial from suppressing another. `dag_runtime.client_session_key` locates that
local session. Runtime events retain server condition evaluations separately from client
`delivered_advice` records. Inserting an Ask or Call suggestion never marks the underlying
information request or tool step as completed. These delivery facts and evaluated slots flow
through Trajectory metadata into Experience learning without a client-side shadow DAG state.


The former ACK endpoint is removed. Restart OpenViking and Tau2 together before new rollouts;
old server `.experience_instances` files are no longer read or written. Old server-side delivery
history is not migrated to client sessions.

Experience recall defaults to `score_threshold=0.15` and `limit=3`. This only relaxes candidate
retrieval: DAG applicability checks and delivered-advice deduplication still apply. Explicit
threshold overrides and other search interfaces are unchanged.
Context selection uses graded Score questions, normalized to [0, 1]. Its cutoff is
`agent_evolution.dag_decider.relevance_threshold` (default `0.7`), independent of both the vector
cutoff and DAG node truth thresholds. Fractional priorities are retained; true ties preserve
vector order. Changing the cutoff cannot rescue an Experience ranked below the final Top3.

`rollout.json` records each automatic attempt in `metadata.advice_recalls` (user-message index,
trigger (`user_message` or `before_action`), query, status, matched experiences, advice count,
and duration). Actual injected reminders are
preserved in the trajectory and `memory_context.md`. Reports count automatic recalls separately
from model tool calls. With advice disabled, neither automatic recall nor Skill loading runs.

Each newly executed VikingBot rollout records `initial_llm_request.json`: a credential-redacted
snapshot of the first `messages` and `tools` passed from the Agent to its model provider, after
context assembly and tool filtering. It includes any initial automatic advice and the visible tool descriptions;
it is not an HTTP request dump and contains no provider configuration or authentication headers.
Later model calls do not overwrite it. `prompt_for_llm.md` remains the analysis guide. Old cached
rollouts without a captured request do not get a reconstructed snapshot.

When comparing this automatic path with older runs, use `--no-reuse-train-rollout-cache` and
`--force-baseline-recompute` so rollouts from an older advice/Skill setup are not reused.

Default train/eval arguments, when no custom train/eval args are passed, are:

```bash
--commit-concurrency 200 --epochs 2 --trials 8 --train-trials 1 --skip-final-eval
```

If you pass any train/eval arguments to `restart_vikingbot_train_eval.sh`, that
custom argument list replaces the launcher's default list, so include the options
you still want, such as `--skip-final-eval`.

Example: train one task and evaluate the same train task for 8 trials after each
epoch, without pre-training baseline or extra final eval:

```bash
bash benchmark/tau2/train/restart_vikingbot_train_eval.sh \
  --epochs 2 \
  --train-index 14 \
  --eval-split train \
  --eval-index 14 \
  --trials 8 \
  --train-trials 1 \
  --skip-baseline-eval \
  --skip-final-eval
```

Example: reuse the cached epoch-0/no-memory train rollout if it already exists;
on cache miss, run the rollout normally and write the cache:

```bash
bash benchmark/tau2/train/restart_vikingbot_train_eval.sh \
  --epochs 3 \
  --train-index 5 \
  --eval-split train \
  --eval-index 5 \
  --trials 8 \
  --train-trials 4 \
  --skip-baseline-eval \
  --skip-final-eval \
  --reuse-train-rollout-cache
```

Train rollout cache reuse is enabled by default and only affects training
rollouts for epoch `0`, before memory training has changed the policy. Use
`--no-reuse-train-rollout-cache` to disable cache reads and writes and recompute
the epoch-0 train rollouts. The explicit `--reuse-train-rollout-cache` flag is
still supported. Later training epochs and eval rollouts are always executed normally.

## 2. Evaluation modes from the restart launcher

The restart launcher always runs the full VikingBot path. Evaluation behavior is
controlled by the train/eval args passed after any launcher-only options.

### Eval-only score

Use `--epochs 0` to restart services and run evaluation without training:

```bash
bash benchmark/tau2/train/restart_vikingbot_train_eval.sh \
  --epochs 0 \
  --eval-index 24 \
  --trials 8
```

By default, eval uses the `test` split. Use `--eval-split train` to evaluate on
train tasks, or `--eval-split none` to disable eval.

### Training with baseline and per-epoch eval

The Tau2 wrapper enables `--eval-each-epoch`, so a normal training run evaluates
after every epoch using `--eval-split` and `--eval-index`.

Before training, the runner also computes a baseline eval unless
`--skip-baseline-eval` is set. For the same dataset/domain, eval indices, trials,
and rollout options, the baseline is cached under
`result/tau2/<result-dir-name>/cache/baseline/` and reused by later runs. Use
`--force-baseline-recompute` only when you intentionally want to refresh it.

For quick train-split iteration, the common pattern is:

```bash
bash benchmark/tau2/train/restart_vikingbot_train_eval.sh \
  --epochs 3 \
  --train-index 5 \
  --eval-split train \
  --eval-index 5 \
  --trials 8 \
  --train-trials 4 \
  --skip-baseline-eval \
  --skip-final-eval \
  --reuse-train-rollout-cache
```

Use `--skip-final-eval` to avoid the extra final eval pass. This is common with
Tau2 because per-epoch eval is already enabled.

## 3. Multiple isolated slots

The restart launcher accepts a launcher-only `--slot N` before the normal
train/eval arguments. Slot `0` is the default legacy setup. Slot `N > 0` uses
independent ports, OpenViking config/data, logs, and result directory so multiple
experiments can run at the same time:

| Slot value | OpenViking port | VikingBot port | Tau2 service port | OpenViking root | Result directory |
|------------|-----------------|----------------|-------------------|-----------------|------------------|
| `0` | `1933` | `18790` | `1944` | `~/.openviking` | `result/tau2/train` |
| `1` | `1934` | `18791` | `1945` | `~/.openviking_1` | `result/tau2/train_1` |
| `N` | `1933 + N` | `18790 + N` | `1944 + N` | `~/.openviking_N` | `result/tau2/train_N` |

Example: run slot 1 without touching slot 0 services or data:

```bash
bash benchmark/tau2/train/restart_vikingbot_train_eval.sh \
  --slot 1 \
  --epochs 2 \
  --train-index 14 \
  --eval-split train \
  --eval-index 14 \
  --trials 8 \
  --train-trials 1 \
  --skip-baseline-eval \
  --skip-final-eval
```

Environment variables such as `OPENVIKING_PORT`, `OPENVIKING_BOT_PORT`,
`TAU2_SERVICE_PORT`, `OPENVIKING_CONFIG_FILE`, `OPENVIKING_DATA_DIR`,
`RESULT_DIR_NAME`, and `LOG_DIR` can still override the slot-derived defaults.
For non-zero slots, the launcher copies base `~/.openviking/*.conf*` config files
when needed and rewrites the slot config's `storage.workspace`, `server.port`,
`server.bot_api_url`, and `bot.ov_server.server_url`.

The launcher writes service logs and pid files under:

```text
result/tau2/<result-dir-name>/service_logs/
```

## 4. Options

### Launcher-only options

| Option | Default | Description |
|--------|---------|-------------|
| `--slot N` | `0` | Run an isolated experiment slot. Must appear before train/eval args. |
| `--enable-agent-evolution` | off | Enable Case, Trajectory, and Experience learning for the selected account. |

The launcher maps `output_language_override: "zh-CN"` in the selected `ov.conf`
to Tau2 `--rollout-language zh`. Set `TAU2_ROLLOUT_LANGUAGE=default` or `zh` to
override this mapping for one run.

### Common train/eval options

| Option | Default | Description |
|--------|---------|-------------|
| `--domain` | `airline` | Benchmark domain to run |
| `--epochs` | `1`; restart default `2` | Number of training epochs. Use `0` for eval-only. |
| `--batch-size` | whole split | Train/eval batch size (cases per batch) |
| `--concurrency` | `200` in Tau2 wrapper | Max concurrent rollout executions |
| `--commit-concurrency` | `200` in Tau2 wrapper | Max concurrent `session.commit` submissions during training |
| `--trials` | `8` | Run each eval case N times and aggregate scores |
| `--train-trials` | `1` | Run each train case N times per epoch |
| `--train-index` | all | Run train sample(s) at 0-based split index/indices, e.g. `7` or `1,5,6` |
| `--eval-split` | `test` | Split used for baseline/per-epoch/final eval: `test`, `train`, or `none` |
| `--eval-index` | all | Run eval sample(s) at 0-based split index/indices within `--eval-split`, e.g. `14` or `1,5,6` |
| `--max-iterations` | `30` | Max steps per rollout |
| `--force-baseline-recompute` | off | Recompute cached pre-training baseline instead of reusing it |
| `--skip-baseline-eval` | off | Skip pre-training baseline eval/cache entirely |
| `--eval-each-epoch` | on in Tau2 wrapper | Run eval after every training epoch using `--eval-split` |
| `--skip-final-eval` | off; restart default on | Skip the extra final eval pass |
| `--reuse-train-rollout-cache` / `--no-reuse-train-rollout-cache` | on | Reuse cached epoch-0/no-memory train rollouts when present; write cache on miss; disable to recompute |
| `--clean-result` / `--no-clean-result` | clean | Whether to prune previous result artifacts |
| `--keep-recent-results` | `5` | Number of recent default `run_` directories to keep when cleaning; cache and non-`run_` directories are preserved |
| `--output` | auto | JSON report output path |
| `--events-output` | auto | Streaming JSONL event output path |
| `--result-dir-name` | `train`; slots use `train_N` | Result subdirectory under `result/<dataset>/` |
| `--benchmark-service-url` | set by restart launcher | Benchmark runtime service URL |
| `--config` | set by restart launcher | ov.conf path |
| `--server-url` | set by restart launcher | OpenViking server URL |
| `--api-key` | from config | OpenViking API key |
| `--account-id` | `default` | OpenViking trusted account id |
| `--user-id` | `default` | OpenViking trusted user id |

## 5. Manual service mode

Use this only when you want to manage services yourself instead of using
`restart_vikingbot_train_eval.sh`.

Start the Tau2 service manually:

```bash
bash benchmark/tau2/train/run_service.sh --host 127.0.0.1 --port 1944
```

Service options:

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | `127.0.0.1` | Service listen address |
| `--port` | `1944` | Service listen port |
| `--data-root` | auto-detect / `$TAU2_DATA_ROOT` | Path to `tau2-bench/data/tau2` |
| `--config` | `~/.openviking/ov.conf` | ov.conf for VikingBot / OpenViking access |
| `--rollout-language` | `default` | Rollout response language. Use `zh` for Chinese user-facing replies. |
| `--rollout-backend` | `vikingbot` | Rollout implementation backend. `native` for fast Python executor, `vikingbot` for full VikingBot AgentLoop. |
| `--native-thread-workers` | `128` | Thread pool size for native rollout executor. |
| `--rollout-thread-workers` | `200` | Worker threads used to host rollout executions off the uvicorn event loop. Use `0` to disable threaded hosting. |
| `--max-rollout-concurrency` | `200` | Maximum concurrent rollout executions accepted by the service. |
| `--no-kill-existing` | off | Don't kill existing process on the same port. |

Then run the lower-level Tau2 wrapper:

```bash
bash benchmark/tau2/train/run_batch_train_eval.sh \
  --epochs 4 \
  --trials 8
```

The wrapper expands to the generic runner with Tau2 defaults:

```bash
bash openviking/session/train/run_batch_train_eval.sh \
  --dataset tau2 \
  --domain airline \
  --eval-each-epoch \
  --concurrency 200 \
  --commit-concurrency 200 \
  --benchmark-service-url http://127.0.0.1:1944
```

The batch runner does **not** send a backend choice — it always uses whatever the
Tau2 service is configured with.

## 6. Result and rollout artifacts

By default each run writes artifacts under the repository-level result directory:

```text
result/tau2/<result-dir-name>/run_<domain>_<timestamp>/
  report.json
  rollouts_index.json
  rollouts/
```

`result/tau2/<result-dir-name>/latest_rollouts` points to the most recent
rollouts directory. Each rollout artifact group is one original task; each
rollout has its own subdirectory with `memory_context.md`, `messages.json`,
`tool_calls.json`, `evaluation.json`, and `commit_messages.json`. These files,
plus `rollouts_index.json`, are written as soon as each remote rollout finishes.
Train rollouts are enriched later with `commit_result.json` and
`memory_diff.json` as commit progress becomes available.

Streaming JSONL events are written to
`result/tau2/<result-dir-name>/run_<domain>_<timestamp>/events.jsonl`; train
commit events include `trace_id` for live `tail -f` debugging. Use
`--events-output` to override the path.

### Stateless advice search

Automatic advice uses `POST /api/v1/search/search` with `mode: "advice"`, `query`, and the native
conversation `state`. It does not create or load an OpenViking session. The client-local
`dag_runtime.runtime_id` is used only for rollout isolation/correlation; delivered-advice
deduplication remains in the local bot session. `tau2_train_*` commits for trajectory learning
are unaffected. Reload OpenViking and the Tau2 service together; old session-scoped advice
endpoints are removed. Previously cached rollouts and existing empty sessions are left intact.


## Native user simulation and rollout deadlines

The `vikingbot` rollout executor uses Tau2's native `UserSimulator` and its original
LiteLLM generation path for the user role. Only the customer-service agent uses
VikingBot. The experimental VikingBot-backed user provider and its 60/180-second
request/reply budgets have been removed; native user model selection and retry
behavior are restored.

The independent generic rollout deadline/cancellation changes remain:
`POST /v1/rollouts/execute` accepts `execution_timeout_seconds` (default 3600),
including queue time. Client timeout/cancellation also sends
`POST /v1/rollouts/executions/{execution_id}/cancel`. The service targets the actual
inline or worker event-loop coroutine, and records `failed` with `TimeoutError` or
`CancelledError` after coroutine cleanup. Cancelling terminal executions is idempotent.

Important: cancelling a coroutine cannot forcibly stop a synchronous native-user
SDK call already running in another thread. Restoring the native simulator also
restores that limitation and its original request timeout/retry behavior; do not
interpret a failed execution record as proof that native model I/O was interrupted.

Reload the Tau2 rollout service for the rollback to take effect. Existing running
processes and cached rollouts are not modified. For a comparison run, pass
`--no-reuse-train-rollout-cache --force-baseline-recompute` to avoid reusing results
produced by the experimental user provider.


### Agent token usage

The rollout service returns per-attempt Agent model usage in
`rollout.metadata.token_usage` (`prompt_tokens`, `completion_tokens`, `total_tokens`).
VikingBot already collects this in its agent loop; the native backend sums only assistant
message usage. User-simulator usage is excluded. External advice/DAG judge, grading, and
training/extraction model calls are not included in this Agent-loop metric.

The train pipeline aggregates this field in `report.token_usage` and displays
`avg_tokens` alongside rollout accuracy, including epoch summaries. The same reports
are written to `events.jsonl` and the final report JSON. `average_tokens_per_rollout` means
one problem's complete attempt (all Agent model turns), averaged across attempts; it is not
an average per conversation turn or a sum over all problems sharing a trial index. Each
`trials[]` report also contains its own usage summary.

`reported_rollout_count / rollout_count` records usage coverage. Missing or invalid totals
are excluded from the mean and shown as `n/a` when none are available, not filled with zero;
an explicitly reported zero is valid. Cached rollouts retain the original attempt's usage,
so these are comparable rollout-cost metrics, **not newly billed tokens for this run**.
Old cache records without usage remain unknown; no history is rewritten or recomputed.
