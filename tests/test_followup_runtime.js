const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('web/static/index.html', 'utf8');
const script = html.match(/<script>([\s\S]*)<\/script>/)[1].replace(/\ninit\(\);\s*$/, '\n');

function element(id) {
  return {
    id, value: '', textContent: '', innerHTML: '', disabled: false,
    style: {}, dataset: {}, classList: {
      add() {}, remove() {}, toggle() {}, contains() { return false; }
    },
    setAttribute(name, value) { this[name] = value; },
    addEventListener() {},
    scrollIntoView() {},
    scrollTop: 0,
  };
}

const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, element(id));
    return elements.get(id);
  },
  querySelectorAll() { return []; },
  querySelector() { return null; },
  createElement() { return element('created'); },
};

let alerts = [];
let fetchCalls = 0;
const context = {
  console, document,
  fetch: async () => { fetchCalls += 1; return {ok: false, status: 409, json: async () => ({detail: 'Japan is not installed'})}; },
  alert: message => alerts.push(message),
  setInterval: () => 1,
  clearInterval: () => {},
  setTimeout, clearTimeout,
  URLSearchParams,
  EventSource: class {},
};
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync('web/static/vendor/marked.min.js', 'utf8'), context);
vm.runInContext(script, context);
vm.runInContext(`personaDatasets = [{id: 'USA', label: 'USA', ready: true, default: true}, {id: 'generated', label: 'LLM-generated personas', ready: true}]; selectedDataset = 'USA'; configReady = true;`, context);


(async () => {
  vm.runInContext("sessionId='test'; evalResultsData=Array.from({length:30},(_,i)=>({score:5,concerns:['Concern '+i]}));", context);
  document.getElementById('entityText').value='draft';
  let sent;
  context.fetch=async (url,opts)=>{sent=JSON.parse(opts.body);return {ok:false,status:400,json:async()=>({detail:'stop after request capture'})};};
  await context.runDirections();
  assert.equal(sent.concerns.length,15,'Send only the 15 concerns the backend will use');
  let stream;
  context.SGOEventSource=class {constructor(){stream=this;this.handlers={};}addEventListener(n,f){this.handlers[n]=f;}close(){}};
  document.querySelector=()=>document.getElementById('audit-body');
  document.getElementById('probeOrder').checked=true;
  context.runBiasAudit();
  stream.handlers.complete({data:JSON.stringify({status:'failed',error:'Model-call limit reached',analyses:[{probe:'order',error:'No valid results'}],report:'failed report'})});
  assert.match(document.getElementById('auditProgressText').textContent,/failed/i);
  assert.doesNotMatch(document.getElementById('auditProgressText').textContent,/Audit complete/);
  context.runBiasAudit();
  document.getElementById('biasCalibration').checked=false;
  stream.handlers.complete({data:JSON.stringify({status:'partial',error:'Some pairs failed',analyses:[{probe:'order',n:1,failed_pairs:1,shifted_pct:100,avg_abs_delta:1}],report:'partial report'})});
  assert.match(document.getElementById('auditProgressText').textContent,/partially/);
  assert.equal(document.getElementById('biasCalibration').checked,false);
  assert.match(document.getElementById('audit-body').innerHTML,/Incomplete sample/);
  context.runBiasAudit();
  stream.onerror({data:JSON.stringify({message:'Model-call limit reached; retry later'})});
  assert.match(document.getElementById('auditProgressText').textContent,/Model-call/);
  console.log('Bounded concern request and truthful audit failure UI: ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
