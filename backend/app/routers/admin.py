"""Admin dashboard: what the service is costing and who is using it.

Auth is a single bearer token from `ADMIN_TOKEN`. That is deliberate - the SRS
rules out user accounts, and a shared token is the least machinery that still
keeps this closed. Two properties matter:

  * an unset token disables the dashboard entirely, rather than opening it;
  * the comparison is constant-time, so the token can't be guessed by timing.

The HTML page never puts the token in a URL. It prompts, keeps it in
sessionStorage, and sends it as an Authorization header - a token in a query
string ends up in server logs, browser history and Referer headers.
"""

import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from app import stats
from app.config import Settings, get_settings
from app.db import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Reject anything without a valid admin bearer token."""
    if not settings.admin_token:
        # Not configured means closed, never open.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "NOT_FOUND", "message": "Not found"},
        )

    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()

    if not hmac.compare_digest(supplied, settings.admin_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "UNAUTHORIZED", "message": "Invalid admin token"},
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.get("/stats", dependencies=[Depends(require_admin)])
def read_stats(
    days: int = 14,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict:
    days = max(1, min(days, 90))
    return stats.collect(session, settings, days=days)


@router.get("", response_class=HTMLResponse)
def dashboard(settings: Settings = Depends(get_settings)) -> HTMLResponse:
    """The dashboard page itself.

    Served without auth on purpose: it contains no data, only the code that
    asks for a token and then fetches `/admin/stats`. The data endpoint is
    what's protected.
    """
    if not settings.admin_token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "NOT_FOUND", "message": "Not found"},
        )
    return HTMLResponse(DASHBOARD_HTML)


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>LeetDecode Admin</title>
<style>
  :root {
    --bg: #ffffff; --surface: #f6f7f9; --border: #e2e5ea;
    --text: #16191d; --muted: #5f6672; --accent: #2f6df6;
    --good: #1a7f4b; --warn: #b45309; --bad: #9b1c1c; --radius: 10px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #17191d; --surface: #202329; --border: #2e323a;
      --text: #e8eaed; --muted: #9aa1ad; --accent: #5b8dff;
      --good: #4ade80; --warn: #fbbf24; --bad: #f0a3a3;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 16px; background: var(--bg); color: var(--text);
    font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  .wrap { max-width: 1000px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 2px; letter-spacing: -0.01em; }
  .sub { color: var(--muted); font-size: 12px; margin: 0 0 20px; }
  h2 {
    font-size: 11px; text-transform: uppercase; letter-spacing: .07em;
    color: var(--muted); margin: 26px 0 8px;
  }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
  .tile { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 14px; }
  .tile .label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
  .tile .value { font-size: 22px; font-weight: 650; margin-top: 3px; font-variant-numeric: tabular-nums; }
  .tile .note { font-size: 11px; color: var(--muted); margin-top: 2px; }
  table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--border); }
  th { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); font-weight: 600; }
  td.num, th.num { text-align: right; }
  .bars { display: flex; align-items: flex-end; gap: 3px; height: 90px; margin-top: 6px; }
  .bar { flex: 1; background: var(--accent); border-radius: 3px 3px 0 0; min-height: 2px; opacity: .85; }
  .bar:hover { opacity: 1; }
  .barlabels { display: flex; gap: 3px; margin-top: 4px; }
  .barlabels span { flex: 1; font-size: 9px; color: var(--muted); text-align: center; overflow: hidden; }
  .meter { height: 7px; background: var(--border); border-radius: 4px; overflow: hidden; margin-top: 6px; }
  .meter > div { height: 100%; background: var(--good); }
  .meter.warn > div { background: var(--warn); }
  .meter.bad > div { background: var(--bad); }
  input, button {
    font: inherit; padding: 8px 10px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--bg); color: var(--text);
  }
  button { background: var(--accent); color: #fff; border: 0; font-weight: 600; cursor: pointer; }
  #gate { max-width: 380px; margin: 60px auto; text-align: center; }
  #gate p { color: var(--muted); font-size: 13px; }
  .err { color: var(--bad); font-size: 12px; min-height: 16px; }
  .muted { color: var(--muted); }
  code { background: var(--surface); padding: 1px 5px; border-radius: 4px; font-size: 12px; }
</style>
</head>
<body>
<div class="wrap">
  <div id="gate">
    <h1>LeetDecode Admin</h1>
    <p>Paste the <code>ADMIN_TOKEN</code> from your environment.</p>
    <p><input id="token" type="password" placeholder="admin token" style="width:100%" /></p>
    <p><button id="unlock" style="width:100%">Unlock</button></p>
    <p class="err" id="gate-error"></p>
  </div>

  <div id="dash" hidden>
    <h1>LeetDecode Admin</h1>
    <p class="sub" id="generated"></p>

    <h2>Today</h2>
    <div class="grid" id="today"></div>

    <h2>Daily spend cap</h2>
    <div class="tile">
      <div class="label">LLM calls today</div>
      <div class="value" id="cap-value"></div>
      <div class="meter" id="cap-meter"><div></div></div>
      <div class="note" id="cap-note"></div>
    </div>

    <h2>LLM calls per day</h2>
    <div class="tile">
      <div class="bars" id="bars"></div>
      <div class="barlabels" id="barlabels"></div>
    </div>

    <h2>Period totals</h2>
    <div class="grid" id="period"></div>

    <h2>By provider</h2>
    <table id="providers"></table>

    <h2>Top installs by usage</h2>
    <table id="installs"></table>

    <h2>Cache</h2>
    <div class="grid" id="cache"></div>
  </div>
</div>

<script>
const KEY = "leetdecode_admin_token";

const el = (id) => document.getElementById(id);
const money = (n) => "$" + Number(n || 0).toFixed(4);
const pct = (n) => (n === null || n === undefined ? "-" : (n * 100).toFixed(1) + "%");

function tile(label, value, note) {
  const d = document.createElement("div");
  d.className = "tile";
  const l = document.createElement("div"); l.className = "label"; l.textContent = label;
  const v = document.createElement("div"); v.className = "value"; v.textContent = value;
  d.append(l, v);
  if (note) { const n = document.createElement("div"); n.className = "note"; n.textContent = note; d.append(n); }
  return d;
}

function table(node, headers, rows) {
  node.replaceChildren();
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  headers.forEach((h, i) => {
    const th = document.createElement("th");
    if (i > 0) th.className = "num";
    th.textContent = h;
    hr.append(th);
  });
  thead.append(hr);
  const tbody = document.createElement("tbody");
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = headers.length; td.className = "muted"; td.textContent = "No data yet.";
    tr.append(td); tbody.append(tr);
  }
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    row.forEach((cell, i) => {
      const td = document.createElement("td");
      if (i > 0) td.className = "num";
      td.textContent = cell;
      tr.append(td);
    });
    tbody.append(tr);
  });
  node.append(thead, tbody);
}

function render(d) {
  el("generated").textContent =
    "Generated " + new Date(d.generated_at).toLocaleString() +
    " \\u00b7 " + d.window_days + "-day window";

  const t = d.today;
  el("today").replaceChildren(
    tile("Requests", t.requests),
    tile("Cache hits", t.cache_hits, "free, no quota"),
    tile("LLM calls", t.llm_calls, "billable"),
    tile("Cost", money(t.cost_usd)),
    tile("Cache hit rate", pct(t.cache_hit_rate))
  );

  el("cap-value").textContent = t.cap_used + " / " + t.cap_limit;
  const meter = el("cap-meter");
  meter.firstElementChild.style.width = Math.min(100, t.cap_pct) + "%";
  meter.className = "meter" + (t.cap_pct >= 90 ? " bad" : t.cap_pct >= 70 ? " warn" : "");
  el("cap-note").textContent =
    t.cap_pct + "% used. At the cap the service serves cache only and returns 503 for new generations.";

  const max = Math.max(1, ...d.series.map((s) => s.calls));
  el("bars").replaceChildren(...d.series.map((s) => {
    const b = document.createElement("div");
    b.className = "bar";
    b.style.height = Math.round((s.calls / max) * 100) + "%";
    b.title = s.date + ": " + s.calls + " calls, " + money(s.cost_usd);
    return b;
  }));
  el("barlabels").replaceChildren(...d.series.map((s) => {
    const x = document.createElement("span");
    x.textContent = s.date.slice(5);
    return x;
  }));

  const p = d.period;
  el("period").replaceChildren(
    tile("LLM calls", p.calls),
    tile("Cost", money(p.cost_usd)),
    tile("Avg per call", p.avg_cost_per_call === null ? "-" : "$" + p.avg_cost_per_call.toFixed(6)),
    tile("Failed calls", p.failures)
  );

  table(el("providers"), ["Provider / model", "Calls", "Cost", "Avg latency"],
    d.providers.map((x) => [x.provider + " \\u00b7 " + x.model, x.calls, money(x.cost_usd), x.avg_latency_ms + " ms"]));

  table(el("installs"), ["Install ID", "Calls", "Cost"],
    d.top_installs.map((x) => [x.install_id, x.calls, money(x.cost_usd)]));

  el("cache").replaceChildren(
    tile("Cached problems", d.cache.total),
    tile("Preseeded", d.cache.preseeded, "always free"),
    tile("Organic", d.cache.organic),
    tile("Installs", d.installs.total),
    tile("Quota exhausted", d.installs.quota_exhausted)
  );
}

async function load(token) {
  const res = await fetch("/admin/stats?days=14", {
    headers: { Authorization: "Bearer " + token },
  });
  if (res.status === 401) throw new Error("That token was rejected.");
  if (!res.ok) throw new Error("Request failed (" + res.status + ").");
  return res.json();
}

async function unlock(token, fromStorage) {
  try {
    const data = await load(token);
    sessionStorage.setItem(KEY, token);
    el("gate").hidden = true;
    el("dash").hidden = false;
    render(data);
  } catch (err) {
    sessionStorage.removeItem(KEY);
    if (!fromStorage) el("gate-error").textContent = err.message;
  }
}

el("unlock").addEventListener("click", () => unlock(el("token").value.trim(), false));
el("token").addEventListener("keydown", (e) => {
  if (e.key === "Enter") unlock(el("token").value.trim(), false);
});

const saved = sessionStorage.getItem(KEY);
if (saved) unlock(saved, true);
</script>
</body>
</html>
"""
