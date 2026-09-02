// index.html self-test runner: eval the <script> block (minus the DOM boot tail)
// under light DOM stubs. Self-tests are IIFEs that call console.error on failure.
import fs from 'node:fs';
import vm from 'node:vm';

const html = fs.readFileSync(process.argv[2] || 'index.html', 'utf8');
let script = html.slice(html.indexOf('<script>') + '<script>'.length, html.indexOf('</script>'));
// Cut the DOM boot tail (refresh()/selectWeapon('') need heavy canvas/DOM). Self-tests run before it.
const bootAt = script.indexOf("$('unit').value = unit;");
if (bootAt < 0) { console.error('boot marker not found'); process.exit(2); }
script = script.slice(0, bootAt);

const fakeCtx = () => new Proxy({}, { get: () => () => {} });
function fakeEl() {
  const t = { style: {}, dataset: {}, classList: { add(){}, remove(){}, toggle(){}, contains(){return false} },
              value: '', textContent: '', innerHTML: '', checked: false, children: [], options: [] };
  return new Proxy(t, {
    get(o, k) {
      if (k in o) return o[k];
      if (typeof k === 'symbol') return undefined;
      if (['appendChild','removeChild','addEventListener','removeEventListener','setAttribute',
           'removeAttribute','insertBefore','append','remove','focus','blur','click','scrollIntoView'].includes(k))
        return () => {};
      if (k === 'getContext') return () => fakeCtx();
      if (k === 'getBoundingClientRect') return () => ({x:0,y:0,top:0,left:0,right:0,bottom:0,width:0,height:0});
      if (k === 'querySelector' || k === 'closest') return () => null;  // 실제 빈 DOM처럼 "없음"
      if (k === 'querySelectorAll' || k === 'getElementsByClassName') return () => [];
      return fakeEl();
    },
    set(o, k, v) { o[k] = v; return true; },
  });
}

const errors = [];
const documentStub = {
  getElementById: () => fakeEl(),
  createElement: () => fakeEl(),
  querySelector: () => fakeEl(),
  querySelectorAll: () => [],
  addEventListener: () => {},
  body: fakeEl(),
  documentElement: fakeEl(),
};
const sandbox = {
  document: documentStub,
  window: { addEventListener: () => {}, matchMedia: () => ({ matches:false, addEventListener(){}, addListener(){} }),
            location: { href:'', search:'', hash:'' }, devicePixelRatio: 1 },
  localStorage: { _s:{}, getItem(k){ return k in this._s ? this._s[k] : null; },
                  setItem(k,v){ this._s[k]=String(v); }, removeItem(k){ delete this._s[k]; } },
  navigator: { language: 'ko', clipboard: { writeText: async () => {} } },
  fetch: async () => ({ ok: false, status: 404, json: async () => ({}) }),
  requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
  Image: function(){ return fakeEl(); },
  console: { log: () => {}, warn: () => {}, info: () => {}, error: (...a) => errors.push(a.map(String).join(' ')) },
};
sandbox.self = sandbox; sandbox.globalThis = sandbox;

try {
  vm.runInNewContext(script, sandbox, { filename: 'index.html.script' });
} catch (e) {
  console.error('UNCAUGHT during eval:', e && e.stack || e);
  process.exit(1);
}
if (errors.length) { console.error('SELF-TEST FAIL:\n' + errors.join('\n')); process.exit(1); }
console.log('index.html self-test PASS');
