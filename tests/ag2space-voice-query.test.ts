// query_messages: "to_me" is the owner's (inbox where offered, else mentions and replies in every room); "all" merges rooms;
// "new" moves only after a complete, unfiltered read, kept per scope and owner; partial reads say so; never a throw.
import { describe, it, test, beforeEach, after } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const PLUGIN_DIR = join(process.cwd(), 'skills', 'ag2space-voice');
const PRESENT = existsSync(join(PLUGIN_DIR, 'query.ts'));
if (!PRESENT) test('ag2space-voice plugin not installed', { skip: true }, () => {});

if (PRESENT) {
const {
	queryMessages, windowStart, resetLastChecked, recordCallEnded, createQueryMessagesTool, addressedTo, QUERY_PROMPT_RULE, QUERY_TURN_CHECK_RULE,
	QUERY_FIRST_WINDOW_MS, QUERY_ROOM_PAGE, QUERY_ROOM_MAX_PAGES, QUERY_SEARCH_MAX_PAGES, QUERY_BUDGET_MS,
} = await import('../skills/ag2space-voice/query.ts');

type Call = { name: string; args: Record<string, unknown> };
const ok = (v: unknown) => ({ structuredContent: v });
const err = (code: string) => ({ isError: true, structuredContent: { code } });
const ME = '@me.agent:ag2.space';
const OWNER = '@susan:ag2.space';
const NOW = Date.UTC(2026, 9, 15, 5, 0); // Oct 14 22:00 PDT
const ROOMS = [{ room_id: '!a:ag2.space', name: 'Alpha' }, { room_id: '!b:ag2.space', name: 'Beta' }];

/** `ownerId`: what whoami says of the owner (none by default); `ownerScope`: whether mentions.inbox offers scope "owner". */
function fake(handler: (name: string, args: Record<string, unknown>) => unknown, rooms: Array<{ room_id: string; name?: string }> = ROOMS, opts: { ownerId?: string; ownerScope?: boolean } = {}) {
	const calls: Call[] = [];
	const client = {
		async callTool(name: string, args: Record<string, unknown>) {
			if (name === 'room.actions.describe') return ok({ input_schema: { properties: opts.ownerScope ? { scope: { enum: ['self', 'owner'] } } : {} } }) as never;
			if (name === 'ag2.whoami') return ok({ actor: { id: ME, kind: 'agent' }, runtime: opts.ownerId ? { owner_id: opts.ownerId } : {} }) as never;
			calls.push({ name, args });
			if (name === 'room.list') return ok({ rooms, next_cursor: null }) as never;
			const r = handler(name, args); if (r instanceof Error) throw r; return r as never;
		},
		close() {},
	};
	const named = rooms.filter(r => r.name) as Array<{ room_id: string; name: string }>;
	const index = { rooms: () => named, refresh: async () => named, fetchedAt: () => 1 };
	const deps = { client: () => client, currentRoom: () => null, index, log: () => {} };
	return { calls, deps, client };
}

const msg = (i: number, sender: string, body: string, minutesAgo: number) => ({ sender, body, event_id: `$m${i}`, ts: String(NOW - minutesAgo * 60_000) });
const item = (i: number, extra: Record<string, unknown> = {}) => ({
	room_id: '!a:ag2.space', room_name: 'Alpha', event_id: `$i${i}`, sender: { user_id: '@chi:ag2.space', display_name: 'Chi Wang' },
	sent_at: new Date(NOW - i * 60_000).toISOString(), body: `ping ${i}`, reason: 'mention', in_reply_to: null, thread_root: null, replied: false, link: null, ...extra,
});
const args = (c: Call) => c.args.arguments as Record<string, unknown>;

const TMP = mkdtempSync(join(tmpdir(), 'voice-query-'));
let heardFile = '';
let n = 0;
beforeEach(() => { heardFile = join(TMP, `heard-${++n}.json`); resetLastChecked(heardFile); });
after(() => rmSync(TMP, { recursive: true, force: true }));

describe('the window', () => {
	it('hour, day, week, today from local midnight; "new" is the last check, else the last day', () => {
		assert.equal(windowStart('hour', 'all', NOW), NOW - 3_600_000);
		assert.equal(windowStart('day', 'all', NOW), NOW - 86_400_000);
		assert.equal(windowStart('week', 'all', NOW), NOW - 7 * 86_400_000);
		const mid = new Date(NOW); mid.setHours(0, 0, 0, 0);
		assert.equal(windowStart('today', 'all', NOW), mid.getTime());
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW), NOW - QUERY_FIRST_WINDOW_MS);
	});

	it('"new" moves to the time of the last complete, unfiltered check, per scope, and only when asked as "new"', async () => {
		const f = fake(() => ok({ result: { mentions: [], next_cursor: null, has_more: false } }));
		await queryMessages({}, f.deps, () => NOW);
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 5), NOW);
		assert.equal(windowStart('last_check', 'all', NOW + 5), NOW + 5 - QUERY_FIRST_WINDOW_MS);
		await queryMessages({ since: 'hour' }, f.deps, () => NOW + 1000);
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 5), NOW);
	});
});

describe('to_me: the agent\'s inbox', () => {
	it('reads mentions.inbox as the account since the window, counts and lists newest first', async () => {
		const f = fake(() => ok({ result: { mentions: [item(3), item(2), item(1, { room_id: '!dm:ag2.space', room_name: null, reason: 'dm' })], next_cursor: null, has_more: false } }));
		const r = await queryMessages({ latest: 2, since: 'day' }, f.deps, () => NOW);
		const c = f.calls.find(x => x.name === 'room.action.read')!;
		assert.equal(c.args.target_kind, 'account');
		assert.equal(c.args.action, 'mentions.inbox');
		assert.equal(args(c).since, new Date(NOW - 86_400_000).toISOString());
		assert.equal(args(c).include_replied, true);
		assert.ok(r.ok);
		if (r.ok) {
			assert.equal(r.scope, 'to_me');
			assert.equal(r.count, 3);
			assert.deepEqual(r.latest.map(m => m.event_id), ['$i1', '$i2']);
			assert.equal(r.latest[0].room, 'a direct message');
			assert.equal(r.latest[0].why, 'dm');
			assert.deepEqual(r.by_room, [{ room: 'Alpha', count: 2 }, { room: 'a direct message', count: 1 }]);
		}
	});

	it('a room narrows the inbox; unanswered_only leaves the answered out; more pages mean "at least"', async () => {
		const f = fake(() => ok({ result: { mentions: [item(1, { room_id: '!b:ag2.space', room_name: null })], next_cursor: 'c', has_more: true } }));
		const r = await queryMessages({ room: 'Beta', unanswered_only: true }, f.deps, () => NOW);
		const reads = f.calls.filter(x => x.name === 'room.action.read');
		assert.deepEqual(args(reads[0]).room_ids, ['!b:ag2.space']);
		assert.equal(args(reads[0]).include_replied, false);
		assert.ok(r.ok && r.at_least && r.latest[0].room === 'Beta');
	});
});

describe('all: every room, merged', () => {
	it('reads each room back to the window, leaves its own messages out, merges newest first', async () => {
		const f = fake((_n, a) => {
			const room = a.room_id as string;
			if (room === '!a:ag2.space') return ok({ result: { messages: [msg(1, '@chi:ag2.space', 'hi', 5), msg(2, ME, 'mine', 6), msg(3, '@old:ag2.space', 'old', 90)], cursor: 'x' } });
			return ok({ result: { messages: [msg(4, '@yixuan:ag2.space', 'yo', 1)], cursor: null } });
		});
		const r = await queryMessages({ scope: 'all', since: 'hour' }, f.deps, () => NOW);
		assert.ok(r.ok);
		if (r.ok) {
			assert.equal(r.count, 2);
			assert.deepEqual(r.latest.map(m => [m.room, m.from]), [['Beta', 'yixuan'], ['Alpha', 'chi']]);
			assert.equal(r.at_least, false);
			assert.equal(r.rooms_unread, undefined);
		}
		assert.equal(f.calls.filter(c => c.name === 'room.action.read').length, 2);
	});

	it('pages a busy room back with the cursor, and says "at least" when the pages run out inside the window', async () => {
		let n = 0;
		const f = fake((_n, a) => {
			if (a.room_id !== '!a:ag2.space') return ok({ result: { messages: [], cursor: null } });
			n++;
			if (n > 1) assert.equal(args({ name: '', args: a }).before, `cur${n - 1}`);
			return ok({ result: { messages: Array.from({ length: QUERY_ROOM_PAGE }, (_, i) => msg(n * 1000 + i, '@chi:ag2.space', 'x', 1)), cursor: `cur${n}` } });
		});
		const r = await queryMessages({ scope: 'all', since: 'day' }, f.deps, () => NOW);
		assert.equal(n, QUERY_ROOM_MAX_PAGES);
		assert.ok(r.ok && r.at_least && r.count === QUERY_ROOM_PAGE * QUERY_ROOM_MAX_PAGES);
	});

	it('filters by sender name and by word; a room that cannot be read is counted', async () => {
		const f = fake((_n, a) => a.room_id === '!b:ag2.space' ? err('FORBIDDEN')
			: ok({ result: { messages: [msg(1, '@chi:ag2.space', 'The Launch plan', 5), msg(2, '@yixuan:ag2.space', 'launch', 6), msg(3, '@chi:ag2.space', 'lunch', 7)], cursor: null } }));
		const r = await queryMessages({ scope: 'all', since: 'hour', from: 'CHI' }, f.deps, () => NOW);
		assert.ok(r.ok && r.count === 2 && r.rooms_unread === 1);
	});

	it('every room unreadable, or no rooms at all, is "unavailable"', async () => {
		assert.deepEqual(await queryMessages({ scope: 'all' }, fake(() => err('FORBIDDEN')).deps, () => NOW), { ok: false, error: 'unavailable' });
		assert.deepEqual(await queryMessages({ scope: 'all' }, fake(() => null, []).deps, () => NOW), { ok: false, error: 'unavailable' });
	});

	it('a word over all rooms is the server\'s full-text search, room ids named from the index', async () => {
		const f = fake((name, a) => {
			assert.equal(name, 'room.action.read');
			assert.equal(a.action, 'space.search.messages');
			assert.equal(a.target_kind, 'account');
			const x = args({ name, args: a });
			assert.equal(x.query, 'launch');
			assert.equal(x.after, new Date(NOW - 7 * 86_400_000).toISOString());
			return ok({ result: { results: [
				{ room_id: '!a:ag2.space', event_id: '$s1', sender: '@chi:ag2.space', snippet: 'launch video', origin_server_ts: NOW - 60_000 },
				{ room_id: '!z:ag2.space', event_id: '$s2', sender: ME, snippet: 'my launch', origin_server_ts: NOW - 30_000 },
				{ room_id: '!z:ag2.space', event_id: '$s3', sender: '@x:ag2.space', snippet: 'launch', origin_server_ts: NOW - 30 * 86_400_000 },
			], cursor: '' } });
		});
		const r = await queryMessages({ scope: 'all', since: 'week', contains: 'Launch' }, f.deps, () => NOW);
		assert.ok(r.ok);
		if (r.ok) { assert.equal(r.count, 1); assert.equal(r.latest[0].room, 'Alpha'); }
	});

	it('search pages follow the cursor and stop at the cap with "at least"; a refusal is its code', async () => {
		let n = 0;
		const f = fake(() => { n++; return ok({ result: { results: [{ room_id: '!a:ag2.space', event_id: `$s${n}`, sender: '@c:ag2.space', snippet: 'w', origin_server_ts: NOW }], cursor: `c${n}` } }); });
		const r = await queryMessages({ scope: 'all', since: 'day', contains: 'w', room: 'Alpha' }, f.deps, () => NOW);
		assert.equal(n, QUERY_SEARCH_MAX_PAGES);
		assert.deepEqual(args(f.calls.find(c => c.name === 'room.action.read')!).room_ids, ['!a:ag2.space']);
		assert.ok(r.ok && r.at_least);
		assert.deepEqual(await queryMessages({ scope: 'all', contains: 'w' }, fake(() => err('RATE_LIMITED')).deps, () => NOW), { ok: false, error: 'RATE_LIMITED' });
	});
});

describe('"new" is never consumed by a filtered or partial read', () => {
	const inbox = (items: unknown[], extra: Record<string, unknown> = {}) => () => ok({ result: { mentions: items, next_cursor: null, has_more: false, rooms_skipped: 0, ...extra } });
	const people = [item(1, { sender: { user_id: '@chi:ag2.space', display_name: 'Chi' } }), item(2, { sender: { user_id: '@bob:ag2.space', display_name: 'Bob' } }), item(3, { sender: { user_id: '@ann:ag2.space', display_name: 'Ann' } })];

	it('"new from Chi" leaves Bob\'s and Ann\'s new for the next "new"', async () => {
		const f = fake(inbox(people));
		const r1 = await queryMessages({ from: 'Chi' }, f.deps, () => NOW);
		assert.ok(r1.ok && r1.count === 1);
		const r2 = await queryMessages({}, f.deps, () => NOW + 1000);
		assert.ok(r2.ok && r2.count === 3);
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 3000), NOW + 1000);
	});

	it('a room, a word or unanswered_only is a filter too', async () => {
		for (const a of [{ room: 'Alpha' }, { contains: 'ping' }, { unanswered_only: true }]) {
			resetLastChecked();
			await queryMessages(a, fake(inbox(people)).deps, () => NOW);
			assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 5), NOW + 5 - QUERY_FIRST_WINDOW_MS, JSON.stringify(a));
		}
	});

	it('rooms the server skipped, more pages, or an unread room make it partial, and "new" stays', async () => {
		const skipped = await queryMessages({}, fake(inbox(people, { rooms_skipped: 1 })).deps, () => NOW);
		assert.ok(skipped.ok && !skipped.complete && skipped.rooms_skipped === 1);
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 5), NOW + 5 - QUERY_FIRST_WINDOW_MS);
		const half = fake((_n, a) => a.room_id === '!b:ag2.space' ? err('FORBIDDEN') : ok({ result: { messages: [], cursor: null } }));
		const r = await queryMessages({ scope: 'all' }, half.deps, () => NOW);
		assert.ok(r.ok && !r.complete && r.rooms_unread === 1 && r.rooms_read === 1);
		assert.equal(windowStart('last_check', 'all', NOW + 5), NOW + 5 - QUERY_FIRST_WINDOW_MS);
	});

	it('a later check never moves "new" back', async () => {
		const f = fake(inbox([]));
		await queryMessages({}, f.deps, () => NOW + 10_000);
		await queryMessages({}, f.deps, () => NOW);
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 20_000), NOW + 10_000);
	});

	it('a read past the budget is partial and does not move "new"', async () => {
		const f = fake(() => ok({ result: { messages: Array.from({ length: QUERY_ROOM_PAGE }, (_, i) => msg(i, '@c:ag2.space', 'x', 1)), cursor: 'more' } }));
		const r = await queryMessages({ scope: 'all' }, f.deps, () => NOW, 0);
		assert.ok(r.ok === false || !r.complete);
		assert.equal(windowStart('last_check', 'all', NOW + 5), NOW + 5 - QUERY_FIRST_WINDOW_MS);
		assert.ok(QUERY_BUDGET_MS < createQueryMessagesTool(f.deps).timeout!);
	});
});

describe('to_me is the owner\'s where the server offers it', () => {
	it('reads with scope "owner" and says whose', async () => {
		const f = fake(() => ok({ result: { mentions: [item(1)], next_cursor: null, has_more: false, rooms_skipped: 0 } }), ROOMS, { ownerId: OWNER, ownerScope: true });
		const r = await queryMessages({}, f.deps, () => NOW);
		assert.equal(args(f.calls[0]).scope, 'owner');
		assert.ok(r.ok && r.whose === 'owner');
	});

	it('a word is matched in the whole message, not the cut shown', async () => {
		const long = 'x'.repeat(600) + ' needle';
		const f = fake(() => ok({ result: { mentions: [item(1, { body: long })], next_cursor: null, has_more: false, rooms_skipped: 0 } }));
		const r = await queryMessages({ contains: 'needle' }, f.deps, () => NOW);
		assert.ok(r.ok && r.count === 1 && r.latest[0].text.endsWith('…'));
	});
});

describe('to_me without the owner\'s inbox: what mentions or answers the owner in every room this agent is in', () => {
	const ROOMS3 = [{ room_id: '!a:ag2.space', name: 'Alpha' }, { room_id: '!b:ag2.space', name: 'Beta' }, { room_id: '!u:ag2.space' }];
	const m = (i: number, sender: string, extra: Record<string, unknown> = {}, minutesAgo = 5) => ({ ...msg(i, sender, `msg ${i}`, minutesAgo), ...extra });
	const rooms = (byRoom: Record<string, unknown[]>) => (_n: string, a: Record<string, unknown>) => ok({ result: { messages: byRoom[a.room_id as string] ?? [], cursor: null } });

	it('the rule: named in mentions; with no mentions, its id in the body; or a reply to the owner', () => {
		assert.equal(addressedTo({ body: 'hi', mentions: [OWNER] }, OWNER), 'mention');
		assert.equal(addressedTo({ body: `hi ${OWNER}`, mentions: ['@x:ag2.space'] }, OWNER), null);
		assert.equal(addressedTo({ body: `hi ${OWNER}` }, OWNER), 'mention');
		assert.equal(addressedTo({ body: `hi ${OWNER}`, mentions: [] }, OWNER), 'mention');
		assert.equal(addressedTo({ body: 'ok', replyTo: OWNER }, OWNER), 'reply');
		assert.equal(addressedTo({ body: 'Susan, look', mentions: [] }, OWNER), null);
	});

	it('reads every room it is in, named or not, and keeps only what is for the owner', async () => {
		const f = fake(rooms({
			'!a:ag2.space': [m(1, '@chi:ag2.space', { mentions: [OWNER] }), m(2, '@chi:ag2.space', { mentions: ['@bob:ag2.space'] }), m(3, OWNER, { mentions: [OWNER] }), m(4, ME, { mentions: [OWNER] })],
			'!b:ag2.space': [m(5, '@yx:ag2.space', { reply_to_sender: OWNER }, 2)],
			'!u:ag2.space': [{ ...msg(6, '@ann:ag2.space', `ping ${OWNER}`, 1) }],
		}), ROOMS3, { ownerId: OWNER });
		const r = await queryMessages({ since: 'day' }, f.deps, () => NOW);
		assert.ok(f.calls.some(c => c.name === 'room.list'));
		assert.equal(f.calls.filter(c => c.name === 'room.action.read').length, 3);
		assert.ok(!f.calls.some(c => (c.args as { action?: string }).action === 'mentions.inbox'));
		assert.ok(r.ok);
		if (r.ok) {
			assert.equal(r.whose, 'owner');
			assert.equal(r.source, 'rooms');
			assert.equal(r.rooms_read, 3);
			assert.deepEqual(r.latest.map(x => [x.event_id, x.room, x.why]), [['$m6', 'an unnamed room', 'mention'], ['$m5', 'Beta', 'reply'], ['$m1', 'Alpha', 'mention']]);
			assert.equal(r.complete, true);
		}
	});

	it('the owner\'s inbox where the server offers it, not a scan', async () => {
		const f = fake(() => ok({ result: { mentions: [item(1)], next_cursor: null, has_more: false, rooms_skipped: 0 } }), ROOMS3, { ownerId: OWNER, ownerScope: true });
		const r = await queryMessages({}, f.deps, () => NOW);
		assert.ok(r.ok && r.whose === 'owner' && r.source === 'inbox');
		assert.ok(!f.calls.some(c => c.name === 'room.list'));
	});

	it('offered but refused: the scan, with the refusal', async () => {
		const f = fake((n, a) => (a.action === 'mentions.inbox' ? err('OWNER_SCOPE_NOT_GRANTED') : rooms({ '!a:ag2.space': [m(1, '@chi:ag2.space', { mentions: [OWNER] })] })(n, a)), ROOMS3, { ownerId: OWNER, ownerScope: true });
		const r = await queryMessages({ since: 'day' }, f.deps, () => NOW);
		assert.ok(r.ok && r.source === 'rooms' && r.whose === 'owner' && r.count === 1);
	});

	it('a room not read makes it partial; "new" is kept per owner and stays', async () => {
		const f = fake((n, a) => (a.room_id === '!b:ag2.space' ? err('FORBIDDEN') : ok({ result: { messages: [], cursor: null } })), ROOMS3, { ownerId: OWNER });
		const r = await queryMessages({}, f.deps, () => NOW);
		assert.ok(r.ok && !r.complete && r.rooms_unread === 1);
		assert.equal(windowStart('last_check', `to_me:${OWNER}`, NOW + 5), NOW + 5 - QUERY_FIRST_WINDOW_MS);
		const g = fake(rooms({}), ROOMS3, { ownerId: OWNER });
		await queryMessages({}, g.deps, () => NOW);
		assert.equal(windowStart('last_check', `to_me:${OWNER}`, NOW + 5), NOW);
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 5), NOW + 5 - QUERY_FIRST_WINDOW_MS);
	});

	it('a room read that fails once (an upstream 502) is tried again; a timeout is not', async () => {
		let n = 0;
		const f = fake((_n, a) => (a.room_id === '!a:ag2.space' && ++n === 1 ? new Error('room.action.read: AG2 MCP upstream failure (not retried): upstream http 502') : ok({ result: { messages: [m(1, '@chi:ag2.space', { mentions: [OWNER] })], cursor: null } })), ROOMS3, { ownerId: OWNER });
		const r = await queryMessages({ since: 'day' }, f.deps, () => NOW);
		assert.ok(r.ok && r.complete && !r.rooms_unread);
		assert.equal(f.calls.filter(c => c.args.room_id === '!a:ag2.space').length, 2);
		let k = 0;
		const g = fake((_n, a) => (a.room_id === '!a:ag2.space' ? (k++, new Error('tools/call timed out after 15000ms')) : ok({ result: { messages: [], cursor: null } })), ROOMS3, { ownerId: OWNER });
		const t = await queryMessages({ since: 'day' }, g.deps, () => NOW);
		assert.ok(t.ok && t.rooms_unread === 1);
		assert.equal(k, 1);
	});

	it('one malformed inbox item is skipped, never a crash', async () => {
		const f = fake(() => ok({ result: { mentions: [null, 7, { sender: null }, item(1)], next_cursor: null, has_more: false, rooms_skipped: 0 } }));
		const r = await queryMessages({}, f.deps, () => NOW);
		assert.ok(r.ok && r.count === 1);
	});
});

describe('"new" across calls: kept when a call ends (post-processing), read after a restart', () => {
	const inbox = () => ok({ result: { mentions: [], next_cursor: null, has_more: false, rooms_skipped: 0 } });

	it('a call that checked "new" leaves its point behind; a restarted process starts there', async () => {
		await queryMessages({}, fake(inbox).deps, () => NOW);
		recordCallEnded();
		assert.deepEqual(JSON.parse(readFileSync(heardFile, 'utf-8')), { 'to_me:this agent': NOW });
		resetLastChecked(heardFile);
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 60_000), NOW);
	});

	it('a call that never asked leaves the earlier point as it was', async () => {
		await queryMessages({}, fake(inbox).deps, () => NOW);
		recordCallEnded();
		resetLastChecked(heardFile);
		recordCallEnded();
		assert.deepEqual(JSON.parse(readFileSync(heardFile, 'utf-8')), { 'to_me:this agent': NOW });
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW + 60_000), NOW);
	});

	it('only a complete, unfiltered "new" moves it; the point never goes back', async () => {
		await queryMessages({}, fake(inbox).deps, () => NOW);
		recordCallEnded();
		resetLastChecked(heardFile);
		await queryMessages({ from: 'Chi' }, fake(inbox).deps, () => NOW + 5_000);
		await queryMessages({}, fake(inbox).deps, () => NOW - 5_000);
		recordCallEnded();
		assert.deepEqual(JSON.parse(readFileSync(heardFile, 'utf-8')), { 'to_me:this agent': NOW });
	});

	it('no file yet, or a broken one: the last day', () => {
		assert.equal(windowStart('last_check', 'to_me:this agent', NOW), NOW - QUERY_FIRST_WINDOW_MS);
		resetLastChecked(join(TMP, 'missing', 'x.json'));
		assert.equal(windowStart('last_check', 'all', NOW), NOW - QUERY_FIRST_WINDOW_MS);
	});

	it('the voice agent runs skills\' call-ended steps from its hang-up report, before telling the core', () => {
		const src = readFileSync(join(process.cwd(), 'src', 'voice-agent.ts'), 'utf-8');
		assert.match(src, /onCallEnded: \(handler\) => \{ callEndedHandlers\.push\(handler\); \}/);
		const onEnded = src.slice(src.indexOf('const callEnd = createCallEndReporter('), src.indexOf('beforeConversationClear = () => callEnd.collect()'));
		assert.ok(onEnded.indexOf('for (const h of callEndedHandlers)') >= 0 && onEnded.indexOf('for (const h of callEndedHandlers)') < onEnded.indexOf('submitVoiceSessionEndTask'));
	});

	it('the plugin registers it as the voice agent\'s call-ended step', async () => {
		const { setup } = await import('../skills/ag2space-voice/tools.ts');
		let handler: (() => unknown) | null = null;
		setup({ onCallEnded: (h: () => unknown) => { handler = h; } } as never);
		assert.ok(handler);
		await queryMessages({}, fake(inbox).deps, () => NOW);
		await handler!();
		assert.deepEqual(JSON.parse(readFileSync(heardFile, 'utf-8')), { 'to_me:this agent': NOW });
	});
});

describe('answers, never throws', () => {
	it('no client, an unknown room, an inbox refusal', async () => {
		const f = fake(() => err('FORBIDDEN'), []);
		assert.deepEqual(await queryMessages({}, { ...f.deps, client: () => null }), { ok: false, error: 'not_connected' });
		const nf = await queryMessages({ room: 'Gamma' }, f.deps, () => NOW);
		assert.equal(nf.ok, false);
		assert.deepEqual(await queryMessages({}, f.deps, () => NOW), { ok: false, error: 'FORBIDDEN' });
	});

	it('the tool answers through queryMessages, and the rules name it', async () => {
		const f = fake(() => ok({ result: { mentions: [], next_cursor: null, has_more: false } }));
		const t = createQueryMessagesTool(f.deps);
		assert.equal(t.name, 'query_messages');
		assert.equal(t.execution, 'inline');
		assert.equal(((await t.execute({}, {} as never)) as { ok: boolean }).ok, true);
		assert.equal(((await createQueryMessagesTool({ ...f.deps, client: () => null }).execute({ scope: 'all', room: 'Alpha', from: 'x', contains: 'y', since: 'hour' }, {} as never)) as { ok: boolean }).ok, false);
		assert.match(t.description, /no read\/unread marker/);
		assert.match(QUERY_PROMPT_RULE, /query_messages/);
		assert.match(QUERY_TURN_CHECK_RULE, /^- none:/);
	});
});
}
