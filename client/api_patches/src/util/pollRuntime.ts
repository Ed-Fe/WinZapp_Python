// Self-contained: Puppeteer serializes these functions into the WhatsApp page.
// JavaScript syntax also lets Node tests run the exact browser functions.
//
// Read from WhatsApp Web's own source over CDP (WinZapp's HeadlessChrome,
// 2026-10-11), not guessed:
//  - a poll is a Msg with type 'poll_creation'; pollOptions is [{name, localId}];
//  - votes live in WAWebPollsPollVoteCollection.PollVoteCollection; the votes
//    of one poll are PollVoteCollection.getForParentAddressingModeInsensitive([msg.id])[0]
//    (a group with toArray()), each vote having selectedOptionLocalIds (empty
//    = the voter cleared the vote);
//  - WAWebPollsSendVoteMsgAction.sendVote(pollMsg, Set<localId>) casts or
//    changes the current user's vote; an empty Set clears it.
// Only counts and option names leave the page, never voter identities.

function loadModule(name) {
  const wpp = globalThis['WPP'];
  const loader = wpp?.loader;
  if (typeof loader?.loadModule === 'function') return loader.loadModule(name);
  if (typeof loader?.moduleRequire === 'function') return loader.moduleRequire(name);
  return undefined;
}

async function pollMessage(messageId) {
  const wpp = globalThis['WPP'];
  const msg = await wpp?.chat?.getMessageById?.(messageId);
  return msg && msg.type === 'poll_creation' ? msg : undefined;
}

export async function readPollVotes({ messageIds }) {
  const collection = loadModule('WAWebPollsPollVoteCollection')?.PollVoteCollection;
  if (typeof collection?.getForParentAddressingModeInsensitive !== 'function') {
    throw new Error('poll_votes_unavailable');
  }
  const meUser = loadModule('WAWebUserPrefsMeUser');
  const hydrate = loadModule('WAWebAddonHydrationUtils')?.hydrateAddons;
  const hydrationType = loadModule('WAWebMsgType')?.MSG_TYPE?.POLL_UPDATE;
  const result = {};
  for (const messageId of messageIds) {
    const msg = await pollMessage(messageId);
    if (!msg) continue;
    if (typeof hydrate === 'function' && hydrationType !== undefined) {
      try { await hydrate({ ids: [msg.id], hydrationType }); } catch { /* best effort */ }
    }
    const group = collection.getForParentAddressingModeInsensitive([msg.id])?.[0];
    const votes = group?.toArray?.() ?? [];
    let mine = [];
    try {
      const me = meUser?.getMeLidUserOrThrow?.();
      const mineVote = me ? group?.getVoteFromSenderAddressingModeInsensitive?.(me) : undefined;
      mine = mineVote?.selectedOptionLocalIds ?? [];
    } catch { /* not knowing my vote must not hide the counts */ }
    const counts = new Map();
    let voters = 0;
    for (const vote of votes) {
      const ids = vote?.selectedOptionLocalIds ?? [];
      if (!ids.length) continue;
      voters += 1;
      for (const id of ids) counts.set(id, (counts.get(id) ?? 0) + 1);
    }
    result[messageId] = {
      voters,
      options: (msg.pollOptions ?? []).map((option) => ({
        name: option.name,
        localId: option.localId,
        votes: counts.get(option.localId) ?? 0,
        mine: mine.includes(option.localId),
      })),
    };
  }
  return result;
}

export async function writePollVote({ messageId, localIds }) {
  try {
    if (typeof messageId !== 'string' || !messageId || !Array.isArray(localIds)
        || !localIds.every((id) => Number.isInteger(id) && id >= 0)) {
      return { ok: false, error: 'poll_vote_invalid' };
    }
    const sendVote = loadModule('WAWebPollsSendVoteMsgAction')?.sendVote;
    const msg = await pollMessage(messageId);
    if (typeof sendVote !== 'function' || !msg) {
      return { ok: false, error: 'poll_vote_unavailable' };
    }
    const valid = new Set((msg.pollOptions ?? []).map((option) => option.localId));
    if (!localIds.every((id) => valid.has(id))) {
      return { ok: false, error: 'poll_vote_invalid' };
    }
    const limit = Number(msg.pollSelectableOptionsCount) || 0;
    if (limit > 0 && localIds.length > limit) {
      return { ok: false, error: 'poll_vote_invalid' };
    }
    await sendVote(msg, new Set(localIds));
    return { ok: true };
  } catch {
    // Native errors may carry contact names, message content and identifiers.
    return { ok: false, error: 'poll_vote_failed' };
  }
}
