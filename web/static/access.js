'use strict';
// All API fetches, including streaming GETs, carry a same-origin CSRF marker.
const sgoFetch = window.fetch.bind(window);
window.fetch = async (input, init = {}) => {
  const url = new URL(typeof input === 'string' ? input : input.url, location.href);
  if (url.origin === location.origin) {
    const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
    headers.set('X-SGO-Request', '1');
    init = {...init, headers, credentials: 'same-origin'};
  }
  const response = await sgoFetch(input, init);
  if (url.origin === location.origin && response.status === 401) location.replace('/login');
  return response;
};
// No automatic reconnect: reconnecting a chargeable endpoint can run it twice.
class SGOEventSource extends EventTarget {
  constructor(url) {
    super();
    this.controller = new AbortController();
    this.closed = false;
    this.run(url);
  }
  close() { this.closed = true; this.controller.abort(); }
  async run(url) {
    try {
      const response = await fetch(url, {signal: this.controller.signal});
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || `Request failed (${response.status})`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (!this.closed) {
        const {value, done} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream: true});
        // SSE server emits CRLF; preserve partial trailing CR across chunks.
        let match;
        while ((match = /\r?\n\r?\n/.exec(buffer))) {
          const block = buffer.slice(0, match.index);
          buffer = buffer.slice(match.index + match[0].length);
          let type = 'message'; const lines = [];
          for (const line of block.split(/\r?\n/)) {
            if (line.startsWith('event:')) type = line.slice(6).trim();
            if (line.startsWith('data:')) lines.push(line.slice(5).replace(/^ /, ''));
          }
          if (lines.length) this.dispatchEvent(new MessageEvent(type, {data: lines.join('\n')}));
          if (this.closed) break;
        }
      }
      if (!this.closed) throw new Error('Connection ended before the analysis completed.');
    } catch (error) {
      if (!this.closed) {
        const event = new MessageEvent('error', {data: JSON.stringify({message: error.message})});
        this.dispatchEvent(event);
        if (this.onerror) this.onerror(event);
      }
    }
  }
}
window.addEventListener('DOMContentLoaded', async () => {
  const response = await fetch('/auth/me');
  if (!response.ok) return;
  const user = await response.json();
  document.getElementById('signedInEmail').textContent = user.email;
  document.getElementById('logoutButton').addEventListener('click', async () => {
    const result = await fetch('/auth/logout', {method: 'POST'});
    if (result.ok) location.replace('/login');
  });
  document.getElementById('datasetAdvancedPath').hidden = true;
  const loadButton = document.getElementById('datasetLoadBtn');
  if (loadButton && !user.admin) loadButton.hidden = true;
});

// Generated rows use delegation instead of inline JavaScript.
document.addEventListener('click', event => {
  const row = event.target.closest('[data-toggle-row]');
  if (row && /^gradient-detail-\d+$/.test(row.dataset.toggleRow)) {
    document.getElementById(row.dataset.toggleRow)?.classList.toggle('hidden');
  }
});
