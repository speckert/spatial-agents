/* global React */
const { useMemo: useMemoEmpty } = React;

function EmptyState({ data, onSelectNode }) {
  const groups = useMemoEmpty(() => {
    const byDomain = new Map();
    for (const n of data.nodes) {
      if (!byDomain.has(n.domain)) byDomain.set(n.domain, []);
      byDomain.get(n.domain).push(n);
    }
    const order = ["weather", "maritime", "aviation", "airspace"];
    return order
      .filter(d => byDomain.has(d))
      .map(d => ({ domain: d, items: byDomain.get(d) }))
      .concat([...byDomain.entries()]
        .filter(([d]) => !order.includes(d))
        .map(([d, items]) => ({ domain: d, items })));
  }, [data]);

  return (
    <section className="empty">
      <header className="empty__head">
        <div className="empty__title">No causal structure detected</div>
        <div className="empty__subtitle">
          {data.node_count} events observed, 0 causal edges. Showing detected events grouped by domain — analyst review recommended.
        </div>
      </header>
      <div className="empty__body">
        {groups.map(g => (
          <div key={g.domain} className="dom-group">
            <div className="dom-group__head" data-domain={g.domain}>
              <span className="swatch" style={{ background: `var(--bar)` }} />
              <span className="name">{g.domain}</span>
              <span className="ct">{g.items.length}</span>
            </div>
            <div className="dom-group__list">
              {g.items.map(n => (
                <EmptyRow key={n.id} node={n} onClick={() => onSelectNode(n)} />
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function EmptyRow({ node, onClick }) {
  const isVessel = node.event_type === "vessel_loitering";
  const v = isVessel ? compactVesselLabel(node.label) : null;
  const title = isVessel ? v.name : node.label;
  const sub = isVessel ? v.speed : node.event_type.replace(/_/g, " ");
  return (
    <button className="effect" data-domain={node.domain} onClick={onClick} style={{ textAlign: "left", width: "100%", border: "1px solid var(--line)" }}>
      <div className="effect__top">
        <div className="effect__title">{title}</div>
        <div className="effect__id">{shortId(node.id)}</div>
      </div>
      <div className="effect__meta">
        <span className="effect__type">{node.event_type}</span>
        {sub && <span style={{ color: "var(--ink-3)" }}>{sub}</span>}
        <span className="effect__conf" title="confidence">
          <span className="bar" style={{ "--pct": pct(node.observed_value) }} />
          {pct(node.observed_value)}
        </span>
      </div>
    </button>
  );
}

window.EmptyState = EmptyState;
