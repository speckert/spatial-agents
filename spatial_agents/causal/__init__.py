"""
Causal — Structural causal model construction and serialization.

Implements Pearl's SCM framework applied to geospatial event streams.
The Python layer builds causal graphs from observed patterns;
the on-device FM narrates them into actionable intelligence.

"Know the How, Show the Why"

Version History:
    0.1.0  2026-03-28  Initial causal package with event detector, DAG builder,
                       do-calculus intervention engine, and graph serializer
"""

from spatial_agents.causal.event_detector import EventDetector
from spatial_agents.causal.dag_builder import DAGBuilder
from spatial_agents.causal.intervention import InterventionEngine
from spatial_agents.causal.graph_serializer import GraphSerializer

__all__ = ["EventDetector", "DAGBuilder", "InterventionEngine", "GraphSerializer"]
