"""ReasoningLibrary — persistent, self-improving library of reasoning scripts.

Key design:
  - On-disk JSON for portability, no DB dependency
  - Grows across runs: new traces → new/refined scripts
  - Multiple TaskConfigs can coexist (generalization point)
  - Three-tier routing: hardcoded → script → fallback
"""

from __future__ import annotations
import json
import time
import logging
from pathlib import Path
from typing import Any
from collections import defaultdict
import numpy as np

from .models import (
    TaskConfig, ReasoningTrace, ReasoningScript,
    HardcodedRule, RoutingDecision, dict_to_trace,
)

logger = logging.getLogger("reasoning_library")


class ReasoningLibrary:
    """Persistent library of reasoning scripts with routing.

    Usage:
        lib = ReasoningLibrary("/tmp/reasoning_library.json")
        lib.add_trace(trace)          # accumulate
        decision = lib.route(inputs)  # find best script
        lib.add_script(script)        # add compressed script
        lib.save()
    """

    def __init__(self, path: str = "/tmp/reasoning_library.json"):
        self.path = Path(path)
        self._scripts: dict[str, ReasoningScript] = {}       # id → script
        self._hardcoded: list[HardcodedRule] = []            # rules (ordered)
        self._traces: dict[str, ReasoningTrace] = {}         # key → trace (recent)
        self._embed_dim: int = 1024  # bge-m3 default
        
        # Centroids as numpy arrays (for fast cosine similarity)
        self._script_centroids: dict[str, np.ndarray] = {}
        
        self.load()

    # ── Persistence ──────────────────────────────────────────────────

    def save(self):
        """Serialize to JSON."""
        data = {
            "version": 2,
            "scripts": {
                sid: {
                    "id": s.id,
                    "task_config_hash": s.task_config_hash,
                    "system_prompt": s.system_prompt,
                    "centroid": s.centroid,
                    "layer_pair": list(s.layer_pair) if s.layer_pair else None,
                    "domain_tags": s.domain_tags,
                    "source_trace_count": s.source_trace_count,
                    "estimated_edge_type": s.estimated_edge_type,
                    "confidence": s.confidence,
                    "version": s.version,
                    "created_at": s.created_at,
                }
                for sid, s in self._scripts.items()
            },
            "hardcoded": [
                {
                    "id": r.id,
                    "task_config_hash": r.task_config_hash,
                    "field_patterns": r.field_patterns,
                    "result": r.result,
                    "is_positive": r.is_positive,
                    "match_count": r.match_count,
                }
                for r in self._hardcoded
            ],
            "traces": {
                key: self._trace_to_dict(t)
                for key, t in self._traces.items()
            },
            "saved_at": time.time(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))
        logger.info(f"Saved reasoning library: {len(self._scripts)} scripts, "
                    f"{len(self._hardcoded)} rules, {len(self._traces)} traces")

    def load(self):
        """Deserialize from JSON."""
        if not self.path.exists():
            logger.info(f"No existing library at {self.path}, starting fresh")
            return
        
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load library: {e}")
            return
        
        # Load scripts
        for sid, sd in data.get("scripts", {}).items():
            self._scripts[sid] = ReasoningScript(
                id=sd["id"],
                task_config_hash=sd.get("task_config_hash", ""),
                system_prompt=sd["system_prompt"],
                centroid=sd["centroid"],
                layer_pair=tuple(sd["layer_pair"]) if sd.get("layer_pair") else None,
                domain_tags=sd.get("domain_tags", []),
                source_trace_count=sd.get("source_trace_count", 0),
                estimated_edge_type=sd.get("estimated_edge_type"),
                confidence=sd.get("confidence", 0.0),
                version=sd.get("version", 1),
                created_at=sd.get("created_at", 0.0),
            )
            if sd["centroid"]:
                self._script_centroids[sid] = np.array(sd["centroid"])
        
        # Load hardcoded rules
        for rd in data.get("hardcoded", []):
            self._hardcoded.append(HardcodedRule(
                id=rd["id"],
                task_config_hash=rd.get("task_config_hash", ""),
                field_patterns=rd["field_patterns"],
                result=rd["result"],
                is_positive=rd.get("is_positive", True),
                match_count=rd.get("match_count", 0),
            ))
        
        # Load traces (most recent only, cap at 500)
        traces = {}
        for key, td in data.get("traces", {}).items():
            try:
                traces[key] = dict_to_trace(td)
            except Exception as e:
                logger.debug(f"Skipping trace {key}: {e}")
        
        # Keep only the most recent 500 traces
        sorted_traces = sorted(traces.items(), key=lambda x: x[1].timestamp, reverse=True)
        self._traces = dict(sorted_traces[:500])
        
        logger.info(f"Loaded library: {len(self._scripts)} scripts, "
                    f"{len(self._hardcoded)} rules, {len(self._traces)} traces")

    # ── Trace management ─────────────────────────────────────────────

    def add_trace(self, trace: ReasoningTrace):
        """Store a reasoning trace. Overwrites if same key exists."""
        self._traces[trace.pair_key] = trace
        # Keep only the 500 most recent
        if len(self._traces) > 500:
            sorted_keys = sorted(self._traces.keys(),
                                key=lambda k: self._traces[k].timestamp,
                                reverse=True)
            for k in sorted_keys[500:]:
                del self._traces[k]

    def get_traces(self, 
                   task_config_hash: str | None = None,
                   min_confidence: float = 0.0) -> list[ReasoningTrace]:
        """Get traces, optionally filtered."""
        traces = list(self._traces.values())
        if task_config_hash:
            traces = [t for t in traces if t.task_config_hash == task_config_hash]
        if min_confidence > 0:
            # Only include traces that resulted in a positive
            traces = [t for t in traces if t.is_positive]
        return traces

    def traces_by_cluster(self, task_config_hash: str) -> dict[str, list[ReasoningTrace]]:
        """Group traces by (layer_pair, edge_type) for compression."""
        clusters = defaultdict(list)
        for t in self.get_traces(task_config_hash=task_config_hash, min_confidence=0.5):
            if t.result and t.is_positive:
                layer_pair = (
                    t.inputs.get("source_layer", "?"),
                    t.inputs.get("target_layer", "?"),
                )
                edge_type = t.result.get("edge_type", "?")
                cluster_key = f"{layer_pair[0]}→{layer_pair[1]}::{edge_type}"
                clusters[cluster_key].append(t)
        return dict(clusters)

    # ── Script management ────────────────────────────────────────────

    def add_script(self, script: ReasoningScript):
        """Add or update a reasoning script."""
        self._scripts[script.id] = script
        if script.centroid:
            self._script_centroids[script.id] = np.array(script.centroid)

    def get_script(self, script_id: str) -> ReasoningScript | None:
        return self._scripts.get(script_id)

    def list_scripts(self, task_config_hash: str | None = None) -> list[ReasoningScript]:
        if task_config_hash:
            return [s for s in self._scripts.values() 
                    if s.task_config_hash == task_config_hash]
        return list(self._scripts.values())

    def remove_script(self, script_id: str):
        self._scripts.pop(script_id, None)
        self._script_centroids.pop(script_id, None)

    # ── Hardcoded rules ──────────────────────────────────────────────

    def add_rule(self, rule: HardcodedRule):
        """Add a hardcoded rule."""
        self._hardcoded.append(rule)

    def get_rules(self, task_config_hash: str | None = None) -> list[HardcodedRule]:
        if task_config_hash:
            return [r for r in self._hardcoded 
                    if r.task_config_hash == task_config_hash]
        return list(self._hardcoded)

    def remove_rule(self, rule_id: str):
        self._hardcoded = [r for r in self._hardcoded if r.id != rule_id]

    # ── Routing ──────────────────────────────────────────────────────

    def route(self,
              inputs: dict[str, str],
              task_config_hash: str,
              embedding: list[float] | None = None,
              script_threshold: float = 0.70,
              ) -> RoutingDecision:
        """Route an input pair through the three-tier system.

        Resolution order (cheapest first):
          1. Hardcoded rule match → immediate result, no LLM
          2. Script centroid match → system prompt, short LLM
          3. No match → fallback, full LLM reasoning

        Returns a RoutingDecision describing the chosen path.
        """
        # Tier 1: Hardcoded rules
        for rule in self._hardcoded:
            if rule.task_config_hash != task_config_hash:
                continue
            if rule.matches(inputs):
                rule.match_count += 1
                return RoutingDecision(
                    kind="hardcoded",
                    rule_id=rule.id,
                    result=rule.result,
                    is_positive=rule.is_positive,
                    needs_llm=False,
                )
        
        # Tier 2: Script centroid match
        if embedding and self._script_centroids:
            pair_embed = np.array(embedding)
            best_sim, best_id = 0.0, None
            
            for sid, centroid in self._script_centroids.items():
                script = self._scripts.get(sid)
                if not script or script.task_config_hash != task_config_hash:
                    continue
                sim = self._cosine_similarity(pair_embed, centroid)
                if sim > best_sim:
                    best_sim, best_id = sim, sid
            
            if best_sim >= script_threshold and best_id:
                script = self._scripts[best_id]
                return RoutingDecision(
                    kind="script",
                    script_id=best_id,
                    similarity=float(best_sim),
                    needs_llm=True,  # Still needs LLM, but with pre-baked prompt
                )
        
        # Tier 3: Fallback
        return RoutingDecision(kind="fallback", needs_llm=True)

    # ── Stats ────────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """Summary statistics."""
        return {
            "scripts": len(self._scripts),
            "hardcoded_rules": len(self._hardcoded),
            "traces": len(self._traces),
            "positive_traces": sum(1 for t in self._traces.values() if t.is_positive),
            "by_layer_pair": self._layer_pair_counts(),
            "by_edge_type": self._edge_type_counts(),
        }

    def _layer_pair_counts(self) -> dict[str, int]:
        counts = defaultdict(int)
        for t in self._traces.values():
            if t.is_positive:
                sl = t.inputs.get("source_layer", "?")
                tl = t.inputs.get("target_layer", "?")
                counts[f"{sl}→{tl}"] += 1
        return dict(counts)

    def _edge_type_counts(self) -> dict[str, int]:
        counts = defaultdict(int)
        for t in self._traces.values():
            if t.result and t.is_positive:
                counts[t.result.get("edge_type", "?")] += 1
        return dict(counts)

    # ── Internal helpers ─────────────────────────────────────────────

    @staticmethod
    def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
        """Cosine similarity between two vectors."""
        norm = np.linalg.norm(a) * np.linalg.norm(b)
        if norm == 0:
            return 0.0
        return float(np.dot(a, b) / norm)

    @staticmethod
    def _trace_to_dict(t: ReasoningTrace) -> dict:
        d = {
            "pair_key": t.pair_key,
            "inputs": t.inputs,
            "reasoning_content": t.reasoning_content,
            "final_content": t.final_content,
            "result": t.result,
            "is_positive": t.is_positive,
            "total_tokens": t.total_tokens,
            "timestamp": t.timestamp,
            "task_config_hash": t.task_config_hash,
        }
        if t.embedding:
            d["_embedding_prefix"] = str(t.embedding[:3])
            d["_embedding_len"] = len(t.embedding)
        return d
