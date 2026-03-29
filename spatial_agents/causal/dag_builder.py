"""
DAG Builder — Construct structural causal model DAGs from detected events.

Implements Pearl's structural causal model framework:
    - Nodes represent observed events (from EventDetector)
    - Edges represent causal relationships with estimated strengths
    - DAG structure encodes conditional independence assumptions

Causal discovery uses a combination of:
    - Temporal ordering (causes precede effects)
    - Domain knowledge rules (encoded as edge templates)
    - Spatial proximity (co-located events are potential causes)
    - Statistical association (when sufficient data exists)

Version History:
    0.1.0  2026-03-28  Initial DAG builder with domain rule engine
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import networkx as nx

from spatial_agents.causal.event_detector import DetectedEvent
from spatial_agents.models import CausalEdge, CausalGraph, CausalNode, DataDomain

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domain Knowledge Rules — encoded causal relationships
# ---------------------------------------------------------------------------

# Each rule: (cause_type, effect_type, base_strength, mechanism)
CAUSAL_RULES: list[tuple[str, str, float, str]] = [
    # Weather → traffic disruption
    ("weather_event", "vessel_loitering", 0.7,
     "Adverse weather causes vessels to anchor or slow down"),
    ("weather_event", "ground_stop_indicator", 0.8,
     "Severe weather triggers ground stops at airports"),
    ("weather_event", "density_anomaly_high", 0.6,
     "Weather-driven delays increase local vessel/aircraft density"),

    # Port congestion chains
    ("density_anomaly_high", "vessel_loitering", 0.65,
     "High traffic density causes vessels to wait for berth availability"),
    ("vessel_loitering", "density_anomaly_high", 0.4,
     "Loitering vessels contribute to increased local density"),

    # Dark vessel activity
    ("dark_vessel_gap", "vessel_loitering", 0.3,
     "Vessels may loiter after re-enabling AIS following a dark period"),
    ("vessel_loitering", "dark_vessel_gap", 0.25,
     "Loitering vessels may disable AIS to avoid detection"),

    # Aviation chains
    ("ground_stop_indicator", "density_anomaly_high", 0.75,
     "Ground stops cause aircraft to accumulate on the ground"),

    # Cross-domain
    ("density_anomaly_high", "ground_stop_indicator", 0.3,
     "High airspace density may trigger flow control measures"),
]


class DAGBuilder:
    """
    Build structural causal model DAGs from detected events.

    Uses temporal ordering, domain knowledge rules, and spatial
    proximity to establish causal edges between event nodes.

    Usage:
        builder = DAGBuilder()
        events = detector.detect_all(vessels, aircraft, h3_cell)
        graph = builder.build(events, h3_cell)
        print(graph.nodes, graph.edges)
    """

    def __init__(
        self,
        rules: list[tuple[str, str, float, str]] | None = None,
        max_temporal_gap: timedelta = timedelta(hours=6),
    ) -> None:
        self._rules = rules or CAUSAL_RULES
        self._max_temporal_gap = max_temporal_gap
        self._rule_index = self._build_rule_index()

    def _build_rule_index(self) -> dict[tuple[str, str], tuple[float, str]]:
        """Index rules by (cause_type, effect_type) for fast lookup."""
        return {
            (cause, effect): (strength, mechanism)
            for cause, effect, strength, mechanism in self._rules
        }

    def build(
        self,
        events: list[DetectedEvent],
        h3_cell: str,
    ) -> CausalGraph:
        """
        Build a causal DAG from detected events.

        Algorithm:
        1. Create nodes from events
        2. For each pair of events, check:
           a. Temporal ordering (cause must precede effect)
           b. Domain rule match (known causal relationship)
           c. Temporal proximity (within max_temporal_gap)
        3. Assign edge strengths based on rule strength × temporal decay
        4. Verify DAG acyclicity
        """
        if not events:
            return CausalGraph(
                h3_cell=h3_cell,
                nodes=[],
                edges=[],
                generated_at=datetime.now(timezone.utc),
            )

        # Build NetworkX DAG for structural operations
        G = nx.DiGraph()

        # Create nodes
        nodes: list[CausalNode] = []
        for i, event in enumerate(events):
            node_id = f"e{i}_{event.event_type}"
            node = event.to_causal_node(node_id)
            nodes.append(node)
            G.add_node(node_id, event=event, node=node)

        # Discover edges using rules and temporal ordering
        edges: list[CausalEdge] = []
        node_list = list(G.nodes(data=True))

        for i, (src_id, src_data) in enumerate(node_list):
            for j, (tgt_id, tgt_data) in enumerate(node_list):
                if i == j:
                    continue

                src_event: DetectedEvent = src_data["event"]
                tgt_event: DetectedEvent = tgt_data["event"]

                edge = self._evaluate_edge(src_id, src_event, tgt_id, tgt_event)
                if edge is not None:
                    # Check that adding this edge doesn't create a cycle
                    G.add_edge(src_id, tgt_id)
                    if nx.is_directed_acyclic_graph(G):
                        edges.append(edge)
                    else:
                        G.remove_edge(src_id, tgt_id)

        logger.info(
            "DAG built for %s: %d nodes, %d edges",
            h3_cell, len(nodes), len(edges),
        )

        return CausalGraph(
            h3_cell=h3_cell,
            nodes=nodes,
            edges=edges,
            generated_at=datetime.now(timezone.utc),
        )

    def _evaluate_edge(
        self,
        src_id: str,
        src_event: DetectedEvent,
        tgt_id: str,
        tgt_event: DetectedEvent,
    ) -> CausalEdge | None:
        """
        Evaluate whether a causal edge should exist between two events.

        Returns CausalEdge if conditions are met, None otherwise.
        """
        # Check temporal ordering: cause must precede or coincide with effect
        if src_event.timestamp > tgt_event.timestamp:
            return None

        # Check temporal proximity
        time_gap = tgt_event.timestamp - src_event.timestamp
        if time_gap > self._max_temporal_gap:
            return None

        # Check domain rule match
        rule_key = (src_event.event_type, tgt_event.event_type)
        rule = self._rule_index.get(rule_key)

        if rule is None:
            return None

        base_strength, mechanism = rule

        # Apply temporal decay: strength decreases with time gap
        max_gap_seconds = self._max_temporal_gap.total_seconds()
        gap_seconds = time_gap.total_seconds()
        temporal_factor = 1.0 - (gap_seconds / max_gap_seconds) if max_gap_seconds > 0 else 1.0

        # Final strength = base × temporal_decay × geometric mean of confidences
        confidence_factor = (src_event.confidence * tgt_event.confidence) ** 0.5
        strength = base_strength * temporal_factor * confidence_factor

        if strength < 0.1:
            return None  # Below minimum threshold

        return CausalEdge(
            source=src_id,
            target=tgt_id,
            strength=round(min(strength, 1.0), 3),
            mechanism=mechanism,
        )

    def add_rule(
        self,
        cause_type: str,
        effect_type: str,
        strength: float,
        mechanism: str,
    ) -> None:
        """Add a custom causal rule."""
        self._rules.append((cause_type, effect_type, strength, mechanism))
        self._rule_index[(cause_type, effect_type)] = (strength, mechanism)

    def get_ancestors(self, graph: CausalGraph, node_id: str) -> set[str]:
        """Find all ancestor nodes (direct and indirect causes) of a node."""
        G = self._to_networkx(graph)
        if node_id not in G:
            return set()
        return nx.ancestors(G, node_id)

    def get_descendants(self, graph: CausalGraph, node_id: str) -> set[str]:
        """Find all descendant nodes (direct and indirect effects) of a node."""
        G = self._to_networkx(graph)
        if node_id not in G:
            return set()
        return nx.descendants(G, node_id)

    def get_root_causes(self, graph: CausalGraph) -> list[CausalNode]:
        """Find nodes with no incoming edges — the root causes."""
        G = self._to_networkx(graph)
        roots = [n for n in G.nodes() if G.in_degree(n) == 0]
        node_map = {n.id: n for n in graph.nodes}
        return [node_map[r] for r in roots if r in node_map]

    @staticmethod
    def _to_networkx(graph: CausalGraph) -> nx.DiGraph:
        """Convert a CausalGraph to NetworkX DiGraph."""
        G = nx.DiGraph()
        for node in graph.nodes:
            G.add_node(node.id)
        for edge in graph.edges:
            G.add_edge(edge.source, edge.target, strength=edge.strength)
        return G
