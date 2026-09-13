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

for (const [id, value] of Object.entries({
  entityText: 'Draft text', goalText: 'Improve response', cohortDesc: 'Reviewers',
  nemotronDataset: 'USA', panelSize: '80',
})) document.getElementById(id).value = value;

assert.match(context.marked.parse('# Heading\n\n**bold**', {breaks: true}), /<h1[^>]*>Heading/);
context.setEntityMode('preview');
assert.equal(document.getElementById('entityText').value, 'Draft text', 'preview preserves raw draft');
context.setEntityMode('edit');
context.setAnalysis('### Findings\n\n**Keep the headline**');
assert.equal(document.getElementById('evalAnalysis').textContent, '### Findings\n\n**Keep the headline**', 'analysis source is preserved');
context.setAnalysisMode('source');
context.setAnalysisMode('rendered');

context.setPipelineBusy(true, 'Dataset setup in progress');
const requestsBeforeBusyGuard = fetchCalls;
Promise.resolve(context.setupNemotron()).then(() => context.runFullPipeline()).then(() => {
  assert.equal(fetchCalls, requestsBeforeBusyGuard, 'busy guards prevent duplicate requests');
  context.setPipelineBusy(false);
  return context.runFullPipeline();
}).then(() => {
  assert.equal(Number(document.getElementById('panelSize').value), 50, 'display the enforced panel size');
  assert.equal(document.getElementById('goBtn').disabled, false, 'CTA unlocks after failure');
  assert.equal(document.getElementById('nemotronDataset').disabled, false, 'dataset selector unlocks after failure');
  assert.equal(document.getElementById('datasetLoadBtn').disabled, false, 'dataset action unlocks after failure');
  assert.ok(alerts.length === 0, 'server failure is shown in pipeline log instead of alert');
  console.log('UI pipeline failure recovery: ok');
}).catch(error => {
  console.error(error);
  process.exitCode = 1;
});
