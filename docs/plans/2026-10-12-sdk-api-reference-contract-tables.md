# SDK API Reference Contract Tables Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Turn each SDK README's API quick reference into a source-grounded Chinese contract reference that states method inputs, defaults, omission rules, output shape, and reusable option/result fields.

**Architecture:** Keep one canonical README per SDK. Each README gets grouped method-contract tables plus reusable type tables for Options and result objects, avoiding copied fields while making every public client operation discoverable. A documentation checker verifies required contract sections and that every public client method is named in its corresponding README.

**Tech Stack:** Markdown tables, Go source declarations, Python type hints and `TypedDict` definitions, TypeScript declarations, Node.js filesystem checks and test runner.

---

### Task 1: Add contract-reference coverage checks

**Files:**
- Modify: `docs/scripts/check-sdk-readmes.mjs`
- Modify: `docs/scripts/check-sdk-readmes.test.mjs`

**Step 1: Write failing tests**

Add fixtures showing that a README missing the `方法契约` and `字段契约` subsections fails, and that an exported client method absent from the README is reported.

**Step 2: Verify the tests fail**

Run: `node --test docs/scripts/check-sdk-readmes.test.mjs`

Expected: FAIL because the current checker only validates high-level headings.

**Step 3: Implement the minimal checker**

Add required subsection checks and per-language public-client method extraction. Allow documented methods to appear in tables or code, but exclude constructors and intentionally non-client helper symbols.

**Step 4: Verify the tests pass**

Run: `node --test docs/scripts/check-sdk-readmes.test.mjs`

Expected: PASS for complete fixtures and explicit errors for missing contract coverage.

### Task 2: Write the Go API contract reference

**Files:**
- Modify: `sdk/go/README.md`

**Step 1: Inventory public methods and types**

Use `sdk/go/*.go` and `types.go` as the source of truth for every `Client` method and its Options/result types.

**Step 2: Replace the method-name-only quick reference**

Add grouped method tables with inputs, defaults/omission semantics, and output fields. Add reusable field tables for `Config`, common options, pagination, task, search and error contracts. Document pointer versus zero-value semantics where relevant.

**Step 3: Verify coverage**

Run: `cd docs && npm run check:sdk-readmes`

Expected: Go public method coverage and required sections pass.

### Task 3: Write the Python API contract reference

**Files:**
- Modify: `sdk/python/README.md`

**Step 1: Inventory sync public methods and typed option dictionaries**

Use `SyncHTTPClient`, session wrappers, and package type definitions as the source of truth. Do not document async methods separately when they have the same contract.

**Step 2: Replace the method-name-only quick reference**

Add grouped method tables with Python parameter names, defaults, return structures, exceptions, and reusable option/result field tables. Explicitly explain `None` versus omitted values where contracts distinguish them.

**Step 3: Verify coverage**

Run: `cd docs && npm run check:sdk-readmes`

Expected: Python sync client surface is fully named and required sections pass.

### Task 4: Write the TypeScript API contract reference

**Files:**
- Modify: `sdk/typescript/README.md`

**Step 1: Inventory public class methods and exported interfaces**

Use `src/client.ts`, `src/types.ts`, and `src/errors.ts` as the source of truth.

**Step 2: Replace the method-name-only quick reference**

Add grouped method tables with TypeScript parameter names, defaults, Promise result types, `undefined`/`null` serialization rules, and reusable option/result field tables.

**Step 3: Verify coverage**

Run: `cd docs && npm run check:sdk-readmes`

Expected: TypeScript public client surface is fully named and required sections pass.

### Task 5: Full validation and review

**Files:**
- Verify: `docs/scripts/check-sdk-readmes.mjs`
- Verify: `docs/scripts/check-sdk-readmes.test.mjs`
- Verify: all `sdk/*/README.md`

**Step 1: Run focused checks**

Run: `cd docs && node --test scripts/check-sdk-readmes.test.mjs && npm run check:sdk-readmes && npm run check:docs && npm run check:api`

Expected: all checks pass.

**Step 2: Build the documentation site**

Run: `cd docs && npm run docs:build`

Expected: exit 0.

**Step 3: Inspect the final diff**

Run: `git diff --check && git status --short`

Expected: only the intended README, checker, test, and plan changes.
