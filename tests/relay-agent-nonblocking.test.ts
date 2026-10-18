/**
 * The `work` call is NON_BLOCKING and stays open until the core's result returns through it
 * (gemini-3.8-live native tool calls; Yixuan's probe 2026-10-17). Pins the three things that make
 * that safe in sutando:
 *   1. the tool's declaration: `behavior: 'NON_BLOCKING'`, no `pendingMessage` (a pending message
 *      makes bodhi answer the call at once and inject the real result as unrelated text);
 *   2. a call the model re-issues after its result was returned is closed quietly, never resent;
 *   3. a reconnect ends every open call, so a result is never returned to a call issued on the
 *      old connection (bodhi drops such a response at its dial-generation fence).
 *
 * Run: npx tsx --test tests/relay-agent-nonblocking.test.ts
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { RelayAgent, createVoiceTaskStore, RECENT_ANSWER_WINDOW_MS } from '../src/relay-agent.js';
import type { WorkRequestCheck } from '../src/work-request-check.js';

test('work tool: NON_BLOCKING background tool with no pending message', async () => {
	const { workTool } = await import('../src/task-bridge.js');
	assert.equal(workTool.execution, 'background');
	assert.equal(workTool.behavior, 'NON_BLOCKING');
	assert.equal(workTool.pendingMessage, undefined, 'a pendingMessage closes the call id at once; the result must return through the open call');
});

function agent(opts: { check?: WorkRequestCheck; submit?: (args: Record<string, unknown>) => Promise<{ status: string; taskId?: string }> } = {}) {
	const store = createVoiceTaskStore();
	const submitted: string[] = [];
	const logs: string[] = [];
	const relay = new RelayAgent({
		store,
		submit: opts.submit ?? (async (args) => {
			const id = `task-${submitted.length + 1}`;
			submitted.push(String(args.task));
			store.add(id, String(args.task));
			return { status: 'pending', taskId: id };
		}),
		check: opts.check,
		log: (m) => logs.push(m),
	});
	return { relay, store, submitted, logs };
}

test('a call re-issued after its result was returned is closed quietly and never resent', async () => {
	const { relay, store, submitted, logs } = agent({
		// The check compares the new request with open AND recently answered tasks.
		check: async ({ task, openTasks }) => {
			const same = openTasks.find((t) => t.text === task);
			return same ? { action: 'duplicate', of: same.id } : { action: 'submit' };
		},
	});
	// First call: submitted, waits, and gets its result through the call.
	const first = relay.invoke('work', { task: 'find the apple' });
	await new Promise((r) => setImmediate(r));
	assert.deepEqual(submitted, ['find the apple']);
	assert.equal(relay.isWaiting('task-1'), true);
	assert.equal(relay.offerResult({ taskId: 'task-1', text: 'the apple is red' }), true);
	assert.match(await first, /the apple is red/);
	assert.equal(store.get('task-1')?.delivery, 'injected');

	// The model re-issues the same call seconds later (seen on 3.8): closed, not resent, told to stay silent.
	const again = await relay.invoke('work', { task: 'find the apple' });
	assert.deepEqual(submitted, ['find the apple'], 'the core must not see the request twice');
	assert.match(again, /answered a moment ago/);
	assert.match(again, /Stay silent/);
	assert.ok(logs.some((l) => l.includes('asked again after its result was returned')), logs.join('\n'));
});

test('recentlyAnswered lists delivered tasks only inside the window', () => {
	let now = 1_000_000;
	const store = createVoiceTaskStore(() => now);
	const relay = new RelayAgent({ store, submit: async () => ({ status: 'pending' }) });
	store.add('old', 'old request'); store.set('old', 'spoken');
	now += RECENT_ANSWER_WINDOW_MS + 1;
	store.add('fresh', 'fresh request'); store.set('fresh', 'injected');
	store.add('open', 'still open');
	assert.deepEqual(relay.recentlyAnswered(now).map((t) => t.id), ['fresh']);
	assert.deepEqual(relay.openTasks(now).map((t) => t.id), ['open']);
});

test('a reconnect ends every open call; the results then go another way', async () => {
	const { relay, logs } = agent();
	const a = relay.invoke('work', { task: 'draw a picture' });
	const b = relay.invoke('work', { task: 'what is the weather' });
	await new Promise((r) => setImmediate(r));
	assert.equal(relay.isWaiting('task-1'), true);
	assert.equal(relay.isWaiting('task-2'), true);

	const ended = relay.endAllCalls('continuing');
	assert.deepEqual(ended.sort(), ['task-1', 'task-2']);
	assert.match(await a, /still running; its result will come in a separate message/);
	assert.match(await b, /still running; its result will come in a separate message/);
	assert.equal(relay.isWaiting('task-1'), false);
	// A result that lands now is NOT returned to the dead call: the caller delivers it elsewhere.
	assert.equal(relay.offerResult({ taskId: 'task-1', text: 'here is the picture' }), false);
	assert.equal(relay.endAllCalls('continuing').length, 0, 'idempotent');
	void logs;
});

test('an open call returns its result as that call\'s own response (no fake first answer)', async () => {
	const { relay } = agent();
	let settled = false;
	const call = relay.invoke('work', { task: 'summarize the paper' }).then((r) => { settled = true; return r; });
	await new Promise((r) => setTimeout(r, 20));
	assert.equal(settled, false, 'the call stays open while the core works');
	assert.equal(relay.offerResult({ taskId: 'task-1', text: 'three findings' }), true);
	const text = await call;
	assert.equal(settled, true);
	assert.match(text, /TASK_RESULT_START/);
	assert.match(text, /three findings/);
});
