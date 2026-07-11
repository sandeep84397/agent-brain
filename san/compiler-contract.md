# Canonical SAN Compiler Contract

This document is the normative, provider-neutral contract for generating
Structured Associative Notation (SAN) v2. Host adapters load this contract but
must not redefine its semantics. Source code is authoritative; SAN is a compact
map back to that source.

## 1. Source scope and output path

### Supported source extensions

Compile hand-written source ending in:

`.kt`, `.java`, `.py`, `.ts`, `.tsx`, `.js`, `.jsx`, `.swift`, `.go`, `.rs`,
`.rb`, `.c`, `.cpp`, `.h`, `.cs`, `.php`, `.scala`, `.m`, or `.mm`.

### Skipped directories

Skip source under `build/`, `bin/`, `out/`, `dist/`, `.gradle/`,
`node_modules/`, `Pods/`, `.output/`, `.wxt/`, `dist-unpacked/`, and
`.wrangler/`. Skip generated code and vendored dependencies even when their
extensions are supported. Never modify a source file.

SAN uses the canonical append output rule. Mirror the source-relative path
under `.san/` and append `.san` to the complete source filename:

```text
src/A.py -> .san/src/A.py.san
src/auth/Auth.kt -> .san/src/auth/Auth.kt.san
```

The compiler supplies a source-relative path to Agent Brain. Agent Brain alone
derives the destination; the compiler never chooses or writes an arbitrary
output path.

## 2. SAN v2 block grammar

Every top-level block starts at column zero with exactly:

```text
<qualified_name> @<kind> {
  src: <line_start>-<line_end>
  <canonical facts>
}
```

The indexed header grammar is `^(\S+)\s+@(\w+)\s*\{`. Comment-style headers
are invalid. Allowed kinds are `svc`, `repo`, `route`, `model`, `iface`,
`config`, `test`, `util`, `vm`, `usecase`, `fragment`, `activity`, `module`,
and `fn`.

`src: <line_start>-<line_end>` is the first field in every block. Both values
are one-based source line numbers, `line_start <= line_end`, and the range must
fit inside the current source file. Braces must balance. A qualified-name/kind
pair appears at most once per SAN file.

Trivial source still receives a real block listing its exports. Never emit a
stub, placeholder, or comment-only SAN file.

## 3. Canonical field order and source order

### Canonical field order

Fields are optional only when the source contains no corresponding fact. When
present, fields occur in this order:

```text
src
purpose
impl
deps
fn:...
@state
@errors
@constraint
@threading
patterns
risk
```

Write one independently meaningful fact per line. Prefer compact operators:
`->`/`→` for flow, `=>`/`⇒` for result, `=` for identity, `:` for containment,
`?` for condition, `+` for conjunction, `|` for alternatives, and `xN`/`×N`
for repetition. Unicode and ASCII forms are equivalent.

### Source order

Blocks and `fn:` entries preserve source order, never alphabetical or perceived
importance order. Prefix private or internal members with `-fn:`. Omit
timestamps, commentary, opinions, and generation metadata. Unchanged source
must produce byte-identical SAN.

## 4. Exact identifiers, signatures, and public surfaces

### Exact identifiers and signatures

Copy qualified names, class names, function names, parameter names, parameter
types, return types, generic bounds, overload distinctions, modifiers, and
visibility from source without abbreviation or renaming. Compress prose, not
code. Preserve constructor and callable signatures precisely in canonical
`fn:` form.

### Public surfaces

Inventory every public class, interface, object, constructor, property,
constant, top-level callable, and public method. Record every public callable's
complete signature. Record private/internal callables too, marked with `-fn:`.
Do not infer an API that the source does not expose.

## 5. Required semantic facts

Preserve every fact needed to understand behavior:

- `dependencies`: imports used by the entity, injected interfaces, inherited
  types, services, libraries, and important call targets;
- `@state`: fields, mutable state, state transitions, caches, and persisted
  values;
- `@errors`: explicit throws, caught exceptions, error returns, retries,
  fallbacks, and failure-state transitions;
- `@constraint`: validation, bounds, invariants, preconditions, and security or
  authorization requirements;
- `@threading`: dispatchers, locks, tasks, actors, callbacks, async boundaries,
  and concurrency guarantees;
- `patterns`: named implementation or architectural patterns actually present;
- `risk`: concrete sensitive surfaces such as authentication, money, privacy,
  destructive writes, or race conditions.

`@errors` is mandatory when source contains a throw, catch, error result,
retry, fallback, or explicit failure transition. Do not replace training-native
technical terms with longer paraphrases. Do not use pronouns whose subject is
ambiguous.

## 6. Semantic parity checks

### Semantic parity checks

Before publication, the compiler compares source and candidate SAN and verifies:

1. function, constructor, class, interface, and object counts match;
2. blocks and members remain in source order;
3. identifiers and signatures are verbatim and complete;
4. all public surfaces and dependencies are inventoried;
5. state, constraints, threading, patterns, and risks are preserved when
   present;
6. every required error fact appears under `@errors`;
7. every block header and `src:` range follows SAN v2 grammar;
8. no stub, placeholder, dropped entity, or invented behavior remains.

These are compiler-side meaning checks. Agent Brain performs deterministic
structural validation but does not claim to prove semantic completeness. If any
parity check fails, revise the candidate before calling `publish_san`.

## 7. Agent Brain protocol boundaries

For one bounded refresh batch:

1. Call `get_roadmap` to recover relevant open work.
2. Call `pre_check` before reading planned source.
3. Call `log_decision` once for the batch before generation.
4. Call `plan_san_refresh` for the read-only work plan.
5. Generate, check, and call `publish_san` once per planned source file.
6. Call `log_outcome` once after the batch with accepted, revised, or failed
   status.

Decision and outcome records may contain repository name, source-relative
paths, digests, provider metadata, counts, validation summaries, and retryable
paths. They never contain source text or SAN text. A decision covers only the
planned batch; unrelated roadmap work stays untouched.

## 8. Read-only planning

`plan_san_refresh` is read-only. It reports missing, stale, fresh, orphaned,
unsupported, and malformed states plus the current source digest for each file
eligible for generation. Planning must not create `.san/`, delete orphans,
backfill hashes, update mtimes, rebuild indexes, write metrics, or mutate
configuration.

Read only missing or stale source paths returned by the plan. Do not generate
from an earlier file list or reuse an earlier digest.

## 9. Per-file publication protocol

For each planned file:

1. Read the current source and generate one complete candidate.
2. Run all compiler-side Semantic parity checks.
3. Call `publish_san` with repository, source-relative path, expected source
   digest, candidate SAN content, provider identifier, model identifier, and
   reasoning metadata when the active host supplies it.
4. Accept the result only when Agent Brain confirms validation, atomic
   replacement, hash update, and index update.

The compiler never writes `.san` directly. `publish_san` derives the canonical
destination, rejects path escape and unsupported source, verifies the source
digest and SAN structure, and retains the previous valid SAN on failure. A
successful file remains published when another file in the batch fails. Retry
only failed or source-changed files.

## 10. Failure classification

### Retryable failures

- Source digest changed: request a new plan, reread that source, regenerate,
  and publish with the new digest.
- Candidate validation failed: correct the reported structural or semantic
  defect and republish only that file.
- Temporary Agent Brain transport or publication failure: retain the previous
  SAN and retry only after the tool is healthy.
- Partial batch failure: keep confirmed publications and retry only reported
  paths.

### Terminal failures

- Configured compiler model unavailable: stop before generation, report the
  active provider and configured model, and do not fall back.
- Installed contract or host adapter missing: stop and report the exact missing
  artifact; do not switch hosts.
- Unsupported extension, path escape, unconfigured repository, invalid
  configuration, or denied protocol pre-check: stop that file or batch as
  reported.
- Repeated publication failure with unchanged inputs: preserve the previous SAN
  and report the terminal validation or storage error.

Never hide, downgrade, or silently skip a failure.

## 11. Privacy and reporting

Never log source or SAN contents. Never include source or candidate excerpts in
decisions, outcomes, metrics, telemetry, or routine status reports. Record only
minimal metadata: repository, source-relative path, digest, provider/model
identifiers, validation codes, counts, elapsed time, and retryability.

Final reporting includes generated, skipped, stale-during-generation, invalid,
and failed counts plus retryable paths. Host execution and isolation belong to
the thin adapter; this contract does not bootstrap or launch another provider.
