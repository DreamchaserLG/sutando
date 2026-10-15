// read_inbox: what is addressed to this agent across all its rooms (mentions.inbox, read as the
// account) — mentions, replies and DMs, newest first. The owner's own inbox is not readable by a
// linked agent today; the tool says whose inbox it is.

import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { roomRead, ROOM_COMMONS_TIMEOUT_MS, ROOM_READ_BODY_MAX, type ReadRoomDeps } from './room-read.js';

export const INBOX_DEFAULT_LIMIT = 10;
export const INBOX_MAX_LIMIT = 30;
export const INBOX_DEFAULT_DAYS = 7;
export const INBOX_MAX_DAYS = 30;
/** mentions.inbox lists oldest first, at most 100 a page; the newest are on the last page. */
export const INBOX_PAGE = 100;
export const INBOX_MAX_PAGES = 5;

export interface InboxItem { at: string; room: string; from: string; text: string; why: string; event_id: string; answered?: boolean }

/** mentions.inbox items (oldest first) → newest-first lines the model can retell. */
export function toInboxItems(items: unknown[], tz?: string): InboxItem[] {
	const out: InboxItem[] = [];
	for (const m of items) {
		if (!m || typeof m !== 'object') continue;
		const it = m as { room_id?: string; room_name?: string | null; event_id?: string; sender?: { user_id?: string; display_name?: string | null }; sent_at?: string | null; body?: string; reason?: string; replied?: boolean };
		if (typeof it.event_id !== 'string' || typeof it.body !== 'string' || !it.body.trim()) continue;
		const from = it.sender?.display_name || (it.sender?.user_id ?? '?').replace(/^@/, '').replace(/:.*$/, '');
		const t = it.sent_at ? Date.parse(it.sent_at) : NaN;
		const at = Number.isFinite(t) ? new Date(t).toLocaleString('en-US', { timeZone: tz, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short' }) : '';
		const text = it.body.replace(/\s+/g, ' ').trim();
		out.push({
			at, room: it.room_name || it.room_id || '?', from,
			text: text.length > ROOM_READ_BODY_MAX ? `${text.slice(0, ROOM_READ_BODY_MAX)}…` : text,
			why: String(it.reason ?? 'mention'), event_id: it.event_id,
			...(it.replied ? { answered: true } : {}),
		});
	}
	return out.reverse();
}

export type ReadInboxResult =
	| { ok: true; whose: 'this agent'; items: InboxItem[]; more: boolean; ms: number }
	| { ok: false; error: string };

/** What is addressed to this agent across its rooms (mentions.inbox, read as the agent), newest first. */
export async function readInbox(args: { limit?: number; days?: number; include_answered?: boolean }, deps: ReadRoomDeps, now: () => number = Date.now): Promise<ReadInboxResult> {
	const t0 = Date.now();
	const client = deps.client();
	if (!client) return { ok: false, error: 'not_connected' };
	const limit = Math.min(INBOX_MAX_LIMIT, Math.max(1, Math.floor(args.limit ?? INBOX_DEFAULT_LIMIT)));
	const days = Math.min(INBOX_MAX_DAYS, Math.max(1, Math.floor(args.days ?? INBOX_DEFAULT_DAYS)));
	const since = new Date(now() - days * 86_400_000).toISOString();
	const all: unknown[] = [];
	let cursor: string | null = null;
	let more = false;
	for (let page = 0; page < INBOX_MAX_PAGES; page++) {
		const read = await roomRead(client, null, 'mentions.inbox', { limit: INBOX_PAGE, since, include_replied: !!args.include_answered, ...(cursor ? { cursor } : {}) }, deps, ROOM_COMMONS_TIMEOUT_MS);
		if (!read.ok) return read;
		if (Array.isArray(read.result.mentions)) all.push(...read.result.mentions);
		cursor = typeof read.result.next_cursor === 'string' ? read.result.next_cursor : null;
		more = read.result.has_more === true;
		if (!more || !cursor) break;
	}
	const items = toInboxItems(all);
	return { ok: true, whose: 'this agent', items: items.slice(0, limit), more: more || items.length > limit, ms: Date.now() - t0 };
}

export function createReadInboxTool(deps: ReadRoomDeps): ToolDefinition {
	return {
		name: 'read_inbox',
		description:
			'Read this agent\'s AG2 Space inbox, right now (a few seconds): messages in any of its rooms that @-mention this agent, reply to it, or are sent to it directly. ' +
			'Call it when the user asks whether anyone is looking for them or for you, who mentioned or messaged you, or what is waiting for an answer. ' +
			'It is this agent\'s inbox, not the user\'s own account: a mention of the user\'s personal account is not in it — say so when it matters. ' +
			'Unanswered items only, unless `include_answered`. Returns {ok:true, items:[{at, room, from, text, why, event_id}], more} newest first — answer in a sentence or two: who, where, about what; to read the discussion around one, call read_room with that room and `around` = its event_id. ' +
			'No items means nothing is waiting; say so. Errors: "not_connected", "timeout" or "unavailable" — say the inbox cannot be read right now.',
		parameters: z.object({
			limit: z.number().int().min(1).max(INBOX_MAX_LIMIT).optional().describe(`How many items (default ${INBOX_DEFAULT_LIMIT}).`),
			days: z.number().int().min(1).max(INBOX_MAX_DAYS).optional().describe(`How far back, in days (default ${INBOX_DEFAULT_DAYS}).`),
			include_answered: z.boolean().optional().describe('Also list items already answered.'),
		}),
		execution: 'inline',
		timeout: INBOX_MAX_PAGES * ROOM_COMMONS_TIMEOUT_MS + 1000,
		async execute(args) {
			const a = (args ?? {}) as { limit?: number; days?: number; include_answered?: boolean };
			const r = await readInbox(a, deps);
			deps.log?.(`[RoomActions] read_inbox → ${r.ok ? `${r.items.length} items${r.more ? ' (more)' : ''} in ${r.ms}ms` : r.error}`);
			return r;
		},
	};
}

export const INBOX_PROMPT_RULE = '- AG2 SPACE INBOX: "is anyone looking for me / for you", "who mentioned you", "what is waiting for an answer" → read_inbox, every time, never from memory. It is your own inbox, not the user\'s personal account — say so when it matters. Never submit work for it.';

export const INBOX_TURN_CHECK_RULE = '- none: the AG2 Space inbox ("anyone looking for me", "who mentioned you", "what is waiting for an answer") is the assistant\'s own to read, never work.';
