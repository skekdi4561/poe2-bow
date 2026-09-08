// 워커 단위 테스트 — `node worker/test.mjs`. 네트워크·D1 없음: 순수 함수(validRow/lidOf)와 소스 문자열만 본다.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import worker, { validRow, lidOf } from "./src/index.js";
const src0 = readFileSync(new URL("./src/index.js", import.meta.url), "utf8");

const ok = { id: "abc", name: "활", pdps: 900, edps: 100, aps: 1.4, crit: 6, price: 3, cur: "divine",
             rarity: "Rare", mods: ["x"], fee: 5, league: "Runes of Aldur", cat: "weapon.bow" };
const r = validRow(ok);
assert.ok(r && r.id === "abc" && r.fee === 5 && r.cat === "weapon.bow", "정상 행이 거부되면 크라우드가 통째로 죽는다");
assert.equal(validRow({ ...ok, fee: -1 }), null);             // 음수 수수료 = 조작
assert.equal(validRow({ ...ok, pdps: 100001 }), null);        // 상한 밖 DPS 가 통과하면 곡선 축이 날아간다
assert.equal(validRow({ ...ok, cat: "weapon.wand" }), null);  // 캐스터/임의 종류는 목록 밖
const { league: _l, ...noLeague } = ok;
assert.equal(validRow(noLeague), null);                       // 리그 없는 행은 수집기가 대조할 수 없다
assert.equal(validRow({ ...ok, league: "" }), null);
assert.equal(validRow({ ...ok, cur: "transmute" }), null);    // 하급 화폐
assert.ok(validRow({ ...ok, cur: "mirror" }), "미러를 빼면 최상위 무기가 크라우드에서 사라진다");
const { cat: _c, ...noCat } = ok;
assert.equal(validRow(noCat).cat, "weapon.bow");              // cat 없는 옛 행은 활
assert.equal(validRow({ ...ok, fee: 0 }).fee, null);           // fee 0 = 관측 없음

// 값 하한 — 상한만 있으면 price 5e-324 / pdps 1e-9 가 통과한다. 그런 행은 언제나 최전선을
// 갈아치우는 것처럼 보여 수집기의 진위 확인 예산과 거래소 검색 호출을 매 사이클 태운다.
assert.equal(validRow({ ...ok, price: 5e-324 }), null, "denormal 가격이 통과하면 검증 예산을 태운다");
assert.equal(validRow({ ...ok, price: 0.009 }), null);
assert.ok(validRow({ ...ok, price: 0.01 }), "실거래 가능한 최저가는 통과해야 한다");
assert.equal(validRow({ ...ok, pdps: 1e-9, edps: 0 }), null, "0에 수렴하는 지표도 최전선을 훔친다");
assert.ok(validRow({ ...ok, pdps: 1, edps: 0 }), "지표 1은 통과");

// 방패 막기 — 방어구에만 있고 언제나 양수라 0 은 "미수집"이다(serve.py normalize 와 같은 규약).
assert.equal(validRow({ ...ok, block: 26 }).block, 26);
assert.equal("block" in validRow(ok), false, "무기 행에 근거 없는 block 을 달지 않는다");
assert.equal(validRow({ ...ok, block: 0 }), null, "0 은 미수집이지 값이 아니다");
assert.equal(validRow({ ...ok, block: 101 }), null);
assert.equal(validRow({ ...ok, block: "26" }), null);

// 매물 id 선점 방지: 같은 lid 재관측이 값을 갱신해야 한다. 예전엔 INSERT OR IGNORE 라
// 공격자가 실재 id 로 price 999 를 먼저 올리면 진짜 관측(50)도 인하도 48시간 거부됐다.
{
  const w = src0.slice(src0.indexOf('url.pathname === "/harvest"'));
  assert.ok(/ON CONFLICT\(lid\) DO UPDATE/.test(w), "재관측이 갱신되지 않으면 먼저 쓴 쪽이 48시간 독점한다");
  assert.ok(/known\.get\(lid\) !== r\.price/.test(w), "값이 달라진 행만 통과시켜야 쓰기 절약이 유지된다");
  // 레이트 리밋은 요청이 아니라 행 수에 걸려야 한다 — 요청 단위면 3600행/분으로 /recent 창(800)을 덮는다
  assert.ok(/Math\.ceil\(rows\.length \/ 10\)/.test(w), "행 수 기준 리밋이 빠지면 한 IP가 창을 100% 채운다");
}

// t 인덱스 — 없으면 /recent 와 청소 DELETE 가 풀스캔이라 D1 rows_read 가 테이블 크기에 비례한다
assert.ok(/CREATE INDEX IF NOT EXISTS harvest_t ON harvest\(t\)/.test(src0));

// lidOf: fee 없는 행이 같은 id 의 정당한 fee 행을 48시간 선점(INSERT OR IGNORE)하지 못하게 lid 를 나눈다
assert.equal(lidOf(validRow(ok)), "abc");
assert.equal(lidOf(validRow({ ...ok, fee: 0 })), "nofee:abc");
const { fee: _f, ...noFee } = ok;
assert.equal(lidOf(validRow(noFee)), "nofee:abc");

// /recent 는 fee 없는 행을 SQL 에서 걸러 창(LIMIT 3000)을 낭비하지 않는다 — 소스 문자열 단언
const src = readFileSync(new URL("./src/index.js", import.meta.url), "utf8");
// /recent 의 리그 필터는 **핸들러를 실제로 돌려서** 본다. 소스 문자열만 보면 SQL 은 있는데
// 바인딩 개수가 안 맞는 식의 결함을 놓친다 — v1.1.0 에서 방패 막기를 그렇게 놓쳤다.
// (D1 은 없으니 prepare/bind 를 가로채 SQL 과 바인딩을 그대로 받아 본다.)
{
  const calls = [];
  const env = {
    DB: {
      exec: async () => ({}),          // ensureSchema 용
      prepare(sql) {
        const c = { sql, args: null };
        calls.push(c);
        return { bind: (...a) => { c.args = a; return { all: async () => ({ results: [] }) }; } };
      },
    },
  };
  const hit = async (qs) => {
    calls.length = 0;
    await worker.fetch(new Request("https://w.dev/recent" + qs), env);
    return calls[calls.length - 1];
  };

  const plain = await hit("?cat=weapon.bow");
  assert.ok(!/\$\.league/.test(plain.sql), "league 를 안 줬는데 리그로 잘랐다 — 옛 수집기가 빈 창을 받는다");
  assert.equal(plain.args.length, 2, "cat 만 줬을 때 바인딩 2개");

  const filtered = await hit("?cat=weapon.bow&league=HC%20Forbidden%20Rites");
  assert.match(filtered.sql, /json_extract\(row, '\$\.league'\)/, "리그를 줬는데 SQL 이 안 잘랐다");
  assert.equal(filtered.args.length, 3, "리그까지 주면 바인딩 3개 — 개수가 어긋나면 D1 이 통째로 던진다");
  assert.equal(filtered.args[2], "hc forbidden rites", "리그 비교는 소문자·trim (수집기 _norm_league 와 같은 규약)");

  // 대소문자·여백이 달라도 같은 리그로 잡혀야 한다
  const messy = await hit("?cat=weapon.bow&league=%20forbidden%20RITES%20");
  assert.equal(messy.args[2], "forbidden rites");

  // 길이 상한 — 임의 문자열이 SQL 파라미터로 그대로 흐르지 않게
  const long = await worker.fetch(
    new Request("https://w.dev/recent?cat=weapon.bow&league=" + "x".repeat(61)), env);
  assert.equal(long.status, 400, "과도하게 긴 리그 문자열은 거부");
}

const recent = src.slice(src.indexOf('url.pathname === "/recent"'));
assert.ok(recent.length > 0, "/recent 분기를 못 찾았다");
assert.match(recent, /SELECT t, fee, row FROM harvest WHERE[^"]*fee IS NOT NULL/);   // 조건이 빠지면 fee 없는 행이 창을 채운다

console.log("worker/test.mjs PASS");
