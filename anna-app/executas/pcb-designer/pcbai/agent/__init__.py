"""Standalone PCB agent surface (docs/AUDIT.md §11, Q2).

This package is the repo-level home of the agent. It deliberately keeps Phase 2 scope small:

* ``tools.py``    — the callable tool layer (same functions the Anna plugin exposes), usable without Anna.
* ``registry.py`` — name → handler registry, ``read-only`` vs ``mutating`` classification, JSON schemas.
* ``cli.py``      — ``pcbai-agent`` console script: list/describe/call tools.

The LLM-driven loop (inspect → plan → act → validate → report) is Phase 3; until then the agent
(that is, me) calls these tools directly and must read their ``success``/``warnings`` fields rather
than trusting that a file was written.
"""
