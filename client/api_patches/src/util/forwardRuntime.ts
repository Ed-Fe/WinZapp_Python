/*
 * In-page runtime for POST /forward-messages (see deviceController.forwardMessages).
 *
 * wa-js's WPP.chat.forwardMessages() does `await ensureLazyModule(
 * 'WAWebChatForwardMessage')` and then needs its own `functions.forwardMessages`
 * binding. On WhatsApp Web 2.3000.1049007170 that fails with
 * forward_messages_not_available: the module is REGISTERED but cannot be
 * required ("Requiring module ... with unresolved dependencies"), because one
 * of its dependencies (WAWebDualUploadsSendPolicy) lives in a script that the
 * Bootloader lists among the page's resources and nobody loaded. wa-js stops
 * as soon as the module is registered, so it never fetches that script, and
 * its binding, made by module NAME, never happens late either.
 * docs/traps/whatsapp-web-version-pin.md has the measurements.
 *
 * So the function below (1) loads the unloaded Bootloader resources of the
 * components that carry the module and (2) calls WhatsApp's own
 * forwardMessages({chat, msgs, ...}) directly. The heal half is generic (module
 * name + component list): it is the remedy for wa-js's ensureLazyModule
 * weakness in general, only forwarding is wired to it for now.
 *
 * It is ONE self-contained plain-JS function body (no TypeScript, no closure
 * over Node variables, no backticks and no dollar-brace sequences), kept as a
 * string so that exactly this text runs in the page, in the Python source
 * contract tests and over CDP.
 *
 * Contract: resolves { ok: true, response } or { ok: false, detail }. ok:false
 * is ONLY "the module could not be made available" and nothing was sent. A
 * rejected forward throws and is never retried: the caller must not send twice.
 */

export const FORWARD_RUNTIME_SOURCE = String.raw`async function (args) {
  const win = window;
  const moduleName = (args && args.moduleName) || 'WAWebChatForwardMessage';
  const fixedComponents = (args && args.components) || [
    'WAWebMediaForwardMediaMsg',
    'WAWebForwardMessageFlow.react',
    'WAWebForwardMessageModal.react',
  ];
  const maxComponents = 12;
  const perScriptMs = 15000;
  const budgetMs = (args && args.budgetMs) || 10000;
  const chatId = args && args.chatId;
  const messageIds = (args && args.messageIds) || [];

  let lastRequireError = '';
  const tryRequire = (name) => {
    try {
      const mod = win.require(name);
      lastRequireError = '';
      return mod;
    } catch (e) {
      const params = e && e.messageParams;
      lastRequireError = String(
        (params && params[1]) || (e && e.message) || e
      ).slice(0, 400);
      return null;
    }
  };
  const forwardModule = () => {
    const mod = tryRequire(moduleName);
    return mod && typeof mod.forwardMessages === 'function' ? mod : null;
  };
  const get = (coll, key) => {
    if (!coll) return undefined;
    return typeof coll.get === 'function' ? coll.get(key) : coll[key];
  };
  const entries = (coll) => {
    if (!coll) return [];
    if (typeof coll.keys === 'function') return Array.from(coll.keys());
    return Object.keys(coll);
  };
  const usableUrl = (value) => {
    // Only the WhatsApp static CDN and only scripts: never css, never another host.
    const raw = typeof value === 'string' ? value : value && (value.src || value.url);
    try {
      const url = new URL(String(raw));
      if (
        url.protocol === 'https:' &&
        url.hostname === 'static.whatsapp.net' &&
        url.port === '' &&
        !url.username &&
        !url.password &&
        url.pathname.endsWith('.js')
      ) {
        return url.href;
      }
    } catch (_) {
      // not a URL
    }
    return null;
  };
  const loadScript = (src, timeoutMs) =>
    new Promise((resolve) => {
      const script = document.createElement('script');
      let timer = null;
      const done = (ok) => {
        clearTimeout(timer);
        script.onload = null;
        script.onerror = null;
        resolve(ok);
      };
      script.onload = () => done(true);
      script.onerror = () => done(false);
      timer = setTimeout(() => done(false), timeoutMs);
      script.async = true;
      script.src = src;
      document.head.appendChild(script);
    });

  const notes = [];
  // Idempotent and shared: concurrent forwards wait on one heal, and a heal
  // that worked is remembered for the life of the page.
  const heal = async () => {
    const state = (win.__winzappLazyHeal = win.__winzappLazyHeal || {});
    if (state[moduleName] && state[moduleName].done) return true;
    if (!state[moduleName] || !state[moduleName].promise) {
      state[moduleName] = {
        done: false,
        promise: (async () => {
          let bootloader = tryRequire('Bootloader');
          bootloader = bootloader && bootloader.default ? bootloader.default : bootloader;
          const dbg = bootloader && bootloader.__debug;
          if (!dbg) {
            notes.push('no Bootloader.__debug');
            return false;
          }
          const names = [];
          for (const name of fixedComponents) {
            if (get(dbg.componentMap, name) !== undefined) names.push(name);
          }
          for (const name of entries(dbg.componentMap)) {
            if (names.length >= maxComponents) break;
            if (/forward/i.test(name) && names.indexOf(name) < 0) names.push(name);
          }
          const isLoaded = (hash) => {
            const loaded = dbg.loaded;
            if (loaded) {
              if (typeof loaded.has === 'function' ? loaded.has(hash) : loaded[hash]) {
                return true;
              }
            }
            const res = get(dbg.resources, hash);
            return Boolean(res && typeof res === 'object' && res.loaded === true);
          };
          const urls = [];
          for (const name of names) {
            const comp = get(dbg.componentMap, name);
            const hashes = Array.isArray(comp) ? comp : (comp && comp.r) || [];
            for (const hash of hashes) {
              if (isLoaded(hash)) continue;
              const url = usableUrl(get(dbg.resources, hash));
              if (!url) {
                notes.push('skipped resource ' + String(hash));
                continue;
              }
              if (urls.indexOf(url) < 0) urls.push(url);
            }
          }
          const startedAt = Date.now();
          for (const url of urls) {
            const left = budgetMs - (Date.now() - startedAt);
            if (left <= 0) {
              notes.push('budget exhausted');
              break;
            }
            const ok = await loadScript(url, Math.min(perScriptMs, left));
            notes.push((ok ? 'loaded ' : 'failed ') + url);
            if (forwardModule()) return true;
          }
          return Boolean(forwardModule());
        })(),
      };
    }
    const ok = await state[moduleName].promise;
    if (ok) {
      state[moduleName].done = true;
    } else {
      delete state[moduleName];
    }
    return ok;
  };

  // The response crosses back to Node by value. A raw result that cannot be
  // serialized would fail the evaluate AFTER the send, i.e. a 500 and a retry
  // from Python that forwards the same messages again.
  const serializable = (value) => {
    try {
      return JSON.parse(JSON.stringify(value));
    } catch (_) {
      return null;
    }
  };

  const run = async () => {
    const WPP = win.WPP;
    let mod = forwardModule();
    const bound = Boolean(
      WPP && WPP.whatsapp && WPP.whatsapp.functions &&
        typeof WPP.whatsapp.functions.forwardMessages === 'function'
    );
    if (mod && bound) {
      // A build that works: exactly what the library call does.
      const libResponse = await WPP.chat.forwardMessages(chatId, messageIds);
      return { ok: true, response: serializable(libResponse) };
    }
    if (!mod) {
      await heal();
      mod = forwardModule();
    }
    if (!mod) {
      return {
        ok: false,
        detail:
          'forwardMessages unavailable: require(' + moduleName + ') failed: ' +
          (lastRequireError || 'no forwardMessages export') +
          (notes.length ? ' [' + notes.join('; ') + ']' : ''),
      };
    }
    const chat = await WPP.chat.find(chatId);
    const msgs = [];
    for (const id of messageIds) msgs.push(await WPP.chat.getMessageById(id));
    // The single send. Whatever it throws propagates: no retry, no second path.
    const response = await mod.forwardMessages({
      chat: chat,
      msgs: msgs,
      multicast: false,
      includeCaption: false,
      appendedText: false,
    });
    return { ok: true, response: serializable(response) };
  };

  // Python gives up on its POST after 20 s and posts the same forward again
  // while this evaluate is still running. The second request must wait for
  // the first one's result, never send. The key is released on any outcome so
  // a later, deliberate forward of the same messages is not blocked.
  const inflight = (win.__winzappForwardInflight = win.__winzappForwardInflight || {});
  const key = String(chatId) + '|' + messageIds.join(',');
  if (inflight[key]) return await inflight[key];
  const pending = run();
  inflight[key] = pending;
  try {
    return await pending;
  } finally {
    delete inflight[key];
  }
}`;

export function buildForwardRuntimeExpression(args: {
  chatId: string;
  messageIds: string[];
}): string {
  return `(${FORWARD_RUNTIME_SOURCE})(${JSON.stringify(args)})`;
}
