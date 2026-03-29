"""
Graph Serializer — Serialize causal graphs for client consumption.

Converts CausalGraph models to compact JSON payloads optimized for:
    1. On-device FM context window (must fit within token budget)
    2. Swift client visualization (MapKit overlays, graph views)
    3. REST API delivery

Version History:
    0.1.0  2026-03-28  Initial graph serializer
"""

from __future__ import annotations

import logging
from typing import Any

import orjson

from spatial_agents.causal.intervention import InterventionResult
from spatial_agents.models import CausalGraph

logger = logging.getLogger(__name__)


class GraphSerializer:
    """
    Serialize and compress causal graphs for delivery to clients.

    Usage:
        serializer = GraphSerializer()

        # Full JSON for REST API
        json_bytes = serializer.to_json(graph)

        # Compact version for FM context window
        compact = serializer.to_compact(graph, max_nodes=10)

        # With intervention results
        payload = serializer.to_fm_payload(graph, interventions)
    """

    def to_json(self, graph: CausalGraph) -> bytes:
        """Serialize full graph to JSON bytes."""
        return orjson.dumps(
            graph.model_dump(mode="json"),
            option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
        )

    def to_dict(self, graph: CausalGraph) -> dict[str, Any]:
        """Serialize graph to dict."""
        return graph.model_dump(mode="json")

    def to_compact(
        self,
        graph: CausalGraph,
        max_nodes: int = 10,
        max_edges: int = 15,
    ) -> dict[str, Any]:
        """
        Generate a compact representation optimized for FM context window.

        Prioritizes nodes by connectivity (most connected first) and
        preserves the strongest edges.
        """
        # Sort nodes by number of connections (edges involving this node)
        node_ids = {n.id for n in graph.nodes}
        edge_counts: dict[str, int] = {nid: 0 for nid in node_ids}
        for edge in graph.edges:
            if edge.source in edge_counts:
                edge_counts[edge.source] += 1
            if edge.target in edge_counts:
                edge_counts[edge.target] += 1

        # Keep top N nodes by connectivity
        sorted_nodes = sorted(graph.nodes, key=lambda n: edge_counts.get(n.id, 0), reverse=True)
        kept_nodes = sorted_nodes[:max_nodes]
        kept_ids = {n.id for n in kept_nodes}

        # Keep edges where both endpoints are in kept nodes, sorted by strength
        kept_edges = sorted(
            [e for e in graph.edges if e.source in kept_ids and e.target in kept_ids],
            key=lambda e: e.strength,
            reverse=True,
        )[:max_edges]

        return {
            "cell": graph.h3_cell,
            "nodes": [
                {
                    "id": n.id,
                    "type": n.event_type,
                    "label": n.label,
                    "value": n.observed_value,
                }
                for n in kept_nodes
            ],
            "edges": [
                {
                    "src": e.source,
                    "tgt": e.target,
                    "str": e.strength,
                    "why": e.mechanism,
                }
                for e in kept_edges
            ],
            "ts": graph.generated_at.isoformat(),
        }

    def to_fm_payload(
        self,
        graph: CausalGraph,
        interventions: list[InterventionResult] | None = None,
        max_nodes: int = 8,
    ) -> str:
        """
        Generate a text payload for FM prompt inclusion.

        Formats the graph as structured text that fits within
        the FM's context window alongside system instructions
        and response space.
        """
        lines: list[str] = []

        # Compact node descriptions
        sorted_nodes = sorted(graph.nodes, key=lambda n: n.timestamp or graph.generated_at)
        for node in sorted_nodes[:max_nodes]:
            ts = node.timestamp.strftime("%H:%M") if node.timestamp else "?"
            lines.append(f"- [{ts}] {node.event_type}: {node.label} (conf: {node.observed_value})")

        # Edge descriptions
        if graph.edges:
            lines.append("")
            lines.append("Causal links:")
            for edge in sorted(graph.edges, key=lambda e: e.strength, reverse=True):
                lines.append(f"- {edge.source} → {edge.target} (strength: {edge.strength}): {edge.mechanism}")

        # Intervention results
        if interventions:
            lines.append("")
            lines.append("Intervention analysis:")
            for iv in interventions:
                lines.append(f"- {iv.intervention}: {iv.description}")

        return "\n".join(lines)

    def estimate_token_cost(self, text: str) -> int:
        """Rough estimate of token count for a text payload."""
        return max(1, len(text) // 4)

    def to_geojson_overlay(self, graph: CausalGraph) -> dict[str, Any]:
        """
        Generate a GeoJSON-compatible overlay for map visualization.

        Causal nodes that have geographic positions are rendered as points;
        edges are rendered as lines connecting related events.

        Note: This is a simplified version — actual positions would come
        from the event's associated entity positions.
        """
        features: list[dict] = []

        for node in graph.nodes:
            features.append({
                "type": "Feature",
                "geometry": None,  # Would be populated from event entity positions
                "properties": {
                    "node_id": node.id,
                    "event_type": node.event_type,
                    "label": node.label,
                    "domain": node.domain.value,
                    "observed_value": node.observed_value,
                },
            })

        return {
            "type": "FeatureCollection",
            "features": features,
            "properties": {
                "graph_type": "causal_model",
                "h3_cell": graph.h3_cell,
                "node_count": len(graph.nodes),
                "edge_count": len(graph.edges),
            },
        }
