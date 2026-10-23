/* Read WhatsApp's presence model. Event.t is emission time, never last seen.
 * Keep pending/unsupported distinct from a ready but withheld timestamp.
 * This source also runs in offline Node tests, without a browser or account.
 */
export const CONTACT_PRESENCE_SOURCE = String.raw`function (id) {
  const wpp = window.WPP;
  const wa = wpp && wpp.whatsapp;
  if (!wa || !wa.PresenceStore || !wa.WidFactory) {
    return { status: 'unsupported' };
  }
  try {
    const wid = wa.WidFactory.createWid(id);
    const chat = wa.ChatStore && wa.ChatStore.get(wid);
    const model = wa.PresenceStore.get(wid) || (chat && chat.presence);
    if (!model || !model.hasData || !model.chatstate) {
      return { status: 'pending' };
    }
    const state = model.chatstate;
    const online = model.isOnline === true;
    const restricted = state.deny === true;
    const value = state.t;
    const number = typeof value === 'boolean' || value == null ? NaN : Number(value);
    const type = { typing: 'composing', recording_audio: 'recording',
      online: 'available', offline: 'unavailable' }[state.type] || state.type;
    const active = type === 'composing' || type === 'recording';
    const lastSeen = !restricted && !online && !active && Number.isFinite(number) && number > 0
      ? Math.floor(number > 1e12 ? number / 1000 : number) : null;
    return { status: 'ready', restricted, isOnline: online,
      state: active ? type : online ? 'available'
        : 'unavailable', lastSeen };
  } catch (_) {
    return { status: 'pending' };
  }
}`;
