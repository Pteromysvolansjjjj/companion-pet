/** Presentation metadata only: never changes the Session log or model history. */
export const CHAT_PROJECTION_KEY = 'lolCompanionChat';

export function initialChatProjection() {
  return { currentTurn: null, startSeq: null, game: false, human: false, view: { hiddenTurns: [], hiddenRanges: [], mixedTurns: [] } };
}

export function foldChatProjection(state, event) {
  if (event.type === 'turn/start') {
    const turn = event.data?.turn;
    if (!Number.isSafeInteger(turn) || turn < 0) return state;
    return { ...state, currentTurn: turn, startSeq: event.seq, game: false, human: false };
  }
  if (event.type === 'turn/end') {
    if (state.currentTurn !== event.data?.turn) return state;
    const hiddenRanges = state.view.hiddenRanges.map(range => range.turn === state.currentTurn ? { ...range, endSeq: event.seq } : range);
    return { ...state, currentTurn: null, startSeq: null, view: { ...state.view, hiddenRanges } };
  }
  if (event.type !== 'user/message' || event.surfaceOp !== 'append' || state.currentTurn === null) return state;
  const source = event.data?.source?.kind;
  const game = state.game || source === 'lol-companion';
  const human = state.human || (source !== 'lol-companion' && source !== 'runtime-context');
  if (game === state.game && human === state.human) return state;
  const turn = state.currentTurn;
  const hidden = game && !human;
  const mixed = game && human;
  const hiddenTurns = state.view.hiddenTurns.includes(turn) === hidden ? state.view.hiddenTurns
    : hidden ? [...state.view.hiddenTurns, turn] : state.view.hiddenTurns.filter(value => value !== turn);
  const mixedTurns = state.view.mixedTurns.includes(turn) === mixed ? state.view.mixedTurns
    : mixed ? [...state.view.mixedTurns, turn] : state.view.mixedTurns.filter(value => value !== turn);
  const hiddenRanges = hiddenTurns === state.view.hiddenTurns ? state.view.hiddenRanges
    : hidden ? [...state.view.hiddenRanges, { turn, startSeq: state.startSeq, endSeq: null }]
      : state.view.hiddenRanges.filter(range => range.turn !== turn);
  const view = hiddenTurns === state.view.hiddenTurns && mixedTurns === state.view.mixedTurns
    ? state.view : { hiddenTurns, hiddenRanges, mixedTurns };
  return { ...state, game, human, view };
}

/** z is the host's existing Zod instance. No independent model, session, or persistence store. */
export function createChatProjection(z) {
  const turn = z.number().int().nonnegative();
  const range = z.object({ turn, startSeq: turn, endSeq: turn.nullable() });
  const viewSchema = z.object({ hiddenTurns: z.array(turn), hiddenRanges: z.array(range), mixedTurns: z.array(turn) });
  return {
    key: CHAT_PROJECTION_KEY,
    stateVersion: 1,
    stateSchema: z.object({ currentTurn: turn.nullable(), startSeq: turn.nullable(), game: z.boolean(), human: z.boolean(), view: viewSchema }),
    init: initialChatProjection,
    apply: foldChatProjection,
    wire: { viewSchema, view: state => state.view },
  };
}

export function registerChatProjection(ctx, z) {
  return ctx.sessionProjections.register(createChatProjection(z));
}
