// A result returned through its open call counts as heard only when a spoken turn carries its content.
// 2026-10-18 02:52: "61 PRs merged" returned to its call; the model's next words were "Yes, I can hear you",
// the table recorded the result spoken, and the user had to ask for it. Own process (the result watcher is
// process-wide).
// Run: npx tsx --test --test-force-exit tests/relay-agent-heard-by-content.test.ts
import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const TMP = mkdtempSync(join(tmpdir(), 'sutando-relay-heard-'));
process.env.SUTANDO_WORKSPACE = TMP;
process.env.SUTANDO_TEST_MODE = '1';
mkdirSync(join(TMP, 'tasks'), { recursive: true });
mkdirSync(join(TMP, 'results'), { recursive: true });

const { wireDurableChannels, resultEvidence, turnSaysResult } = await import('../src/live-agent-runtime.js');
const { RelayAgent } = await import('../src/relay-agent.js');
const { voiceTaskStore, setVoiceTaskEndedListener, voiceTaskResultText } = await import('../src/task-bridge.js');

after(() => rmSync(TMP, { recursive: true, force: true }));

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));
const until = async (done: () => boolean, ms = 10_000) => { for (let w = 0; w < ms / 100 && !done(); w++) await tick(100); };

const notices: string[] = [];
const items: Array<{ role: string; content: string }> = [];
const handlers: Record<string, Array<(e?: unknown) => void>> = {};
const session = {
	emit: (event: string, e: unknown = {}) => { for (const h of handlers[event] ?? []) h(e); },
	eventBus: { subscribe: (event: string, h: (e?: unknown) => void) => { (handlers[event] ??= []).push(h); } },
	getTranscriptionMode: () => 'agent',
	sessionManager: { isActive: true },
	clientConnected: true,
	conversationContext: { items },
	tryPublishSystemNotification(text: string) { notices.push(text); return true; },
	transport: { sendContent: () => {} },
};
let submitted: Record<string, unknown> = {};
const relay = new RelayAgent({ submit: async () => submitted, store: voiceTaskStore });
setVoiceTaskEndedListener((taskId, why) => { relay.endCall(taskId, why); });
// eslint-disable-next-line @typescript-eslint/no-explicit-any
wireDurableChannels(session as any, { relay, notReadyRetriesMs: [10, 10], reconcileMs: 300, handoffConfirmMs: 200, returnNudgeMs: 400, returnReleaseMs: 1_500 });

let seq = 0;
function voiceTask(text: string): string {
	const id = `task-19200${String(++seq).padStart(8, '0')}`;
	writeFileSync(join(TMP, 'tasks', `${id}.txt`), `id: ${id}\nsource: voice\ntask: ${text}\n`);
	voiceTaskStore.add(id, text);
	return id;
}
/** The model speaks one turn with these words. */
const modelSays = (text: string, turnId: string) => {
	session.emit('turn.start');
	session.emit('response.first_audio');
	items.push({ role: 'assistant', content: text });
	session.emit('turn.end', { turnId });
};

describe('a returned result is heard only when the model says it', () => {
	it('a spoken turn about something else leaves it unheard; a nudge follows; the turn that says it marks it heard', async () => {
		const id = voiceTask('count the PRs merged in the last 3 days');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'count the PRs merged in the last 3 days' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'Sixty-one pull requests merged in sonichi/sutando in the last three days.');
		assert.match(await Promise.race([call, tick(5_000).then(() => 'timed out')]), /Sixty-one/);
		assert.equal(voiceTaskStore.get(id)?.delivery, 'injected', 'returned, not yet heard');
		modelSays('Yes, I can hear you.', 't1');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'injected', 'an unrelated sentence is not the result');
		modelSays('There are six pending tasks right now.', 't2');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'injected', 'a status answer is not the result either');
		await until(() => notices.some((n) => /came back through its work call/.test(n)), 3_000);
		const nudge = notices.find((n) => /came back through its work call/.test(n)) ?? '';
		assert.match(nudge, /count the PRs merged in the last 3 days/, 'the nudge names the request');
		assert.equal(voiceTaskResultText(id), 'Sixty-one pull requests merged in sonichi/sutando in the last three days.', 'the status tool can read the result to say it');
		modelSays('Sixty-one pull requests were merged in the Sutando repository in the past three days.', 't3');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
	});

	it('a turn that carries a number from the result counts at once, with no nudge', async () => {
		const before = notices.length;
		const id = voiceTask('check PR 5617');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'check PR 5617' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'PR 5617 is merged; it went in on Saturday.');
		await Promise.race([call, tick(5_000)]);
		modelSays('Pull request 5617 was merged on Saturday.', 't4');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
		await tick(600);
		assert.equal(notices.length, before, 'no nudge for a result that was said');
	});

	it('still unsaid after the release time: handed over again by the reconcile loop', async () => {
		const id = voiceTask('how many tasks are open in the tracker');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'how many tasks are open in the tracker' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'The tracker lists 14 open items, 3 of them blocked.');
		await Promise.race([call, tick(5_000)]);
		modelSays('Sure, what else can I do for you?', 't5');
		await until(() => voiceTaskStore.get(id)?.delivery === undefined || notices.filter((n) => n.includes('14 open items')).length > 0, 6_000);
		await until(() => notices.some((n) => n.includes('14 open items')), 6_000);
		assert.ok(notices.some((n) => n.includes('14 open items')), 'the result itself is handed over again as a notice');
	});
});

describe('resultEvidence / turnSaysResult', () => {
	it('numbers or two distinctive words; file markers and filler words are not evidence', () => {
		const e = resultEvidence('Here\'s your bird.\n[file: /x/y/generated-1792373489180.jpg]');
		assert.deepEqual(e.numbers, [], 'the file name\'s digits are not the result');
		assert.deepEqual(e.words, ['bird'], "'here's' and 'your' are filler");
		assert.ok(turnSaysResult('Your bird drawing is ready.', e), 'one distinctive word, and it was said');
		assert.equal(turnSaysResult('Yes, I can hear you.', e), false);
		const prs = resultEvidence('Sixty-one pull requests merged in sonichi/sutando in the last three days.');
		assert.equal(turnSaysResult('Yes, I can hear you.', prs), false);
		assert.equal(turnSaysResult('There are six pending tasks right now.', prs), false);
		assert.equal(turnSaysResult('Sixty-one pull requests were merged this week.', prs), true);
		const n = resultEvidence('PR 5617 is merged.');
		assert.equal(turnSaysResult('5617 went in on Saturday', n), true);
		assert.equal(turnSaysResult('It was merged.', n), false);
	});
});
