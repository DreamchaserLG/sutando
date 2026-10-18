/**
 * add_project_image — a generated picture put into a LaTeX or Typst project by voice, without the core.
 *
 * The voice model passes what to draw and where it goes; this generates the image, uploads it, files it in
 * the project's folder, has edit_project_file reference it, and compiles once to check the build.
 */
import { randomUUID } from 'node:crypto';
import { z } from 'zod';
import type { ToolDefinition } from 'bodhi-realtime-agent';
import { resolveCredential } from '../../src/credential-resolver.js';
import {
	OUTCOME_UNKNOWN_ERROR, ROOM_COMMONS_TIMEOUT_MS, ROOM_INDEX_COLD_TIMEOUT_MS, ROOM_NAME_ERRORS,
	roomExecute, roomRead, targetRoom, type McpClient, type ReadRoomDeps, type RoomRef,
} from './room-read.js';
import { editProjectFile, geminiPlanEdits, EDIT_PLAN_TIMEOUT_MS, type PlanEdits } from './edit-file.js';

export const IMAGE_MODEL = process.env.VOICE_IMAGE_MODEL || 'gemini-2.5-flash-image';
export const IMAGE_TIMEOUT_MS = 60_000;

export type Picture = { ok: true; data_b64: string; mime: string } | { ok: false; error: string };
export type MakeImage = (description: string) => Promise<Picture>;

export type AddImageResult =
	| { ok: true; room: string; file: string; image: string; summary: string; build: string; problem?: string; ms: number; next: string }
	| { ok: false; error: string; detail?: string; image?: string; done?: false; next?: string; candidates?: string[]; rooms?: string[] };

export const IMAGE_DONE_NEXT = 'Done. Tell the user in one sentence what you added ("I have …"); if `build` is not "ok", also say the build reports `problem`.';
export const IMAGE_NOT_DONE_NEXT = 'The file was not changed. Tell the user in a sentence what went wrong (if `image` is set, the picture itself was uploaded to the room\'s media under that name); do not say it is done.';

const STOP_WORDS = new Set(['a', 'an', 'the', 'of', 'picture', 'image', 'photo']);
/** A file name from the description: lowercase words joined by "-", kept unique among names in use. */
export function imageName(description: string, ext: string, taken: ReadonlySet<string>): string {
	const base = description.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').split('-').filter((w) => !STOP_WORDS.has(w)).slice(0, 4).join('-') || 'figure';
	let name = `${base}.${ext}`;
	for (let i = 2; taken.has(name); i++) name = `${base}-${i}.${ext}`;
	return name;
}

/** Gemini's image model over REST: one picture for a figure. */
export const geminiMakeImage: MakeImage = async (description) => {
	const apiKey = resolveCredential('gemini-voice').key;
	if (!apiKey) return { ok: false, error: 'no Gemini key' };
	try {
		// The key in a header, never the URL, so it cannot end up in a logged URL.
		const res = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${IMAGE_MODEL}:generateContent`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json', 'x-goog-api-key': apiKey },
			body: JSON.stringify({
				contents: [{ parts: [{ text: `A clean illustration for a figure in a paper, on a plain white background, no text in the image: ${description}` }] }],
				generationConfig: { responseModalities: ['IMAGE', 'TEXT'] },
			}),
			signal: AbortSignal.timeout(IMAGE_TIMEOUT_MS),
		});
		const data = await res.json() as { candidates?: Array<{ content?: { parts?: Array<{ inlineData?: { data?: string; mimeType?: string } }> } }>; error?: { message?: string } };
		const img = data.candidates?.[0]?.content?.parts?.find((p) => p.inlineData?.data)?.inlineData;
		if (!img?.data) return { ok: false, error: `no picture came back (${data.error?.message ?? 'empty'})` };
		return { ok: true, data_b64: img.data, mime: img.mimeType || 'image/png' };
	} catch (err) {
		return { ok: false, error: `the picture could not be made (${err instanceof Error ? err.message : String(err)})` };
	}
};

async function read(client: McpClient, room: RoomRef, action: string, args: Record<string, unknown>, deps: ReadRoomDeps) {
	return roomRead(client, room, action, args, deps, ROOM_COMMONS_TIMEOUT_MS);
}

export async function addProjectImage(
	args: { kind: string; picture: string; instruction: string; room?: string },
	deps: ReadRoomDeps,
	opts: { makeImage?: MakeImage; plan?: PlanEdits } = {},
): Promise<AddImageResult> {
	const t0 = Date.now();
	const client = deps.client();
	if (!client) return { ok: false, error: 'not_connected' };
	const kind = (args.kind ?? '').trim();
	if (!/^markdown-[a-z0-9]{8}$/.test(kind)) return { ok: false, error: 'no_file' };
	const picture = (args.picture ?? '').trim();
	const instruction = (args.instruction ?? '').trim();
	if (!picture || !instruction) return { ok: false, error: 'no_instruction' };
	const target = await targetRoom(args.room, deps);
	if (!target.ok) return target;
	const room = target.room;

	// Where the file lives: a folder project's images are filed in its folder and read by #image at their names.
	const pages = await read(client, room, 'room.commons.pages.list', { surface: 'doc' }, deps);
	if (!pages.ok) return pages;
	const page = ((pages.result.pages ?? []) as Array<{ kind: string; title: string; folder_id: string | null }>).find((p) => p.kind === kind);
	if (!page) return { ok: false, error: 'NOT_FOUND' };
	const folder = page.folder_id ?? null;
	if (!folder) return { ok: false, error: 'one_file_project', detail: `${page.title} is a one-file project, which has no folder to hold a picture.`, done: false, next: IMAGE_NOT_DONE_NEXT };

	const media = await read(client, room, 'room.media.list', {}, deps);
	if (!media.ok) return media;
	const files = (media.result.files ?? []) as Array<{ id: string; name: string | null }>;

	const made = await (opts.makeImage ?? geminiMakeImage)(picture);
	if (!made.ok) return { ok: false, error: 'cannot_make_image', detail: made.error, done: false, next: IMAGE_NOT_DONE_NEXT };
	const ext = made.mime === 'image/jpeg' ? 'jpg' : made.mime === 'image/webp' ? 'webp' : 'png';
	const name = imageName(picture, ext, new Set(files.map((f) => f.name ?? '')));

	const up = await roomExecute(client, room, 'room.media.upload', { data_b64: made.data_b64, mime: made.mime, filename: name, post: false }, deps, { operationId: randomUUID() });
	if (!up.ok) return { ...up, done: false, next: IMAGE_NOT_DONE_NEXT };
	const mxc = typeof up.result.mxc === 'string' ? up.result.mxc : '';
	if (!mxc) return { ok: false, error: 'upload_failed', done: false, next: IMAGE_NOT_DONE_NEXT };
	const added = await roomExecute(client, room, 'room.media.add', { mxc, name, mime: made.mime, folder_id: folder }, deps, { operationId: randomUUID() });
	if (!added.ok) return { ...added, done: false, next: IMAGE_NOT_DONE_NEXT };
	const fileId = typeof added.result.file_id === 'string' ? added.result.file_id : '';

	// The user's instruction, with the name the picture now has, so the edit may reference it.
	// The room is already resolved: the edit runs there as the current room, not looked up again by name.
	const inRoom = { ...deps, currentRoom: () => ({ id: room.room_id, name: room.name }) };
	const edit = await editProjectFile({ kind, project: folder, instruction: `${instruction} Use the image file "${name}", which is now in the project.` }, inRoom, opts.plan ?? geminiPlanEdits);
	if (!edit.ok) return { ...edit, image: name, done: false, next: IMAGE_NOT_DONE_NEXT };

	// One compile with the folder's images, so a broken build is said now rather than found later.
	const placed = await read(client, room, 'room.commons.folders.list', { surface: 'media' }, deps);
	const inFolder = new Set(placed.ok ? ((placed.result.placements ?? []) as Array<{ item_id: string; folder_id: string }>).filter((p) => p.folder_id === folder).map((p) => p.item_id) : []);
	if (fileId) inFolder.add(fileId);
	const names = new Map([...files.map((f) => [f.id, f.name ?? ''] as const), [fileId, name] as const]);
	const mediaArg = [...inFolder].filter((id) => names.get(id)).map((id) => ({ id, path: names.get(id)! }));
	const built = await roomExecute(client, room, 'room.commons.typst.compile', { project: folder, main: kind, media: mediaArg, trigger: 'manual' }, deps, { operationId: randomUUID() });
	const build = built.ok ? String(built.result.status ?? 'unknown') : 'not_checked';
	const first = built.ok ? ((built.result.problems ?? []) as Array<{ severity: string; message: string }>).find((p) => p.severity === 'error')?.message : undefined;
	deps.log?.(`[AddImage] ${name} into ${page.title} in ${room.name}: build ${build}${first ? ` (${first})` : ''} in ${Date.now() - t0}ms`);
	return { ok: true, room: room.name, file: page.title, image: name, summary: edit.summary, build, ...(first ? { problem: first } : {}), ms: Date.now() - t0, next: IMAGE_DONE_NEXT };
}

export function createAddImageTool(deps: ReadRoomDeps, opts: { makeImage?: MakeImage; plan?: PlanEdits } = {}): ToolDefinition {
	return {
		name: 'add_project_image',
		description:
			'Put a newly generated picture into a LaTeX or Typst project file in an AG2 Space room: "add figure 3, a pineapple", "replace figure 3 with a picture of an orange". ' +
			'Pass `kind` = the file\'s kind from search_commons, `picture` = what to draw in a few words, `instruction` = where it goes, in the user\'s words, and `room`. ' +
			'It makes the picture, adds it to the project, changes the file and compiles once; it takes about 15 seconds, so say in a few words that you are making it, then call. Do not read it back or ask for a yes. ' +
			'Returns {ok:true, image, summary, build} — then tell the user what you added ("I have …"), and if build is not "ok", the `problem`. ' +
			'Errors: "one_file_project" — the project has no folder for a picture, say so; "cannot_make_image" or "cannot_edit" — `detail` says why; ' + ROOM_NAME_ERRORS +
			'"FORBIDDEN" — you may not change it there; "needs_approval" — the room requires an approval first; "not_connected", "timeout" or "unavailable" — it could not be done right now; ' + OUTCOME_UNKNOWN_ERROR,
		parameters: z.object({
			kind: z.string().describe('The file\'s kind from a search_commons hit, e.g. "markdown-qop83g48".'),
			picture: z.string().min(1).max(300).describe('What the picture shows, e.g. "a pineapple".'),
			instruction: z.string().min(1).max(1_000).describe('Where it goes, as the user said it, e.g. "replace figure 3 with it".'),
			room: z.string().optional().describe('The room name. Omit for the current room.'),
		}),
		execution: 'inline',
		timeout: ROOM_INDEX_COLD_TIMEOUT_MS + IMAGE_TIMEOUT_MS + 6 * ROOM_COMMONS_TIMEOUT_MS + 2 * EDIT_PLAN_TIMEOUT_MS + 1000,
		async execute(a) {
			return addProjectImage(a as { kind: string; picture: string; instruction: string; room?: string }, deps, opts);
		},
	};
}

export const ADD_IMAGE_PROMPT_RULE = '- PICTURES IN A PROJECT: a new or replacement picture or figure in a LaTeX or Typst project ("add a figure of a pineapple", "replace figure 3 with an orange") → search_commons for the file, say in a few words that you are making it, then add_project_image at once. Never submit work for it.';
