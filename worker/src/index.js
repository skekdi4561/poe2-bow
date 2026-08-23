// 활 시세 감정소 — 크라우드 수집 수합 서버
// POST /harvest : 오버레이 앱이 보낸 활 매물 행(익명, 공개 정보만)을 저장
// GET  /recent  : 최근 24시간 행을 감정소 수집기(serve.py)가 끌어감
// 저장은 매물 id 로 중복 제거, 48시간 지나면 지운다.

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
};
const CURRENCIES = new Set(["exalted", "chaos", "divine", "annul"]);
const RARITIES = new Set(["Normal", "Magic", "Rare", "Unique", ""]);

let schemaReady = false;
async function ensureSchema(db) {
  if (schemaReady) return;
  await db.exec(
    "CREATE TABLE IF NOT EXISTS harvest(lid TEXT PRIMARY KEY, t INTEGER NOT NULL, fee INTEGER, row TEXT NOT NULL)",
  );
  schemaReady = true;
}

// serve.py normalize 스키마와 같은 행만 통과 — 이상한 값은 조용히 버린다
function validRow(r) {
  if (!r || typeof r !== "object") return null;
  const num = (v) => typeof v === "number" && isFinite(v);
  if (typeof r.id !== "string" || !r.id || r.id.length > 64) return null;
  if (!num(r.price) || r.price <= 0) return null;
  if (!CURRENCIES.has(r.cur)) return null;
  if (!num(r.pdps) || !num(r.edps) || r.pdps < 0 || r.edps < 0) return null;
  if (r.pdps + r.edps <= 0) return null;
  if (!num(r.aps) || !num(r.crit)) return null;
  if (!RARITIES.has(r.rarity ?? "")) return null;
  if (typeof r.name !== "string" || r.name.length > 120) return null;
  if (!Array.isArray(r.mods) || r.mods.length > 40) return null;
  if (!r.mods.every((m) => typeof m === "string" && m.length <= 200)) return null;
  if (r.league !== "Standard") return null; // 감정소는 카카오 스탠다드만
  return {
    id: r.id,
    name: r.name,
    pdps: r.pdps,
    edps: r.edps,
    aps: r.aps,
    crit: r.crit,
    price: r.price,
    cur: r.cur,
    rarity: r.rarity ?? "",
    mods: r.mods,
    fee: num(r.fee) ? r.fee : null,
  };
}

export default {
  async fetch(req, env) {
    if (req.method === "OPTIONS") return new Response(null, { headers: CORS });
    const url = new URL(req.url);
    await ensureSchema(env.DB);

    if (req.method === "POST" && url.pathname === "/harvest") {
      let body;
      try {
        const text = await req.text();
        if (text.length > 300_000) return json({ error: "too big" }, 413);
        body = JSON.parse(text);
      } catch {
        return json({ error: "bad json" }, 400);
      }
      const rows = (Array.isArray(body?.rows) ? body.rows : [])
        .slice(0, 60)
        .map(validRow)
        .filter(Boolean);
      const now = Date.now();
      const stmt = env.DB.prepare(
        "INSERT OR IGNORE INTO harvest(lid, t, fee, row) VALUES (?1, ?2, ?3, ?4)",
      );
      if (rows.length) {
        await env.DB.batch(
          rows.map((r) => stmt.bind(r.id, now, r.fee, JSON.stringify(r))),
        );
      }
      // ponytail: 요청 2% 확률로 이틀 지난 행 청소 — 전용 cron 은 필요해지면
      if (Math.random() < 0.02) {
        await env.DB.prepare("DELETE FROM harvest WHERE t < ?1")
          .bind(now - 48 * 3600 * 1000)
          .run();
      }
      return json({ ok: true, accepted: rows.length });
    }

    if (req.method === "GET" && url.pathname === "/recent") {
      const cut = Date.now() - 24 * 3600 * 1000;
      const { results } = await env.DB.prepare(
        "SELECT t, fee, row FROM harvest WHERE t >= ?1 ORDER BY t DESC LIMIT 3000",
      )
        .bind(cut)
        .all();
      const rows = results.map((r) => ({ ...JSON.parse(r.row), t: r.t }));
      return json({ taken_at: Date.now(), rows });
    }

    return json({ error: "not found" }, 404);
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", ...CORS },
  });
}
