import { defineTool } from '@deepseek-ai/dsh-tools';
import { createUserMessage } from '@deepseek-ai/dsh-llm';
import { z } from 'zod';
import { mountCompanion } from './src/host.mjs';
import { registerChatProjection } from './src/chat-projection.mjs';

export const name = 'lol-companion';
export const inject = ['tools', 'sessionProjections'];
export function apply(ctx, config = {}) {
  registerChatProjection(ctx, z);
  return mountCompanion(ctx, { defineTool, createUserMessage }, config);
}
