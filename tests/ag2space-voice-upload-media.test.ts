/**
 * upload-media — a local image into a room's media without the bytes passing through a model.
 *
 * Pins: the type comes from the bytes, the file is uploaded then listed in the folder, and every
 * failure is one JSON answer.
 */
import { describe, it, test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const PRESENT = existsSync(join(process.cwd(), 'skills', 'ag2space-voice', 'scripts', 'upload-media.ts'));
if (!PRESENT) test('ag2space-voice plugin not installed', { skip: true }, () => {});

if (PRESENT) {
const { uploadMedia, sniffImage, fileName, parseArgs } = await import('../skills/ag2space-voice/scripts/upload-media.ts');
const JPEG = Buffer.concat([Buffer.from([0xff, 0xd8, 0xff, 0xe0]), Buffer.alloc(100, 7)]);
const dir = mkdtempSync(join(tmpdir(), 'upload-media-'));
const jpgAsPng = join(dir, 'pineapple.png');
writeFileSync(jpgAsPng, JPEG);
const text = join(dir, 'notes.txt');
writeFileSync(text, 'hello');

function fake(fail?: string) {
	const calls: Array<{ action: string; args: Record<string, unknown>; room: unknown; op: unknown }> = [];
	const client = {
		async callTool(_n: string, a: Record<string, unknown>) {
			calls.push({ action: String(a.action), args: a.arguments as Record<string, unknown>, room: a.room_id, op: a.operation_id });
			if (a.action === fail) return { isError: true, structuredContent: { code: 'FORBIDDEN', message: 'not allowed here' } };
			if (a.action === 'room.media.upload') return { structuredContent: { status: 'completed', result: { mxc: 'mxc://ag2.space/x' } } };
			return { structuredContent: { status: 'completed', result: { file_id: 'f1234567' } } };
		},
		close() {},
	};
	return { calls, client };
}

describe('uploadMedia', () => {
	it('uploads the file\'s own bytes with the type they are, then files it in the folder', async () => {
		const f = fake();
		const r = await uploadMedia({ room: '!r:ag2.space', file: jpgAsPng, folder: 'a499a739' }, f.client);
		assert.ok(r.ok, JSON.stringify(r));
		if (r.ok) assert.deepEqual([r.name, r.mime, r.file_id, r.bytes, r.folder_id], ['pineapple.jpg', 'image/jpeg', 'f1234567', JPEG.length, 'a499a739']);
		assert.deepEqual(f.calls[0].args, { data_b64: JPEG.toString('base64'), mime: 'image/jpeg', filename: 'pineapple.jpg', post: false });
		assert.deepEqual(f.calls[1].args, { mxc: 'mxc://ag2.space/x', name: 'pineapple.jpg', mime: 'image/jpeg', folder_id: 'a499a739' });
		assert.ok(f.calls.every(c => c.room === '!r:ag2.space' && typeof c.op === 'string'));
		assert.notEqual(f.calls[0].op, f.calls[1].op);
	});

	it('a refusal is the server\'s words; a listing refused after the upload keeps the mxc', async () => {
		assert.deepEqual(await uploadMedia({ room: '!r:ag2.space', file: jpgAsPng }, fake('room.media.upload').client), { ok: false, error: 'FORBIDDEN', detail: 'not allowed here' });
		const r = await uploadMedia({ room: '!r:ag2.space', file: jpgAsPng }, fake('room.media.add').client) as { error: string; mxc?: string };
		assert.deepEqual([r.error, r.mxc], ['FORBIDDEN', 'mxc://ag2.space/x']);
	});

	it('a bad room, folder, file or non-image sends nothing', async () => {
		const f = fake();
		assert.equal(((await uploadMedia({ room: 'susan agents', file: jpgAsPng }, f.client)) as { error: string }).error, 'bad_room');
		assert.equal(((await uploadMedia({ room: '!r:ag2.space', file: jpgAsPng, folder: 'nope' }, f.client)) as { error: string }).error, 'bad_folder');
		assert.equal(((await uploadMedia({ room: '!r:ag2.space', file: join(dir, 'missing.png') }, f.client)) as { error: string }).error, 'bad_file');
		assert.equal(((await uploadMedia({ room: '!r:ag2.space', file: text }, f.client)) as { error: string }).error, 'not_an_image');
		assert.equal(f.calls.length, 0);
	});
});

describe('helpers', () => {
	it('sniffs PNG, JPEG, GIF and WebP from the bytes', () => {
		assert.equal(sniffImage(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))?.mime, 'image/png');
		assert.equal(sniffImage(JPEG)?.mime, 'image/jpeg');
		assert.equal(sniffImage(Buffer.from('GIF89a......'))?.mime, 'image/gif');
		assert.equal(sniffImage(Buffer.from('RIFF0000WEBPVP8 '))?.mime, 'image/webp');
		assert.equal(sniffImage(Buffer.from('<svg')), null);
	});

	it('names it with the extension its bytes call for', () => {
		assert.equal(fileName(undefined, '/x/generated-1.png', 'jpg'), 'generated-1.jpg');
		assert.equal(fileName('pineapple', '/x/a.png', 'png'), 'pineapple.png');
	});

	it('reads its flags', () => {
		assert.deepEqual(parseArgs(['--room', '!r:x', '--file', 'a.png', '--folder', 'a499a739']), { room: '!r:x', file: 'a.png', folder: 'a499a739' });
	});
});
}
