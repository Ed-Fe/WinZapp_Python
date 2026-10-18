const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert/strict');
const ts = require('../client/api/node_modules/typescript');
const source = ts.transpileModule(fs.readFileSync(path.resolve(__dirname,
  '../client/api_patches/src/util/announcementReplyBridge.ts'), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020},
}).outputText;

function load(window) {
  const context = {exports: {}, window, setTimeout: () => 0};
  vm.runInNewContext(source, context);
  return context.exports;
}

function store() {
  const handlers = {};
  return {handlers, on: (name, fn) => { (handlers[name] = handlers[name] || []).push(fn); }};
}

// Objects built inside the vm have their own Object.prototype.
const plain = (value) => JSON.parse(JSON.stringify(value));
const wid = (value) => ({_serialized: value, toString: () => value});
const msg = (remote, replyCount) => ({
  id: {_serialized: `false_${remote}_PARENT_456@lid`, remote: wid(remote)},
  replyCount, body: 'private text', messageSecret: 'secret',
});

(async () => {
  // The page side waits for WA-JS and the binding, then installs once per store.
  const sent = [];
  const msgStore = store();
  const window = {WPP: {isFullReady: false, whatsapp: {MsgStore: msgStore}}};
  const api = load(window);
  assert.equal(api.installAnnouncementReplyBridgeInPage(), false);
  window.WPP.isFullReady = true;
  assert.equal(api.installAnnouncementReplyBridgeInPage(), false);  // no binding yet
  window.__winzappOnAnnouncementReplies = (raw) => sent.push(raw);
  assert.equal(api.installAnnouncementReplyBridgeInPage(), true);
  assert.equal(api.installAnnouncementReplyBridgeInPage(), true);
  assert.equal(msgStore.handlers['change:replyCount'].length, 1);

  const fire = (m) => msgStore.handlers['change:replyCount'][0](m);
  fire(msg('123@g.us', 3));
  fire(msg('5511@c.us', 3));     // not a group
  fire(msg('123@g.us', -1));     // not a count
  fire(msg('123@g.us', 1.5));
  fire(null);                    // never throws into WhatsApp's event loop
  assert.equal(sent.length, 1);
  const payload = JSON.parse(sent[0]);
  assert.deepEqual(Object.keys(payload).sort(), ['chatId', 'id', 'replyCount']);
  assert.equal(payload.id, 'false_123@g.us_PARENT_456@lid');
  assert.equal(payload.replyCount, 3);
  assert.ok(!sent[0].includes('private text') && !sent[0].includes('secret'));

  // A reload brings a new MsgStore: it gets its own listener.
  window.WPP.whatsapp.MsgStore = store();
  assert.equal(api.installAnnouncementReplyBridgeInPage(), true);
  assert.equal(window.WPP.whatsapp.MsgStore.handlers['change:replyCount'].length, 1);

  // The Node side forwards only well-formed payloads, to this session's room.
  assert.deepEqual(plain(api.announcementRepliesPayload(sent[0], 's1')),
                   {session: 's1', ...payload});
  for (const bad of [42, 'not json', JSON.stringify({id: '', chatId: '1@g.us', replyCount: 1}),
                     JSON.stringify({id: 'x', chatId: '1@lid', replyCount: 1}),
                     JSON.stringify({id: 'x', chatId: '1@g.us', replyCount: '1'}),
                     'x'.repeat(600)]) {
    assert.equal(api.announcementRepliesPayload(bad, 's1'), null);
  }

  const emitted = [];
  let binding = null;
  const loads = [];
  const page = {
    exposeFunction: async (name, fn) => { assert.equal(name, '__winzappOnAnnouncementReplies'); binding = fn; },
    evaluate: async () => true,
    on: (name, fn) => loads.push(name),
  };
  const io = {to: (room) => ({emit: (event, data) => emitted.push({room, event, data})})};
  await api.installAnnouncementReplyBridge({page, session: 's1'}, io, null);
  assert.deepEqual(loads, ['load']);
  binding(sent[0]);
  binding('garbage');
  assert.deepEqual(plain(emitted), [{room: 'session:s1', event: 'announcement-replies',
                              data: {session: 's1', ...payload}}]);
  console.log('ok');
})().catch((error) => { console.error(error); process.exit(1); });
