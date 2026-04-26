/* global React */
const { useState: useFlowState, useMemo: useFlowMemo, useRef: useFlowRef, useEffect: useFlowEffect } = React;

/**
 * Real DAG layout.
 * Sugiyama-style layered layout customized for the actual data shape:
 *   layer 0: causes (root events, no inbound)
 *   layer 1: mechanism hubs (one per unique mechanism string)
 *   layer 2: effects (downstream events)
 *   layer "free": unattributed events (no edges)
 *
 * Original edges (cause -> effect) are routed through the mechanism hub of that edge,
 * so the visual is cause -> hub -> effect, with hub showing the mechanism text and
 * acting as a clickable bundle. This keeps it readable when 5x12 = 60 edges fan
 * through 1 mechanism.
 */

function layoutGraph(graph, opts) {
  const W = opts.width;
  const padX = 32;
  const padY = 90;

  // column x positions
  const xCause   = padX + 230;             // cause node x — leave room for left-anchored labels
  const xMech    = W / 2;                  // hub x
  const xEffect  = W - padX - 230;         // effect node x — leave room for right-anchored labels

  const causeR  = (n) => 8 + (n.observed_value || 0.5) * 10;   // 8..18
  const effectR = (n) => 6 + (n.observed_value || 0.5) * 10;   // 6..16
  const hubR    = (b) => 14 + Math.min(20, Math.log2(b.edges.length + 1) * 4);

  // y placement: distribute evenly per layer
  function placeColumn(items, x, top, gap) {
    const positions = new Map();
    items.forEach((it, i) => {
      positions.set(it.id || it.mechanism, { x, y: top + i * gap });
    });
    return positions;
  }

  // sort effects by sum of inbound strengths (most "influenced" up top)
  const effectScore = new Map();
  for (const e of graph.edges) {
    effectScore.set(e.target, (effectScore.get(e.target) || 0) + e.strength);
  }
  const sortedEffects = graph.effects.slice().sort((a,b) => (effectScore.get(b.id)||0) - (effectScore.get(a.id)||0));
  // sort causes by total outbound strength
  const causeScore = new Map();
  for (const e of graph.edges) {
    causeScore.set(e.source, (causeScore.get(e.source) || 0) + e.strength);
  }
  const sortedCauses = graph.causes.slice().sort((a,b) => (causeScore.get(b.id)||0) - (causeScore.get(a.id)||0));
  // sort mechanisms by avg strength (already done)
  const sortedMechs = graph.bundles.slice();

  const causeGap  = 88;
  const effectGap = 56;
  const mechGap   = 120;

  const causePos  = placeColumn(sortedCauses,  xCause,  padY + 30, causeGap);
  const effectPos = placeColumn(sortedEffects, xEffect, padY + 30, effectGap);

  // mechanism y = centroid of its endpoints' y
  const mechPos = new Map();
  let mechY = padY + 30;
  sortedMechs.forEach((b, i) => {
    const ys = [];
    for (const id of b.sourceIds) if (causePos.has(id))  ys.push(causePos.get(id).y);
    for (const id of b.targetIds) if (effectPos.has(id)) ys.push(effectPos.get(id).y);
    const cy = ys.length ? ys.reduce((a,c)=>a+c,0) / ys.length : (mechY);
    mechPos.set(b.mechanism, { x: xMech, y: cy });
    mechY += mechGap;
  });
  // resolve overlaps (simple O(n^2) untangle: push apart if too close)
  {
    const arr = sortedMechs.map(b => ({ key: b.mechanism, ...mechPos.get(b.mechanism) }));
    arr.sort((a,b) => a.y - b.y);
    const minGap = 70;
    for (let i = 1; i < arr.length; i++) {
      if (arr[i].y - arr[i-1].y < minGap) arr[i].y = arr[i-1].y + minGap;
    }
    arr.forEach(p => mechPos.set(p.key, { x: xMech, y: p.y }));
  }

  const causeBottom  = sortedCauses.length  ? padY + 30 + (sortedCauses.length - 1) * causeGap  + 30 : padY + 60;
  const effectBottom = sortedEffects.length ? padY + 30 + (sortedEffects.length - 1) * effectGap + 30 : padY + 60;
  const mechBottom = sortedMechs.length ? Math.max(...[...mechPos.values()].map(p => p.y)) + 30 : padY + 60;
  const height = Math.max(causeBottom, effectBottom, mechBottom, 360);

  return {
    width: W,
    height,
    xCause, xMech, xEffect,
    causePos, effectPos, mechPos,
    sortedCauses, sortedEffects, sortedMechs,
    causeR, effectR, hubR,
    causeScore, effectScore
  };
}

function CausalFlow({ data, onSelectNode }) {
  const graph = useFlowMemo(() => buildGraph(data), [data]);
  const wrapRef = useFlowRef(null);
  const [width, setWidth] = useFlowState(1100);
  const [hover, setHover] = useFlowState(null); // {kind, id}
  const [openMech, setOpenMech] = useFlowState(null); // mechanism string

  useFlowEffect(() => {
    if (!wrapRef.current) return;
    const ro = new ResizeObserver(entries => {
      for (const ent of entries) {
        const w = ent.contentRect.width;
        if (w) setWidth(Math.max(720, Math.floor(w)));
      }
    });
    ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, []);

  const layout = useFlowMemo(() => layoutGraph(graph, { width }), [graph, width]);

  // hover dimming
  const isCauseHi = (id) => {
    if (!hover) return null;
    if (hover.kind === "cause")  return hover.id === id;
    if (hover.kind === "effect") return graph.edges.some(e => e.target === hover.id && e.source === id);
    if (hover.kind === "mech")   return graph.bundles.find(b => b.mechanism === hover.id)?.sourceIds.includes(id);
    return null;
  };
  const isEffectHi = (id) => {
    if (!hover) return null;
    if (hover.kind === "effect") return hover.id === id;
    if (hover.kind === "cause")  return graph.edges.some(e => e.source === hover.id && e.target === id);
    if (hover.kind === "mech")   return graph.bundles.find(b => b.mechanism === hover.id)?.targetIds.includes(id);
    return null;
  };
  const isMechHi = (mech) => {
    if (!hover) return null;
    if (hover.kind === "mech")   return hover.id === mech.mechanism;
    if (hover.kind === "cause")  return mech.sourceIds.includes(hover.id);
    if (hover.kind === "effect") return mech.targetIds.includes(hover.id);
    return null;
  };
  const isEdgeHi = (e) => {
    if (!hover) return null;
    if (hover.kind === "cause")  return e.source === hover.id;
    if (hover.kind === "effect") return e.target === hover.id;
    if (hover.kind === "mech")   return e.mechanism === hover.id;
    return null;
  };

  return (
    <div className="dag-wrap">
      <div className="dag-canvas" ref={wrapRef}>
        <svg
          width={layout.width}
          height={layout.height}
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          className="dag-svg"
          xmlns="http://www.w3.org/2000/svg"
        >
          <defs>
            <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5"
              markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M0,1 L8,5 L0,9 z" fill="currentColor" />
            </marker>
            <pattern id="dotgrid" x="0" y="0" width="22" height="22" patternUnits="userSpaceOnUse">
              <circle cx="1" cy="1" r="0.7" fill="rgba(20,22,28,.05)" />
            </pattern>
          </defs>

          <rect x="0" y="0" width={layout.width} height={layout.height} fill="url(#dotgrid)" />

          {/* column rails */}
          <ColumnRail x={layout.xCause}  height={layout.height} label="CAUSES"     count={graph.causes.length} />
          <ColumnRail x={layout.xMech}   height={layout.height} label="MECHANISMS" count={graph.bundles.length} />
          <ColumnRail x={layout.xEffect} height={layout.height} label="EFFECTS"    count={graph.effects.length} />

          {/* edges: cause -> hub */}
          <g className="edges">
            {graph.edges.map((e, i) => {
              const cp = layout.causePos.get(e.source);
              const mp = layout.mechPos.get(e.mechanism);
              const ep = layout.effectPos.get(e.target);
              if (!cp || !mp || !ep) return null;
              const hi = isEdgeHi(e);
              const dimmed = hi === false;
              const active = hi === true;
              const sw = 0.6 + e.strength * 6;
              const op = dimmed ? 0.06 : (active ? 0.95 : 0.32);
              return (
                <g key={i} className="edge" data-active={active || undefined}>
                  <path
                    d={bezier(cp.x + layout.causeR(graph.nodeById.get(e.source)), cp.y, mp.x - layout.hubR({edges: graph.bundles.find(b=>b.mechanism===e.mechanism).edges}), mp.y)}
                    stroke="var(--edge)" strokeWidth={sw} fill="none" opacity={op}
                  />
                  <path
                    d={bezier(mp.x + layout.hubR({edges: graph.bundles.find(b=>b.mechanism===e.mechanism).edges}), mp.y, ep.x - layout.effectR(graph.nodeById.get(e.target)), ep.y)}
                    stroke="var(--edge)" strokeWidth={sw} fill="none" opacity={op}
                    markerEnd="url(#arrow)" style={{ color: "var(--edge)" }}
                  />
                </g>
              );
            })}
          </g>

          {/* mechanism hubs */}
          <g className="hubs">
            {layout.sortedMechs.map(b => {
              const p = layout.mechPos.get(b.mechanism);
              const r = layout.hubR(b);
              const hi = isMechHi(b);
              const dimmed = hi === false;
              const active = hi === true;
              return (
                <g key={b.mechanism}
                   transform={`translate(${p.x},${p.y})`}
                   className="hub"
                   data-active={active || undefined}
                   data-dimmed={dimmed || undefined}
                   onMouseEnter={() => setHover({ kind: "mech", id: b.mechanism })}
                   onMouseLeave={() => setHover(null)}
                   onClick={() => setOpenMech(openMech === b.mechanism ? null : b.mechanism)}
                   style={{ cursor: "pointer" }}>
                  <circle r={r + 6} className="hub__halo" />
                  <circle r={r} className="hub__core" />
                  <text textAnchor="middle" dy={4} className="hub__count">{b.edges.length}</text>
                  <text y={r + 18} textAnchor="middle" className="hub__avg">avg {b.avgStrength.toFixed(2)}</text>
                  <foreignObject x={-150} y={-r - 80} width={300} height={70} style={{ pointerEvents: "none", overflow: "visible" }}>
                    <div xmlns="http://www.w3.org/1999/xhtml" className="hub__label">
                      {b.mechanism}
                    </div>
                  </foreignObject>
                </g>
              );
            })}
          </g>

          {/* causes */}
          <g className="causes">
            {layout.sortedCauses.map(n => {
              const p = layout.causePos.get(n.id);
              const r = layout.causeR(n);
              const hi = isCauseHi(n.id);
              const dimmed = hi === false;
              const active = hi === true;
              const isWeather = n.event_type === "weather_alert";
              const w = isWeather ? compactWeatherLabel(n.label) : null;
              const title = isWeather ? `${w.kind}` : n.label;
              const sub = isWeather ? `${w.office}${w.time ? " · " + w.time : ""}` : "";
              return (
                <g key={n.id}
                   transform={`translate(${p.x},${p.y})`}
                   className="node node--cause"
                   data-domain={n.domain}
                   data-active={active || undefined}
                   data-dimmed={dimmed || undefined}
                   onMouseEnter={() => setHover({ kind: "cause", id: n.id })}
                   onMouseLeave={() => setHover(null)}
                   onClick={() => onSelectNode(n)}
                   style={{ cursor: "pointer" }}>
                  <circle r={r} className="node__circle" />
                  <circle r={r - 3} className="node__inner" />
                  <text x={-(r + 8)} y={4} textAnchor="end" className="node__title">{title}</text>
                  {sub && <text x={-(r + 8)} y={20} textAnchor="end" className="node__sub">{sub}</text>}
                  <text x={-(r + 8)} y={-r - 4} textAnchor="end" className="node__id">{shortId(n.id)} · {pct(n.observed_value)}</text>
                </g>
              );
            })}
          </g>

          {/* effects */}
          <g className="effects">
            {layout.sortedEffects.map(n => {
              const p = layout.effectPos.get(n.id);
              const r = layout.effectR(n);
              const hi = isEffectHi(n.id);
              const dimmed = hi === false;
              const active = hi === true;
              const v = n.event_type === "vessel_loitering" ? compactVesselLabel(n.label) : null;
              const title = v ? v.name : n.label;
              const sub = v ? v.speed : n.event_type.replace(/_/g, " ");
              return (
                <g key={n.id}
                   transform={`translate(${p.x},${p.y})`}
                   className="node node--effect"
                   data-domain={n.domain}
                   data-active={active || undefined}
                   data-dimmed={dimmed || undefined}
                   onMouseEnter={() => setHover({ kind: "effect", id: n.id })}
                   onMouseLeave={() => setHover(null)}
                   onClick={() => onSelectNode(n)}
                   style={{ cursor: "pointer" }}>
                  <circle r={r} className="node__circle" />
                  <circle r={r - 3} className="node__inner" />
                  <text x={r + 8} y={4} className="node__title">{title}</text>
                  {sub && <text x={r + 8} y={20} className="node__sub">{sub}</text>}
                  <text x={r + 8} y={-r - 4} className="node__id">{shortId(n.id)} · {pct(n.observed_value)}</text>
                </g>
              );
            })}
          </g>
        </svg>

        {openMech && (
          <MechPopover
            mech={graph.bundles.find(b => b.mechanism === openMech)}
            graph={graph}
            onClose={() => setOpenMech(null)}
          />
        )}
      </div>

      {graph.orphans.length > 0 && (
        <UnattributedSection nodes={graph.orphans} onSelectNode={onSelectNode} />
      )}
    </div>
  );
}

function ColumnRail({ x, height, label, count }) {
  return (
    <g className="rail">
      <line x1={x} y1={0} x2={x} y2={height} stroke="var(--line)" strokeDasharray="2 4" opacity="0.6" />
      <g transform={`translate(${x}, 14)`}>
        <rect x={-58} y={-2} width={116} height={20} rx={10} className="rail__chip" />
        <text textAnchor="middle" className="rail__label">
          {label} <tspan className="rail__count">{count}</tspan>
        </text>
      </g>
    </g>
  );
}

function bezier(x1, y1, x2, y2) {
  const dx = (x2 - x1) * 0.5;
  return `M${x1},${y1} C${x1+dx},${y1} ${x2-dx},${y2} ${x2},${y2}`;
}

function MechPopover({ mech, graph, onClose }) {
  if (!mech) return null;
  return (
    <div className="mech-pop" role="dialog">
      <div className="mech-pop__head">
        <div>
          <div className="mech-pop__kicker">MECHANISM · {mech.edges.length} edges</div>
          <div className="mech-pop__title">{mech.mechanism}</div>
          <div className="mech-pop__meta">
            avg <b>{mech.avgStrength.toFixed(3)}</b> · max <b>{mech.maxStrength.toFixed(3)}</b> ·
            {" "}{mech.sourceIds.length} sources → {mech.targetIds.length} targets
          </div>
        </div>
        <button className="details__close" onClick={onClose} aria-label="Close">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        </button>
      </div>
      <div className="mech-pop__rows">
        {mech.edges.slice().sort((a,b) => b.strength - a.strength).map((e, i) => {
          const s = graph.nodeById.get(e.source);
          const t = graph.nodeById.get(e.target);
          const sName = s?.event_type === "weather_alert" ? compactWeatherLabel(s.label).office : compactVesselLabel(s?.label || "").name;
          const tName = compactVesselLabel(t?.label || "").name || t?.label;
          return (
            <div key={i} className="edge-row">
              <span className="src">{shortId(e.source)} {sName}</span>
              <span className="arr">→</span>
              <span className="tgt">{shortId(e.target)} {tName}</span>
              <span className="s">{e.strength.toFixed(3)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function UnattributedSection({ nodes, onSelectNode }) {
  const groups = (() => {
    const byDomain = new Map();
    for (const n of nodes) {
      if (!byDomain.has(n.domain)) byDomain.set(n.domain, []);
      byDomain.get(n.domain).push(n);
    }
    const order = ["weather", "maritime", "aviation", "airspace"];
    return order.filter(d => byDomain.has(d)).map(d => ({ domain: d, items: byDomain.get(d) }))
      .concat([...byDomain.entries()].filter(([d]) => !order.includes(d)).map(([d, items]) => ({ domain: d, items })));
  })();
  return (
    <section className="unattributed">
      <header className="unattributed__head">
        <div>
          <div className="unattributed__title">Unattributed events <span className="unattributed__count">{nodes.length}</span></div>
          <div className="unattributed__sub">Detected, but no inferred cause or downstream effect in the current window</div>
        </div>
      </header>
      <div className="unattributed__body">
        {groups.map(g => (
          <div key={g.domain} className="dom-group">
            <div className="dom-group__head" data-domain={g.domain}>
              <span className="swatch" style={{ background: `var(--bar)` }} />
              <span className="name">{g.domain}</span>
              <span className="ct">{g.items.length}</span>
            </div>
            <div className="dom-group__list">
              {g.items.map(n => {
                const isVessel = n.event_type === "vessel_loitering";
                const isWeather = n.event_type === "weather_alert";
                const v = isVessel ? compactVesselLabel(n.label) : null;
                const w = isWeather ? compactWeatherLabel(n.label) : null;
                const title = isVessel ? v.name : (isWeather ? `${w.kind} · ${w.office}${w.time ? " · " + w.time : ""}` : n.label);
                const sub = isVessel ? v.speed : "";
                return (
                  <button
                    key={n.id}
                    className="effect"
                    data-domain={n.domain}
                    onClick={() => onSelectNode(n)}
                    style={{ textAlign: "left", width: "100%" }}
                  >
                    <div className="effect__top">
                      <div className="effect__title">{title}</div>
                      <div className="effect__id">{shortId(n.id)}</div>
                    </div>
                    <div className="effect__meta">
                      <span className="effect__type">{n.event_type}</span>
                      {sub && <span style={{ color: "var(--ink-3)" }}>{sub}</span>}
                      <span className="effect__conf" title="confidence">
                        <span className="bar" style={{ "--pct": pct(n.observed_value) }} />
                        {pct(n.observed_value)}
                      </span>
                    </div>
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

window.CausalFlow = CausalFlow;
