import { deleteOwnStatus, deleteStatus } from '../../controller/statusDeleteController';

const STATUS = '3EB0ABCDEF1234567890';
const own = { id: { id: STATUS, fromMe: true } };
const theirs = { id: { id: 'OTHER1234567', fromMe: false } };

function page({ revoke = jest.fn().mockResolvedValue(undefined), loaded = true, mine = true }: any = {}) {
  const myStatus = { getAllMsgs: () => [theirs, own] };
  let available = loaded;
  const bootloader = { loadModules: jest.fn((_names: string[], done: () => void) => { available = true; done(); }) };
  const functions = {};
  Object.defineProperty(functions, 'revokeStatus', { get: () => (available ? revoke : undefined) });
  return {
    revoke, bootloader, myStatus,
    WPP: { status: { getMyStatus: jest.fn().mockResolvedValue(mine ? myStatus : null) }, whatsapp: { functions } },
    require: (name: string) => (name === 'Bootloader' ? bootloader : undefined),
  };
}

async function inPage(fake: any, statusId: string) {
  const root = globalThis as any;
  const previous = { WPP: root.WPP, require: root.require };
  root.WPP = fake.WPP;
  root.require = fake.require;
  try {
    return await deleteOwnStatus({ statusId });
  } finally {
    root.WPP = previous.WPP;
    root.require = previous.require;
  }
}

test("the account's own status is revoked once, the way WhatsApp Web does", async () => {
  const fake = page();
  expect(await inPage(fake, STATUS)).toEqual({ ok: true });
  expect(fake.revoke).toHaveBeenCalledTimes(1);
  expect(fake.revoke).toHaveBeenCalledWith(fake.myStatus, own);
  expect(fake.bootloader.loadModules).not.toHaveBeenCalled();
});

test('the status drawer code is loaded when nobody opened Status yet', async () => {
  const fake = page({ loaded: false });
  expect(await inPage(fake, STATUS)).toEqual({ ok: true });
  expect(fake.bootloader.loadModules.mock.calls[0][0]).toEqual(['WAWebStatusDrawerFlow.react']);
  expect(fake.revoke).toHaveBeenCalledTimes(1);
});

test.each([
  ['a status of someone else', 'OTHER1234567', 'not_found'],
  ['an id this account never posted', 'MISSING12345', 'not_found'],
  ['something that is not an id', '../etc', 'invalid_request'],
])('%s is never revoked', async (_name, id, code) => {
  const fake = page();
  expect(await inPage(fake, id)).toEqual({ ok: false, code });
  expect(fake.revoke).not.toHaveBeenCalled();
});

test('no status of the account at all is "not found"', async () => {
  expect(await inPage(page({ mine: false }), STATUS)).toEqual({ ok: false, code: 'not_found' });
});

test('a failed revoke is unconfirmed, with no native text, and not tried again', async () => {
  const fake = page({ revoke: jest.fn().mockRejectedValue(new Error('private status text')) });
  const result = await inPage(fake, STATUS);
  expect(result).toEqual({ ok: false, code: 'delete_unconfirmed' });
  expect(fake.revoke).toHaveBeenCalledTimes(1);
  expect(JSON.stringify(result)).not.toMatch(/private/);
});

function response() {
  const res: any = { statusCode: 0, body: null };
  res.status = (code: number) => { res.statusCode = code; return res; };
  res.json = (body: unknown) => { res.body = body; return res; };
  return res;
}

test.each([
  [{ ok: true }, 200, { status: 'success' }],
  [{ ok: false, code: 'not_found' }, 404, { status: 'error', code: 'not_found', retrySafe: false }],
  [{ ok: false, code: 'delete_unconfirmed' }, 502,
   { status: 'error', code: 'delete_unconfirmed', retrySafe: false }],
])('the route answers %o as HTTP %i', async (result, status, body) => {
  const evaluate = jest.fn().mockResolvedValue(result);
  const res = response();
  await deleteStatus({ params: { statusId: STATUS }, client: { page: { isClosed: () => false, evaluate } } } as any, res);
  expect(evaluate).toHaveBeenCalledWith(deleteOwnStatus, { statusId: STATUS });
  expect(res.statusCode).toBe(status);
  expect(res.body).toEqual(body);
});
