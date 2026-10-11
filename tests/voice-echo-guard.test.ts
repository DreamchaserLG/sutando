// The voice session drops the model's own speaker echo at ingress (bodhi EchoGuard).
// Run: npx tsx --test --test-force-exit tests/voice-echo-guard.test.ts
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

describe('voice echo guard', () => {
	it('voice-agent turns on bodhi\'s EchoGuard', () => {
		const src = readFileSync(join(import.meta.dirname, '..', 'src', 'voice-agent.ts'), 'utf-8');
		assert.match(src, /echoGuard: \{ enabled: true \},/);
	});
});
