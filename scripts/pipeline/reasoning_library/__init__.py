"""Reasoning Library — self-improving, embedding-routed reasoning scripts.

A general framework for accelerating structured LLM classification tasks
by accumulating reasoning traces, compressing them into reusable scripts,
and routing new tasks to the best script via embedding similarity.

Design pattern:
  1. Run N examples with full LLM reasoning → collect traces
  2. Compress traces into system prompts (one meta-call per cluster)
  3. Route new examples: hardcoded rule → script → fallback
  4. Repeat: new traces refine existing scripts or create new ones

The framework is task-agnostic. Set up a TaskConfig for your domain,
seed traces, compress them into scripts, and the router dispatches
new inputs to the cheapest adequate path.

Example applications:
  - cross-layer dependency discovery (the original use case)
  - API endpoint ↔ database table dependency analysis
  - UI component ↔ state slice subscription verification
  - Any repeated structured LLM classification with similar reasoning

Module structure:
  - models.py: Core data structures (TaskConfig, ReasoningTrace, etc.)
  - library.py: Persistent storage and management (ReasoningLibrary)
  - compressor.py: Meta-compression of traces into scripts
  - router.py: Embedding-based routing to scripts
"""

from .models import (
    TaskConfig, ReasoningTrace, ReasoningScript,
    HardcodedRule, RoutingDecision,
)
from .library import ReasoningLibrary
from .compressor import MetaCompressor, DEFAULT_SYSTEM_PROMPT
from .router import EmbeddingRouter

__all__ = [
    "TaskConfig", "ReasoningTrace", "ReasoningScript",
    "HardcodedRule", "RoutingDecision",
    "ReasoningLibrary",
    "MetaCompressor",
    "EmbeddingRouter",
    "DEFAULT_SYSTEM_PROMPT",
]
