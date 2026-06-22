"""MetaCompressor — compresses N reasoning traces into a single system prompt.

The core idea: instead of the 35B re-deriving its reasoning strategy for every
pair, we collect traces from N similar pairs, send them all to the 35B with a
meta-prompt asking "what reasoning strategy did you use?", and distill that
into a ~500 token system prompt. Subsequent pairs in the same cluster reuse
this prompt, saving ~2350 tokens of reasoning per pair.

Usage:
    compressor = MetaCompressor(llm_api="http://localhost:8080/v1/chat/completions")
    script = compressor.compress(traces, task_config, cluster_key)
    # Returns a ReasoningScript ready to add to the library
"""

from __future__ import annotations
import json
import time
import hashlib
import logging
from typing import Any
import requests

from .models import (
    TaskConfig, ReasoningTrace, ReasoningScript,
)

logger = logging.getLogger("reasoning_library.compressor")

# ── Meta-prompt ──────────────────────────────────────────────────────

META_SYSTEM_PROMPT = """You are a meta-reasoning compressor. Your job is to analyze a set of reasoning traces from a structured LLM classification task, extract the common reasoning strategy, and output a concise system prompt that captures this strategy so a future invocation doesn't need to re-derive it.

The traces below all belong to the same cluster of inputs — same task, same layer pair, same output edge type. They show how the model reasoned about each specific pair.

Extract:
1. DECISION CRITERIA: What specific features/patterns does the model look for to determine if a dependency exists? (2-3 bullet points)
2. EDGE TYPE RATIONALE: How does the model decide which edge type label to use? What distinguishes one label from another?
3. INVARIANT TEMPLATE: What structure does the invariant description follow? Give the template with placeholders.
4. FAILURE MODE TEMPLATE: What structure does the failure mode description follow? Give the template with placeholders.

Then output a system prompt (500-800 tokens) that captures ALL of the above. The system prompt should be written in second person ("You are analyzing...") and should be detailed enough that a model given this system prompt plus a new input pair can produce the correct output with minimal additional reasoning.

Output format:
DECISION_CRITERIA:
- ...
- ...

EDGE_TYPE_RATIONALE:
...

INVARIANT_TEMPLATE:
...

FAILURE_MODE_TEMPLATE:
...

SYSTEM_PROMPT:
```
[the system prompt]
```"""


META_USER_PROMPT = """Below are {count} reasoning traces from cluster "{cluster_key}". Each trace shows the full thinking process for evaluating a specific input pair.

TASK DESCRIPTION:
{task_description}

OUTPUT TAXONOMY:
{output_taxonomy}

INPUT FIELDS:
{input_fields}

OUTPUT FORMAT:
{format_instruction}

--- TRACES ---

{traces_text}

---

Analyze these traces and produce the compressed system prompt as specified."""


# ── Default base system prompt (used before any compression) ────────

DEFAULT_SYSTEM_PROMPT = """You are a structured classification engine. Given two software modules from different architectural layers, determine whether a semantic dependency exists between them.

Follow this reasoning process:
1. Read the source and target module descriptions carefully
2. Identify shared concepts, terms, and invariants
3. Determine if the target module implements, constrains, or depends on something the source defines
4. If a dependency exists, classify the edge type
5. If no dependency exists, respond with NONE

Output exactly one of:
- DEPENDENCY|EDGE_TYPE|INVARIANT|FAILURE_MODE
- NONE"""


class MetaCompressor:
    """Compresses N reasoning traces into a reasoning script.

    The compressor sends all traces to the 35B with a meta-prompt asking
    it to extract the common reasoning pattern. The output is a system
    prompt that can be prepended to future verification requests.
    """

    def __init__(self, 
                 llm_api: str = "http://localhost:8080/v1/chat/completions",
                 llm_model: str = "Qwen3.6-35B-A3B-Q4_K_M",
                 min_traces: int = 3,
                 max_traces: int = 5,
                 meta_max_tokens: int = 8192):
        self.llm_api = llm_api
        self.llm_model = llm_model
        self.min_traces = min_traces  # Minimum traces to attempt compression
        self.max_traces = max_traces  # Cap to avoid exceeding context window
        self.meta_max_tokens = meta_max_tokens  # Need room for reasoning + content

    def compress(self,
                 traces: list[ReasoningTrace],
                 task_config: TaskConfig,
                 cluster_key: str,
                 ) -> ReasoningScript | None:
        """Compress a list of traces into a ReasoningScript.

        Returns None if there are too few traces or if the LLM call fails.
        """
        if len(traces) < self.min_traces:
            logger.info(f"Only {len(traces)} traces for {cluster_key}, "
                       f"need {self.min_traces} — skipping compression")
            return None

        # Cap traces
        traces = traces[:self.max_traces]

        # Build traces text
        traces_text = self._format_traces(traces)

        # Build the meta-prompt
        user_prompt = META_USER_PROMPT.format(
            count=len(traces),
            cluster_key=cluster_key,
            task_description=task_config.description,
            output_taxonomy=self._format_taxonomy(task_config),
            input_fields=self._format_input_fields(task_config),
            format_instruction=task_config.format_instruction,
            traces_text=traces_text,
        )

        # Call 35B
        logger.info(f"Compressing {len(traces)} traces for cluster '{cluster_key}'...")
        
        payload = {
            "model": self.llm_model,
            "messages": [
                {"role": "system", "content": META_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "seed": int(hashlib.sha256(cluster_key.encode()).hexdigest()[:8], 16),
            "max_tokens": self.meta_max_tokens,
            "cache_prompt": False,
        }

        for attempt in range(3):
            try:
                resp = requests.post(self.llm_api, json=payload, timeout=300)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                reasoning = data["choices"][0]["message"].get("reasoning_content", "")
                break
            except Exception as e:
                logger.warning(f"Compression attempt {attempt+1} failed: {e}")
                time.sleep(5)
        else:
            logger.error(f"Failed to compress traces for {cluster_key}")
            return None

        # Extract the system prompt from the response
        system_prompt = self._extract_system_prompt(content)
        
        if not system_prompt:
            logger.warning(f"No system prompt found in compression output, "
                          f"using full response as prompt")
            system_prompt = content[:2000]

        # Compute centroid from trace embeddings
        embeddings = [t.embedding for t in traces if t.embedding]
        if embeddings:
            import numpy as np
            centroid = np.mean(embeddings, axis=0).tolist()
        else:
            centroid = []

        # Determine most common edge type and confidence
        edge_types: dict[str, int] = {}
        for t in traces:
            if t.result:
                et = t.result.get("edge_type", "?") or "?"
                edge_types[et] = edge_types.get(et, 0) + 1
        
        if edge_types:
            best_et = max(edge_types.items(), key=lambda x: x[1])[0]
            confidence = edge_types[best_et] / len(traces)
        else:
            best_et = None
            confidence = 0.0

        # Parse layer pair from cluster key
        layer_pair = None
        if "::" in cluster_key:
            pair_part = cluster_key.split("::")[0]
            if "→" in pair_part:
                parts = pair_part.split("→")
                layer_pair = (parts[0], parts[1])

        script = ReasoningScript(
            id=f"script_{hashlib.sha256(cluster_key.encode()).hexdigest()[:12]}",
            task_config_hash=task_config.input_schema_hash(),
            system_prompt=system_prompt,
            centroid=centroid,
            layer_pair=layer_pair,
            domain_tags=self._extract_domain_tags(traces),
            source_trace_count=len(traces),
            estimated_edge_type=best_et,
            confidence=confidence,
            version=1,
        )

        logger.info(f"Created script '{script.id}' for cluster '{cluster_key}' "
                    f"(confidence={confidence:.2f}, {len(traces)} traces, "
                    f"{len(system_prompt)} chars)")
        return script

    # ── Internal helpers ─────────────────────────────────────────────

    def _format_traces(self, traces: list[ReasoningTrace]) -> str:
        """Format traces for the meta-prompt (concise version).

        Instead of dumping full reasoning (~2000 chars each), extract just
        the essence: inputs, edge type, invariant, failure mode, and a
        concise reasoning summary (first 300 chars of reasoning).
        """
        parts = []
        for i, t in enumerate(traces):
            inputs_str = "\n".join(f"  {k}: {v}" for k, v in t.inputs.items())
            
            # Extract just the first meaningful part of reasoning
            reasoning = t.reasoning_content
            # Take first reasoning paragraph (before first blank line), capped at 300 chars
            first_para = reasoning.split("\n\n")[0] if "\n\n" in reasoning else reasoning
            reasoning_snip = first_para[:300]
            
            # Final output
            output = t.final_content
            
            parts.append(
                f"=== TRACE {i+1}: {t.pair_key} ===\n"
                f"INPUTS:\n{inputs_str}\n\n"
                f"REASONING (first 300 chars):\n{reasoning_snip}\n\n"
                f"OUTPUT:\n{output}\n"
            )
        return "\n\n".join(parts)

    def _format_taxonomy(self, task_config: TaskConfig) -> str:
        """Format the output taxonomy for the meta-prompt."""
        lines = []
        for field, values in task_config.output_taxonomy.items():
            lines.append(f"  {field}:")
            for label, desc in values.items():
                lines.append(f"    - {label}: {desc}")
        return "\n".join(lines)

    def _format_input_fields(self, task_config: TaskConfig) -> str:
        """Format input fields for the meta-prompt."""
        lines = []
        for name, desc in task_config.input_fields.items():
            lines.append(f"  - {name}: {desc}")
        return "\n".join(lines)

    def _extract_system_prompt(self, content: str) -> str | None:
        """Extract the system prompt from the 35B's response.

        The prompt is expected between ``` markers after "SYSTEM_PROMPT:".
        """
        # Look for code-fenced block after SYSTEM_PROMPT
        if "SYSTEM_PROMPT" in content:
            # Find the code block
            if "```" in content:
                start = content.index("```")
                # Skip opening ```
                start = content.index("```", start) + 3
                # Skip language identifier if on same line
                end = content.index("```", start)
                return content[start:end].strip()
        
        # Fallback: try to extract anything that looks like a system prompt
        # (starts with "You are" or "Your job is")
        lines = content.split("\n")
        prompt_lines = []
        in_prompt = False
        for line in lines:
            if line.startswith("You are ") or line.startswith("Your job "):
                in_prompt = True
            if in_prompt:
                prompt_lines.append(line)
                if line.strip().endswith("```"):
                    break
        
        if prompt_lines:
            return "\n".join(prompt_lines)
        
        return None

    def _extract_domain_tags(self, traces: list[ReasoningTrace]) -> list[str]:
        """Extract domain-relevant tags from trace inputs."""
        # Collect all unique significant words from module descriptions
        # Keep it simple: extract from source/target module names
        import re
        tags = set()
        for t in traces:
            src = t.inputs.get("source_module", "")
            tgt = t.inputs.get("target_module", "")
            # Extract meaningful words (split on underscore/camelCase)
            for name in [src, tgt]:
                # Split on underscore
                for part in name.split("_"):
                    # Filter short/trivial words
                    if len(part) > 3 and part.lower() not in {
                        "true", "false", "none", "this", "that", "with", "from"
                    }:
                        tags.add(part.lower())
        return sorted(tags)[:10]
