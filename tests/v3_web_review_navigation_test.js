// Run via WebReviewNavigationTest; stdin is the real /app.js source.
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(id) {
    this.id = id;
    this.children = [];
    this.textContent = '';
    this.events = new Map();
    this.classList = { add() {} };
  }
  get firstChild() { return this.children[0]; }
  append(child) { this.children.push(child); }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  setAttribute() {}
  addEventListener(name, handler) { this.events.set(name, handler); }
  text() { return [this.textContent, ...this.children.map(child => child.text())].join(' '); }
}

const nodes = new Map();
const get = id => {
  if (!nodes.has(id)) { nodes.set(id, new Element(id)); }
  return nodes.get(id);
};
const window = {
  location: { hash: '#gozden-gecir' },
  addEventListener(name, handler) { this[name] = handler; },
};
const document = {
  querySelector: () => ({ content: '1' }),
  querySelectorAll: selector => selector === '.gorunum'
    ? ['gorunum-gozden-gecir', 'gorunum-arama'].map(get) : [],
  getElementById: get,
  createElement: getTag => new Element(getTag),
  addEventListener(name, handler) { this[name] = handler; },
};
const pending = [];
const fetch = url => url === '/api/durum'
  ? Promise.resolve({ ok: true, json: async () => ({ kasa: 'sentetik' }) })
  : new Promise((resolve, reject) => pending.push({ url, resolve, reject }));
vm.runInNewContext(fs.readFileSync(0, 'utf8'), { Node: Element, window, document, fetch });
document.DOMContentLoaded();

function navigate(hash) {
  window.location.hash = hash;
  window.hashchange();
}
function next(path) {
  const request = pending.shift();
  assert.equal(request && request.url, path);
  return request;
}
function respond(request, data) {
  request.resolve({ ok: true, json: async () => data });
}
const flush = () => new Promise(resolve => setImmediate(resolve));
const route = '/api/gozden-gecir';
const inbox = get('gozden-inbox');
const preview = get('gozden-onizleme');

async function test() {
  const first = next(route);
  navigate('#arama');
  navigate('#gozden-gecir');
  respond(next(route), { inbox: { count: 8 }, bayat_konular: [
    { path: 'notes/current.md', title: 'Current', record_id: 'current' },
  ] });
  await flush();
  respond(first, { inbox: { count: 99 } });
  await flush();
  assert.equal(inbox.children.length, 1, 'late first response appended to the current panel');
  assert.match(inbox.text(), /Öğe sayısı: 8/);
  assert.doesNotMatch(inbox.text(), /99/);

  const topic = get('gozden-konular').children[0];
  const button = topic.children[1].children[0];
  button.events.get('click')();
  const oldPreview = next('/api/kayit?id=current');
  navigate('#arama');
  respond(oldPreview, { baslik: 'Late', kaynak: 'notes/current.md', metin: 'Late preview' });
  await flush();
  assert.equal(preview.textContent, 'Kayıt seçin…', 'preview completed after leaving the panel');

  navigate('#gozden-gecir');
  const rejected = next(route);
  navigate('#arama');
  navigate('#gozden-gecir');
  respond(next(route), { inbox: { count: 9 } });
  await flush();
  rejected.reject(new Error('Obsolete error'));
  await flush();
  assert.equal(inbox.children.length, 1, 'stale failure appended to the current panel');
  assert.doesNotMatch(inbox.text(), /Obsolete error/);

  navigate('#arama');
  navigate('#gozden-gecir');
  const abandoned = next(route);
  navigate('#arama');
  respond(abandoned, { inbox: { count: 27 } });
  await flush();
  assert.equal(inbox.children.length, 0, 'panel rendered after navigating away');

  navigate('#gozden-gecir');
  next(route).reject(new Error('Current error'));
  await flush();
  assert.match(inbox.text(), /Current error/, 'current fetch errors must still be shown');
}

test().catch(error => { console.error(error); process.exitCode = 1; });
