// 워커 단위 테스트 — `node worker/test.mjs`. 네트워크·D1 없음: 순수 함수(validRow/lidOf)와 소스 문자열만 본다.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { validRow, lidOf } from "./src/index.js";

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

// lidOf: fee 없는 행이 같은 id 의 정당한 fee 행을 48시간 선점(INSERT OR IGNORE)하지 못하게 lid 를 나눈다
assert.equal(lidOf(validRow(ok)), "abc");
assert.equal(lidOf(validRow({ ...ok, fee: 0 })), "nofee:abc");
const { fee: _f, ...noFee } = ok;
assert.equal(lidOf(validRow(noFee)), "nofee:abc");

// /recent 는 fee 없는 행을 SQL 에서 걸러 창(LIMIT 3000)을 낭비하지 않는다 — 소스 문자열 단언
const src = readFileSync(new URL("./src/index.js", import.meta.url), "utf8");
const recent = src.slice(src.indexOf('url.pathname === "/recent"'));
assert.ok(recent.length > 0, "/recent 분기를 못 찾았다");
assert.match(recent, /SELECT t, fee, row FROM harvest WHERE[^"]*fee IS NOT NULL/);   // 조건이 빠지면 fee 없는 행이 창을 채운다

console.log("worker/test.mjs PASS");
