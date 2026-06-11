# SAN — Structured Associative Notation (v2)

A compression protocol for converting source code and documentation into a dense format optimized for LLM processing — any LLM, not just Claude.

## Why SAN?

| Metric | Raw Code | SAN |
|--------|----------|-----|
| Tokens per file (avg) | ~1200 | ~150 |
| Compression ratio | — | ~85% |
| Full repo in context | ~15% | ~100% |
| Post-compaction recovery | Re-read all files | Query SAN brain instantly |

## Format Contract (machine-parsed — STRICT)

Every entity block MUST start at column 0 with:

```
<qualified_name> @<kind> {
  ...
}
```

The server indexes SAN files with the regex `^(\S+)\s+@(\w+)\s*\{`. **Any other
header style (e.g. `# qualified_name: ...` comment headers) breaks indexing** —
the file falls back to filename-only lookup and loses its kind and symbol names.

- One block per class/interface/object/top-level function group. Multiple blocks per file are fine.
- Kinds: `svc` | `repo` | `route` | `model` | `iface` | `config` | `test` | `util` | `vm` | `usecase` | `fragment` | `activity` | `module` | `fn`
- **No stub files.** If a source file is trivial (DI wiring, constants), still emit one real block listing its exports. Never emit comment-only placeholders.

## Canonical Section Order

Keys are optional, but when present MUST appear in this order — predictable
structure parses better and prompt-caches better:

```
<qualified_name> @<kind> {
  src: <line_start>-<line_end>          ← ALWAYS include; lets the AI jump to source
  purpose: <one line>
  impl: <interfaces implemented>
  deps: <A + B + C>                     ← mark DI/iface where true
  fn:<name>(<params>) → <return>        ← one per function, source order
    [step → step → step]               ← flow detail, indent under its fn
  @state: <state machine / fields>
  @errors: <error paths, throws, fallbacks>
  @constraint: <limits, validation, invariants>
  @threading: <dispatchers, locks, concurrency>
  patterns: <named patterns: DIP-clean, repository, ...>
  risk: <auth-critical, money-handling, ...>
}
```

`src:` is the accuracy anchor: SAN is a map, not a replacement — a consuming AI
that needs exact code follows `src:` to the lines instead of guessing.

## The 6 Rules

### Rule 1: Lead with the subject
```
Human:  "When the network is unavailable, the system queues uploads locally"
SAN:    "UploadQueue: local_persist (trigger: network_unavailable)"
```

### Rule 2: Replace verbs with operators
```
→  flow/sends/passes to        (ASCII fallback: ->)
=  is/defined as/maps to
:  contains/includes
?  if/when/triggers
+  and/along with
|  or/alternatively
⇒  results in/produces         (ASCII fallback: =>)
×N repeats/retries N times     (ASCII fallback: xN)
```
Unicode and ASCII forms are equivalent; prefer Unicode, accept either.
(Models on any platform read both — declare equivalence so non-Claude
compilers don't invent their own notation.)

### Rule 3: Extract facts into key:value pairs
```
Human: "The system implements exponential backoff with max 3 retries"
SAN:   "retry: exponential_backoff ×3, on_exhaust → State.Failed"
```

### Rule 4: Use training-native vocabulary — but NEVER rename identifiers
```
Weak:   "tries again after waiting longer each time"
Strong: "exponential_backoff"
```
Compress prose, not code. Function names, parameter names, types, and field
names are copied **verbatim** from source — `login(email, password)`, never
`login(email, pwd)`. A consuming AI writes call sites from these signatures;
paraphrased identifiers produce wrong code.

### Rule 5: Mark visibility
Public API is what consumers call; internals are context. Prefix non-public
members with `-`:
```
fn:login(email, password) → AuthResult        ← public
-fn:hashPassword(raw) → String                ← private/internal
```

### Rule 6: Group related facts, separate unrelated ones
```
@state: sealed(Idle, Uploading, Failed, Success)
@errors: on_fail(network) → retry ×3 → State.Failed; InvalidToken ⇒ 401
@constraint: max_file = 10MB, validate = client_side
@threading: IO_dispatcher via AppDispatchers
```
`@errors` is mandatory whenever the source has catch blocks, error returns, or
fallbacks — error paths are where consuming AIs hallucinate most.

## Full Example

**Before** (80 lines Kotlin, ~1200 tokens):
```kotlin
class AuthServiceImpl(
    private val userRepo: UserRepository,
    private val tokenProvider: TokenProvider,
    private val rateLimiter: RateLimiter
) : AuthService {
    override suspend fun login(email: String, password: String): AuthResult { ... }
    override suspend fun register(request: RegisterRequest): AuthResult { ... }
    override suspend fun refreshToken(refreshToken: String): TokenPair { ... }
    private fun hashPassword(raw: String): String { ... }
}
```

**After** (SAN, ~150 tokens):
```
com.example.auth.AuthServiceImpl @svc {
  src: 12-87
  purpose: email/password auth + JWT issuance
  impl: AuthService iface
  deps: UserRepository + TokenProvider + RateLimiter (all iface/DI)
  fn:login(email, password) → AuthResult
    [validate → check_rate_limit → verify_password → issue_jwt]
  fn:register(request: RegisterRequest) → AuthResult
    [validate → check_exists → hash_pwd → create_user → issue_jwt]
  fn:refreshToken(refreshToken) → TokenPair
    [verify_refresh → rotate_pair]
  -fn:hashPassword(raw) → String
  @errors: RateLimitExceeded ⇒ AuthResult.Throttled; bad_credentials ⇒ AuthResult.Denied (no user enumeration)
  patterns: DIP-clean, all-deps-injected
  risk: auth-critical, token-handling
}
```

## Determinism (diff-friendly regeneration)

Same source must compress to the same SAN:
- Functions in **source order**, not alphabetical or "importance" order
- No timestamps, no commentary, no model opinions
- Regenerating an unchanged file should produce a byte-identical SAN
  (small diffs on regeneration = cheap re-review + stable hashes)

## Sentence Cheat Sheet

```
"When X, then Y"              →  "Y (trigger: X)"
"A sends/passes to B"         →  "A → B"
"A which contains B and C"    →  "A: [B, C]"
"either A or B"               →  "A | B"
"A with B"                    →  "A + B"
"using A to do B"             →  "A → B" or "B via A"
"A is responsible for B"      →  "A: B"
"if A fails, then B"          →  "on_fail(A) → B"
"up to N times"               →  "×N"
"however" / "but"             →  new line with contrasting fact
"therefore" / "so"            →  → (consequence = flow)
"in order to"                 →  drop, just state the action
"it is important to note"     →  drop entirely
```

## v1 → v2 migration

v1 files using the `<name> @<kind> {` header remain valid — v2 adds `src:`,
visibility markers, `@errors`, and ordering on top. Files using comment-style
headers (`# qualified_name: ...`) were never index-compatible and should be
regenerated. Find them:

```bash
grep -rL '^\S\+ @\w\+ {' <repo>/.san --include='*.san'
```
