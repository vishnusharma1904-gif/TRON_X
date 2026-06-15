# TRON-X OFFLINE & Freezing - Complete Root Cause Analysis

## Summary
The OFFLINE status and freezing issue has **MULTIPLE CAUSES**:

1. **Frontend Streaming Bug** - THREE instances of improper loop control (FIXED)
2. **Backend Event Loop Blocking** - Synchronous embedding calls block async event loop
3. **Cascading Request Failures** - Health check fails due to server being busy

---

## Problem 1: Frontend Streaming Loop Bug (FIXED)

**Location:** `static/js/panels.js`

Three functions have the same bug where `break` only exits inner loop, not outer while loop:

1. **Line 777-791** - `speakReply()` function
2. **Line 834-845** - `sendChat()` function  
3. **Line 1452-1467** - `startPTT()` function (voice input)

When `[DONE]` signal is received:
- Inner `for` loop breaks
- Outer `while(true)` loop continues
- `reader.read()` hangs indefinitely waiting for more data
- UI freezes

**Status:** ✅ FIXED - Added `streamDone` flag to properly exit both loops

---

## Problem 2: Backend Event Loop Blocking (CRITICAL)

**Location:** `src/intelligence/orchestrator.py` line 1046

### The Call Chain

```
chat_stream() [async generator]
  ↓
_build_messages() [synchronous]
  ↓
_trim_history() [synchronous]
  ↓
_score_turns() [synchronous]
  ↓
_cached_embed_one() [synchronous]
  ↓
embed_one() [synchronous]
  ↓
embed() [synchronous - BLOCKING]
  ↓
model.encode() [CPU-intensive embedding]
  ↓
BLOCKS ENTIRE EVENT LOOP!
```

### Why This Causes OFFLINE Status

1. User sends message
2. Frontend makes request to `/api/chat/stream`
3. Backend enters `chat_stream()` async generator
4. Yields "meta" event successfully
5. Calls `_build_messages()` at line 1046
6. **BLOCKS** in `model.encode()` for embedding (CPU-intensive)
7. Event loop is frozen - can't process other requests
8. Frontend's `probe()` function tries to reach `/api/health`
9. Health check times out (3 second timeout) because server is blocked
10. Status changes to OFFLINE
11. After embedding finishes, streaming resumes
12. But status is already OFFLINE

### Code Evidence

**orchestrator.py line 1046:**
```python
async def chat_stream(self, ...):
    # ... earlier code ...
    # 6. Messages
    messages = self._build_messages(session["messages"], user_message, system_prompt)
    # ↑ THIS IS BLOCKING!
    
    # 7. Stream from best available model
    chain = self.router._get_chain(...)
    for model_id in chain:
        # ... later, streaming happens ...
```

**embeddings.py line 80-94:**
```python
def embed(texts: list[str]) -> list[list[float]]:
    """SYNCHRONOUS function - blocks event loop if called from async context"""
    model = _get_model()
    vectors = model.encode(texts, show_progress_bar=False, convert_to_numpy=True)
    # ↑ CPU-intensive operation that can take 100-500ms
    return vectors.tolist()
```

---

## Problem 3: No Async Wrapper for Blocking Operations

The embedding functions (`embed()`, `embed_one()`) are purely synchronous and comment even states:

**embeddings.py line 65-66:**
```python
"""Synchronous (httpx.Client), since embed()/embed_one() are called from
sync contexts throughout the codebase."""
```

But this is WRONG - they're being called from async contexts (the `chat_stream()` generator).

---

## Complete Fix Required

### Fix 1: Frontend Loop Control (ALREADY DONE)
✅ Added `streamDone` flag to all three streaming handlers

### Fix 2: Make Embedding Non-Blocking in Async Context
The embedding calls need to be wrapped in `loop.run_in_executor()` when called from async contexts.

**Solution:**
```python
# In orchestrator.py
import asyncio

async def _async_trim_history(self, messages, current_query):
    """Async wrapper that runs blocking _trim_history in thread pool"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,  # use default ThreadPoolExecutor
        self._trim_history,
        messages,
        current_query
    )

# Then in chat_stream():
messages = await self._async_trim_history(session["messages"], user_message, system_prompt)
```

### Fix 3: Proper Error Handling
Ensure that even if embedding fails, the stream continues and doesn't hang.

---

## Why It Happens Consistently

1. **Large history** - More messages = more embeddings to score = longer blocking time
2. **Cold start** - First request after server starts has warmup delay
3. **Concurrent requests** - Multiple users trigger multiple blocking calls
4. **No thread pool** - All blocking happens on main event loop thread

---

## Testing Evidence

Looking at the logs:
```
[06/11/26 19:09:29] INFO      New session f041350e... (persona=jarvis)
[06/11/26 19:09:30] INFO      OK fireworks_ai/.../deepseek-v4-flash in 1058ms
                    INFO      'namaste ra mawaa nenu gurthu unnanaaa…' → chat (conf=0.60, method=llm, 1093ms)
```

The 1093ms latency includes:
- Intent classification
- Embedding for history scoring
- RAG retrieval  
- LLM response

But the embedding step (inside `_trim_history()`) blocks the event loop during this entire time!

---

## Files That Need Fixes

1. **`static/js/panels.js`** ✅ FIXED
   - speakReply() - Added streamDone flag
   - sendChat() - Added streamDone flag
   - startPTT() - Added streamDone flag

2. **`src/intelligence/orchestrator.py`** ❌ NEEDS FIX
   - Line 1046: Make `_build_messages()` call non-blocking
   - Create async wrapper: `_async_trim_history()`

3. **`src/memory/embeddings.py`** (Optional Enhancement)
   - Add async wrapper for `embed()` and `embed_one()`
   - Document when to use async vs sync version

---

## Implementation Priority

1. **CRITICAL** - Fix orchestrator.py embedding blocking (causes immediate OFFLINE)
2. **HIGH** - Frontend fixes already done (prevents UI hangs)
3. **MEDIUM** - Add async embedding wrappers for consistency
4. **LOW** - Add monitoring/logging for blocking operations
