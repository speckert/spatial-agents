/* global React */
const { useState, useEffect, useMemo, useRef, useCallback } = React;

// shorten labels for compact display
function compactWeatherLabel(label) {
  // "Flood Warning: Flood Warning issued April 25 at 9:00PM CDT until April 29 at 7:00PM CDT by NWS "
  // -> { kind: "Flood Warning", office: "NWS", time: "9:00PM CDT" }
  const m = label.match(/^([A-Z][A-Za-z ]+?):\s*\1\s+issued\s+([^]+?)(?:\s+by\s+(.+))?$/);
  if (!m) return { kind: label, office: "", time: "" };
  const kind = m[1];
  const issued = m[2];
  const office = (m[3] || "").trim() || "NWS";
  const tMatch = issued.match(/at\s+([0-9:APMapm]+\s+[A-Z]{2,4})/);
  const untilMatch = issued.match(/until\s+(.+)$/);
  return {
    kind,
    office: office.replace(/^NWS\s+/i, ""),
    time: tMatch ? tMatch[1] : "",
    until: untilMatch ? untilMatch[1] : ""
  };
}

function compactVesselLabel(label) {
  // "Vessel ALPENA loitering at 0.2 knots"
  const m = label.match(/^Vessel\s+(.+?)\s+loitering\s+at\s+(\S+)\s+knots/i);
  if (!m) return { name: label, speed: "" };
  const speed = m[2] === "nan" ? "—" : `${m[2]} kn`;
  return { name: m[1], speed };
}

function shortId(fullId) {
  // chicago::e6_dark_vessel_gap -> e6
  const m = fullId.match(/::([a-z]\d+)/);
  return m ? m[1] : fullId.slice(0, 6);
}

function fmtTime(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch { return ""; }
}

function fmtRelative(iso) {
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return `${Math.round(diff)}s ago`;
  if (diff < 3600) return `${Math.round(diff/60)}m ago`;
  if (diff < 86400) return `${Math.round(diff/3600)}h ago`;
  return `${Math.round(diff/86400)}d ago`;
}

function pct(v) { return `${Math.round(v * 100)}%`; }

// build the bipartite mechanism-grouped graph
function buildGraph(data) {
  const nodes = data.nodes;
  const edges = data.edges || [];
  const nodeById = new Map(nodes.map(n => [n.id, n]));

  // group edges by mechanism + (sourceType -> targetType) signature
  const bundles = new Map();
  for (const e of edges) {
    const key = e.mechanism;
    if (!bundles.has(key)) bundles.set(key, { mechanism: key, edges: [], strengths: [] });
    bundles.get(key).edges.push(e);
    bundles.get(key).strengths.push(e.strength);
  }

  // identify sources (nodes with outbound edges) and effects (nodes with inbound)
  const inbound = new Map();
  const outbound = new Map();
  for (const e of edges) {
    outbound.set(e.source, (outbound.get(e.source) || 0) + 1);
    inbound.set(e.target, (inbound.get(e.target) || 0) + 1);
  }

  const causes = nodes.filter(n => outbound.has(n.id));
  const effects = nodes.filter(n => inbound.has(n.id));
  const orphans = nodes.filter(n => !outbound.has(n.id) && !inbound.has(n.id));

  const bundleList = [...bundles.values()].map(b => ({
    ...b,
    avgStrength: b.strengths.reduce((a,c) => a+c, 0) / b.strengths.length,
    maxStrength: Math.max(...b.strengths),
    sourceIds: [...new Set(b.edges.map(e => e.source))],
    targetIds: [...new Set(b.edges.map(e => e.target))]
  })).sort((a,b) => b.avgStrength - a.avgStrength);

  return { nodes, edges, nodeById, causes, effects, orphans, bundles: bundleList, outbound, inbound };
}

window.compactWeatherLabel = compactWeatherLabel;
window.compactVesselLabel = compactVesselLabel;
window.shortId = shortId;
window.fmtTime = fmtTime;
window.fmtRelative = fmtRelative;
window.pct = pct;
window.buildGraph = buildGraph;
