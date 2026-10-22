// A result returned through its open call counts as heard only when a spoken turn carries its content: an
// unrelated sentence after the return ("Yes, I can hear you") is not it. Own process (the result watcher is process-wide).
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

const { wireDurableChannels, resultEvidence, turnSaysResult, spokenNumberForms } = await import('../src/live-agent-runtime.js');
const { RelayAgent } = await import('../src/relay-agent.js');
const { voiceTaskStore, setVoiceTaskEndedListener, voiceTaskResultText } = await import('../src/task-bridge.js');

after(() => rmSync(TMP, { recursive: true, force: true }));

const tick = (ms: number) => new Promise((r) => setTimeout(r, ms));
const until = async (done: () => boolean, ms = 10_000) => { for (let w = 0; w < ms / 100 && !done(); w++) await tick(100); };

const notices: string[] = [];
/** Notices about one task (its request or its result): a test counts only its own, so one that fails leaves no
 * nudge or hand-over that makes a later test fail too (they share one session). */
const about = (s: string) => notices.filter((n) => n.includes(s)).length;
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
wireDurableChannels(session as any, { relay, notReadyRetriesMs: [10, 10], reconcileMs: 300, handoffConfirmMs: 200, returnNudgeMs: 400, returnReleaseMs: 1_500, returnReleaseTurnWaitMs: 2_500 });

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

	it('a nudge due while the model is mid-turn waits for the turn: none when that turn says the result', async () => {
		// 2026-10-20 18:14:09: the nudge for the Use Case 03 summary went in while the model was saying it; it said it again.
		const id = voiceTask('watch the Use Case 03 video and summarize it');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'watch the Use Case 03 video and summarize it' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'The Use Case 03 video is a 38-second demo: a person and an agent edit one page; the agent formats the notes as a list and adds the missing video link.');
		await Promise.race([call, tick(5_000)]);
		session.emit('turn.start');
		session.emit('response.first_audio');
		await tick(600); // past the nudge time, the model still speaking
		assert.equal(about('Use Case 03'), 0, 'no nudge into a turn in progress');
		items.push({ role: 'assistant', content: 'The Use Case three video shows a person and an agent collaborating on a document to format a list and add a video link.' });
		session.emit('turn.end', { turnId: 'tu1' });
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
		await tick(100);
		assert.equal(about('Use Case 03'), 0, 'and none after it: the turn said it');
	});

	it('a nudge due mid-turn goes as soon as a turn ends without the result', async () => {
		const id = voiceTask('summarize the launch plan');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'summarize the launch plan' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'The launch plan has four phases; the beta opens on November 12.');
		await Promise.race([call, tick(5_000)]);
		session.emit('turn.start');
		session.emit('response.first_audio');
		await tick(600);
		assert.equal(about('launch plan'), 0);
		items.push({ role: 'assistant', content: 'Sure, here is a joke about skeletons.' });
		session.emit('turn.end', { turnId: 'tu2' });
		assert.equal(about('summarize the launch plan'), 1, 'nudged once the turn ended');
		modelSays('The launch plan has four phases and the beta opens on November 12.', 'tu3');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
	});

	it('a result returned while the model is mid-turn counts when that turn says it: no nudge after it', async () => {
		// The turn that calls the tool goes on and says the answer once it is back; on a turn longer than the
		// nudge time, the nudge held for its end would have it said a second time.
		const id = voiceTask('when is the design review');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		session.emit('turn.start');
		await tick(5);
		const call = relay.invoke('Execute tool: work', { task: 'when is the design review' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'The design review is on Thursday afternoon in the Juniper room.');
		await Promise.race([call, tick(5_000)]);
		session.emit('response.first_audio');
		await tick(600);
		items.push({ role: 'assistant', content: 'The design review is Thursday afternoon, in the Juniper room.' });
		session.emit('turn.end', { turnId: 'tu-mid' });
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken', 'the turn said it');
		await tick(100);
		assert.equal(about('design review'), 0, 'no nudge');
	});

	it('a result returned mid-turn is not heard by a turn that says something else', async () => {
		const id = voiceTask('what is the wifi password');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		session.emit('turn.start');
		await tick(5);
		const call = relay.invoke('Execute tool: work', { task: 'what is the wifi password' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'The guest wifi password is marigold-harbor.');
		await Promise.race([call, tick(5_000)]);
		session.emit('response.first_audio');
		items.push({ role: 'assistant', content: 'Let me look that up for you.' });
		session.emit('turn.end', { turnId: 'tu-mid2' });
		assert.equal(voiceTaskStore.get(id)?.delivery, 'injected');
		modelSays('The guest wifi password is marigold-harbor.', 'tu-mid3');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
	});

	it('a nudge due during a turn with no speech (a tool call only) goes when that turn ends', async () => {
		const id = voiceTask('list the open milestones');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'list the open milestones' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'Two milestones are open: Beta on November 12 and GA on December 3.');
		await Promise.race([call, tick(5_000)]);
		session.emit('turn.start');
		await tick(600);
		assert.equal(about('list the open milestones'), 0, 'no nudge into a turn in progress');
		session.emit('turn.end', { turnId: 'tu-silent' });
		assert.equal(about('list the open milestones'), 1, 'nudged as the silent turn ended, not one turn later');
		modelSays('Two milestones are open: Beta on November 12 and GA on December 3.', 'tu-s2');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
	});

	it('a turn that carries a number from the result counts at once, with no nudge', async () => {
		const id = voiceTask('check PR 5617');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'check PR 5617' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'PR 5617 is merged; it went in on Saturday.');
		await Promise.race([call, tick(5_000)]);
		modelSays('Pull request 5617 was merged on Saturday.', 't4');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken');
		await tick(600);
		assert.equal(about('5617'), 0, 'no nudge for a result that was said');
	});

	it('a release due while the model is mid-turn waits for the turn to end', async () => {
		// Handed over into a turn, the result lands on top of whatever the model is saying.
		const id = voiceTask('how many seats are left on the flight');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'how many seats are left on the flight' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'There are 17 seats left on flight UA 857.');
		await Promise.race([call, tick(5_000)]);
		await until(() => about('how many seats are left') > 0, 3_000);
		assert.equal(about('how many seats are left'), 1, 'nudged');
		session.emit('turn.start');
		session.emit('response.first_audio');
		await tick(2_000); // past the release time, the model still speaking
		assert.equal(about('17 seats left'), 0, 'not handed over into the turn');
		items.push({ role: 'assistant', content: 'Anything else I can help with?' });
		session.emit('turn.end', { turnId: 'tu-rel' });
		await until(() => about('17 seats left') > 0, 3_000);
		assert.equal(about('17 seats left'), 1, 'handed over again once the turn ended');
		modelSays('There are 17 seats left on flight UA 857.', 'tu-rel2');
	});

	it('a turn that stays open does not hold the release for good', async () => {
		const id = voiceTask('which room is the offsite in');
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: 'which room is the offsite in' });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'The offsite is in the Larkspur room on the fourth floor.');
		await Promise.race([call, tick(5_000)]);
		session.emit('turn.start'); // a turn held open, for example by a blocking call
		await tick(3_000);
		assert.equal(about('Larkspur'), 0, 'still waiting for the turn');
		await until(() => about('Larkspur') > 0, 3_000);
		assert.equal(about('Larkspur'), 1, 'handed over once the wait ran out, the turn still open');
		session.emit('turn.end', { turnId: 'tu-open' });
		modelSays('The offsite is in the Larkspur room on the fourth floor.', 'tu-open2');
	});

	// Live 2026-10-20 11:14 (desktop voice): the answer was "3", said as "three" inside a turn that also carried another
	// result. The evidence was only 19/13/october/sutando, so the relay nudged and handed it over again, and the
	// user heard it three times.
	it('a small-number answer said before its own noun counts, even inside a combined turn', async () => {
		const before = notices.length;
		const request = 'Count the number of pull requests merged by myself yesterday.';
		const id = voiceTask(request);
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: request });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'Yesterday, October 19, you merged 3 pull requests in the Sutando repository, out of 13 merged in total that day. 3 of your own pull requests were also merged yesterday.');
		await Promise.race([call, tick(5_000)]);
		modelSays('The Use Case three video shows a person and an agent collaborating on a document to format a list and add a video link, and you merged three pull requests yesterday. I currently have one pending task, which is building the San Francisco restroom finder app.', 't88');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken', 'the answer was said');
		await tick(600);
		assert.equal(notices.length, before, 'no nudge and no second hand-over for a result that was said');
	});

	// Replay 2026-10-21 18:39 (ws-113802): "You merged 3 pull requests yesterday: number 5641, ..." was said as
	// "Regarding how many pull requests you merged yesterday, you merged three of them." The noun came first, the
	// number after it, so the relay judged it unsaid, nudged, and the model said it a second time.
	it('a small-number answer said after its noun ("three of them") counts', async () => {
		const before = notices.length;
		const request = 'How many pull requests did I merge yesterday?';
		const id = voiceTask(request);
		submitted = { status: 'pending', taskId: id, queuedAhead: 0, watcherOnline: true };
		const call = relay.invoke('Execute tool: work', { task: request });
		await tick(0);
		writeFileSync(join(TMP, 'results', `${id}.txt`), 'You merged 3 pull requests yesterday: number 5641, number 5648 and number 5650.');
		await Promise.race([call, tick(5_000)]);
		modelSays('Regarding how many pull requests you merged yesterday, you merged three of them.', 't89');
		assert.equal(voiceTaskStore.get(id)?.delivery, 'spoken', 'the answer was said');
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

	// The live transcript never writes digits: "98" is spoken as "ninetyeight" or "九十八", and a matcher that looks
	// for the digits nudges and re-hands a result the user has already heard.
	it('a small number counts only next to the result\'s own noun', () => {
		const e = resultEvidence('Yesterday, October 19, you merged 3 pull requests in the Sutando repository, out of 13 merged in total that day.', 'Count the number of pull requests merged by myself yesterday.');
		assert.ok(turnSaysResult('You merged three pull requests yesterday.', e), 'spelled out, before its noun');
		assert.ok(turnSaysResult('You merged 3 pull requests.', e), 'digits, before its noun');
		assert.equal(turnSaysResult('I currently have three pending tasks.', e), false, 'the same number before another noun is not it');
		assert.equal(turnSaysResult('There are three of them.', e), false);
		const merged = resultEvidence('You merged 3 pull requests yesterday: number 5641, number 5648 and number 5650.', 'How many pull requests did I merge yesterday?');
		assert.ok(turnSaysResult('Regarding how many pull requests you merged yesterday, you merged three of them.', merged), 'the number after its noun, named earlier in the turn');
		assert.ok(turnSaysResult('Pull requests you merged yesterday: three.', merged), 'the number alone, its noun named');
		assert.equal(turnSaysResult("I'm checking how many pull requests you merged yesterday.", merged), false, 'the noun with no number is the question, not the answer');
		assert.equal(turnSaysResult("I'm checking your pull requests; you have three pending tasks.", merged), false, 'the number counting another noun');
		assert.equal(turnSaysResult('You merged three of them.', merged), false, 'the number with its noun never named');
		// Holding turns that name the noun and a number that is not the count: "one of", "one more", "at 3 pm", "one by one".
		assert.equal(turnSaysResult('I will check your pull requests at 3 pm.', merged), false, 'a number followed by another word is not the count');
		assert.equal(turnSaysResult('Three of the pull requests are still loading.', merged), false, 'the number before its noun with words between');
		assert.equal(turnSaysResult('I am checking pull requests now, three more to go.', merged), false, 'the number after its noun but not closing the sentence');
		assert.equal(turnSaysResult('Three. Let me check your pull requests.', merged), false, 'the number in another sentence than its noun');
		const one = resultEvidence('You merged 1 pull request yesterday: number 5641.', 'How many pull requests did I merge yesterday?');
		assert.ok(turnSaysResult('You merged one pull request yesterday.', one), 'one, right before its noun');
		assert.equal(turnSaysResult('One of the pull requests is still loading, give me a second.', one), false, '"one of" is not the count');
		assert.equal(turnSaysResult('I am checking pull requests now, one more moment.', one), false, '"one more" is not the count');
		assert.equal(turnSaysResult('Let me go through your pull requests one by one.', one), false, '"one by one" is not the count');
		assert.equal(turnSaysResult('Checking your pull requests, one at a time.', one), false, '"one at a time" is not the count');
		assert.equal(turnSaysResult('Your pull requests: one of them.', one), false, '"one" after its noun is a pronoun, not the count');
		const asked = resultEvidence('5 tasks are open.', 'are 5 tasks open?');
		assert.equal(turnSaysResult('Five tasks are open.', asked), false, 'a phrase the request already had is not the answer');
	});

	// Review of #5774 (2026-10-22): the word after a small number was taken as its noun even when it was an adjective
	// ("new", "open") or a function word ("and"), so an unrelated turn with the same number counted as the result said,
	// and that result was never nudged or handed over again.
	it('a small number counts only before the result\'s own noun, never its adjective or a function word', () => {
		const msg = resultEvidence('You have 1 new message from Alice about invoice 4521.');
		assert.equal(turnSaysResult('You have one new task in the queue.', msg), false, 'same number and adjective, another noun');
		assert.ok(turnSaysResult('You have one new message from Alice.', msg), 'the noun after the adjective is said');
		const prs = resultEvidence('There are 2 open pull requests waiting for your review.');
		assert.equal(turnSaysResult('I have two open questions.', prs), false, 'same number and adjective, another noun');
		assert.ok(turnSaysResult('There are two open pull requests waiting.', prs), 'the noun after the adjective is said');
		assert.equal(turnSaysResult('one and two and three', resultEvidence('Found 2 and closed them.')), false, 'a function word is not a noun');
		const invoices = resultEvidence('You have 3 invoices waiting.');
		assert.ok(turnSaysResult('You have three invoices.', invoices), 'the noun ending its sentence counts');
		assert.deepEqual(resultEvidence('3 tasks.').phrases, [], 'a stop word stays stopped in the plural');
	});

	// Review of #5774: "three minutes" or "two days" is said in many a turn about something else; a time unit after a
	// small number is how long or when, never the count a result is about.
	it('a small number before a time unit is not the result said', () => {
		const build = resultEvidence('Your build finished in 3 minutes.');
		assert.equal(turnSaysResult('I will check back in three minutes.', build), false, 'minutes is not a noun the result counts');
		assert.ok(turnSaysResult('Your build finished in three minutes.', build), 'the result\'s own words still count');
		const due = resultEvidence('The Acme report is due in 2 days for the renewal.');
		assert.equal(turnSaysResult('Remind me again in two days.', due), false, 'days is not a noun the result counts');
		for (const unit of ['seconds', 'minute', 'hours', 'days', 'weeks', 'month', 'years', 'percent', 'times']) {
			assert.deepEqual(resultEvidence(`It took 3 ${unit}.`).phrases, [], `3 ${unit} is not a count phrase`);
		}
	});

	it('a number the transcript spells out counts: English, hyphen-free, pairwise for four digits, Chinese', () => {
		const prs = resultEvidence('98 PRs merged in sonichi/sutando in the last seven days — 2026-10-12 through today. Newest is #5717.');
		assert.equal(turnSaysResult('To follow up on the PRs, there were ninetyeight merged in the repository last week.', prs), true);
		assert.equal(turnSaysResult('关于上周合并的PR数量， 一共有九十八个合并到 Sutando 仓库里。', prs), true);
		assert.equal(turnSaysResult('Ninety-eight pull requests.', prs), true);
		assert.equal(turnSaysResult('The newest one is fifty-seven seventeen.', prs), true);
		assert.equal(turnSaysResult('Your disk is sixty percent free, with thirtyone gigabytes to clear.', prs), false, 'other numbers are not this result');
		assert.equal(turnSaysResult('Yes, I can hear you.', prs), false);
		const pct = resultEvidence('Disk is 60% free; 31 GB of caches can go.');
		assert.equal(turnSaysResult('Your disk is sixty percent free, and about thirtyone gigabytes of caches can be cleared.', pct), true);
		const dec = resultEvidence('It weighs 3.5 kg.');
		assert.equal(turnSaysResult('It weighs three point five kilograms.', dec), true);
		assert.equal(turnSaysResult('它重三点五公斤。', dec), true);
		for (const [n, f] of [['98', 'ninety eight'], ['98', '九十八'], ['5717', 'fifty seven seventeen'], ['2026', 'two thousand twenty six'], ['1105', '一千一百零五'], ['120', 'one hundred and twenty'], ['100', 'a hundred'], ['2500', 'twenty five hundred'], ['205', 'two oh five'], ['250', '两百五十'], ['3.50', 'three fifty']]) {
			assert.ok(spokenNumberForms(n).includes(f), `${n} → ${f}: ${spokenNumberForms(n).join(' | ')}`);
		}
		assert.ok(!spokenNumberForms('2005').includes('twenty five'), '2005 is never read as twenty-five');
	});

	// A spelled number must be whole words of the turn: "one" inside "done", "ten" inside "listening" or "attention",
	// and a date's day are not the result. Marked heard by one of these, a result was never handed over again.
	it('a number word inside another word, a small number, a date part, or the request\'s own number never count', () => {
		const pr = resultEvidence('PR 5691 has 2 approvals and 1 failing check.', 'check PR 5691');
		assert.deepEqual(pr.numbers, [], 'the request\'s number and the small counts are not evidence');
		for (const said of ["Don't worry, I'm on it.", 'Done!', 'Sure, I\'m listening.', 'Still checking 5691']) assert.equal(turnSaysResult(said, pr), false, said);
		assert.equal(turnSaysResult('Two approvals, one failing check.', pr), true, 'the distinctive words still count');
		const at10 = resultEvidence('The sync is set for 2026-10-20 at 10 tomorrow.');
		assert.deepEqual(at10.numbers, [], 'a date and a number under 13 are not evidence');
		assert.equal(turnSaysResult('People often ask me that.', at10), false);
		const prs = resultEvidence('98 PRs merged in sonichi/sutando in the last seven days.');
		assert.equal(turnSaysResult('Sure, I\'m listening.', prs), false);
		assert.equal(turnSaysResult('Ninety-eight were merged.', prs), true);
		assert.equal(turnSaysResult('Ninetyeight.', prs), true, 'a join inside the number is fine');
		assert.equal(turnSaysResult('The eightyninety eight.', prs), false, 'a join with the neighbouring word is not');
		const n100 = resultEvidence('There are 100 rows.');
		assert.equal(turnSaysResult('一共一百行。', n100), true);
		assert.equal(turnSaysResult('一共一百五十行。', n100), false, 'a longer numeral is a different number');
		for (const [n, said] of [['15', '合并了十五个。'], ['60', '六十个合并了。'], ['250', '两百五十个。'], ['250', '二百五十个。']]) assert.equal(turnSaysResult(said, resultEvidence(`${n} PRs merged.`)), true, said);
		assert.equal(turnSaysResult('Anything at all.', resultEvidence('OK', 'OK')), false, 'no evidence: never by content');
		const cn = resultEvidence('合并了15个PR。');
		assert.ok(cn.words.includes('合并'), 'CJK words are two-character grams');
		assert.equal(turnSaysResult('合并了十五个。', cn), true);
	});
});
