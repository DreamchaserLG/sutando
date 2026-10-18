/**
 * add_project_image — a generated picture put into a project file, by voice, without the core.
 *
 * Pins: the picture is uploaded and filed in the file's folder under a fresh name, the edit references
 * that name, and one compile with the folder's images says whether the build is ok.
 */
import { describe, it, test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

const PRESENT = existsSync(join(process.cwd(), 'skills', 'ag2space-voice', 'image-file.ts'));
if (!PRESENT) test('ag2space-voice plugin not installed', { skip: true }, () => {});

if (PRESENT) {
const { addProjectImage, imageName, createAddImageTool, ADD_IMAGE_PROMPT_RULE, IMAGE_DONE_NEXT, IMAGE_NOT_DONE_NEXT } = await import('../skills/ag2space-voice/image-file.ts');
type Opts = NonNullable<Parameters<typeof addProjectImage>[2]>;

const FILE = '= Results\n#figure(image("apple.png"), caption: [An apple.]) <fig1>\n#figure(image("orange.png"), caption: [An orange.]) <fig3>\n';
const ok = (v: unknown) => ({ structuredContent: v });
type Call = { name: string; action: string; args: Record<string, unknown> };

function fake(o: { folder?: string | null; compile?: unknown; failOn?: string } = {}) {
	const calls: Call[] = [];
	const folder = o.folder === undefined ? 'fold1234' : o.folder;
	const client = {
		async callTool(name: string, a: Record<string, unknown>) {
			const action = String(a.action);
			calls.push({ name, action, args: (a.arguments ?? {}) as Record<string, unknown> });
			if (action === o.failOn) return { isError: true, structuredContent: { code: 'FORBIDDEN', message: 'no' } } as never;
			switch (action) {
				case 'room.commons.pages.list': return ok({ pages: [{ kind: 'markdown-qop83g48', surface: 'doc', page_id: 'qop83g48', title: 'main.typ', folder_id: folder }] }) as never;
				case 'room.media.list': return ok({ files: [{ id: 'apl00001', name: 'apple.png' }, { id: 'pin00001', name: 'pineapple.png' }, { id: 'out00001', name: 'elsewhere.png' }], truncated: false, encrypted: false }) as never;
				case 'room.media.upload': return ok({ status: 'completed', result: { mxc: 'mxc://ag2.space/abc' } }) as never;
				case 'room.media.add': return ok({ status: 'completed', result: { file_id: 'new00001', name: (a.arguments as { name: string }).name } }) as never;
				case 'room.commons.pages.read': return ok({ kind: 'markdown-qop83g48', title: 'main.typ', text: FILE, next_offset: null, content_hash: 'h1' }) as never;
				case 'room.commons.changes.apply': return ok({ status: 'completed', result: {} }) as never;
				case 'room.commons.folders.list': return ok({ folders: [], placements: [{ surface: 'media', item_id: 'apl00001', folder_id: 'fold1234' }, { surface: 'media', item_id: 'out00001', folder_id: 'other999' }], may_manage: true, may_file: [] }) as never;
				case 'room.commons.typst.compile': return ok({ status: 'completed', result: o.compile ?? { build_id: 'b1', status: 'ok', problems: [] } }) as never;
			}
			return ok({}) as never;
		},
		close() {},
	};
	const deps = { client: () => client, currentRoom: () => ({ id: '!r:ag2.space', name: 'susan’s agents' }), index: { rooms: () => [], refresh: async () => [], fetchedAt: () => 0 }, log: () => {} };
	return { calls, deps };
}
const pictures: string[] = [];
const instructions: string[] = [];
const opts: Opts = {
	makeImage: async (d) => { pictures.push(d); return { ok: true, data_b64: 'iVBORw0KGgo=', mime: 'image/png' }; },
	plan: async (i) => { instructions.push(i.instruction); return { ok: true, edits: [{ old_text: '#figure(image("orange.png"), caption: [An orange.]) <fig3>', new_text: '#figure(image("pineapple-2.png"), caption: [A pineapple.]) <fig3>' }], summary: 'Replaced figure 3 with a pineapple' }; },
};
const ARGS = { kind: 'markdown-qop83g48', picture: 'a pineapple', instruction: 'replace figure 3 with it' };
const by = (f: ReturnType<typeof fake>, action: string) => f.calls.filter(c => c.action === action);

describe('add_project_image', () => {
	it('makes the picture, files it in the folder under a fresh name, edits the file to use it, and compiles with the folder\'s images', async () => {
		const f = fake();
		const r = await addProjectImage(ARGS, f.deps, opts);
		assert.ok(r.ok, JSON.stringify(r));
		if (r.ok) { assert.equal(r.image, 'pineapple-2.png'); assert.equal(r.build, 'ok'); assert.equal(r.next, IMAGE_DONE_NEXT); }
		assert.equal(pictures.at(-1), 'a pineapple');
		assert.deepEqual(by(f, 'room.media.upload')[0].args, { data_b64: 'iVBORw0KGgo=', mime: 'image/png', filename: 'pineapple-2.png', post: false });
		assert.deepEqual(by(f, 'room.media.add')[0].args, { mxc: 'mxc://ag2.space/abc', name: 'pineapple-2.png', mime: 'image/png', folder_id: 'fold1234' });
		assert.match(instructions.at(-1)!, /replace figure 3 with it Use the image file "pineapple-2\.png"/);
		assert.equal((by(f, 'room.commons.changes.apply')[0].args as { project: string }).project, 'fold1234');
		assert.deepEqual(by(f, 'room.commons.typst.compile')[0].args, { project: 'fold1234', main: 'markdown-qop83g48', media: [{ id: 'apl00001', path: 'apple.png' }, { id: 'new00001', path: 'pineapple-2.png' }], trigger: 'manual' });
	});

	it('a broken build is said with its first error', async () => {
		const f = fake({ compile: { build_id: 'b2', status: 'errors', problems: [{ severity: 'warning', message: 'w' }, { severity: 'error', message: 'file not found (searched at orange.png)' }] } });
		const r = await addProjectImage(ARGS, f.deps, opts);
		assert.ok(r.ok && r.build === 'errors' && r.problem === 'file not found (searched at orange.png)');
	});

	it('a one-file project has no folder for a picture: nothing is made or uploaded', async () => {
		const f = fake({ folder: null });
		const before = pictures.length;
		const r = await addProjectImage(ARGS, f.deps, opts) as { error: string; next?: string };
		assert.equal(r.error, 'one_file_project');
		assert.equal(r.next, IMAGE_NOT_DONE_NEXT);
		assert.equal(pictures.length, before);
		assert.equal(by(f, 'room.media.upload').length, 0);
	});

	it('a refused upload stops before the file is touched', async () => {
		const f = fake({ failOn: 'room.media.add' });
		const r = await addProjectImage(ARGS, f.deps, opts) as { error: string };
		assert.equal(r.error, 'FORBIDDEN');
		assert.equal(by(f, 'room.commons.changes.apply').length, 0);
	});

	it('an edit that cannot be made says the picture was uploaded under its name', async () => {
		const f = fake();
		const r = await addProjectImage(ARGS, f.deps, { ...opts, plan: async () => ({ ok: false, error: 'there is no figure 3' }) }) as { error: string; image?: string; detail?: string };
		assert.deepEqual([r.error, r.image, r.detail], ['cannot_edit', 'pineapple-2.png', 'there is no figure 3']);
	});

	it('a picture that cannot be made changes nothing', async () => {
		const f = fake();
		const r = await addProjectImage(ARGS, f.deps, { ...opts, makeImage: async () => ({ ok: false, error: 'quota' }) }) as { error: string; detail?: string };
		assert.deepEqual([r.error, r.detail], ['cannot_make_image', 'quota']);
		assert.equal(by(f, 'room.media.upload').length, 0);
	});
});

describe('names and the tool', () => {
	it('a name from the description, kept unique', () => {
		assert.equal(imageName('a picture of a pineapple', 'png', new Set()), 'pineapple.png');
		assert.equal(imageName('Pineapple', 'jpg', new Set(['pineapple.jpg', 'pineapple-2.jpg'])), 'pineapple-3.jpg');
		assert.equal(imageName('菠萝', 'png', new Set()), 'figure.png');
	});

	it('takes what to draw and where it goes, runs inline, and the rule keeps it from the core', () => {
		const t = createAddImageTool(fake().deps, opts);
		assert.equal(t.name, 'add_project_image');
		assert.equal(t.execution, 'inline');
		assert.match(t.description, /say in a few words that you are making it, then call/);
		assert.match(ADD_IMAGE_PROMPT_RULE, /then add_project_image at once. Never submit work for it/);
	});
});
}
