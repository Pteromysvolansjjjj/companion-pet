import test from 'node:test';
import assert from 'node:assert/strict';
import { parseSpeech, createReplyCollector } from '../../src/replies.mjs';

test('speech accepts one/two short utterances and exact silence', () => {
  assert.deepEqual(parseSpeech('这波我站对面。\n再来一次？'), { type: 'reply', lines: ['这波我站对面。', '再来一次？'] });
  assert.deepEqual(parseSpeech(' <silent/>\n'), { type: 'clear' });
  assert.equal(parseSpeech('先等等<silent/>'), null);
});
test('visible-character boundary treats emoji clusters as one; never truncates', () => {
  for (const char of ['字', '👨‍👩‍👧‍👦', '🇨🇳', '👍🏽', 'e\u0301']) {
    assert.deepEqual(parseSpeech(char.repeat(20)), { type: 'reply', lines: [char.repeat(20)] });
    assert.deepEqual(parseSpeech(char.repeat(21)), { type: 'reply', lines: [char.repeat(20), char] });
  }
  assert.deepEqual(parseSpeech('一\n二\n三'), { type: 'reply', lines: ['一', '二', '三'] });
  assert.equal(parseSpeech(''), null);
  assert.equal(parseSpeech('ab\u0000cd'), null);
  assert.equal(parseSpeech('\ud800'), null);
});
test('invalid completed speech is diagnosed without displaying or changing history', () => {
  const outputs = [], invalid = [];
  const collector = createReplyCollector(x => outputs.push(x), { onInvalid: x => invalid.push(x) });
  const event = { type: 'assistant/message', data: { turn: 3, message: { content: [{ type: 'text', text: '不能\u0000显示' }] } } };
  const original = structuredClone(event);
  collector.accept(event);
  assert.equal(collector.candidate(3), '不能\u0000显示');
  collector.accept({ type: 'turn/end', data: { turn: 3, reason: { kind: 'completed' } } });
  assert.deepEqual(outputs, []); assert.equal(invalid.length, 1); assert.deepEqual(event, original);
});

test('long replies paginate locally with no total page ceiling, preserving all visible text', () => {
  const text = '这句话没有被模型重写或截断。'.repeat(500);
  const output = parseSpeech(text);
  assert.ok(output.lines.length > 100);
  assert.equal(output.lines.join(''), text);
  const segmenter = new Intl.Segmenter('zh', { granularity: 'grapheme' });
  assert.ok(output.lines.every(line => [...segmenter.segment(line)].length <= 20));
  for (const bad of ['字\t字', '字\u0085字', '字\u009f字', 'e' + '\u0301'.repeat(5000)]) assert.equal(parseSpeech(bad), null);
  assert.ok(parseSpeech('字'.repeat(20) + ' '.repeat(21) + '末').lines.every(line => line.trim()));
});
