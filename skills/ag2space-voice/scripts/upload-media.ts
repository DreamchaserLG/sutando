/**
 * upload-media — put a local image into an AG2 Space room's media, optionally filed in a Commons folder.
 *
 * room.media.upload takes the bytes as base64 inside the call. A model that writes that argument itself
 * spends minutes on it and corrupts it; this reads the file from disk, so the bytes never pass through a
 * model. It talks to the same ag2-space MCP server the agent uses, as the same agent.
 *
 *   npx tsx skills/ag2space-voice/scripts/upload-media.ts --room '!abc:ag2.space' --file pic.jpg [--name pineapple.jpg] [--folder a499a739]
 *
 * Prints one JSON line: {"ok":true,"mxc","file_id","name","mime","bytes","sha256","folder_id"} or {"ok":false,"error",...}.
 */
import { createHash, randomUUID } from 'node:crypto';
import { readFileSync, statSync } from 'node:fs';
import { basename } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createMcpStdioClient, findAg2SpaceMcpEntry, mcpPayload, type McpClient, type McpToolResult } from '../room-read.js';

/** room.media.add's limit, the media read cap. */
export const MAX_BYTES = 25 * 1024 * 1024;
const CALL_TIMEOUT_MS = 120_000;

/** The type read from the bytes, never from the name: a JPEG saved as .png is still a JPEG. */
export function sniffImage(b: Buffer): { mime: string; ext: string } | null {
	if (b.length >= 8 && b.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return { mime: 'image/png', ext: 'png' };
	if (b.length >= 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return { mime: 'image/jpeg', ext: 'jpg' };
	if (b.length >= 6 && /^GIF8[79]a$/.test(b.subarray(0, 6).toString('latin1'))) return { mime: 'image/gif', ext: 'gif' };
	if (b.length >= 12 && b.subarray(0, 4).toString('latin1') === 'RIFF' && b.subarray(8, 12).toString('latin1') === 'WEBP') return { mime: 'image/webp', ext: 'webp' };
	return null;
}

/** The name to file it under, with the extension its bytes call for. */
export function fileName(requested: string | undefined, path: string, ext: string): string {
	const base = (requested?.trim() || basename(path)).replace(/\.[A-Za-z0-9]+$/, '');
	return `${base || 'image'}.${ext}`;
}

export type UploadResult =
	| { ok: true; mxc: string; file_id: string | null; name: string; mime: string; bytes: number; sha256: string; folder_id: string | null }
	| { ok: false; error: string; detail?: string; mxc?: string };

/** The server's own words for a refusal: its message, else the first line of a text reply. */
function refusal(res: McpToolResult, payload: Record<string, unknown> | null): string | undefined {
	const msg = payload && typeof payload.message === 'string' ? payload.message : res.content?.find((c) => c.type === 'text')?.text?.split('\n')[0];
	return msg ? msg.slice(0, 300) : undefined;
}

async function execute(client: McpClient, room: string, action: string, args: Record<string, unknown>): Promise<{ ok: true; result: Record<string, unknown> } | { ok: false; error: string; detail?: string }> {
	const res = await client.callTool('room.action.execute', { room_id: room, action, arguments: args, operation_id: randomUUID() }, CALL_TIMEOUT_MS);
	const payload = mcpPayload(res);
	if (res.isError || !payload) {
		const detail = refusal(res, payload);
		return { ok: false, error: String((payload as { code?: string } | null)?.code || 'failed'), ...(detail ? { detail } : {}) };
	}
	if (payload.approval_id || /approval/i.test(String(payload.status ?? ''))) return { ok: false, error: 'needs_approval' };
	return { ok: true, result: (payload.result ?? payload) as Record<string, unknown> };
}

export async function uploadMedia(opts: { room: string; file: string; name?: string; folder?: string }, client: McpClient): Promise<UploadResult> {
	if (!/^![^:\s]+:\S+$/.test(opts.room)) return { ok: false, error: 'bad_room', detail: 'pass the room id, e.g. !abc:ag2.space' };
	if (opts.folder && !/^[a-z0-9]{8}$/.test(opts.folder)) return { ok: false, error: 'bad_folder', detail: 'a folder id is 8 lowercase letters or digits' };
	let bytes: Buffer;
	try {
		if (statSync(opts.file).size > MAX_BYTES) return { ok: false, error: 'too_large', detail: `over ${MAX_BYTES} bytes` };
		bytes = readFileSync(opts.file);
	} catch (err) {
		return { ok: false, error: 'bad_file', detail: err instanceof Error ? err.message : String(err) };
	}
	const kind = sniffImage(bytes);
	if (!kind) return { ok: false, error: 'not_an_image', detail: 'PNG, JPEG, GIF or WebP only' };
	const name = fileName(opts.name, opts.file, kind.ext);
	const up = await execute(client, opts.room, 'room.media.upload', { data_b64: bytes.toString('base64'), mime: kind.mime, filename: name, post: false });
	if (!up.ok) return up;
	const mxc = typeof up.result.mxc === 'string' ? up.result.mxc : '';
	if (!mxc) return { ok: false, error: 'no_mxc' };
	const added = await execute(client, opts.room, 'room.media.add', { mxc, name, mime: kind.mime, folder_id: opts.folder ?? null });
	if (!added.ok) return { ...added, mxc };
	return {
		ok: true, mxc, name, mime: kind.mime, bytes: bytes.length,
		file_id: typeof added.result.file_id === 'string' ? added.result.file_id : null,
		sha256: createHash('sha256').update(bytes).digest('hex'),
		folder_id: opts.folder ?? null,
	};
}

export function parseArgs(argv: readonly string[]): { room?: string; file?: string; name?: string; folder?: string } {
	const out: Record<string, string> = {};
	for (let i = 0; i < argv.length; i++) {
		const m = /^--(room|file|name|folder)$/.exec(argv[i]);
		if (m && i + 1 < argv.length) out[m[1]] = argv[++i];
	}
	return out;
}

async function main(): Promise<number> {
	const a = parseArgs(process.argv.slice(2));
	const print = (r: UploadResult) => { console.log(JSON.stringify(r)); return r.ok ? 0 : 1; };
	if (!a.room || !a.file) return print({ ok: false, error: 'usage', detail: 'upload-media --room <room id> --file <path> [--name <name>] [--folder <folder id>]' });
	const entry = findAg2SpaceMcpEntry();
	if (!entry) return print({ ok: false, error: 'no_mcp', detail: 'no ag2-space MCP server in this agent\'s .claude.json (CLAUDE_CONFIG_DIR)' });
	const client = createMcpStdioClient(entry, { log: (m) => console.error(m) });
	try {
		return print(await uploadMedia({ room: a.room, file: a.file, name: a.name, folder: a.folder }, client));
	} catch (err) {
		return print({ ok: false, error: 'failed', detail: err instanceof Error ? err.message : String(err) });
	} finally {
		client.close();
	}
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) main().then((code) => process.exit(code));
