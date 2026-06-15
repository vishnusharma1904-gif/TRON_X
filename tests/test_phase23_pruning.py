"""
Phase 23 verification: context-aware dynamic prompt pruning.

Standalone script (no pytest dependency assumed) -- run with:
    python3 tests/test_phase23_pruning.py

Exercises Orchestrator._score_turns / _trim_history / _build_messages /
_content_to_text / _cosine_sim directly. Orchestrator.__new__ is used to
avoid the heavy __init__ (router/chroma/etc.) since none of these methods
touch instance state beyond calling each other.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# chromadb is a heavy optional dependency (not installed in this sandbox) that
# orchestrator.py only reaches transitively via src.memory.rag -> chroma_db.
# Its real behavior is irrelevant to the Phase 23 pruning logic under test, so
# stub it out before importing orchestrator. All chromadb usage in chroma_db.py
# is inside method bodies (never at module import time), so MagicMock attribute
# access is sufficient for the import to succeed.
if "chromadb" not in sys.modules:
    from unittest.mock import MagicMock
    chromadb_mock = MagicMock()
    chromadb_config_mock = MagicMock()
    chromadb_mock.config = chromadb_config_mock
    sys.modules["chromadb"] = chromadb_mock
    sys.modules["chromadb.config"] = chromadb_config_mock

from src.intelligence import orchestrator as orch_mod  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def make_orch():
    return orch_mod.Orchestrator.__new__(orch_mod.Orchestrator)


# ── Embedding preflight ─────────────────────────────────────────────────────
EMBED_OK = True
try:
    _v = orch_mod._cached_embed_one("preflight check")
    EMBED_OK = isinstance(_v, list) and len(_v) > 0
except Exception as e:
    print(f"[preflight] embedding model unavailable ({e}); "
          f"semantic-similarity cases will be skipped, marker-bonus and "
          f"structural cases still run.")
    EMBED_OK = False

print(f"[preflight] embeddings available = {EMBED_OK}\n")


# ── 1. _content_to_text ──────────────────────────────────────────────────────
print("== _content_to_text ==")
check("plain string", orch_mod._content_to_text("hello") == "hello")
check(
    "multimodal list",
    orch_mod._content_to_text(
        [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "x"}}]
    )
    == "hi",
)
check("empty/none", orch_mod._content_to_text(None) == "")


# ── 2. _cosine_sim ────────────────────────────────────────────────────────────
print("\n== _cosine_sim ==")
check("identical vectors -> 1.0", abs(orch_mod._cosine_sim([1, 2, 3], [1, 2, 3]) - 1.0) < 1e-9)
check("orthogonal vectors -> 0.0", abs(orch_mod._cosine_sim([1, 0], [0, 1])) < 1e-9)
check("zero vector -> 0.0 (no div by zero)", orch_mod._cosine_sim([0, 0], [1, 1]) == 0.0)


# ── 3. _score_turns: empty / count ───────────────────────────────────────────
print("\n== _score_turns basics ==")
orch = make_orch()
sysmsg = msg("system", "You are TRON-X.")

check("empty history -> []", orch._score_turns([sysmsg], "anything") == [])

two_pair_history = [
    sysmsg,
    msg("user", "What's 2+2?"), msg("assistant", "4."),
    msg("user", "What's the capital of France?"), msg("assistant", "Paris."),
]
scores = orch._score_turns(two_pair_history, "")
check("empty current_query -> all zeros, correct count", scores == [0.0, 0.0], detail=str(scores))

scores = orch._score_turns(two_pair_history, "math question")
check("non-empty query -> one score per pair", len(scores) == 2, detail=str(scores))


# ── 4. _score_turns bonuses ───────────────────────────────────────────────────
print("\n== _score_turns bonuses ==")

remember_history = [
    sysmsg,
    msg("user", "Remember that my dog's name is Rex."),
    msg("assistant", 'Got it, sir. I\'ll remember that: "my dog\'s name is Rex".'),
    msg("user", "What's the weather like on Mars?"),
    msg("assistant", "Mars averages around -63 C, with thin CO2 atmosphere."),
]
scores = orch._score_turns(remember_history, "tell me something unrelated")
check(
    "remember-command pair scores higher (+0.3 ack bonus)",
    scores[0] > scores[1],
    detail=str(scores),
)
check("remember-command pair score >= 0.3", scores[0] >= 0.3, detail=str(scores))

forget_history = [
    sysmsg,
    msg("user", "Forget my old wifi password."),
    msg("assistant", 'Done, sir. Forgotten: "wifi password is Sunshine123".'),
    msg("user", "Tell me a joke about cats."),
    msg("assistant", "Why was the cat sitting on the computer? To keep an eye on the mouse."),
]
scores = orch._score_turns(forget_history, "totally different topic")
check("forget-command pair scores higher (+0.3 ack bonus)", scores[0] > scores[1], detail=str(scores))

remembered_fact_history = [
    sysmsg,
    msg("user", "What do you know about my preferences?"),
    msg("assistant", "[Remembered fact] Your favorite color is blue."),
    msg("user", "Tell me about the history of Rome."),
    msg("assistant", "Rome was founded, according to legend, in 753 BC."),
]
scores = orch._score_turns(remembered_fact_history, "completely unrelated query about cooking")
check(
    "[Remembered fact] marker pair scores higher (+0.5 bonus)",
    scores[0] > scores[1],
    detail=str(scores),
)
check("[Remembered fact] marker pair score >= 0.5", scores[0] >= 0.5, detail=str(scores))


# ── 5. _trim_history: no pruning when under budget ───────────────────────────
print("\n== _trim_history: under budget ==")
small = [
    sysmsg,
    msg("user", "Hi"), msg("assistant", "Hello!"),
    msg("user", "How are you?"),
]
out_semantic = orch._trim_history(list(small), current_query="How are you?")
out_fifo = orch._trim_history(list(small), current_query="")
check("semantic: unchanged when under budget", out_semantic == small)
check("fifo: unchanged when under budget", out_fifo == small)


# ── 6. FIFO rollback: reproduces old exact behavior ──────────────────────────
print("\n== _trim_history: FIFO rollback ==")


def build_long_history(n_pairs: int, pad_words: int = 80) -> list[dict]:
    pad = " ".join(["filler"] * pad_words)
    history = [sysmsg]
    for i in range(n_pairs):
        history.append(msg("user", f"[turn {i+1}] question {pad}"))
        history.append(msg("assistant", f"[turn {i+1}] answer {pad}"))
    history.append(msg("user", f"[current] new question {pad}"))
    return history


def old_fifo(messages: list[dict]) -> list[dict]:
    """Exact copy of the pre-Phase-23 _trim_history for rollback comparison."""
    messages = list(messages)
    while orch_mod._messages_tokens(messages) > orch_mod.MAX_CONTEXT_TOKENS and len(messages) > 2:
        messages.pop(1)
        if len(messages) > 1:
            messages.pop(1)
    return messages


# Force a small budget so pruning actually triggers, without needing
# enormous synthetic strings.
orig_max = orch_mod.MAX_CONTEXT_TOKENS
orch_mod.MAX_CONTEXT_TOKENS = 600
try:
    long_hist = build_long_history(20)
    expected = old_fifo(long_hist)
    got_empty_query = orch._trim_history(list(long_hist), current_query="")
    check(
        "fifo (current_query='') matches old behavior exactly",
        got_empty_query == expected,
        detail=f"len got={len(got_empty_query)} expected={len(expected)}",
    )

    orch_mod.settings.pruning_strategy = "fifo"
    got_fifo_flag = orch._trim_history(list(long_hist), current_query="some query")
    check(
        "PRUNING_STRATEGY=fifo matches old behavior exactly even with a query",
        got_fifo_flag == expected,
        detail=f"len got={len(got_fifo_flag)} expected={len(expected)}",
    )
    check(
        "fifo result respects MAX_CONTEXT_TOKENS (or is minimal)",
        orch_mod._messages_tokens(got_fifo_flag) <= orch_mod.MAX_CONTEXT_TOKENS
        or len(got_fifo_flag) <= 2,
    )
finally:
    orch_mod.settings.pruning_strategy = "semantic"
    orch_mod.MAX_CONTEXT_TOKENS = orig_max


# ── 7. Semantic pruning: edge case (few pairs -> no pruning) ────────────────
print("\n== _trim_history: semantic edge cases ==")
orch_mod.MAX_CONTEXT_TOKENS = 1
try:
    few_pairs = build_long_history(orch_mod.RECENCY_ANCHOR)  # exactly RECENCY_ANCHOR pairs
    out = orch._trim_history(list(few_pairs), current_query="anything")
    check(
        "<=RECENCY_ANCHOR pairs -> no pruning even if over budget",
        out == few_pairs,
        detail=f"len out={len(out)} expected={len(few_pairs)}",
    )
finally:
    orch_mod.MAX_CONTEXT_TOKENS = orig_max


# ── 8. Semantic pruning: 20-turn synthetic scenario from spec ───────────────
print("\n== _trim_history: 20-turn semantic scenario ==")

# Build 20 pairs:
#   pair 3  -> a "remember" command (should survive via +0.3 marker bonus)
#   pair 15 -> topically related to current_query ("python performance")
#   pairs 18-20 -> recency anchor (always kept), different topic (cooking)
#   all other pairs -> generic filler, low relevance, candidates for drop
PAD = " ".join(["context"] * 60)

scenario = [sysmsg]
for i in range(1, 21):
    if i == 3:
        u = "Remember that my favorite programming language is Rust."
        a = 'Got it, sir. I\'ll remember that: "favorite programming language is Rust."'
    elif i == 15:
        u = f"How do I profile a slow Python function to find bottlenecks? {PAD}"
        a = f"Use cProfile or line_profiler to find hotspots before optimizing. {PAD}"
    elif i in (18, 19, 20):
        u = f"What's a good recipe for pasta tonight? (turn {i}) {PAD}"
        a = f"Try aglio e olio: garlic, olive oil, chili flakes. (turn {i}) {PAD}"
    else:
        u = f"Random unrelated trivia question number {i}. {PAD}"
        a = f"Here's a generic unrelated trivia answer number {i}. {PAD}"
    scenario.append(msg("user", f"[turn {i}] {u}"))
    scenario.append(msg("assistant", f"[turn {i}] {a}"))

current_query = f"What's the best way to optimize my Python code for speed? {PAD}"
scenario.append(msg("user", current_query))

total_tokens_full = orch_mod._messages_tokens(scenario)

# Force a budget that requires dropping several (but not all) pairs.
orch_mod.MAX_CONTEXT_TOKENS = total_tokens_full // 2
try:
    result = orch._trim_history(list(scenario), current_query=current_query)

    check("system prompt kept", result[0] == sysmsg)
    check("current query kept (last message)", result[-1]["content"] == current_query)
    check("result respects MAX_CONTEXT_TOKENS", orch_mod._messages_tokens(result) <= orch_mod.MAX_CONTEXT_TOKENS,
          detail=f"tokens={orch_mod._messages_tokens(result)} budget={orch_mod.MAX_CONTEXT_TOKENS}")
    check("some pairs were actually dropped", len(result) < len(scenario),
          detail=f"len result={len(result)} len scenario={len(scenario)}")

    contents = " ".join(orch_mod._content_to_text(m["content"]) for m in result)
    check("turn 3 (remembered fact) preserved", "[turn 3]" in contents)
    check("recency anchor turns 18-20 preserved",
          all(f"[turn {i}]" in contents for i in (18, 19, 20)))

    if EMBED_OK:
        check("turn 15 (topically related) preserved", "[turn 15]" in contents,
              detail="(embedding-based relevance)")
    else:
        print("  SKIP  turn 15 relevance check (no embedding model available)")

    # Chronological order check: turn numbers among kept pairs must be increasing.
    import re
    turn_nums = []
    for m_ in result[1:-1]:
        text = orch_mod._content_to_text(m_["content"])
        match = re.search(r"\[turn (\d+)\]", text)
        if match:
            turn_nums.append(int(match.group(1)))
    check("kept pairs remain in chronological order", turn_nums == sorted(turn_nums),
          detail=str(turn_nums))
finally:
    orch_mod.MAX_CONTEXT_TOKENS = orig_max


# ── 9. _build_messages wires current_query through ───────────────────────────
print("\n== _build_messages ==")
orch_mod.MAX_CONTEXT_TOKENS = orig_max
built = orch._build_messages(
    history=[msg("user", "earlier"), msg("assistant", "reply")],
    user_content="new question",
    system_prompt="system prompt text",
)
check("system prompt first", built[0]["content"] == "system prompt text")
check("user content last", built[-1]["content"] == "new question")

# multimodal user_content -> current_query derived from text part only
built_mm = orch._build_messages(
    history=[],
    user_content=[{"type": "text", "text": "describe this image"}, {"type": "image_url", "image_url": {"url": "x"}}],
    system_prompt="sys",
)
check("multimodal content preserved in messages", built_mm[-1]["content"][0]["text"] == "describe this image")


# ── Summary ───────────────────────────────────────────────────────────────────────────────────────
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
