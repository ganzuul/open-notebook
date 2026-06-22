"""EmbeddingRouter — dispatches input pairs to the best reasoning script.

Four-tier routing:
  1. Hardcoded rule match → no LLM call needed (instant)
  2. Script centroid match (embedding similarity) → compressed system prompt
  3. Layer-pair match (source_layer, target_layer) → best script for that pair
  4. No match → fallback to full reasoning
"""

from __future__ import annotations
import logging
from typing import Any, TYPE_CHECKING

import numpy as np

from .models import RoutingDecision, TaskConfig

if TYPE_CHECKING:
    from .models import ReasoningScript

logger = logging.getLogger("reasoning_library.router")


class EmbeddingRouter:
    """Routes input pairs to the best available reasoning script.

    Uses a cascade of routing strategies from cheapest (hardcoded) to
    most expensive (full LLM reasoning). Embedding centroid matching
    catches pairs that look similar to known examples; layer-pair fallback
    catches pairs that share the same layer types (even if embedding is
    novel); fallback handles truly novel pairs.
    """

    def __init__(self,
                 script_threshold: float = 0.60,
                 hardcoded_threshold: float = 0.85):
        self.script_threshold = script_threshold
        self.hardcoded_threshold = hardcoded_threshold

    def route(self,
              inputs: dict[str, str],
              pair_embedding: list[float] | None,
              task_config: TaskConfig,
              hardcoded_rules: list,
              scripts: dict[str, 'ReasoningScript'],
              script_centroids: dict[str, np.ndarray],
              layer_edge_counts: dict[tuple[str, str], dict[str, int]] | None = None,
              ) -> RoutingDecision:
        """Route an input pair to the best path.

        Args:
            inputs: The input fields (source_module, target_module, etc.)
            pair_embedding: Concatenated source+target embedding (1024-dim)
            task_config: The task configuration
            hardcoded_rules: List of HardcodedRule objects to check
            scripts: Dict of script_id → ReasoningScript
            script_centroids: Dict of script_id → numpy centroid array
            layer_edge_counts: Dict of (src_layer, tgt_layer) → {edge_type: count}
                              for weighted layer-pair fallback. If omitted, the first
                              matching script (by insertion order) is used.

        Returns:
            RoutingDecision: hardcoded | script | fallback
        """
        task_hash = task_config.input_schema_hash()

        # Tier 1: Hardcoded rules (cheapest, no LLM)
        for rule in hardcoded_rules:
            if rule.task_config_hash != task_hash:
                continue
            if rule.matches(inputs):
                rule.match_count += 1
                logger.debug(f"Hardcoded rule '{rule.id}' matched {inputs}")
                return RoutingDecision(
                    kind="hardcoded",
                    rule_id=rule.id,
                    result=rule.result,
                    is_positive=rule.is_positive,
                    needs_llm=False,
                )

        best_script_id = None
        best_sim = 0.0

        # Tier 2: Script centroid match (embedding similarity)
        if pair_embedding and script_centroids:
            pair_vec = np.array(pair_embedding, dtype=np.float64)

            for sid, centroid in script_centroids.items():
                script = scripts.get(sid)
                if not script or script.task_config_hash != task_hash:
                    continue
                sim = self._cosine_similarity(pair_vec, centroid)
                if sim > best_sim:
                    best_sim = sim
                    best_script_id = sid

            if best_sim >= self.script_threshold and best_script_id:
                logger.debug(f"Script '{best_script_id}' matched centroid with sim={best_sim:.4f}")
                return RoutingDecision(
                    kind="script",
                    script_id=best_script_id,
                    similarity=float(best_sim),
                    needs_llm=True,
                )

        # Tier 3: Layer-pair fallback (no centroid match, but same layer pair)
        src_layer = inputs.get("source_layer", "")
        tgt_layer = inputs.get("target_layer", "")
        layer_matches = []
        for sid, script in scripts.items():
            if script.task_config_hash != task_hash:
                continue
            if script.layer_pair and script.layer_pair[0] == src_layer and script.layer_pair[1] == tgt_layer:
                layer_matches.append(sid)

        if layer_matches:
            # If centroid-best exists above 0.50, prefer it (even if below strict threshold)
            if best_script_id and best_sim > 0.50:
                chosen = best_script_id
            elif layer_edge_counts and len(layer_matches) > 1:
                # Weight by trace count: pick the edge type most common for this layer pair
                lp = (src_layer, tgt_layer)
                counts = layer_edge_counts.get(lp, {})
                if counts:
                    most_common_type = max(counts, key=counts.get)
                    # Find the script with this edge type
                    type_scripts = [s for s in layer_matches
                                    if scripts[s].estimated_edge_type == most_common_type]
                    chosen = type_scripts[0] if type_scripts else layer_matches[0]
                else:
                    chosen = layer_matches[0]
            else:
                chosen = layer_matches[0]

            logger.debug(f"Layer-pair match: {chosen} for {src_layer}→{tgt_layer} "
                         f"(sim={best_sim:.4f})")
            return RoutingDecision(
                kind="script",
                script_id=chosen,
                similarity=float(best_sim),
                needs_llm=True,
            )

        # Tier 4: Fallback to full reasoning
        logger.debug("No matching script — falling back to full reasoning")
        return RoutingDecision(kind="fallback", needs_llm=True)

    @staticmethod
    def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        """Cosine similarity via dot product."""
        dot = np.dot(a, b)
        norm = np.linalg.norm(a) * np.linalg.norm(b)
        return float(dot / norm) if norm > 0 else 0.0
