# SAN — Structured Associative Notation

A compression protocol for converting source code and documentation into a dense format optimized for LLM processing.

## Why SAN?

| Metric | Raw Code | SAN |
|--------|----------|-----|
| Tokens per file (avg) | ~1200 | ~150 |
| Compression ratio | — | ~85% |
| Full repo in context | ~15% | ~100% |
| Post-compaction recovery | Re-read all files | Query SAN brain instantly |

## The 5 Rules

### Rule 1: Lead with the subject
```
Human:  "When the network is unavailable, the system queues uploads locally"
SAN:    "UploadQueue: local_persist (trigger: network_unavailable)"
```

### Rule 2: Replace verbs with operators
```
→  flow/sends/passes to
=  is/defined as/maps to
:  contains/includes
?  if/when/triggers
+  and/along with
|  or/alternatively
⇒  results in/produces
×N repeats/retries N times
```

### Rule 3: Extract facts into key:value pairs
```
Human: "The system implements exponential backoff with max 3 retries"
SAN:   "retry: exponential_backoff ×3, on_exhaust → State.Failed"
```

### Rule 4: Use training-native vocabulary
```
Weak:   "tries again after waiting longer each time"
Strong: "exponential_backoff"

Weak:   "keeps things separate so one failure doesn't break everything"
Strong: "SupervisorJob isolates child coroutine failures"
```

### Rule 5: Group related facts, separate unrelated ones
```
@engine: WorkManager(android), BGTaskScheduler(ios)
@constraint: max_file = 10MB, validate = client_side
@state: sealed(Idle, Uploading, Failed, Success)
@threading: IO_dispatcher via AppDispatchers
```

## SAN Output Format

```
<qualified_name> @<kind> {
  <san_content>
}
```

Kinds: `svc` | `repo` | `route` | `model` | `iface` | `config` | `test` | `util` | `vm` | `usecase`

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
}
```

**After** (SAN, ~150 tokens):
```
AuthServiceImpl @svc {
  impl: AuthService iface
  deps: UserRepository + TokenProvider + RateLimiter (all iface/DI)
  fn:login(email, pwd) → AuthResult
    [validate → check_rate_limit → verify_password → issue_jwt]
  fn:register(RegisterRequest) → AuthResult
    [validate → check_exists → hash_pwd → create_user → issue_jwt]
  fn:refreshToken(token) → TokenPair
    [verify_refresh → rotate_pair]
  layer: application/service
  patterns: DIP-clean, all-deps-injected
  risk: auth-critical, token-handling
}
```

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
