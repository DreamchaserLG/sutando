import { describe, it, before, after, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync, unlinkSync, mkdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { resolveWorkspace } from '../src/workspace_default.js';
import { buildVoiceTaskHeader, laneDirective, longLaneWorker, longLaneWorkerFrom, longLaneWorkerLabel, LONG_LANE_WORKER_LABEL, queuedAheadInstruction, setVoiceSessionOrigin, getVoiceSessionOrigin, workTool, voiceTasksAhead, _pendingTasksForTest } from '../src/task-bridge.js';
import { readQueueDepth } from '../src/inline-tools.js';
import { localTimeValue } from '../src/local_time.js';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';

// Integration test for PR #460's unified task-file schema. Every voice /
// work-tool task should emit the same set of fields the Discord bridge
// emits, so downstream consumers (Claude Code session, access-tier
// sandboxing) can treat all tasks uniformly.

// Task dir is wherever the bridge writes — `resolveWorkspace()/tasks/`. Was
// `<REPO_ROOT>/tasks/` pre-#821, when the bridge fell back to repo root.
const TASK_DIR = join(resolveWorkspace(), 'tasks');

function listTaskFiles(): string[] {
	if (!existsSync(TASK_DIR)) return [];
	return readdirSync(TASK_DIR).filter(f => f.startsWith('task-') && f.endsWith('.txt'));
}

describe('task-bridge workTool — PR #460 unified format', () => {
	let createdFiles: string[] = [];
	let baselineFiles: Set<string>;

	before(() => {
		mkdirSync(TASK_DIR, { recursive: true });
		baselineFiles = new Set(listTaskFiles());
	});

	afterEach(() => {
		// Clean up only the files we created; leave prod task files alone.
		for (const fn of createdFiles) {
			try { unlinkSync(join(TASK_DIR, fn)); } catch { /* already gone */ }
		}
		createdFiles = [];
	});

	after(() => {
		// Leak check: after all tests + cleanup, only baseline files should
		// remain. Anything extra means a test wrote a file but didn't track
		// it through createdFiles (e.g. a future test that bypasses
		// invokeWorkTool). Surfaces gaps in the cleanup harness early.
		const final = new Set(listTaskFiles());
		const leaked: string[] = [];
		for (const f of final) if (!baselineFiles.has(f)) leaked.push(f);
		assert.deepEqual(leaked, [], 'test leaked task files: ' + leaked.join(', '));
	});

	async function invokeWorkTool(task: string): Promise<string> {
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const result = await (workTool.execute as any)({ task }, null) as { taskId?: string };
		assert.ok(result.taskId, 'workTool should return a taskId');
		const fn = result.taskId + '.txt';
		createdFiles.push(fn);
		return fn;
	}

	it('a long task carries the lane directive two lines under task:, a quick one its own, a plain one none', async () => {
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const exec = workTool.execute as any;
		const written = async (args: Record<string, unknown>) => {
			const r = await exec(args, null) as { taskId?: string };
			assert.ok(r.taskId);
			createdFiles.push(r.taskId + '.txt');
			return readFileSync(join(TASK_DIR, r.taskId + '.txt'), 'utf-8');
		};
		const long = await written({ task: 'draw a picture of a cat', lane: 'long' });
		assert.match(long, /^lane: long$/m);
		let lines = long.split('\n');
		let at = lines.indexOf('task: draw a picture of a cat');
		assert.ok(at > 0);
		assert.equal(lines[at + 1], '');
		assert.equal(lines[at + 2], "--- lane: long (the relay's judgement; binding) ---");
		assert.match(lines[at + 3], /^Spawn a subagent for this task as you read it, in the background/);
		assert.equal(long.indexOf('--- lane:'), long.lastIndexOf('--- lane:'), 'one directive');

		const quick = await written({ task: 'count the merged PRs', lane: 'quick' });
		assert.match(quick, /^lane: quick$/m);
		lines = quick.split('\n');
		at = lines.indexOf('task: count the merged PRs');
		assert.equal(lines[at + 2], "--- lane: quick (the relay's judgement; binding) ---");
		assert.match(lines[at + 3], /^Do it now, inline, before any other task/);

		const plain = await written({ task: 'count the merged PRs again' });
		assert.doesNotMatch(plain, /^lane:/m);
		assert.doesNotMatch(plain, /--- lane:/);
		const odd = await written({ task: 'count the merged PRs once more', lane: 'LONG' });
		assert.doesNotMatch(odd, /^lane:/m);
		assert.doesNotMatch(odd, /--- lane:/);
	});

	it('writes all 7 fields required by the Discord-bridge schema', async () => {
		const fn = await invokeWorkTool('Test task from format unit test');
		const content = readFileSync(join(TASK_DIR, fn), 'utf-8');
		// Each of these is the schema locked in by PR #460.
		assert.match(content, /^id: task-\d+$/m, 'id field');
		assert.match(content, /^timestamp: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/m, 'ISO8601 timestamp');
		assert.match(content, /^task: Test task from format unit test$/m, 'task body');
		assert.match(content, /^source: voice$/m, 'source=voice for workTool');
		assert.match(content, /^channel_id: local-voice$/m, 'channel_id=local-voice');
		assert.match(content, /^user_id: \S+$/m, 'user_id (env or voice-local fallback)');
		assert.match(content, /^access_tier: owner$/m, 'access_tier=owner (voice is owner-only)');
	});

	it('does NOT emit the legacy "reminder:" field dropped in PR #460', async () => {
		const fn = await invokeWorkTool('Test task — no reminder');
		const content = readFileSync(join(TASK_DIR, fn), 'utf-8');
		assert.doesNotMatch(content, /^reminder:/m, 'reminder field was dropped');
	});

	it('uses SUTANDO_DM_OWNER_ID when set, falls back to voice-local sentinel', async () => {
		// Case 1: default (env unset) → voice-local
		const fn1 = await invokeWorkTool('default fallback');
		const c1 = readFileSync(join(TASK_DIR, fn1), 'utf-8');
		if (!process.env.SUTANDO_DM_OWNER_ID) {
			assert.match(c1, /^user_id: voice-local$/m);
		} else {
			assert.match(c1, new RegExp(`^user_id: ${process.env.SUTANDO_DM_OWNER_ID}$`, 'm'));
		}
	});

	it('generates unique task IDs for back-to-back calls', async () => {
		const fn1 = await invokeWorkTool('first');
		const fn2 = await invokeWorkTool('second');
		assert.notEqual(fn1, fn2, 'task IDs must differ');
	});

	it('queuedAheadInstruction: nothing when none are ahead, one sentence otherwise', () => {
		assert.equal(queuedAheadInstruction(0), '');
		assert.match(queuedAheadInstruction(1), /Got it, right after the one I'm on\./, 'one ahead reads as a person, not a queue');
		assert.match(queuedAheadInstruction(2), /Got it, 2 in line before this one\./);
	});

	it('returns queuedAhead: how many of the user\'s voice tasks are still open before this one (a health check is not one)', async () => {
		writeFileSync(join(TASK_DIR, 'task-health-1.txt'), 'id: task-health-1\nsource: health-check\ntask: health\n');
		createdFiles.push('task-health-1.txt');
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const first = await (workTool.execute as any)({ task: 'queue probe one' }, null) as { taskId: string; queuedAhead: number; message: string };
		createdFiles.push(first.taskId + '.txt');
		assert.equal(typeof first.queuedAhead, 'number');
		assert.ok(first.queuedAhead >= 0);
		// The first file is still in tasks/, so the second call sees at least one ahead of it,
		// and its message carries the one sentence the voice agent is to say.
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const second = await (workTool.execute as any)({ task: 'queue probe two' }, null) as { taskId: string; queuedAhead: number; message: string };
		createdFiles.push(second.taskId + '.txt');
		assert.ok(second.queuedAhead >= first.queuedAhead + 1, `second saw ${second.queuedAhead}, first ${first.queuedAhead}`);
		const expected = second.queuedAhead === 1 ? "Got it, right after the one I'm on." : `Got it, ${second.queuedAhead} in line before this one.`;
		assert.ok(second.message.includes(expected), `message carries the spoken line: ${second.message}`);
		// With a task watcher the submitted instruction leads; without one (as in CI), the offline one.
		assert.match(second.message, /^(The task is with the core; this work call returns its result|Task has been saved)/, 'the instruction is kept in front');
	});
});

describe('queue depth helpers (pure, temp dirs)', () => {

	it('readQueueDepth reads state/task-queue.json and treats a stale or absent snapshot as unknown', () => {
		const ws = mkdtempSync(join(tmpdir(), 'queue-depth-'));
		assert.equal(readQueueDepth(ws), null, 'absent');
		mkdirSync(join(ws, 'state'), { recursive: true });
		const now = 1_800_000_000;
		writeFileSync(join(ws, 'state', 'task-queue.json'), JSON.stringify({ ts: now - 30, depth: 3, pending: [] }));
		assert.equal(readQueueDepth(ws, now), 3);
		writeFileSync(join(ws, 'state', 'task-queue.json'), JSON.stringify({ ts: now - 601, depth: 3, pending: [] }));
		assert.equal(readQueueDepth(ws, now), null, 'older than 10 minutes');
		writeFileSync(join(ws, 'state', 'task-queue.json'), '{not json');
		assert.equal(readQueueDepth(ws, now), null, 'torn');
	});
});

// One header writer for the work tool and the cancel tool; an origin-bound task is
// addressed through `channel_id` plus whatever known keys its adapter supplies.
const ORIGIN = { channel: 'fakechan', target: 'place-42', label: 'The Place', headers: { channel_kind: 'place', source_room_id: 'place-42' }, contextLine: 'place_context: place-42 — be brief' };
describe('buildVoiceTaskHeader (pure) — both header shapes', () => {
	const TS = '2026-09-18T10:00:00.000Z';

	it('DM shape: channel_id: local-voice, no room keys, priority urgent, nothing at or after task:', () => {
		const header = buildVoiceTaskHeader('task-1', TS, 'owner-1', null);
		assert.equal(header, [
			'id: task-1',
			`timestamp: ${TS}`,
			`local_time: ${localTimeValue(new Date(TS))}`,
			'source: voice',
			'interaction_type: realtime_audio',
			'media_form: live_stream',
			'channel_id: local-voice',
			'user_id: owner-1',
			'access_tier: owner',
			'priority: urgent',
			'',
		].join('\n'));
		assert.doesNotMatch(header, /^task:/m, 'the header is everything ABOVE task: — the caller appends it');
		assert.doesNotMatch(header, /^(channel_kind|source_room_id):/m);
	});

	it('the lane directive spells the rule out in the body: long spawns a background subagent that writes the result, quick runs now; no lane, nothing', () => {
		const long = laneDirective('long');
		assert.match(long, /^--- lane: long \(the relay's judgement; binding\) ---$/m);
		assert.match(long, /Spawn a subagent for this task as you read it, in the background/);
		assert.match(long, /writes the result file itself, with python3 src\/result_publish\.py, the moment the work is done/);
		assert.match(long, /Nothing waits for your check or for your turn to end/);
		assert.match(long, /If you cannot spawn one, do it inline, write the result the moment it is done, and say in it that it ran inline/);
		const quick = laneDirective('quick');
		assert.match(quick, /^--- lane: quick \(the relay's judgement; binding\) ---$/m);
		assert.match(quick, /Do it now, inline, before any other task/);
		assert.match(quick, /treat it as lane: long/);
		assert.equal(laneDirective(undefined), '');
		// A body line that opens with a header key reads as a header to a lenient consumer; none may.
		for (const line of (long + quick).split('\n')) assert.doesNotMatch(line, /^[A-Za-z_][A-Za-z0-9_]*:/);
	});

	it('a long task is addressed to the long-lane worker when the roster has one; the directive then says do it here', () => {
		const roster = { version: 3, workers: { '7c54b230a8d94ea9b86f52d70134ac68': { label: 'long-lane' }, 'aaaa': { label: 'reviewer', display_label: 'Code reviewer' } } };
		assert.equal(longLaneWorkerFrom(roster, 'long-lane'), 'long-lane');
		assert.equal(longLaneWorkerFrom(roster, 'Code reviewer'), 'Code reviewer', 'a display label addresses too');
		assert.equal(longLaneWorkerFrom(roster, 'painter'), null);
		assert.equal(longLaneWorkerFrom(roster, null), null);
		assert.equal(longLaneWorkerFrom({ workers: 'junk' }, 'long-lane'), null);
		assert.equal(longLaneWorkerFrom(null, 'long-lane'), null);
		assert.equal(longLaneWorkerLabel({}), LONG_LANE_WORKER_LABEL);
		assert.equal(longLaneWorkerLabel({ SUTANDO_VOICE_LONG_LANE_WORKER: 'painter' }), 'painter');
		assert.equal(longLaneWorkerLabel({ SUTANDO_VOICE_LONG_LANE_WORKER: 'none' }), null, '`none` keeps every task on the core');
		// From disk: a roster file, a missing one, a malformed one.
		const dir = mkdtempSync(join(tmpdir(), 'lane-roster-'));
		try {
			const file = join(dir, 'roster.json');
			assert.equal(longLaneWorker(file, {}), null, 'no roster, no worker');
			writeFileSync(file, JSON.stringify(roster));
			assert.equal(longLaneWorker(file, {}), 'long-lane');
			assert.equal(longLaneWorker(file, { SUTANDO_VOICE_LONG_LANE_WORKER: 'none' }), null);
			writeFileSync(file, '{not json');
			assert.equal(longLaneWorker(file, {}), null);
		} finally {
			rmSync(dir, { recursive: true, force: true });
		}
		const header = buildVoiceTaskHeader('task-6', TS, 'owner-1', null, 'long', 'long-lane');
		assert.match(header, /^lane: long\nrequested_worker: long-lane\nuser_id: owner-1$/m, 'addressed right under the lane, above task:');
		assert.doesNotMatch(buildVoiceTaskHeader('task-7', TS, 'owner-1', null, 'quick', 'long-lane'), /requested_worker/, 'a quick task stays with the core');
		assert.doesNotMatch(buildVoiceTaskHeader('task-8', TS, 'owner-1', null, 'long', null), /requested_worker/);
		const routed = laneDirective('long', 'long-lane');
		assert.match(routed, /^--- lane: long \(the relay's judgement; routed to the long-lane worker\) ---$/m);
		assert.match(routed, /For the long-lane worker only: do it here, now/);
		assert.match(routed, /Core: this task is the worker's, not yours\. Do not act on it, spawn nothing for it and write no result for it/);
		assert.match(routed, /draw_and_send\.py/);
		assert.doesNotMatch(routed, /Spawn a subagent/);
		for (const line of routed.split('\n')) assert.doesNotMatch(line, /^[A-Za-z_][A-Za-z0-9_]*:/);
	});

	it('the lane the check judged goes in above task:; no judgement, no line', () => {
		const quick = buildVoiceTaskHeader('task-3', TS, 'owner-1', null, 'quick');
		assert.match(quick, /^lane: quick$/m);
		assert.ok(quick.indexOf('lane:') < quick.indexOf('priority:'));
		assert.match(buildVoiceTaskHeader('task-4', TS, 'owner-1', null, 'long'), /^lane: long$/m);
		assert.doesNotMatch(buildVoiceTaskHeader('task-5', TS, 'owner-1', null), /^lane:/m);
	});

	it('a quick task\'s queue position counts only quick tasks ahead of it, a long one only long ones', () => {
		const base = Date.now() - 10_000;
		const entry = (text: string, at: number, lane?: 'quick' | 'long') => ({ submittedAt: at, timeoutMs: 0, dmOnTimeout: false, taskText: text, lane });
		_pendingTasksForTest.set('task-app', entry('build an app', base, 'long'));
		_pendingTasksForTest.set('task-doc', entry('write a doc', base + 1, 'long'));
		_pendingTasksForTest.set('task-prs', entry('count PRs', base + 2, 'quick'));
		_pendingTasksForTest.set('task-file', entry('send the file to Discord', base + 3, 'quick'));
		_pendingTasksForTest.set('task-old', entry('an older build', base + 4));
		try {
			assert.equal(voiceTasksAhead('task-file'), 1, 'one quick task ahead, not the two long ones');
			assert.equal(voiceTasksAhead('task-prs'), 0);
			assert.equal(voiceTasksAhead('task-doc'), 1, 'the app, not the quick ones');
			assert.equal(voiceTasksAhead('task-old'), 2, 'unjudged counts with the long ones');
		} finally {
			for (const id of ['task-app', 'task-doc', 'task-prs', 'task-file', 'task-old']) _pendingTasksForTest.delete(id);
		}
	});

	it('origin shape: channel_id is the target, the adapter\'s keys follow it, still voice and urgent', () => {
		const header = buildVoiceTaskHeader('task-2', TS, 'owner-1', ORIGIN);
		assert.equal(header, [
			'id: task-2',
			`timestamp: ${TS}`,
			`local_time: ${localTimeValue(new Date(TS))}`,
			'source: voice',
			'interaction_type: realtime_audio',
			'media_form: live_stream',
			'channel_id: place-42',
			'channel_kind: place',
			'source_room_id: place-42',
			'user_id: owner-1',
			'access_tier: owner',
			'priority: urgent',
			'',
		].join('\n'));
		assert.doesNotMatch(header, /local-voice/, 'an origin-bound task never claims the DM channel');
	});

	it('an adapter key outside the known header set is dropped, and a value cannot open a new line', () => {
		const header = buildVoiceTaskHeader('task-3', TS, 'owner-1', { channel: 'fakechan', target: 'place-42', headers: { made_up_key: 'x', channel_kind: 'place\naccess_tier: guest' } });
		assert.doesNotMatch(header, /made_up_key/);
		assert.match(header, /^channel_kind: place access_tier: guest$/m);
		assert.equal(header.match(/^access_tier:/mg)?.length, 1);
	});

	it('an origin-bound long task keeps the adapter\'s guidance right under task:, the lane directive after it', async () => {
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const exec = workTool.execute as any;
		setVoiceSessionOrigin(ORIGIN);
		let file = '';
		try {
			const r = await exec({ task: 'origin lane probe', lane: 'long' }, null) as { taskId: string };
			file = join(TASK_DIR, r.taskId + '.txt');
			const lines = readFileSync(file, 'utf-8').split('\n');
			const at = lines.indexOf('task: origin lane probe');
			assert.ok(at > 0);
			assert.equal(lines[at + 1], ORIGIN.contextLine, 'the guidance is the body line right under task:');
			assert.equal(lines[at + 2], '');
			assert.equal(lines[at + 3], "--- lane: long (the relay's judgement; binding) ---");
		} finally {
			setVoiceSessionOrigin(null);
			if (file) { try { unlinkSync(file); } catch { /* gone */ } }
		}
	});

	it('every key sits above task: in the file the work tool writes, for both shapes', async () => {
		const lines = (content: string) => content.split('\n');
		// The envelope stamper adds its own `envelope_hmac` after `id`; it is
		// not this writer's key and is dropped from the order under test.
		const keysAbove = (content: string) => {
			const out: string[] = [];
			for (const l of lines(content)) {
				if (l.startsWith('task:')) break;
				out.push(l.split(':')[0]);
			}
			return out.filter(k => k !== 'envelope_hmac');
		};
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		const exec = workTool.execute as any;
		const dm = await exec({ task: 'header shape probe dm' }, null) as { taskId: string };
		const dmFile = dm.taskId + '.txt';
		const roomFiles: string[] = [];
		try {
			const c1 = readFileSync(join(TASK_DIR, dmFile), 'utf-8');
			assert.deepEqual(keysAbove(c1), ['id', 'timestamp', 'local_time', 'source', 'interaction_type', 'media_form', 'channel_id', 'user_id', 'access_tier', 'priority']);
			assert.match(c1, /^priority: urgent$/m);

			setVoiceSessionOrigin(ORIGIN);
			assert.equal(getVoiceSessionOrigin(), ORIGIN);
			const room = await exec({ task: 'header shape probe room' }, null) as { taskId: string };
			roomFiles.push(room.taskId + '.txt');
			const c2 = readFileSync(join(TASK_DIR, room.taskId + '.txt'), 'utf-8');
			assert.deepEqual(keysAbove(c2), ['id', 'timestamp', 'local_time', 'source', 'interaction_type', 'media_form', 'channel_id', 'channel_kind', 'source_room_id', 'user_id', 'access_tier', 'priority']);
			assert.match(c2, /^channel_id: place-42$/m);
			assert.match(c2, /^source_room_id: place-42$/m);
			assert.match(c2, /^priority: urgent$/m);
			const body = c2.split('\n');
			assert.equal(body[body.indexOf('task: header shape probe room') + 1], ORIGIN.contextLine, 'the guidance is the body line right under task:');
		} finally {
			setVoiceSessionOrigin(null);
			for (const f of [dmFile, ...roomFiles]) { try { unlinkSync(join(TASK_DIR, f)); } catch { /* gone */ } }
		}
		assert.equal(getVoiceSessionOrigin(), null, 'the origin is released after the probe');
	});
});
