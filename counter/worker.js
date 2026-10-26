// LingFeed click counter: a Cloudflare Worker with a D1 (SQLite) database.
//
//   POST /click   a visitor clicked a paper in LingFeed (sent by the page)
//   GET  /top     the ranking: papers by distinct-visitor clicks over the last 6 months (cached for 30 minutes)
//   GET  /health  says "ok" if the service and its database are working (for an uptime checker)
//   DELETE /admin/paper/<id>   remove a paper and its counts (needs the ADMIN_TOKEN secret)

const WINDOW_DAYS = 183;            // the ranking covers roughly six months
const MIN_COUNT = 1;                // a paper appears in the ranking only after this many counted clicks
const MAX_PAPERS_PER_VISITOR_DAY = 40;   // a single visitor can count for at most this many papers a day
const MAX_PER_NETWORK_PAPER_DAY = 10;    // at most this many counted clicks per paper per day from one network
const BOT_UA = /bot|crawl|spider|slurp|headless|phantom|python|curl|wget|httpclient|java\/|go-http|libwww|scrapy|node-fetch|axios|okhttp|preview|monitor|lighthouse|facebookexternalhit/i;

const escapeRe = s => s.replace(/[.*+?^${}()|[\]\\\/]/g, "\\$&");
const SOURCES = {   // id format, and the only web address a paper of that source may be linked to
  // Zenodo: the id is the number shared by all versions of a paper, the link goes to whichever version was clicked
  z: { id: /^z\d{1,12}$/,                         url: id => /^https:\/\/zenodo\.org\/records\/\d{1,12}$/ },
  l: { id: /^l\d{6}$/,                            url: id => new RegExp(`^https://lingbuzz\\.net/lingbuzz/${id.slice(1)}$`) },
  a: { id: /^a(\d{4}\.\d{4,5}|[a-z-]+(\.[A-Z]{2})?\/\d{7})$/, url: id => new RegExp(`^https://arxiv\\.org/abs/${escapeRe(id.slice(1))}$`) },
  o: { id: /^o[a-z0-9]{5}$/,                      url: id => new RegExp(`^https://osf\\.io/(preprints/[a-z]+/)?${id.slice(1)}(_v\\d+)?/?$`) },
};

const hex = bytes => [...bytes].map(b => b.toString(16).padStart(2, "0")).join("");
const sha = async s => hex(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)))).slice(0, 32);
const dayOf = ms => new Date(ms).toISOString().slice(0, 10);
const clean = (s, max) => String(s ?? "").replace(/[\u0000-\u001f\u007f<>]/g, " ").replace(/\s+/g, " ").trim().slice(0, max);

function corsHeaders(req, env) {
  const origin = req.headers.get("Origin") || "";
  const allowed = (env.ALLOWED_ORIGINS || "").split(",").map(s => s.trim()).filter(Boolean);
  return allowed.includes(origin)
    ? { "Access-Control-Allow-Origin": origin, "Vary": "Origin", "Access-Control-Allow-Methods": "GET,POST,OPTIONS", "Access-Control-Allow-Headers": "Content-Type" }
    : null;
}

// A network, for the per-network limit: first three parts of an IPv4 address, first three groups of IPv6.
function networkOf(ip) {
  if (ip.includes(":")) return ip.split(":").slice(0, 3).join(":");
  return ip.split(".").slice(0, 3).join(".");
}

let saltCache = { day: "", salt: "" };
async function dailySalt(env, day) {
  if (saltCache.day === day) return saltCache.salt;
  let row = await env.DB.prepare("SELECT salt FROM salts WHERE day = ?").bind(day).first();
  if (!row) {
    await env.DB.prepare("INSERT OR IGNORE INTO salts (day, salt) VALUES (?, ?)")
      .bind(day, hex(crypto.getRandomValues(new Uint8Array(16)))).run();
    row = await env.DB.prepare("SELECT salt FROM salts WHERE day = ?").bind(day).first();
  }
  saltCache = { day, salt: row.salt };
  return row.salt;
}

async function handleClick(req, env) {
  const cors = corsHeaders(req, env);
  if (!cors) return new Response("forbidden", { status: 403 });           // only our own site may report clicks
  const ua = req.headers.get("User-Agent") || "";
  if (!ua || BOT_UA.test(ua)) return new Response(null, { status: 204, headers: cors });   // ignore known automated agents

  let body;
  try { body = JSON.parse(await req.text()); } catch { return new Response("bad request", { status: 400, headers: cors }); }
  const id = String(body.id || "");
  const kind = SOURCES[id[0]];
  if (!kind || !kind.id.test(id)) return new Response("bad id", { status: 400, headers: cors });
  if (typeof body.url !== "string" || !kind.url(id).test(body.url)) return new Response("bad url", { status: 400, headers: cors });
  const title = clean(body.title, 300), authors = clean(body.authors, 200);
  if (!title || /https?:\/\/|www\./i.test(title + " " + authors)) return new Response("bad text", { status: 400, headers: cors });

  const now = Date.now(), day = dayOf(now);
  const salt = await dailySalt(env, day);
  const ip = req.headers.get("CF-Connecting-IP") || "unknown";
  const visitor = await sha(`${salt}|${ip}|${ua}`);
  const net = await sha(`${salt}|net|${networkOf(ip)}`);

  // 1. one counted click per visitor per paper per day
  const first = await env.DB.prepare("INSERT OR IGNORE INTO seen (day, id, visitor) VALUES (?, ?, ?)").bind(day, id, visitor).run();
  if (!first.meta.changes) return new Response(null, { status: 204, headers: cors });
  // 2. a visitor can only count for so many different papers per day
  const v = await env.DB.prepare("INSERT INTO vcap (day, visitor, n) VALUES (?, ?, 1) ON CONFLICT (day, visitor) DO UPDATE SET n = n + 1 RETURNING n").bind(day, visitor).first();
  if (v.n > MAX_PAPERS_PER_VISITOR_DAY) return new Response(null, { status: 204, headers: cors });
  // 3. one network can only contribute so many counted clicks to one paper per day
  const n = await env.DB.prepare("INSERT INTO netcap (day, id, net, n) VALUES (?, ?, ?, 1) ON CONFLICT (day, id, net) DO UPDATE SET n = n + 1 RETURNING n").bind(day, id, net).first();
  if (n.n > MAX_PER_NETWORK_PAPER_DAY) return new Response(null, { status: 204, headers: cors });

  await env.DB.batch([
    env.DB.prepare("INSERT OR IGNORE INTO papers (id, src, title, authors, url, first_seen) VALUES (?, ?, ?, ?, ?, ?)").bind(id, id[0], title, authors, body.url, day),
    env.DB.prepare("INSERT INTO daily (day, id, n) VALUES (?, ?, 1) ON CONFLICT (day, id) DO UPDATE SET n = n + 1").bind(day, id),
  ]);
  return new Response(null, { status: 204, headers: cors });
}

const TOP_CACHE_SECONDS = 1800;   // the ranking is worked out at most once per half hour (keeps database reads small)

async function handleTop(req, env, ctx) {
  const cors = corsHeaders(req, env);
  if (!cors) return new Response("forbidden", { status: 403 });
  const url = new URL(req.url);
  const limit = Math.min(Math.max(parseInt(url.searchParams.get("limit") || "100", 10) || 100, 1), 200);
  const cache = globalThis.caches && globalThis.caches.default;
  const key = new Request(`${url.origin}/top?limit=${limit}&o=${encodeURIComponent(req.headers.get("Origin"))}`);
  let res = cache ? await cache.match(key) : null;
  if (!res) {
    const since = dayOf(Date.now() - WINDOW_DAYS * 864e5);
    const { results } = await env.DB.prepare(
      `SELECT p.id, p.src, p.title, p.authors, p.url, SUM(d.n) AS clicks
         FROM daily d JOIN papers p ON p.id = d.id
        WHERE d.day >= ? GROUP BY d.id HAVING clicks >= ?
        ORDER BY clicks DESC, p.first_seen ASC LIMIT ?`).bind(since, MIN_COUNT, limit).all();
    res = new Response(JSON.stringify({ since, generated: new Date().toISOString(), papers: results }), {
      headers: { ...cors, "Content-Type": "application/json", "Cache-Control": `public, max-age=${TOP_CACHE_SECONDS}` },
    });
    if (cache) ctx.waitUntil(cache.put(key, res.clone()));
  }
  const out = new Response(res.body, res);
  out.headers.set("Cache-Control", "public, max-age=300");     // browsers re-check every five minutes
  return out;
}

// For an uptime checker: "ok" when the Worker runs and its database answers; status 503 otherwise.
// Reveals nothing about the data, and costs one tiny database read per check.
async function handleHealth(env) {
  try {
    await env.DB.prepare("SELECT 1").first();
    return new Response("ok", { headers: { "Cache-Control": "no-store" } });
  } catch (e) {
    return new Response("database unavailable", { status: 503, headers: { "Cache-Control": "no-store" } });
  }
}

async function handleAdmin(req, env, path) {
  const auth = req.headers.get("Authorization") || "";
  if (!env.ADMIN_TOKEN || auth !== `Bearer ${env.ADMIN_TOKEN}`) return new Response("forbidden", { status: 403 });
  const id = decodeURIComponent(path.replace("/admin/paper/", ""));
  await env.DB.batch(["papers", "daily", "seen", "netcap"].map(t => env.DB.prepare(`DELETE FROM ${t} WHERE id = ?`).bind(id)));
  return new Response(`removed ${id}`);
}

async function prune(env) {
  const cutoff = dayOf(Date.now() - 2 * 864e5), old = dayOf(Date.now() - (WINDOW_DAYS + 14) * 864e5);
  await env.DB.batch([
    env.DB.prepare("DELETE FROM seen WHERE day < ?").bind(cutoff),
    env.DB.prepare("DELETE FROM vcap WHERE day < ?").bind(cutoff),
    env.DB.prepare("DELETE FROM netcap WHERE day < ?").bind(cutoff),
    env.DB.prepare("DELETE FROM salts WHERE day < ?").bind(cutoff),
    env.DB.prepare("DELETE FROM daily WHERE day < ?").bind(old),
    env.DB.prepare("DELETE FROM papers WHERE id NOT IN (SELECT DISTINCT id FROM daily)"),
  ]);
}

export default {
  async fetch(req, env, ctx) {
    const { pathname } = new URL(req.url);
    if (req.method === "OPTIONS") {
      const cors = corsHeaders(req, env);
      return new Response(null, { status: cors ? 204 : 403, headers: cors || {} });
    }
    if (req.method === "POST" && pathname === "/click") return handleClick(req, env);
    if (req.method === "GET" && pathname === "/top") return handleTop(req, env, ctx);
    if ((req.method === "GET" || req.method === "HEAD") && pathname === "/health") return handleHealth(env);
    if (req.method === "DELETE" && pathname.startsWith("/admin/paper/")) return handleAdmin(req, env, pathname);
    return new Response("not found", { status: 404 });
  },
  async scheduled(event, env) { await prune(env); },
};
