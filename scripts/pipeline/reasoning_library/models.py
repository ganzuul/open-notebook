"""Core data structures for the reasoning library.

The reasoning library generalizes over ANY structured LLM classification task
where you repeatedly call an expensive model with similar inputs:
  - Input: entities with type/role annotations
  - Output: structured classification + explanation
  - Variation: entity-identifiers differ but reasoning structure repeats

For example: "is there a dependency between module A (layer X) and module B (layer Y)?"
Or: "does this API endpoint need this database table?"
Or: "should this UI component subscribe to this state slice?"
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Literal
import time
import hashlib


# ── Configuration ────────────────────────────────────────────────────

@dataclass
class TaskConfig:
    """Describes a structured classification task.

    The task config is the *variable* part — swap this out to apply the
    reasoning library to a different problem domain.
    """
    name: str                            # e.g. "cross-layer-dependency"
    description: str                     # "Determine if a semantic dependency exists..."
    
    # Human-readable description of inputs (for prompt templates)
    input_fields: dict[str, str]         # {"source_module": "The Lean formalization module...", ...}
    
    # The output taxonomy: each field, its possible values, and definitions
    output_taxonomy: dict[str, dict[str, str]]  # {"edge_type": {"SPECIFICATION": "...", "CONSTRAINT": "..."}}
    
    # Format instruction — how to render the output
    format_instruction: str              # "DEPENDENCY|EDGE_TYPE|INVARIANT|FAILURE_MODE or NONE"
    
    # Decision criteria: what makes a "positive" classification
    positive_marker: str                 # "DEPENDENCY" — prefix indicating a positive result
    negative_marker: str                 # "NONE" — the negative result string

    def input_schema_hash(self) -> str:
        """Deterministic hash for caching/cross-checking."""
        raw = f"{self.name}|{sorted(self.input_fields.keys())}|{sorted(self.output_taxonomy.keys())}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]


# ── Traces (raw material for script generation) ──────────────────────

@dataclass
class ReasoningTrace:
    """Record of a single LLM call — the raw material for script compression.

    Fields are keyed by input_field names from TaskConfig.input_fields,
    plus the LLM's reasoning and output.
    """
    # Pair identifier
    pair_key: str                        # Unique key for this input pair
    
    # Input fields (matches TaskConfig.input_fields keys)
    inputs: dict[str, str]              # {"source_module": "EMLRegistry", "target_module": "_eml_tree", ...}
    
    # LLM output
    reasoning_content: str               # The thinking trace (~2700 tokens typical)
    final_content: str                   # The structured output
    
    # Parsed result — extracted from final_content
    result: dict[str, str] | None        # {"is_dependency": True, "edge_type": "SPECIFICATION", ...}
    is_positive: bool                    # True if result matches the task's positive marker
    
    # For routing later
    embedding: list[float] | None = None # Concatenated source+target embedding
    
    # Metadata
    total_tokens: int = 0
    timestamp: float = 0.0
    task_config_hash: str = ""           # Which TaskConfig produced this trace
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


# ── Scripts (compressed reasoning strategies) ───────────────────────

@dataclass
class ReasoningScript:
    """A pre-baked reasoning script, compressed from N traces.

    This is the "bag of tricks" — a library of these grows over time,
    each one capturing a reusable reasoning strategy for a specific
    input profile.
    """
    id: str                              # Unique script ID
    task_config_hash: str                # Which TaskConfig this belongs to
    
    # The compressed reasoning strategy — used as system prompt
    system_prompt: str                   # ~500-1000 tokens
    
    # Routing signature: what inputs does this match?
    centroid: list[float]                # Average embedding of source traces
    layer_pair: tuple[str, str] | None = None  # e.g. ("FORMALIZATION", "API_GATEWAY")
    domain_tags: list[str] = field(default_factory=list)  # ["tamari", "tree", "lattice"]
    
    # Quality metrics
    source_trace_count: int = 0          # How many traces were compressed
    estimated_edge_type: str | None = None  # Most common edge type
    confidence: float = 0.0              # Fraction of source traces that agreed
    
    # Versioning
    version: int = 1
    created_at: float = 0.0
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


# ── Hardcoded rules (no-LLM path) ────────────────────────────────────

@dataclass
class HardcodedRule:
    """A rule so reliable we skip the LLM entirely.

    Rules are matched by pattern on input fields. When matched, the
    result is pre-determined with high confidence.
    """
    id: str
    task_config_hash: str
    
    # Pattern matching — uses Python string ops on input fields
    field_patterns: dict[str, str]       # {"source_module": "*_router.py", "target_module": "*Api.ts"}
    
    # Pre-determined result
    result: dict[str, str]
    is_positive: bool
    
    # Statistics
    match_count: int = 0
    created_at: float = 0.0
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()
    
    def matches(self, inputs: dict[str, str]) -> bool:
        """Check if all field patterns match."""
        for field, pattern in self.field_patterns.items():
            value = inputs.get(field, "")
            if not self._match_pattern(value, pattern):
                return False
        return True
    
    @staticmethod
    def _match_pattern(value: str, pattern: str) -> bool:
        """Glob-like matching: * matches anything, supporting prefix*suffix style.

        Examples:
          "*.lean" matches "EMLRegistry.lean"
          "_*.py" matches "_eml_tree.py"
          "*_service.py" matches "agent_service.py"
          "*Store*" matches "agentStore.ts"
          "api.ts" matches "api.ts" exactly
        """
        if pattern == "*":
            return True
        
        # Split on * to find prefix/suffix parts
        if "*" in pattern:
            parts = pattern.split("*")
            # Empty parts at start/end means wildcard at boundary
            prefix = parts[0] if parts[0] else None
            suffix = parts[-1] if len(parts) > 1 and parts[-1] else None
            
            if prefix and not value.startswith(prefix):
                return False
            if suffix and not value.endswith(suffix):
                return False
            # Check inner parts appear in order (for patterns like "a*b*c")
            if len(parts) > 2:
                rest = value[len(prefix):] if prefix else value
                if suffix:
                    rest = rest[:-len(suffix)] if suffix else rest
                for part in parts[1:-1]:
                    if part and part not in rest:
                        return False
                    rest = rest[rest.index(part) + len(part):] if part in rest else ""
            return True
        
        return value == pattern


# ── Routing ──────────────────────────────────────────────────────────

@dataclass
class RoutingDecision:
    """Result of routing an input pair through the library."""
    kind: Literal["hardcoded", "script", "fallback"]
    
    # Which rule/script matched
    rule_id: str | None = None
    script_id: str | None = None
    
    # Similarity score (for script match)
    similarity: float | None = None
    
    # Pre-determined result (for hardcoded and script paths)
    result: dict[str, str] | None = None
    is_positive: bool | None = None
    
    # For fallback: the pair needs full LLM reasoning
    needs_llm: bool = True


# ── Serialization helpers ────────────────────────────────────────────

def trace_to_dict(t: ReasoningTrace) -> dict[str, Any]:
    d = asdict(t)
    # Convert embedding to compact repr
    if d.get("embedding") and len(d["embedding"]) > 10:
        d["_embedding_prefix"] = str(d["embedding"][:3])
        d["_embedding_len"] = len(d["embedding"])
    return d


def dict_to_trace(d: dict) -> ReasoningTrace:
    return ReasoningTrace(**{k: v for k, v in d.items() 
                           if not k.startswith("_")})
