/**
 * edit_project_file — a project file changed from a short instruction.
 *
 * Pins: the voice model passes only an instruction; the file is read here, a text model plans
 * old_text/new_text edits, and changes.apply gets them on the hash that was read.
 */
import { describe, it, test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

const PRESENT = existsSync(join(process.cwd(), 'skills', 'ag2space-voice', 'edit-file.ts'));
if (!PRESENT) test('ag2space-voice plugin not installed', { skip: true }, () => {});

if (PRESENT) {
const { applyEdits, parseEditPlan, editPlanPrompt, editProjectFile, createEditFileTool, EDIT_FILE_PROMPT_RULE, EDIT_DONE_NEXT, EDIT_NOT_DONE_NEXT } = await import('../skills/ag2space-voice/edit-file.ts');
type PlanEdits = Parameters<typeof editProjectFile>[2];

const FILE = '#set text(fill: black)\n= My Title\nBody text.\n';
const ok = (v: unknown) => ({ structuredContent: v });
type Call = { name: string; args: Record<string, unknown> };

function fake(exec: (args: Record<string, unknown>, n: number) => unknown = () => ok({ status: 'completed', result: {} }), pages: Array<{ text: string; hash: string }> = [{ text: FILE, hash: 'h1' }]) {
	const calls: Call[] = [];
	let reads = 0, writes = 0;
	const client = {
		async callTool(name: string, args: Record<string, unknown>) {
			calls.push({ name, args });
			if (name === 'room.action.read') {
				const p = pages[Math.min(reads++, pages.length - 1)];
				return ok({ kind: 'markdown-qop83g48', title: 'typst.typ', text: p.text, offset: 0, next_offset: null, content_hash: p.hash }) as never;
			}
			return exec(args, writes++) as never;
		},
		close() {},
	};
	const deps = { client: () => client, currentRoom: () => ({ id: '!r:ag2.space', name: 'susan’s agents' }), index: { rooms: () => [], refresh: async () => [], fetchedAt: () => 0 }, log: () => {} };
	return { calls, deps };
}
const greenTitle: PlanEdits = async () => ({ ok: true, edits: [{ old_text: '= My Title', new_text: '#text(fill: green)[= My Title]' }], summary: 'Made the title green' });
const ARGS = { kind: 'markdown-qop83g48', instruction: 'make the title green' };

describe('edit_project_file', () => {
	it('reads the file, applies the planned edits on the hash it read, and asks for an "I have …" report', async () => {
		const f = fake();
		const r = await editProjectFile(ARGS, f.deps, greenTitle);
		assert.ok(r.ok, JSON.stringify(r));
		if (r.ok) { assert.equal(r.summary, 'Made the title green'); assert.equal(r.next, EDIT_DONE_NEXT); }
		const exec = f.calls.filter(c => c.name === 'room.action.execute');
		assert.equal(exec.length, 1);
		assert.equal(exec[0].args.action, 'room.commons.changes.apply');
		assert.deepEqual(exec[0].args.arguments, {
			project: 'markdown-qop83g48', message: 'Made the title green',
			files: [{ page_id: 'markdown-qop83g48', base_hash: 'h1', edits: [{ old_text: '= My Title', new_text: '#text(fill: green)[= My Title]' }] }],
		});
	});

	it('a plan that does not fit the file is planned again with the reason, and is never sent', async () => {
		const problems: Array<string | undefined> = [];
		let n = 0;
		const plan: PlanEdits = async (i) => { problems.push(i.problem); return n++ === 0 ? { ok: true, edits: [{ old_text: '= Wrong', new_text: 'x' }], summary: 's' } : greenTitle(i); };
		const f = fake();
		assert.ok((await editProjectFile(ARGS, f.deps, plan)).ok);
		assert.deepEqual(problems, [undefined, 'edit 1: old_text does not occur in the file']);
		assert.equal(f.calls.filter(c => c.name === 'room.action.execute').length, 1);
	});

	it('a file changed since the read is read again and planned on the new text', async () => {
		const f = fake((_a, n) => n === 0 ? { isError: true, structuredContent: { code: 'stale_base', message: 'read again' } } : ok({ status: 'completed', result: {} }), [{ text: FILE, hash: 'h1' }, { text: FILE, hash: 'h2' }]);
		assert.ok((await editProjectFile(ARGS, f.deps, greenTitle)).ok);
		const hashes = f.calls.filter(c => c.name === 'room.action.execute').map(c => ((c.args.arguments as { files: Array<{ base_hash: string }> }).files[0].base_hash));
		assert.deepEqual(hashes, ['h1', 'h2']);
	});

	it('an unknown outcome is said as such, never retried', async () => {
		const f = fake(() => ({ isError: true, structuredContent: { code: 'ACTION_OUTCOME_UNKNOWN' } }));
		const r = await editProjectFile(ARGS, f.deps, greenTitle) as { error: string; next?: string };
		assert.equal(r.error, 'outcome_unknown');
		assert.equal(r.next, undefined);
		assert.equal(f.calls.filter(c => c.name === 'room.action.execute').length, 1);
	});

	it('a refusal, an editor that cannot do it, or a bad kind changes nothing and says so', async () => {
		const refused = fake(() => ({ isError: true, structuredContent: { code: 'INVALID_INPUT', message: 'the page is in folder ab12cd34' } }));
		const r1 = await editProjectFile(ARGS, refused.deps, greenTitle) as { error: string; detail?: string; next?: string };
		assert.equal(r1.error, 'INVALID_INPUT');
		assert.match(r1.detail ?? '', /folder ab12cd34/);
		assert.equal(r1.next, EDIT_NOT_DONE_NEXT);
		const f = fake();
		const r2 = await editProjectFile(ARGS, f.deps, async () => ({ ok: false, error: 'there is no title' })) as { error: string; detail?: string };
		assert.deepEqual([r2.error, r2.detail], ['cannot_edit', 'there is no title']);
		assert.deepEqual(await editProjectFile({ ...ARGS, kind: 'markdown' }, f.deps, greenTitle), { ok: false, error: 'no_file' });
		assert.equal(f.calls.filter(c => c.name === 'room.action.execute').length, 0);
	});

	it('a folder project is passed through when given', async () => {
		const f = fake();
		await editProjectFile({ ...ARGS, project: 'ab12cd34' }, f.deps, greenTitle);
		assert.equal((f.calls.find(c => c.name === 'room.action.execute')!.args.arguments as { project: string }).project, 'ab12cd34');
	});
});

describe('the edits and the editor\'s answer', () => {
	it('each old_text must occur exactly once in what the edits before it left', () => {
		assert.deepEqual(applyEdits('a b a', [{ old_text: 'b', new_text: 'c' }]), { ok: true, text: 'a c a' });
		assert.equal(applyEdits('a b a', [{ old_text: 'a', new_text: 'x' }]).ok, false);
		assert.deepEqual(applyEdits('ab', [{ old_text: 'a', new_text: 'z' }, { old_text: 'zb', new_text: 'y' }]), { ok: true, text: 'y' });
	});

	it('the answer is parsed strictly', () => {
		assert.deepEqual(parseEditPlan('{"edits":[{"old_text":"a","new_text":"b"}],"summary":"s"}'), { ok: true, edits: [{ old_text: 'a', new_text: 'b' }], summary: 's' });
		assert.deepEqual(parseEditPlan('{"error":"no title"}'), { ok: false, error: 'no title' });
		assert.equal(parseEditPlan('not json').ok, false);
		assert.equal(parseEditPlan('{"edits":[]}').ok, false);
		assert.equal(parseEditPlan('{"edits":[{"old_text":"a"}]}').ok, false);
	});

	it('the prompt carries the instruction, the file and the last problem', () => {
		const p = editPlanPrompt({ title: 'typst.typ', text: FILE, instruction: 'make the title green', problem: 'edit 1: old_text does not occur in the file' });
		assert.match(p, /Instruction: make the title green/);
		assert.match(p, /<<<FILE\n#set text/);
		assert.match(p, /could not be applied: edit 1/);
	});
});

describe('the tool as the voice model sees it', () => {
	it('takes a kind and an instruction, never the file text, and runs inline', () => {
		const f = fake();
		const t = createEditFileTool(f.deps, greenTitle);
		assert.equal(t.name, 'edit_project_file');
		assert.equal(t.execution, 'inline');
		assert.match(t.description, /Never pass the file's text/);
		assert.match(t.description, /do not read the change back or ask for a yes/);
		assert.match(EDIT_FILE_PROMPT_RULE, /search_commons for the file, then edit_project_file/);
		assert.match(EDIT_FILE_PROMPT_RULE, /never write its text out yourself/);
	});
});
}
