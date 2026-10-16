// query_messages: a question over every room at once ("my latest message", "how many new messages"),
// answered by counting. No read marker is visible to the agent, so "new" is since the last full check.

import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { fetchInbox, ownerInboxSupported, toInboxItems, type InboxWhose } from './inbox.js';
import { statusPath } from '../../src/workspace_default.js';
import { listRooms, roomRead, targetRoom, ROOM_COMMONS_TIMEOUT_MS, ROOM_INDEX_COLD_TIMEOUT_MS, ROOM_NAME_ERRORS, ROOM_READ_BODY_MAX, ROOM_READ_CALL_TIMEOUT_MS, type McpClient, type ReadRoomDeps, type RoomRef } from './room-read.js';

export const QUERY_DEFAULT_LATEST = 5;
export const QUERY_MAX_LATEST = 20;
/** room.context.read pages newest first (up to 1000 a page); a room busier than this in the window is "at least". */
export const QUERY_ROOM_PAGE = 200;
export const QUERY_ROOM_MAX_PAGES = 2;
export const QUERY_ROOM_CONCURRENCY = 6;
/** A word over every room goes to the server's full-text search instead (space.search.messages): 50 a page. */
export const QUERY_SEARCH_PAGE = 50;
export const QUERY_SEARCH_MAX_PAGES = 4;
/** "new" with no full check before in this voice process: the last day. */
export const QUERY_FIRST_WINDOW_MS = 24 * 3_600_000;
/** All reads stop starting after this; what was not read is reported, and the tool's own timeout is longer. */
export const QUERY_BUDGET_MS = 30_000;

export type QuerySince = 'last_check' | 'hour' | 'today' | 'day' | 'week';
export type QueryScope = 'to_me' | 'all';

export interface QueryHit { at: string; room: string; from: string; text: string; event_id: string; why?: string }

export type QueryResult =
	| {
		ok: true; scope: QueryScope; since: string; count: number; complete: boolean; at_least: boolean;
		by_room: Array<{ room: string; count: number }>; latest: QueryHit[];
		rooms_read?: number; rooms_unread?: number; rooms_skipped?: number; whose?: InboxWhose; owner_error?: string; ms: number;
	}
	| { ok: false; error: string; candidates?: string[]; rooms?: string[] };

const fmt = (ms: number, tz?: string) => new Date(ms).toLocaleString('en-US', { timeZone: tz, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short' });
const short = (id: string) => id.replace(/^@/, '').replace(/:.*$/, '');
const norm = (s: string) => s.toLowerCase().replace(/\s+/g, ' ').trim();

/** When "new" last meant now, per scope and owner, during this process; never moves back. */
const lastChecked = new Map<string, number>();
/** What earlier calls left behind: written when a call ends (the voice agent's post-processing), read once at first use. */
let heard: Map<string, number> | null = null;
let heardPath: string | null = null;
const heardFile = () => heardPath ?? statusPath('voice-query-heard.json');

/** Tests: nothing checked, and the call-end record kept in `path`. */
export function resetLastChecked(path: string | null = null): void { lastChecked.clear(); heard = null; heardPath = path; }

function readHeard(): Map<string, number> {
	if (heard) return heard;
	heard = new Map();
	try {
		for (const [k, v] of Object.entries(JSON.parse(readFileSync(heardFile(), 'utf-8')) as Record<string, unknown>)) {
			if (typeof v === 'number' && Number.isFinite(v)) heard.set(k, v);
		}
	} catch { /* no call has ended yet, or unreadable: start from the last day */ }
	return heard;
}

/** Post-processing for a call that ended: what this process checked becomes "heard", for the next call and a restart. */
export function recordCallEnded(): void {
	const merged = readHeard();
	let changed = false;
	for (const [k, v] of lastChecked) if (v > (merged.get(k) ?? 0)) { merged.set(k, v); changed = true; }
	if (!changed) return;
	const path = heardFile();
	mkdirSync(dirname(path), { recursive: true });
	const tmp = `${path}.${process.pid}.tmp`;
	writeFileSync(tmp, JSON.stringify(Object.fromEntries(merged)));
	renameSync(tmp, path);
}

/** The start of the window a `since` names; `key` is what "new" is kept for (the scope, and whose messages). */
export function windowStart(since: QuerySince, key: string, now: number): number {
	switch (since) {
		case 'hour': return now - 3_600_000;
		case 'day': return now - 86_400_000;
		case 'week': return now - 7 * 86_400_000;
		case 'today': { const d = new Date(now); d.setHours(0, 0, 0, 0); return d.getTime(); }
		default: return lastChecked.get(key) ?? readHeard().get(key) ?? now - QUERY_FIRST_WINDOW_MS;
	}
}

interface Msg { ts: number; room: string; from: string; fromId: string; body: string; event_id: string; why?: string; mentions?: string[]; replyTo?: string }

/** Why a room message is addressed to `owner`, by the rule the server's inbox uses: named in its mentions, or (with none) its id in the body; or a reply to the owner. */
export function addressedTo(m: Pick<Msg, 'body' | 'mentions' | 'replyTo'>, owner: string): 'mention' | 'reply' | null {
	if (m.mentions?.length ? m.mentions.includes(owner) : m.body.includes(owner)) return 'mention';
	if (m.replyTo === owner) return 'reply';
	return null;
}

/** One room's messages since `start`, newest first, paged back; `full` when the pages or the budget ran out inside the window. */
async function roomSince(client: McpClient, room: RoomRef, start: number, deadline: number, deps: ReadRoomDeps): Promise<{ ok: true; msgs: Msg[]; full: boolean } | { ok: false }> {
	const msgs: Msg[] = [];
	let before: string | null = null;
	for (let page = 0; page < QUERY_ROOM_MAX_PAGES; page++) {
		if (Date.now() > deadline) return page === 0 ? { ok: false } : { ok: true, msgs, full: true };
		const readArgs = { limit: QUERY_ROOM_PAGE, ...(before ? { before } : {}) };
		let read = await roomRead(client, room, 'room.context.read', readArgs, deps, ROOM_COMMONS_TIMEOUT_MS);
		// A read is safe to repeat: one more try after a failed call (an upstream 502 is "unavailable"), never after a timeout.
		if (!read.ok && read.error === 'unavailable' && Date.now() <= deadline) read = await roomRead(client, room, 'room.context.read', readArgs, deps, ROOM_COMMONS_TIMEOUT_MS);
		if (!read.ok) { deps.log?.(`[RoomQuery] ${room.name}: ${read.error}`); return { ok: false }; }
		const list = Array.isArray(read.result.messages) ? read.result.messages : [];
		let older = false;
		for (const m of list) {
			const x = m as { sender?: unknown; body?: unknown; ts?: unknown; event_id?: unknown; mentions?: unknown; reply_to_sender?: unknown };
			const ts = Number(x.ts);
			if (!Number.isFinite(ts)) continue;
			if (ts < start) { older = true; continue; }
			if (typeof x.body !== 'string' || !x.body.trim() || typeof x.event_id !== 'string') continue;
			const sender = typeof x.sender === 'string' ? x.sender : '';
			const mentions = Array.isArray(x.mentions) ? x.mentions.filter((u): u is string => typeof u === 'string') : undefined;
			const replyTo = typeof x.reply_to_sender === 'string' ? x.reply_to_sender : undefined;
			msgs.push({ ts, room: room.name, from: short(sender || '?'), fromId: sender, body: x.body, event_id: x.event_id, ...(mentions ? { mentions } : {}), ...(replyTo ? { replyTo } : {}) });
		}
		before = typeof read.result.cursor === 'string' && read.result.cursor ? read.result.cursor : null;
		if (older || !before || list.length < QUERY_ROOM_PAGE) return { ok: true, msgs, full: false };
	}
	return { ok: true, msgs, full: true };
}

/** space.search.messages since `start`, newest first, paged; `full` when the pages or the budget ran out with more to come. */
async function searchSince(client: McpClient, query: string, start: number, deadline: number, room: RoomRef | null, names: Map<string, string>, deps: ReadRoomDeps): Promise<{ ok: true; msgs: Msg[]; full: boolean } | { ok: false; error: string }> {
	const msgs: Msg[] = [];
	let cursor: string | null = null;
	for (let page = 0; page < QUERY_SEARCH_MAX_PAGES; page++) {
		if (page > 0 && Date.now() > deadline) return { ok: true, msgs, full: true };
		const read = await roomRead(client, null, 'space.search.messages', { query, limit: QUERY_SEARCH_PAGE, kind: 'messages', after: new Date(start).toISOString(), ...(room ? { room_ids: [room.room_id] } : {}), ...(cursor ? { cursor } : {}) }, deps, ROOM_COMMONS_TIMEOUT_MS);
		if (!read.ok) return read;
		for (const h of Array.isArray(read.result.results) ? read.result.results : []) {
			const x = h as { room_id?: unknown; event_id?: unknown; sender?: unknown; snippet?: unknown; origin_server_ts?: unknown };
			const ts = Number(x.origin_server_ts);
			if (typeof x.event_id !== 'string' || typeof x.snippet !== 'string' || !Number.isFinite(ts) || ts < start) continue;
			const roomId = typeof x.room_id === 'string' ? x.room_id : '?';
			const sender = typeof x.sender === 'string' ? x.sender : '';
			msgs.push({ ts, room: names.get(roomId) ?? roomId, from: short(sender || '?'), fromId: sender, body: x.snippet, event_id: x.event_id });
		}
		cursor = typeof read.result.cursor === 'string' && read.result.cursor ? read.result.cursor : null;
		if (!cursor) return { ok: true, msgs, full: false };
	}
	return { ok: true, msgs, full: true };
}

/** Run `fn` over `items`, `n` at a time. */
async function pool<T, R>(items: T[], n: number, fn: (t: T) => Promise<R>): Promise<R[]> {
	const out: R[] = new Array(items.length);
	let next = 0;
	await Promise.all(Array.from({ length: Math.min(n, items.length) }, async () => {
		while (next < items.length) { const i = next++; out[i] = await fn(items[i]); }
	}));
	return out;
}

/** This agent's id and its owner's, from ag2.whoami; null where it cannot say. */
export async function whoami(client: McpClient): Promise<{ self: string | null; owner: string | null }> {
	try {
		const res = await client.callTool('ag2.whoami', {}, ROOM_READ_CALL_TIMEOUT_MS);
		const p = (res.structuredContent ?? null) as { actor?: { id?: unknown }; runtime?: { owner_id?: unknown } } | null;
		const str = (v: unknown) => (typeof v === 'string' && v ? v : null);
		return { self: str(p?.actor?.id), owner: str(p?.runtime?.owner_id) };
	} catch { return { self: null, owner: null }; }
}

/** Read rooms back to the window, n at a time; `unread` counts the rooms that could not be read. */
async function readRooms(client: McpClient, rooms: RoomRef[], start: number, deadline: number, deps: ReadRoomDeps): Promise<{ msgs: Msg[]; full: boolean; unread: number }> {
	const per = await pool(rooms, QUERY_ROOM_CONCURRENCY, r => roomSince(client, r, start, deadline, deps));
	const msgs: Msg[] = [];
	let full = false;
	let unread = 0;
	for (const p of per) {
		if (!p.ok) { unread++; continue; }
		if (p.full) full = true;
		msgs.push(...p.msgs);
	}
	return { msgs, full, unread };
}

export interface QueryArgs { scope?: QueryScope; since?: QuerySince; room?: string; from?: string; contains?: string; unanswered_only?: boolean; latest?: number }

export async function queryMessages(args: QueryArgs, deps: ReadRoomDeps, now: () => number = Date.now, budgetMs = QUERY_BUDGET_MS): Promise<QueryResult> {
	const t0 = Date.now();
	const deadline = t0 + budgetMs;
	const client = deps.client();
	if (!client) return { ok: false, error: 'not_connected' };
	const scope: QueryScope = args.scope === 'all' ? 'all' : 'to_me';
	const since: QuerySince = (['hour', 'today', 'day', 'week'] as const).find(s => s === args.since) ?? 'last_check';
	const ids = await whoami(client);
	// "To me" is the owner's: their inbox where the server offers it, else what mentions or answers them in this agent's rooms.
	const ownerInbox = scope === 'to_me' && !!ids.owner && await ownerInboxSupported(client, now);
	const key = scope === 'all' ? 'all' : ids.owner ? `to_me:${ids.owner}` : 'to_me:this agent';
	const asked = now();
	const start = windowStart(since, key, asked);
	const latestN = Math.min(QUERY_MAX_LATEST, Math.max(1, Math.floor(args.latest ?? QUERY_DEFAULT_LATEST)));
	let room: RoomRef | null = null;
	if ((args.room ?? '').trim()) {
		const t = await targetRoom(args.room, deps);
		if (!t.ok) return t;
		room = t.room;
	}
	const names = new Map(deps.index.rooms().map(r => [r.room_id, r.name] as const));
	const word = norm(args.contains ?? '');
	const from = norm(args.from ?? '');

	let msgs: Msg[] = [];
	let atLeast = false;
	const extra: { rooms_read?: number; rooms_unread?: number; rooms_skipped?: number; whose?: InboxWhose; source?: 'inbox' | 'rooms'; owner_error?: string } = {};
	const fromInbox = async (owner: boolean): Promise<QueryResult | null> => {
		const inboxArgs = { since: new Date(start).toISOString(), include_replied: !args.unanswered_only, owner, deadline, ...(room ? { room_ids: [room.room_id] } : {}) };
		// The owner's inbox refused here (not enabled, revoked) is not an answer: the room scan below is.
		const got = await fetchInbox(client, inboxArgs, deps);
		if (!got.ok) {
			if (owner && !['timeout', 'unavailable', 'bad_response'].includes(got.error)) { extra.owner_error = got.error; return null; }
			return got;
		}
		atLeast = got.more;
		extra.whose = owner ? 'owner' : 'this agent';
		extra.source = 'inbox';
		if (got.rooms_skipped) extra.rooms_skipped = got.rooms_skipped;
		for (const raw of got.items) {
			if (!raw || typeof raw !== 'object') continue;
			const it = raw as { sender?: { user_id?: unknown } | null; sent_at?: unknown; body?: unknown };
			const ts = typeof it.sent_at === 'string' ? Date.parse(it.sent_at) : NaN;
			const [line] = toInboxItems([raw]);
			if (!line || !Number.isFinite(ts) || typeof it.body !== 'string') continue;
			const name = line.room.startsWith('!') ? names.get(line.room) ?? (line.why === 'dm' ? 'a direct message' : line.room) : line.room;
			const sender = it.sender && typeof it.sender === 'object' && typeof it.sender.user_id === 'string' ? it.sender.user_id : '';
			msgs.push({ ts, room: name, from: line.from, fromId: sender, body: it.body, event_id: line.event_id, why: line.why });
		}
		return null;
	};
	if (scope === 'to_me' && (ownerInbox || !ids.owner)) {
		const err = await fromInbox(ownerInbox);
		if (err) return err;
	}
	if (scope === 'to_me' && ids.owner && extra.source !== 'inbox') {
		// No owner inbox here (or refused): the owner's mentions and replies in every room this agent is in, named or not.
		const rooms = room ? [room] : await listRooms(client, ROOM_INDEX_COLD_TIMEOUT_MS, { unnamed: true }).catch(() => deps.index.rooms());
		if (!rooms.length) return { ok: false, error: 'unavailable' };
		const got = await readRooms(client, rooms, start, deadline, deps);
		if (got.unread === rooms.length) return { ok: false, error: 'unavailable' };
		atLeast = got.full;
		for (const m of got.msgs) {
			if (m.fromId === ids.owner || m.fromId === ids.self) continue;
			const why = addressedTo(m, ids.owner);
			if (why) msgs.push({ ...m, why });
		}
		extra.whose = 'owner';
		extra.source = 'rooms';
		extra.rooms_read = rooms.length - got.unread;
		if (got.unread) extra.rooms_unread = got.unread;
	} else if (scope === 'all' && word) {
		const got = await searchSince(client, word, start, deadline, room, names, deps);
		if (!got.ok) return got;
		atLeast = got.full;
		msgs = got.msgs.filter(m => !ids.self || m.fromId !== ids.self);
	} else if (scope === 'all') {
		let rooms = room ? [room] : deps.index.rooms();
		if (!rooms.length) rooms = await deps.index.refresh().catch(() => []);
		if (!rooms.length) return { ok: false, error: 'unavailable' };
		const got = await readRooms(client, rooms, start, deadline, deps);
		if (got.unread === rooms.length) return { ok: false, error: 'unavailable' };
		atLeast = got.full;
		msgs = got.msgs.filter(m => !ids.self || m.fromId !== ids.self);
		extra.rooms_read = rooms.length - got.unread;
		if (got.unread) extra.rooms_unread = got.unread;
	}

	if (from) msgs = msgs.filter(m => norm(m.from).includes(from) || norm(short(m.fromId)).includes(from));
	if (word) msgs = msgs.filter(m => norm(m.body).includes(word));
	msgs.sort((a, b) => b.ts - a.ts);

	const counts = new Map<string, number>();
	for (const m of msgs) counts.set(m.room, (counts.get(m.room) ?? 0) + 1);
	const by_room = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6).map(([r, count]) => ({ room: r, count }));
	const latest = msgs.slice(0, latestN).map(m => {
		const text = m.body.replace(/\s+/g, ' ').trim();
		return { at: fmt(m.ts), room: m.room, from: m.from, text: text.length > ROOM_READ_BODY_MAX ? `${text.slice(0, ROOM_READ_BODY_MAX)}…` : text, event_id: m.event_id, ...(m.why ? { why: m.why } : {}) };
	});
	const complete = !atLeast && !extra.rooms_unread && !extra.rooms_skipped;
	// "New" moves only past what was fully read and not filtered: a "new from Chi" leaves everyone else's new.
	const unfiltered = !room && !from && !word && !args.unanswered_only;
	if (since === 'last_check' && complete && unfiltered && Date.now() <= deadline) lastChecked.set(key, Math.max(lastChecked.get(key) ?? 0, asked));
	return { ok: true, scope, since: fmt(start), count: msgs.length, complete, at_least: atLeast, by_room, latest, ...extra, ms: Date.now() - t0 };
}

export function createQueryMessagesTool(deps: ReadRoomDeps): ToolDefinition {
	return {
		name: 'query_messages',
		description:
			'Count and list AG2 Space messages across all your rooms at once, right now (a few seconds) — for questions that name no single room: "what is my latest message", "how many new messages / notifications do I have", "anything new", "anything from Chi today", "how many messages mention the launch this week". ' +
			'Call it every time such a question is asked, even right after a similar one: counts change, and an earlier answer or earlier search_room results are never the count now. ' +
			'`scope`: "to_me" (default) — messages for the user: those that @-mention them or reply to them in every room you are in (`source` "rooms"), or the user\'s own inbox where the server offers it, which also has their direct messages (`source` "inbox"); `whose` "owner". Without an owner it reads your own inbox (`whose` "this agent" — say so). "all" — every message in your named rooms, your own left out. ' +
			'`source` "rooms" covers only rooms you are in: a mention in a room you are not in, or a direct message to the user, is not seen — say so when the user asks whether anyone is looking for them. ' +
			'`since`: "last_check" (default — new since the last complete check, in this call or the last call where the user asked; the first time, the last day), "hour", "today", "day", "week". The server keeps no read/unread marker you can see, so "new" means since the last check — say so if the user asks about unread. ' +
			'Optional filters: `room` (as listed in ROOMS), `from` (a person\'s name), `contains` (a word), `unanswered_only` (to_me from the inbox only). ' +
			'Returns {ok:true, scope, since, count, complete, at_least, by_room:[{room, count}], latest:[{at, room, from, text, event_id, why}], rooms_read, rooms_unread, rooms_skipped, whose, source} newest first — answer with the count and the latest one or two in a sentence or two. ' +
			'complete:false means the count is partial — at_least:true is "at least <count>", rooms_unread or rooms_skipped rooms were not read — say so, and never say there is nothing new unless complete is true. From the inbox, the server reads only each room\'s latest 50 messages, so over a long window it can come up short. To read around a message, call read_room with its room and `around` = its event_id. ' +
			'Errors: ' + ROOM_NAME_ERRORS + '"not_connected", "timeout" or "unavailable" — say the messages cannot be counted right now.',
		parameters: z.object({
			scope: z.enum(['to_me', 'all']).optional().describe('"to_me" (default): addressed to the user; "all": every message in your rooms.'),
			since: z.enum(['last_check', 'hour', 'today', 'day', 'week']).optional().describe('The window; default "last_check" = new since last asked.'),
			room: z.string().optional().describe('Only this room, e.g. "Sutando Triage". Omit for all rooms.'),
			from: z.string().max(80).optional().describe('Only messages from this person, by name, e.g. "Chi".'),
			contains: z.string().max(200).optional().describe('Only messages containing this word or phrase.'),
			unanswered_only: z.boolean().optional().describe('to_me only: leave out what was already answered.'),
			latest: z.number().int().min(1).max(QUERY_MAX_LATEST).optional().describe(`How many of the newest to return (default ${QUERY_DEFAULT_LATEST}).`),
		}),
		execution: 'inline',
		// The budget stops new reads; the calls already started finish within one call timeout, then the index may be cold.
		timeout: QUERY_BUDGET_MS + ROOM_COMMONS_TIMEOUT_MS + ROOM_INDEX_COLD_TIMEOUT_MS + 2 * ROOM_READ_CALL_TIMEOUT_MS + 1000,
		async execute(args) {
			const a = (args ?? {}) as QueryArgs;
			const r = await queryMessages(a, deps);
			deps.log?.(`[RoomQuery] ${a.scope ?? 'to_me'} since ${a.since ?? 'last_check'}${a.room ? ` in "${a.room}"` : ''}${a.from ? ` from "${a.from}"` : ''}${a.contains ? ` with "${a.contains}"` : ''} → ${r.ok ? `${r.at_least ? '≥' : ''}${r.count}${r.complete ? '' : ' (partial)'} in ${r.ms}ms` : r.error}`);
			return r;
		},
	};
}

export const QUERY_PROMPT_RULE = '- AG2 SPACE ACROSS ROOMS: whether anyone mentioned, tagged, @-ed, replied to or is looking for the user, which rooms have new notifications for them, and any count or list of messages that names no single room — "my latest message", "how many new messages / notifications", "anything new", "anything from <person> today", in any wording → query_messages, every time, even if you counted a moment ago: never answer from an earlier answer or earlier search_room results, and never search room after room for the user\'s name instead. Scope "to_me" (the default) for what is for the user, "all" for everything in your rooms. "New" means since the last complete check; there is no unread marker. A partial result (complete:false) is said as partial, and with source "rooms" say that rooms you are not in and direct messages are not covered. Never submit work for it.';

export const QUERY_TURN_CHECK_RULE = '- none: a count or list of AG2 Space messages or notifications across rooms ("my latest message", "how many new messages", "any new notifications", "anything from <person> today") is the assistant\'s own to answer, never work.';
