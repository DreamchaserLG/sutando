/**
 * edit_project_file — change a LaTeX or Typst project file by voice without the voice model writing the file.
 *
 * The voice model passes a short instruction; a text model turns it into old_text/new_text edits against the
 * file it was read from, and room.commons.changes.apply applies them on that content_hash.
 */
import { randomUUID } from 'node:crypto';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { resolveCredential } from '../../src/credential-resolver.js';
import { WORK_CHECK_MODEL } from '../../src/work-request-check.js';
import {
	OUTCOME_UNKNOWN, OUTCOME_UNKNOWN_ERROR, ROOM_COMMONS_TIMEOUT_MS, ROOM_INDEX_COLD_TIMEOUT_MS, ROOM_NAME_ERRORS,
	roomExecute, roomRead, targetRoom, type ReadRoomDeps,
} from './room-read.js';

/** The most of a file read in one call; a longer file is refused rather than edited from a part. */
export const EDIT_FILE_MAX_BYTES = 262_144;
export const EDIT_PLAN_TIMEOUT_MS = 30_000;

export interface Edit { old_text: string; new_text: string }
export type EditPlan = { ok: true; edits: Edit[]; summary: string } | { ok: false; error: string };
/** Turns an instruction into edits against `text`; `problem` says why the previous plan could not be applied. */
export type PlanEdits = (input: { title: string; text: string; instruction: string; problem?: string }) => Promise<EditPlan>;

export type EditFileResult =
	| { ok: true; room: string; title: string; summary: string; edits: number; ms: number; next: string }
	| { ok: false; error: string; detail?: string; done?: false; next?: string; candidates?: string[]; rooms?: string[] };

export const EDIT_DONE_NEXT = 'Done. Tell the user in one sentence what you changed ("I have …"), in which file.';
export const EDIT_NOT_DONE_NEXT = 'Nothing was changed. Tell the user in a sentence what went wrong; do not say it is done.';

/** Each edit's old_text must occur exactly once in the text the edits before it left. */
export function applyEdits(text: string, edits: readonly Edit[]): { ok: true; text: string } | { ok: false; problem: string } {
	let out = text;
	for (const [i, e] of edits.entries()) {
		const at = out.indexOf(e.old_text);
		if (!e.old_text || at < 0) return { ok: false, problem: `edit ${i + 1}: old_text does not occur in the file` };
		if (out.indexOf(e.old_text, at + 1) >= 0) return { ok: false, problem: `edit ${i + 1}: old_text occurs more than once; quote more of it` };
		out = out.slice(0, at) + e.new_text + out.slice(at + e.old_text.length);
	}
	return { ok: true, text: out };
}

export function editPlanPrompt(input: Parameters<PlanEdits>[0]): string {
	return [
		'You edit one source file of a LaTeX or Typst project. Make the smallest change that does what the instruction asks, and keep the file compiling.',
		`Instruction: ${input.instruction}`,
		...(input.problem ? [`Your previous edits could not be applied: ${input.problem}. Copy old_text exactly from the file.`] : []),
		'Reply with JSON only:',
		'{"edits":[{"old_text":"…","new_text":"…"}],"summary":"<what you changed, one short sentence in the instruction\'s language>"}',
		'Each old_text is copied exactly from the file (whitespace included), long enough to occur exactly once, and no longer than that needs.',
		'If the instruction cannot be done on this file: {"error":"<one sentence why>"}.',
		`File "${input.title}":`,
		'<<<FILE',
		input.text,
		'FILE>>>',
	].join('\n');
}

export function parseEditPlan(raw: string): EditPlan {
	let v: unknown;
	try { v = JSON.parse(raw); } catch { return { ok: false, error: 'the editor gave no usable answer' }; }
	const o = (v ?? {}) as { edits?: unknown; summary?: unknown; error?: unknown };
	if (typeof o.error === 'string' && o.error.trim()) return { ok: false, error: o.error.trim() };
	if (!Array.isArray(o.edits) || o.edits.length === 0) return { ok: false, error: 'the editor proposed no change' };
	const edits = o.edits.filter((e): e is Edit => !!e && typeof (e as Edit).old_text === 'string' && typeof (e as Edit).new_text === 'string');
	if (edits.length !== o.edits.length) return { ok: false, error: 'the editor gave a malformed edit' };
	return { ok: true, edits, summary: typeof o.summary === 'string' ? o.summary.trim() : '' };
}

/** The relay agent's text model, over REST: JSON out, thinking off. */
export const geminiPlanEdits: PlanEdits = async (input) => {
	const apiKey = resolveCredential('gemini-voice').key;
	if (!apiKey) return { ok: false, error: 'no Gemini key' };
	try {
		// The key in a header, never the URL, so it cannot end up in a logged URL.
		const res = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${WORK_CHECK_MODEL}:generateContent`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json', 'x-goog-api-key': apiKey },
			body: JSON.stringify({
				contents: [{ parts: [{ text: editPlanPrompt(input) }] }],
				generationConfig: { maxOutputTokens: 4_000, temperature: 0, responseMimeType: 'application/json', thinkingConfig: { thinkingBudget: 0 } },
			}),
			signal: AbortSignal.timeout(EDIT_PLAN_TIMEOUT_MS),
		});
		const data = await res.json() as { candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }>; error?: { message?: string } };
		const text = data.candidates?.[0]?.content?.parts?.map((p) => p.text ?? '').join('') ?? '';
		return text ? parseEditPlan(text) : { ok: false, error: `the editor gave no answer (${data.error?.message ?? 'empty'})` };
	} catch (err) {
		return { ok: false, error: `the editor failed (${err instanceof Error ? err.message : String(err)})` };
	}
};

export async function editProjectFile(
	args: { kind: string; instruction: string; room?: string; project?: string },
	deps: ReadRoomDeps,
	plan: PlanEdits = geminiPlanEdits,
): Promise<EditFileResult> {
	const t0 = Date.now();
	const client = deps.client();
	if (!client) return { ok: false, error: 'not_connected' };
	const kind = (args.kind ?? '').trim();
	if (!/^markdown-[a-z0-9]{8}$/.test(kind)) return { ok: false, error: 'no_file' };
	const instruction = (args.instruction ?? '').trim();
	if (!instruction) return { ok: false, error: 'no_instruction' };
	const target = await targetRoom(args.room, deps);
	if (!target.ok) return target;
	const room = target.room;
	const project = (args.project ?? '').trim() || kind;
	const operationId = randomUUID();
	let problem: string | undefined;
	// A second round only after a plan that did not fit the file, or a file changed under us.
	for (let round = 0; round < 2; round++) {
		const read = await roomRead(client, room, 'room.commons.pages.read', { kind, max_bytes: EDIT_FILE_MAX_BYTES }, deps, ROOM_COMMONS_TIMEOUT_MS);
		if (!read.ok) return read;
		const text = typeof read.result.text === 'string' ? read.result.text : '';
		const hash = typeof read.result.content_hash === 'string' ? read.result.content_hash : '';
		const title = typeof read.result.title === 'string' ? read.result.title : '';
		if (read.result.next_offset != null) return { ok: false, error: 'file_too_long', done: false, next: EDIT_NOT_DONE_NEXT };
		if (!hash) return { ok: false, error: 'no_content_hash', done: false, next: EDIT_NOT_DONE_NEXT };
		const p = await plan({ title, text, instruction, problem });
		if (!p.ok) return { ok: false, error: 'cannot_edit', detail: p.error, done: false, next: EDIT_NOT_DONE_NEXT };
		const fit = applyEdits(text, p.edits);
		if (!fit.ok) { problem = fit.problem; continue; }
		const summary = p.summary || instruction;
		const r = await roomExecute(client, room, 'room.commons.changes.apply', {
			project, message: summary.slice(0, 500), files: [{ page_id: kind, base_hash: hash, edits: p.edits }],
		}, deps, { operationId });
		if (r.ok) {
			deps.log?.(`[EditFile] ${title || kind} in ${room.name}: ${p.edits.length} edit(s) in ${Date.now() - t0}ms`);
			return { ok: true, room: room.name, title, summary, edits: p.edits.length, ms: Date.now() - t0, next: EDIT_DONE_NEXT };
		}
		if (r.error === OUTCOME_UNKNOWN) return r;
		if (/stale_base/i.test(r.error) && round === 0) { problem = undefined; continue; }
		return { ...r, done: false, next: EDIT_NOT_DONE_NEXT };
	}
	return { ok: false, error: 'cannot_edit', detail: problem, done: false, next: EDIT_NOT_DONE_NEXT };
}

export function createEditFileTool(deps: ReadRoomDeps, plan: PlanEdits = geminiPlanEdits): ToolDefinition {
	return {
		name: 'edit_project_file',
		description:
			'Change what a LaTeX or Typst project file says (.typ, .tex, .bib…) in an AG2 Space room: "make the title green", "add an abstract", "fix the typo in the intro". ' +
			'Pass `kind` = the file\'s kind from search_commons (e.g. "markdown-qop83g48"), `instruction` = what to change, in the user\'s words, and `room` = the same room. ' +
			'Never pass the file\'s text: this tool reads the file and writes the change itself. Call it at once: do not read the change back or ask for a yes. ' +
			'Pass `project` (the folder id) only when a previous call\'s `detail` says the file belongs to a folder project. ' +
			'Returns {ok:true, title, summary} — then tell the user in one sentence what you changed ("I have …"); never say it is done before ok:true. ' +
			'Errors: "cannot_edit" — `detail` says why; "file_too_long" — too long to change by voice; "no_file" — the kind is not a file kind; ' + ROOM_NAME_ERRORS +
			'"FORBIDDEN" — you may not change it there; "not_connected", "timeout" or "unavailable" — it could not be done right now; ' + OUTCOME_UNKNOWN_ERROR,
		parameters: z.object({
			kind: z.string().describe('The file\'s kind from a search_commons hit, e.g. "markdown-qop83g48".'),
			instruction: z.string().min(1).max(1_000).describe('What to change, as the user said it, e.g. "make the title green".'),
			room: z.string().optional().describe('The room name, e.g. "Sutando Triage". Omit for the current room.'),
			project: z.string().optional().describe('The folder id, only when a refusal said the file is in a folder project.'),
		}),
		execution: 'inline',
		timeout: ROOM_INDEX_COLD_TIMEOUT_MS + 2 * (2 * ROOM_COMMONS_TIMEOUT_MS + EDIT_PLAN_TIMEOUT_MS) + 1000,
		async execute(args) {
			return editProjectFile(args as { kind: string; instruction: string; room?: string; project?: string }, deps, plan);
		},
	};
}

/** The rule the voice model reads: a project file is changed through this tool, never by writing it out. */
export const EDIT_FILE_PROMPT_RULE = '- PROJECT FILES: to change what a LaTeX or Typst project file says ("make the title green", "add an abstract", "改一下引言", even "write the paper") → search_commons for the file, then edit_project_file with its kind and the user\'s instruction, at once. Never read the file first and never write its text out yourself: the tool does both. After ok:true say what you changed ("I have …"). Never submit work for it.';
