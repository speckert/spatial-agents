/* global React, ReactDOM */
/*
  causal-flow/app.jsx
  Top-level shell — live version of the design-team handoff App.

  Differences from the handoff prototype:
    - Region list comes from /health, not a hardcoded array.
    - Data fetch hits /api/causal/layer?region=<id> instead of static JSON files.
    - Real 2-min refetch (the prototype just bumped a tick key).
    - "updated Ns ago" badge re-renders every 1s for accurate decay.
    - URL params:
        ?region=<id>      — preselect region on load
        ?node=<full_id>   — open detail popover for that node on load
        ?embed=1          — hide page chrome (brand, footer) for WKWebView embed
    - "Open on map" navigates to map.html?focus=<id> instead of alerting.

  Version History:
      0.1.0  2026-04-25  Initial port from design handoff package
                         (Causal Flow v0.4) — Claude 4.7
      0.1.1  2026-04-25  Active region persisted to / read from
                         localStorage so map.html and causal-flow.html
                         stay in sync. URL ?region= still wins for
                         deep links — Claude 4.7
      0.2.0  2026-04-26  Track regions_version returned by /health and
                         /api/causal/layer. On version drift (slot 1
                         swapped from another tab/client) re-discover
                         the region list and reload datasets so the
                         tab strip is never stuck on a removed region.
                         — Claude 4.7
*/

const { useState, useEffect, useMemo, useCallback } = React;

const API = window.location.hostname === 'agents.specktech.com'
  ? ''
  : 'http://127.0.0.1:8012';

function regionDisplayName(name) {
  if (!name) return '';
  return String(name).split('_')
    .map(p => p.length ? p[0].toUpperCase() + p.slice(1) : p)
    .join(' ');
}

function readUrlParams() {
  const p = new URLSearchParams(window.location.search);
  return {
    embed: p.get('embed') === '1',
    region: p.get('region') || null,
    node: p.get('node') || null,
  };
}

function App() {
  const initialParams = useMemo(readUrlParams, []);

  const [regions, setRegions] = useState([]);            // [{id,label}]
  const [region, setRegion] = useState(null);
  const [datasets, setDatasets] = useState({});           // id -> payload
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [lastUpdated, setLastUpdated] = useState(new Date());
  const [tickKey, setTickKey] = useState(0);
  const [, setNowTick] = useState(0);                     // forces re-render every 1s for relative time
  const [pendingFocusNode, setPendingFocusNode] = useState(initialParams.node);
  // regions_version content hash — server stamps every region-aware
  // response with it. A mismatch means slot 1 was swapped (by the +New
  // button in map.html, by an iOS v4 client, or by direct API call) and
  // we should re-discover the region list.
  const [regionsVersion, setRegionsVersion] = useState('');

  // Discover regions from /health. Pulled out as a function so we can
  // call it both at mount and whenever a poll detects a version drift.
  const discoverRegions = useCallback(() => {
    let cancelled = false;
    fetch(`${API}/health`)
      .then(res => res.json())
      .then(data => {
        if (cancelled) return;
        const ids = Object.keys(data.regions || {});
        const list = ids.map(id => ({ id, label: regionDisplayName(id) }));
        setRegions(list);
        if (data.regions_version) setRegionsVersion(data.regions_version);
        // Pick a sensible region: respect URL on first mount; else keep
        // current region if still present; else fall back to slot 0.
        setRegion(prev => {
          if (prev && ids.includes(prev)) return prev;
          let stored = null;
          try { stored = localStorage.getItem('spatial-agents-region'); } catch (_) {}
          if (initialParams.region && ids.includes(initialParams.region)) return initialParams.region;
          if (stored && ids.includes(stored)) return stored;
          return ids[0] || null;
        });
      })
      .catch(err => {
        if (cancelled) return;
        setError(`Could not reach /health: ${err.message}`);
        setLoading(false);
      });
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 1. Initial discovery
  useEffect(() => {
    const cancel = discoverRegions();
    return cancel;
  }, [discoverRegions]);

  // 2. Once we know the region list, load all regions' causal layers.
  // Each response carries `regions_version`; if any of them differs from
  // our cached value the region list itself is stale, so re-discover.
  const loadAll = useCallback(() => {
    if (!regions.length) return;
    Promise.all(regions.map(r =>
      fetch(`${API}/api/causal/layer?region=${encodeURIComponent(r.id)}`)
        .then(res => res.json())
        .catch(() => ({ nodes: [], edges: [], node_count: 0, edge_count: 0 }))
    )).then(results => {
      const map = {};
      regions.forEach((r, i) => { map[r.id] = results[i]; });
      setDatasets(map);
      setLastUpdated(new Date());
      setTickKey(k => k + 1);
      setLoading(false);

      // Version drift check — any response with a different version
      // means the active set has changed and we need a fresh /health.
      const seen = results
        .map(r => r && r.regions_version)
        .filter(Boolean);
      if (seen.length && regionsVersion) {
        const drift = seen.find(v => v !== regionsVersion);
        if (drift) {
          // eslint-disable-next-line no-console
          console.info('[causal-flow] regions_version drift', regionsVersion, '→', drift);
          discoverRegions();
        }
      } else if (seen.length && !regionsVersion) {
        // First successful poll — seed the cache.
        setRegionsVersion(seen[0]);
      }
    });
  }, [regions, regionsVersion, discoverRegions]);

  useEffect(() => { loadAll(); }, [loadAll]);

  // 3. Real refetch every 120 s
  useEffect(() => {
    const t = setInterval(loadAll, 120_000);
    return () => clearInterval(t);
  }, [loadAll]);

  // 4. Tick every 1 s so "updated Ns ago" stays accurate
  useEffect(() => {
    const t = setInterval(() => setNowTick(n => n + 1), 1000);
    return () => clearInterval(t);
  }, []);

  // 5. Honor ?node=<id> once data arrives
  useEffect(() => {
    if (!pendingFocusNode || !region || !datasets[region]) return;
    const found = (datasets[region].nodes || []).find(n => n.id === pendingFocusNode);
    if (found) {
      setSelected(found);
      setPendingFocusNode(null);
    }
  }, [pendingFocusNode, region, datasets]);

  const data = region ? datasets[region] : null;

  const counts = useMemo(() => {
    const c = {};
    regions.forEach(r => {
      const d = datasets[r.id];
      c[r.id] = d
        ? { nodes: d.node_count ?? (d.nodes ? d.nodes.length : 0),
            edges: d.edge_count ?? (d.edges ? d.edges.length : 0) }
        : null;
    });
    return c;
  }, [datasets, regions]);

  const stats = useMemo(() => {
    if (!data) return null;
    const nodes = data.nodes || [];
    const edges = data.edges || [];
    const byDomain = nodes.reduce((m, n) => (m[n.domain] = (m[n.domain] || 0) + 1, m), {});
    const mechs = new Set(edges.map(e => e.mechanism));
    return {
      nodes: nodes.length,
      edges: edges.length,
      mechs: mechs.size,
      domains: byDomain,
    };
  }, [data]);

  const selectRegion = useCallback((id) => {
    setRegion(id);
    try { localStorage.setItem('spatial-agents-region', id); } catch (_) {}
  }, []);

  const embed = initialParams.embed;
  const refreshSecs = Math.max(0, 120 - Math.floor((Date.now() - lastUpdated.getTime()) / 1000));

  return (
    <div className="app" data-embed={embed || undefined}>
      {!embed && (
        <header className="topbar">
          <div className="topbar__brand">
            <span className="dot" />
            <span className="topbar__title">Causal Flow</span>
            <span className="topbar__sub">live · agents.specktech.com</span>
          </div>
          <div className="topbar__spacer" />
          <div className="regions" role="tablist" aria-label="Region">
            {regions.map(r => (
              <button
                key={r.id}
                className="region"
                role="tab"
                aria-selected={r.id === region}
                onClick={() => selectRegion(r.id)}
              >
                {r.label}
                <span className="count">
                  {counts[r.id] ? `${counts[r.id].nodes}n · ${counts[r.id].edges}e` : '—'}
                </span>
              </button>
            ))}
          </div>
          <div className="live-badge" title={`Updated ${lastUpdated.toLocaleTimeString()}`}>
            <span className="pulse" />
            updated {fmtRelative(lastUpdated.toISOString())}
          </div>
        </header>
      )}

      {embed && regions.length > 1 && (
        <header className="topbar topbar--embed">
          <div className="regions" role="tablist" aria-label="Region">
            {regions.map(r => (
              <button
                key={r.id}
                className="region"
                role="tab"
                aria-selected={r.id === region}
                onClick={() => selectRegion(r.id)}
              >
                {r.label}
                <span className="count">
                  {counts[r.id] ? `${counts[r.id].nodes}n · ${counts[r.id].edges}e` : '—'}
                </span>
              </button>
            ))}
          </div>
          <div className="topbar__spacer" />
          <div className="live-badge" title={`Updated ${lastUpdated.toLocaleTimeString()}`}>
            <span className="pulse" />
            {fmtRelative(lastUpdated.toISOString())}
          </div>
        </header>
      )}

      {data && stats && (
        <div className="statusbar">
          <div className="stat">
            <span className="k">events</span>
            <span className="v">{stats.nodes}</span>
            <span className="sub">
              {Object.entries(stats.domains).map(([d, c]) => `${c} ${d.slice(0, 3)}`).join(' · ') || '—'}
            </span>
          </div>
          <div className="stat">
            <span className="k">edges</span>
            <span className="v">{stats.edges}</span>
            <span className="sub">
              {stats.edges > 0 ? `bundled into ${stats.mechs} mech${stats.mechs === 1 ? '' : 's'}` : 'no causal links'}
            </span>
          </div>
          <div className="stat">
            <span className="k">depth</span>
            <span className="v">{stats.edges > 0 ? 2 : 0}</span>
            <span className="sub">{stats.edges > 0 ? 'roots → effects' : 'flat'}</span>
          </div>
          <div className="stat">
            <span className="k">refresh</span>
            <span className="v">120s</span>
            <span className="sub">next in {refreshSecs}s</span>
          </div>
        </div>
      )}

      <main className="main" key={`${region}-${tickKey}`}>
        {error && (
          <div style={{ padding: 40, color: 'var(--warn)' }}>
            {error}
          </div>
        )}
        {loading && !error && (
          <div style={{ padding: 40, color: 'var(--ink-3)' }}>Loading live feed…</div>
        )}
        {!loading && !error && data && (
          (data.edges?.length || 0) === 0
            ? <EmptyState data={data} onSelectNode={setSelected} />
            : <CausalFlow data={data} onSelectNode={setSelected} />
        )}
        {!loading && !error && !data && region && (
          <div style={{ padding: 40, color: 'var(--ink-3)' }}>
            No data for {regionDisplayName(region)} yet.
          </div>
        )}
      </main>

      {!embed && (
        <footer className="legend">
          <span style={{ color: 'var(--ink-2)', fontWeight: 500 }}>Domains</span>
          <span className="swatch"><span className="sq" style={{ background: 'var(--d-weather)' }} />weather</span>
          <span className="swatch"><span className="sq" style={{ background: 'var(--d-maritime)' }} />maritime</span>
          <span className="swatch"><span className="sq" style={{ background: 'var(--d-aviation)' }} />aviation</span>
          <span className="swatch"><span className="sq" style={{ background: 'var(--d-airspace)' }} />airspace</span>
          <span style={{ flex: 1 }} />
          <span className="swatch">circle ⌀ = confidence</span>
          <span className="swatch">bar = strength</span>
          <span className="swatch">click mechanism row to expand edges</span>
        </footer>
      )}

      {selected && <NodeDetails node={selected} embed={embed} onClose={() => setSelected(null)} />}
    </div>
  );
}

function NodeDetails({ node, embed, onClose }) {
  const isVessel = node.event_type === 'vessel_loitering';
  const v = isVessel ? compactVesselLabel(node.label) : null;
  const w = node.event_type === 'weather_alert' ? compactWeatherLabel(node.label) : null;
  const title = isVessel ? v.name : (w ? w.kind : node.label);

  const openOnMap = () => {
    // Embed mode: tell native side via postMessage (iOS WKWebView userContentController),
    // and also navigate as a fallback for plain-browser viewers.
    try {
      const msg = { type: 'openOnMap', nodeId: node.id, lat: node.lat, lng: node.lng };
      if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.spatialAgents) {
        window.webkit.messageHandlers.spatialAgents.postMessage(msg);
        if (embed) return;  // native will handle the map switch
      }
    } catch (_) { /* fall through to navigation */ }
    window.location.href = `map.html?focus=${encodeURIComponent(node.id)}`;
  };

  return (
    <div className="details" data-domain={node.domain} role="dialog" aria-label="Event details">
      <div className="details__head">
        <div>
          <div className="details__title">{title}</div>
          <div className="details__sub">
            {shortId(node.id)} · {node.event_type} · {fmtTime(node.timestamp)}
          </div>
        </div>
        <button className="details__close" onClick={onClose} aria-label="Close">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        </button>
      </div>
      <div className="details__body">
        <div className="kv"><span className="k">domain</span><span className="v">{node.domain}</span></div>
        <div className="kv"><span className="k">confidence</span><span className="v">{pct(node.observed_value)}</span></div>
        {isVessel && <div className="kv"><span className="k">speed</span><span className="v">{v.speed}</span></div>}
        {w && w.until && <div className="kv"><span className="k">until</span><span className="v">{w.until}</span></div>}
        {Number.isFinite(node.lat) && Number.isFinite(node.lng) && (
          <div className="kv"><span className="k">lat / lng</span>
            <span className="v">{node.lat.toFixed(4)}, {node.lng.toFixed(4)}</span></div>
        )}
        <div className="kv"><span className="k">timestamp</span>
          <span className="v">{new Date(node.timestamp).toISOString().replace('T', ' ').slice(0, 19)}Z</span></div>
        <button className="details__action" onClick={openOnMap}>
          Open on map
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
            <path d="M4 2h6v6M10 2L4 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </button>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App />);
