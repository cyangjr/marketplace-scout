import { FormEvent, useCallback, useEffect, useState } from "react";

type Status = {
  running: boolean;
  last_run_at: string | null;
  last_error: string | null;
  worker_status: string;
  fb_session_ok: boolean;
  fb_circuit_open?: boolean;
  fb_circuit_until?: string | null;
  fb_circuit_reason?: string | null;
  fb_circuit_remaining_hours?: number | null;
  fb_poll_minutes?: number;
  groq_configured: boolean;
  gemini_configured: boolean;
  discord_configured: boolean;
};

type Hunt = {
  id: number;
  query: string;
  max_price: number | null;
  max_miles: number;
  home_zip: string;
  sources: string[];
  kind?: string;
  exclude_keywords: string[];
  image_critical: boolean;
  poll_interval_minutes: number;
  active: boolean;
  last_polled_at: string | null;
  match_count: number;
};

type Match = {
  id: number;
  hunt_id: number;
  hunt_query: string;
  title: string;
  url: string;
  price: number | null;
  source: string;
  images: string[];
  confidence: number;
  reason: string;
  drive_miles: number | null;
  tier_used: string;
  price_outlier: string | null;
  dismissed: boolean;
  saved: boolean;
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json() as Promise<T>;
}

function Dot({ kind }: { kind: "ok" | "warn" | "bad" }) {
  return <span className={`dot ${kind}`} />;
}

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [hunts, setHunts] = useState<Hunt[]>([]);
  const [matches, setMatches] = useState<Match[]>([]);
  const [savedOnly, setSavedOnly] = useState(false);
  const [polling, setPolling] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [maxPrice, setMaxPrice] = useState("400");
  const [maxMiles, setMaxMiles] = useState("25");
  const [homeZip, setHomeZip] = useState("10001");
  const [srcCl, setSrcCl] = useState(true);
  const [srcFb, setSrcFb] = useState(true);
  const [srcSd, setSrcSd] = useState(true);
  const [srcRd, setSrcRd] = useState(true);
  const [huntKind, setHuntKind] = useState<"local" | "online">("local");
  const [imageCritical, setImageCritical] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [s, h, m] = await Promise.all([
        api<Status>("/api/status"),
        api<Hunt[]>("/api/hunts"),
        api<Match[]>(`/api/matches?saved=${savedOnly}`),
      ]);
      setStatus(s);
      setHunts(h);
      setMatches(m);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [savedOnly]);

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), 15000);
    return () => clearInterval(id);
  }, [refresh]);

  async function onCreate(e: FormEvent) {
    e.preventDefault();
    const sources =
      huntKind === "online"
        ? [...(srcSd ? ["slickdeals"] : []), ...(srcRd ? ["reddit"] : [])]
        : [...(srcFb ? ["facebook"] : []), ...(srcCl ? ["craigslist"] : [])];
    if (!query.trim() || sources.length === 0) return;
    await api("/api/hunts", {
      method: "POST",
      body: JSON.stringify({
        query: query.trim(),
        kind: huntKind,
        max_price: maxPrice ? Number(maxPrice) : null,
        ...(huntKind === "local" ? { max_miles: Number(maxMiles) || 25 } : {}),
        home_zip: homeZip.trim(),
        sources,
        image_critical: imageCritical,
      }),
    });
    setQuery("");
    await refresh();
  }

  async function toggleHunt(h: Hunt) {
    await api(`/api/hunts/${h.id}/${h.active ? "pause" : "resume"}`, {
      method: "POST",
    });
    await refresh();
  }

  async function runPoll() {
    setPolling(true);
    try {
      await api("/api/poll?force=true", { method: "POST" });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPolling(false);
    }
  }

  async function resetFbCircuit() {
    try {
      await api("/api/fb/reset-circuit", { method: "POST" });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function flagMatch(id: number, body: { dismissed?: boolean; saved?: boolean }) {
    await api(`/api/matches/${id}/flags`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    await refresh();
  }

  const fbKind = status?.fb_circuit_open
    ? "bad"
    : status?.fb_session_ok
      ? "ok"
      : "warn";
  const fbLabel = status?.fb_circuit_open
    ? `FB paused${
        status.fb_circuit_remaining_hours != null
          ? ` (~${status.fb_circuit_remaining_hours.toFixed(1)}h)`
          : ""
      }`
    : status?.fb_session_ok
      ? "FB session OK"
      : "FB needs login";
  const workerKind =
    status?.worker_status === "running"
      ? "ok"
      : status?.last_error
        ? "bad"
        : "ok";

  return (
    <>
      <h1 className="brand">Marketplace Scout</h1>
      <p className="lede">
        Hunt local pickup on Facebook Marketplace and Craigslist, or national online
        deals from Slickdeals and Reddit. Verified matches only — hard filters, Groq
        text, then Gemini vision when needed. Online hunts skip the mile check.
      </p>

      <div className="status-strip">
        <span className="pill">
          <Dot kind={workerKind} /> Worker {status?.worker_status ?? "…"}
        </span>
        <span className="pill">
          <Dot kind={fbKind} /> {fbLabel}
        </span>
        <span className="pill">
          <Dot kind={status?.groq_configured ? "ok" : "warn"} /> Groq
        </span>
        <span className="pill">
          <Dot kind={status?.gemini_configured ? "ok" : "warn"} /> Gemini
        </span>
        <span className="pill">
          <Dot kind={status?.discord_configured ? "ok" : "warn"} /> Discord
        </span>
        {status?.fb_poll_minutes != null && (
          <span className="pill">FB every {status.fb_poll_minutes}m</span>
        )}
        {status?.last_run_at && (
          <span className="pill">Last poll {new Date(status.last_run_at).toLocaleString()}</span>
        )}
        <button className="btn small ghost" onClick={() => void runPoll()} disabled={polling}>
          {polling ? "Polling…" : "Poll now"}
        </button>
        {status?.fb_circuit_open && (
          <button className="btn small ghost" onClick={() => void resetFbCircuit()}>
            Reset FB circuit
          </button>
        )}
      </div>

      {error && <p className="empty" style={{ color: "var(--danger)" }}>{error}</p>}
      {status?.fb_circuit_open && status.fb_circuit_reason && (
        <p className="empty" style={{ color: "var(--warn)" }}>
          Facebook circuit: {status.fb_circuit_reason}. Craigslist still runs. Re-login with{" "}
          <code>python -m scout.sources.fb_login</code>, then reset the circuit.
        </p>
      )}
      {status?.last_error && !status?.fb_circuit_open && (
        <p className="empty" style={{ color: "var(--warn)" }}>Last error: {status.last_error}</p>
      )}

      <div className="layout">
        <section className="panel">
          <h2>Hunts</h2>
          <form className="hunt-form" onSubmit={(e) => void onCreate(e)}>
            <label>
              What are you hunting?
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder='e.g. Aeron chair under $400'
                required
              />
            </label>
            <div>
              <span className="field-label">Hunt type</span>
              <div className="kind-toggle" role="group" aria-label="Hunt type">
                <button
                  type="button"
                  className={`btn small ${huntKind === "local" ? "" : "ghost"}`}
                  onClick={() => setHuntKind("local")}
                >
                  Local
                </button>
                <button
                  type="button"
                  className={`btn small ${huntKind === "online" ? "" : "ghost"}`}
                  onClick={() => setHuntKind("online")}
                >
                  Online
                </button>
              </div>
            </div>
            <div className={huntKind === "local" ? "row-2" : undefined}>
              <label>
                Max price
                <input value={maxPrice} onChange={(e) => setMaxPrice(e.target.value)} />
              </label>
              {huntKind === "local" && (
                <label>
                  Max miles
                  <input value={maxMiles} onChange={(e) => setMaxMiles(e.target.value)} />
                </label>
              )}
            </div>
            {huntKind === "online" && (
              <p className="hint">National feeds — distance is not applied.</p>
            )}
            <label>
              Home ZIP
              <input value={homeZip} onChange={(e) => setHomeZip(e.target.value)} required />
            </label>
            <div className="checks">
              {huntKind === "local" ? (
                <>
                  <label>
                    <input type="checkbox" checked={srcFb} onChange={(e) => setSrcFb(e.target.checked)} />
                    Facebook
                  </label>
                  <label>
                    <input type="checkbox" checked={srcCl} onChange={(e) => setSrcCl(e.target.checked)} />
                    Craigslist
                  </label>
                </>
              ) : (
                <>
                  <label>
                    <input type="checkbox" checked={srcSd} onChange={(e) => setSrcSd(e.target.checked)} />
                    Slickdeals
                  </label>
                  <label>
                    <input type="checkbox" checked={srcRd} onChange={(e) => setSrcRd(e.target.checked)} />
                    Reddit
                  </label>
                </>
              )}
              <label>
                <input
                  type="checkbox"
                  checked={imageCritical}
                  onChange={(e) => setImageCritical(e.target.checked)}
                />
                Image-critical
              </label>
            </div>
            <button className="btn" type="submit">
              Start hunt
            </button>
          </form>

          <div className="hunt-list">
            {hunts.length === 0 && <p className="empty">No hunts yet.</p>}
            {hunts.map((h) => (
              <div className="hunt-item" key={h.id}>
                <strong>{h.query}</strong>
                <div className="meta">
                  #{h.id} · {h.kind ?? "local"} · {h.active ? "active" : "paused"} ·{" "}
                  {h.sources.join(", ")} · max {h.max_price != null ? `$${h.max_price}` : "∞"}
                  {h.kind === "online" ? "" : ` · ${h.max_miles} mi`} · {h.match_count} matches
                </div>
                <div className="hunt-actions">
                  <button className="btn small ghost" onClick={() => void toggleHunt(h)}>
                    {h.active ? "Pause" : "Resume"}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </section>

        <section className="panel">
          <h2>Matches</h2>
          <div className="toolbar">
            <button
              className={`btn small ${!savedOnly ? "" : "ghost"}`}
              onClick={() => setSavedOnly(false)}
            >
              All alerts
            </button>
            <button
              className={`btn small ${savedOnly ? "" : "ghost"}`}
              onClick={() => setSavedOnly(true)}
            >
              Saved
            </button>
          </div>
          <div className="matches">
            {matches.length === 0 && <p className="empty">No verified matches yet. Create a hunt and poll.</p>}
            {matches.map((m) => (
              <article className="match" key={m.id}>
                {m.images[0] ? (
                  <img src={m.images[0]} alt="" />
                ) : (
                  <div className="ph">No photo</div>
                )}
                <div>
                  <h3>
                    <a href={m.url} target="_blank" rel="noreferrer">
                      {m.title}
                    </a>
                  </h3>
                  <div className="stats">
                    <span>{m.price != null ? `$${m.price}` : "price n/a"}</span>
                    <span>{m.drive_miles != null ? `${m.drive_miles.toFixed(1)} mi` : "miles n/a"}</span>
                    <span>{(m.confidence * 100).toFixed(0)}% · {m.tier_used}</span>
                    <span>{m.source}</span>
                    {m.price_outlier && (
                      <span className={`tag ${m.price_outlier}`}>{m.price_outlier}</span>
                    )}
                  </div>
                  <p className="reason">{m.reason}</p>
                  <div className="hunt-actions">
                    <button
                      className="btn small ghost"
                      onClick={() => void flagMatch(m.id, { saved: !m.saved })}
                    >
                      {m.saved ? "Unsave" : "Save"}
                    </button>
                    <button
                      className="btn small ghost"
                      onClick={() => void flagMatch(m.id, { dismissed: true })}
                    >
                      Dismiss
                    </button>
                  </div>
                </div>
              </article>
            ))}
          </div>
        </section>
      </div>
    </>
  );
}
