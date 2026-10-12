# SDK Chinese README Standard Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make each public SDK's sole `README.md` a complete Chinese usage document, retire duplicate localized READMEs, and automatically prevent the documentation layout from drifting back.

**Architecture:** Keep SDK user documentation beside each published package in `sdk/<language>/README.md`. A lightweight Node-based repository check validates the single-README layout, Chinese-document contract, required user-facing sections, and links that must target the canonical file. Existing API reference pages remain cross-language service-contract documentation.

**Tech Stack:** Markdown, Node.js built-in test runner and filesystem APIs, npm scripts, VitePress documentation checks.

---

### Task 1: Add a regression test for the SDK README contract

**Files:**
- Create: `docs/scripts/check-sdk-readmes.test.mjs`
- Create: `docs/scripts/check-sdk-readmes.mjs`
- Modify: `docs/package.json`

**Step 1: Write the failing test**

Create a Node test that imports the checker and asserts that it reports all SDK README contract violations through a function accepting a repository root. Include a temporary fixture with a duplicate localized README and an incomplete canonical README.

**Step 2: Run the test to verify it fails**

Run: `node --test docs/scripts/check-sdk-readmes.test.mjs`

Expected: FAIL because the checker module does not exist.

**Step 3: Implement the minimal checker**

Export a check function that validates the three SDK paths, rejects duplicate localized READMEs, ensures each `README.md` is Chinese and includes the required SDK user-guide headings, and returns actionable errors. Add a `check:sdk-readmes` command.

**Step 4: Run the test to verify it passes**

Run: `node --test docs/scripts/check-sdk-readmes.test.mjs`

Expected: PASS with both invalid-fixture and valid-fixture coverage.

### Task 2: Consolidate the Python SDK document

**Files:**
- Modify: `sdk/python/README.md`
- Delete: the localized duplicate Python README

**Step 1: Preserve user-facing Chinese content**

Replace the English README with the established Chinese user guide, keeping installation, authentication, sync/async use, common workflows, error handling, compatibility, and development/testing guidance.

**Step 2: Add any required canonical headings**

Ensure the document explicitly covers SDK scope, installation, configuration/authentication, quickstart, core capabilities, task/error behavior, API quick reference, and compatibility.

**Step 3: Run the checker**

Run: `cd docs && npm run check:sdk-readmes`

Expected: the Python entry passes while Go and TypeScript still identify any unfinished contract issues.

### Task 3: Consolidate and strengthen the Go SDK document

**Files:**
- Modify: `sdk/go/README.md`
- Delete: the localized duplicate Go README

**Step 1: Write the user workflow sections**

Retain Chinese setup and capability coverage, then add a single import-task-search workflow, Go `context.Context` cancellation/timeout use, `Error` / `IsCode` handling, pagination, and key zero-value/optional-field warnings.

**Step 2: Add a searchable API quick-reference**

Group the exported client surface by capability, including newer file, ACL, assets, agent-evolution, and task methods, with links to the source API reference where appropriate.

**Step 3: Run the checker**

Run: `cd docs && npm run check:sdk-readmes`

Expected: Go is accepted as a complete Chinese SDK document.

### Task 4: Consolidate and strengthen the TypeScript SDK document

**Files:**
- Modify: `sdk/typescript/README.md`
- Delete: the localized duplicate TypeScript README

**Step 1: Expand the Chinese guide**

Document setup/authentication, Node runtime assumptions, the complete resource-task-search workflow, filesystem/retrieval/session/tags/OVPack/Watch operations, errors, pagination, and migration compatibility.

**Step 2: Add an API quick-reference**

Provide grouped public method names and link readers to the TypeScript declarations for exact option types.

**Step 3: Run the checker**

Run: `cd docs && npm run check:sdk-readmes`

Expected: all three SDKs pass their documentation contract.

### Task 5: Point documentation at the canonical SDK README files

**Files:**
- Modify: `docs/zh/api/01-overview.md`
- Modify: `docs/en/api/01-overview.md` if it references an SDK README
- Modify: any other tracked Markdown file that links to a retired localized SDK README

**Step 1: Update links**

Replace all repository-relative links to retired localized SDK README files with the sole `README.md` counterpart. Keep link labels accurate by stating that the target is Chinese where an English API page links to it.

**Step 2: Verify no legacy links remain**

Run: `npm run check:sdk-readmes`

Expected: no matches.

### Task 6: Full verification and review

**Files:**
- Verify: `docs/scripts/check-sdk-readmes.test.mjs`
- Verify: `docs/scripts/check-sdk-readmes.mjs`
- Verify: `docs/package.json`
- Verify: all three `sdk/*/README.md`

**Step 1: Run targeted validation**

Run: `cd docs && npm run check:sdk-readmes && node --test scripts/check-sdk-readmes.test.mjs && npm run check:docs && npm run check:api`

Expected: all checks pass.

**Step 2: Build the documentation site**

Run: `cd docs && npm run docs:build`

Expected: build exits 0.

**Step 3: Inspect the final diff**

Run: `git diff --check && git status --short`

Expected: only intended SDK README, documentation-link, checker, test, package-script, and plan changes.
