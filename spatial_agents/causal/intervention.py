"""
Intervention Engine — Pearl's do-calculus for counterfactual reasoning.

Implements the "do" operator over structural causal models:
    do(X = x) — "What would happen if we forced X to value x?"

This enables counterfactual reasoning:
    "What would happen to port congestion if we rerouted traffic?"
    "Would the flight diversion have occurred without the weather event?"

The intervention modifies the DAG by cutting incoming edges to the
intervened node and propagating effects through the causal structure.

Version History:
    0.1.0  2026-03-28  Initial intervention engine
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import networkx as nx

from spatial_agents.models import CausalGraph

logger = logging.getLogger(__name__)


@dataclass
class InterventionResult:
    """Result of a do-calculus intervention query."""
    intervention: str             # Human-readable description
    target_node: str              # Node ID being intervened on
    original_value: float | None  # Original observed value
    intervened_value: float       # Forced value after do()
    affected_nodes: dict[str, float]  # Node ID → estimated effect magnitude
    causal_path: list[str]        # Path from intervention to effects
    description: str              # Natural language explanation

    def to_dict(self) -> dict[str, Any]:
        return {
            "intervention": self.intervention,
            "target_node": self.target_node,
            "original_value": self.original_value,
            "intervened_value": self.intervened_value,
            "affected_nodes": self.affected_nodes,
            "causal_path": self.causal_path,
            "description": self.description,
        }


class InterventionEngine:
    """
    Execute do-calculus interventions on causal graphs.

    Implements Pearl's do() operator: removes incoming edges to the
    intervened variable and propagates the forced value through
    downstream causal paths.

    Usage:
        engine = InterventionEngine()
        result = engine.do(
            graph=causal_graph,
            node_id="e0_weather_event",
            value=0.0,  # "What if the weather event hadn't occurred?"
        )
        print(result.affected_nodes)
        print(result.description)
    """

    def do(
        self,
        graph: CausalGraph,
        node_id: str,
        value: float,
        description: str | None = None,
    ) -> InterventionResult:
        """
        Execute do(node_id = value) on the causal graph.

        This implements the truncated factorization:
        P(Y | do(X=x)) ≠ P(Y | X=x) in general.

        The do() operator:
        1. Removes all incoming edges to the target node
        2. Sets the node's value to the intervention value
        3. Propagates effects through outgoing edges
        """
        G = self._graph_to_nx(graph)
        node_map = {n.id: n for n in graph.nodes}
        edge_map = {(e.source, e.target): e for e in graph.edges}

        if node_id not in G:
            return InterventionResult(
                intervention=description or f"do({node_id} = {value})",
                target_node=node_id,
                original_value=None,
                intervened_value=value,
                affected_nodes={},
                causal_path=[],
                description=f"Node {node_id} not found in graph.",
            )

        target_node = node_map.get(node_id)
        original_value = target_node.observed_value if target_node else None

        # Step 1: Create mutilated graph (remove incoming edges to target)
        G_mutilated = G.copy()
        incoming = list(G_mutilated.predecessors(node_id))
        for pred in incoming:
            G_mutilated.remove_edge(pred, node_id)

        # Step 2: Propagate effects through descendants
        descendants = nx.descendants(G_mutilated, node_id)
        affected: dict[str, float] = {}

        # Simple propagation model:
        # Effect on descendant = product of edge strengths along path × delta
        delta = value - (original_value or 0.5)

        for desc_id in descendants:
            # Find all simple paths from intervention to this descendant
            try:
                paths = list(nx.all_simple_paths(G_mutilated, node_id, desc_id))
            except nx.NetworkXError:
                continue

            if not paths:
                continue

            # Take the strongest path
            max_effect = 0.0
            best_path: list[str] = []

            for path in paths:
                path_strength = 1.0
                for k in range(len(path) - 1):
                    edge = edge_map.get((path[k], path[k + 1]))
                    if edge:
                        path_strength *= edge.strength
                    else:
                        path_strength = 0.0
                        break

                effect = abs(delta * path_strength)
                if effect > max_effect:
                    max_effect = effect
                    best_path = path

            if max_effect > 0.01:  # Threshold for reporting
                affected[desc_id] = round(max_effect, 4)

        # Build the primary causal path (longest chain)
        all_paths: list[list[str]] = []
        for desc_id in affected:
            try:
                paths = list(nx.all_simple_paths(G_mutilated, node_id, desc_id))
                all_paths.extend(paths)
            except nx.NetworkXError:
                pass

        primary_path = max(all_paths, key=len) if all_paths else [node_id]

        # Generate description
        intervention_desc = description or f"do({node_id} = {value})"
        if affected:
            effect_summary = ", ".join(
                f"{nid} (effect: {mag:.2f})" for nid, mag in
                sorted(affected.items(), key=lambda x: -x[1])[:5]
            )
            desc_text = (
                f"Intervention: {intervention_desc}. "
                f"Changed from {original_value} to {value}. "
                f"Affected nodes: {effect_summary}."
            )
        else:
            desc_text = (
                f"Intervention: {intervention_desc}. "
                f"No significant downstream effects detected."
            )

        return InterventionResult(
            intervention=intervention_desc,
            target_node=node_id,
            original_value=original_value,
            intervened_value=value,
            affected_nodes=affected,
            causal_path=primary_path,
            description=desc_text,
        )

    def counterfactual(
        self,
        graph: CausalGraph,
        node_id: str,
        question: str = "",
    ) -> InterventionResult:
        """
        Shortcut for counterfactual query: "What if this event hadn't occurred?"

        Equivalent to do(node_id = 0.0) — removing the event.
        """
        desc = question or f"What if {node_id} had not occurred?"
        return self.do(graph, node_id, value=0.0, description=desc)

    def compare_interventions(
        self,
        graph: CausalGraph,
        node_id: str,
        values: list[float],
    ) -> list[InterventionResult]:
        """Compare multiple intervention values on the same node."""
        return [
            self.do(graph, node_id, v, f"do({node_id} = {v})")
            for v in values
        ]

    @staticmethod
    def _graph_to_nx(graph: CausalGraph) -> nx.DiGraph:
        """Convert CausalGraph to NetworkX DiGraph."""
        G = nx.DiGraph()
        for node in graph.nodes:
            G.add_node(node.id, observed_value=node.observed_value)
        for edge in graph.edges:
            G.add_edge(edge.source, edge.target, strength=edge.strength)
        return G
