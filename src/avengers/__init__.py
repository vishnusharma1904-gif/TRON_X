"""
TRON-X  --  JARVIS A.V.E.N.G.E.R.S Protocol
============================================
21-persona command layer wrapped around the existing TRON-X stack.

This package is a NON-DESTRUCTIVE overlay:
  * registry.py    -- the 21 persona definitions + strict tool-calling schemas
  * ops.py         -- deterministic capability handlers (existing modules wired in,
                      plus net-new local-first implementations for ORACLE, ATHENA,
                      ZEUS, ATLAS, SPECTRE, JEROME, THOR)
  * dispatcher.py  -- intent -> persona routing; every conversational reply still
                      flows through src.intelligence.orchestrator so Telugu
                      detection, emotion detection, RAG, persona engine and the
                      smart router remain fully intact.
"""
from src.avengers.registry import AVENGERS, get_roster, get_avenger
from src.avengers.dispatcher import get_dispatcher, AvengersDispatcher

__all__ = [
    "AVENGERS",
    "get_roster",
    "get_avenger",
    "get_dispatcher",
    "AvengersDispatcher",
]
