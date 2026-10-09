---
name: advice_loader
description: Required per-task consultation of past-experience advice when ask_for_advice is available.
---

# Advice Loader

You MUST call `ask_for_advice` at least once per task, even if you are confident
that you already know the answer. Consultation is a required step, not an optional
fallback for errors or uncertainty. Do not wait until you are stuck or have already
made a mistake.

## When to consult

- Once you understand the user's goal, you MUST complete at least one consultation
  before committing to a task-specific plan, taking a state-changing action, or
  delivering a refusal, handoff, or task-completion conclusion.
  You may first clarify the request or read the facts needed to ask a useful question.
- Consult again only when a new subgoal, important fact, constraint, or decision changes
  what needs review. If the initial request was too broad, use the newly confirmed
  facts to ask a more focused question.
- Do not repeat an unchanged query after every message or tool call. Reuse advice
  already received when the relevant question and evidence have not changed.

## How to ask

Write the query yourself: state the current goal, relevant confirmed facts, and the
specific decision or aspect of the rules you want help reviewing. Unknown facts must
remain unknown; do not invent details to make a query more specific. Do not send only
an opaque task identifier or paste the full conversation. The tool attaches current
conversation and tool evidence automatically.

## How to use the response

- Wait for the result and read the advice before deciding the related action.
  Do not batch the advice call with the action that depends on its result.
- Advice highlights questions and rules to review; it is not a final decision,
  an authoritative rule, or permission to act. Check it against the current policy,
  confirmed facts, and the user's request. Current policy remains authoritative.
- For `advice`, review the applicable reminders and use them in your reasoning.
- For `no_match` or `no_applicable_advice`, continue using the current policy and
  available evidence. Absence of advice is not evidence that an action is allowed.
- For `unavailable`, continue using the current policy rather than blocking the task
  on repeated advice requests. Do not pretend that advice was received.
- A `no_match`, `no_applicable_advice`, or `unavailable` response
  also satisfies the initial consultation requirement. Do not keep querying merely
  to obtain a nonempty suggestion.

These instructions are already loaded. Use `ask_for_advice` for consultation; you do
not need to read another Skill or experience file to activate this workflow.
