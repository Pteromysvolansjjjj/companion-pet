// The user selects this transport preset. It never ranks events or decides speech.
const IDENTITY = ['accountId', 'summonerId', 'puuid', 'riotId', 'riotIdGameName', 'riotIdTagLine', 'displayName', 'gameName', 'tagLine', 'summonerName'];
const ACCOUNT = [...IDENTITY, 'summonerLevel'];
const KDA = ['kills', 'deaths', 'assists'];
const PLAYER = [...IDENTITY, 'championName', 'rawChampionName', 'championId', 'championLevel', 'level', 'team', 'teamId', 'position', 'assignedPosition', 'selectedPosition', 'isDead', ...KDA];
const SELECTION_PLAYER = [...PLAYER, 'cellId', 'championPickIntent'];
const QUEUE = ['id', 'queueId', 'name', 'type', 'category', 'description', 'gameMode', 'mapId'];

function select(value, fields, children = {}) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return structuredClone(value);
  const entries = fields.filter(key => Object.hasOwn(value, key)).map(key => [key, structuredClone(value[key])]);
  for (const [key, project] of Object.entries(children)) if (Object.hasOwn(value, key)) entries.push([key, project(value[key])]);
  return Object.fromEntries(entries);
}

const each = project => value => Array.isArray(value) ? value.map(project) : structuredClone(value);
const account = value => select(value, ACCOUNT);
const player = value => select(value, PLAYER, { scores: value => select(value, KDA) });
const selectionPlayer = value => select(value, SELECTION_PLAYER);
const lobbyMember = value => select(value, [...PLAYER, 'isLeader', 'isBot', 'firstPositionPreference', 'secondPositionPreference', 'ready']);
const queue = value => select(value, QUEUE);
const map = value => select(value, ['id', 'name', 'mapId', 'mapName']);

// Automatic observations carry only the cumulative log's metadata. Event bodies
// travel once in update.events; a direct lol_read query still returns the full log.
const EVENT_TRANSPORT_FIELDS = new Set(['Events', 'eventCount', 'lastEventId', 'lastEventTime', 'logIncluded']);
function eventMetadata(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return structuredClone(value);
  const metadata = Object.fromEntries(Object.entries(value)
    .filter(([key]) => !EVENT_TRANSPORT_FIELDS.has(key))
    .map(([key, child]) => [key, structuredClone(child)]));
  if (Array.isArray(value.Events)) {
    metadata.eventCount = value.Events.length;
    const last = value.Events.at(-1);
    const scalar = field => typeof field === 'string' || (typeof field === 'number' && Number.isFinite(field));
    if (scalar(last?.EventID)) metadata.lastEventId = last.EventID;
    if (scalar(last?.EventTime)) metadata.lastEventTime = last.EventTime;
  }
  metadata.logIncluded = false;
  return metadata;
}

function omitCumulativeEvents(snapshot) {
  const projected = structuredClone(snapshot);
  const live = projected.sections?.live?.data;
  if (live && typeof live === 'object' && Object.hasOwn(live, 'events')) live.events = eventMetadata(live.events);
  return projected;
}

function selectionActions(value) {
  if (Array.isArray(value)) return value.map(selectionActions);
  return select(value, ['id', 'actorCellId', 'type', 'championId', 'completed', 'isInProgress', 'isAllyAction', 'pickTurn']);
}

const RESULT_STATS = ['kills', 'deaths', 'assists', 'level', 'win', 'winner', 'KILLS', 'DEATHS', 'ASSISTS', 'LEVEL', 'WIN', 'LOSE'];
function resultStats(value) {
  if (Array.isArray(value)) return value.filter(stat => RESULT_STATS.includes(stat?.statTypeName)).map(stat => select(stat, ['statTypeName', 'value']));
  return select(value, RESULT_STATS);
}
const resultPlayer = value => select(value, [...PLAYER, 'isWinningTeam', 'win', 'winner'], {
  scores: value => select(value, KDA), stats: resultStats,
});
const resultTeam = value => select(value, ['teamId', 'team', 'isWinningTeam', 'isWinner', 'win', 'winner'], {
  players: each(resultPlayer), participants: each(resultPlayer), stats: resultStats,
});

const BASIC_SECTIONS = {
  phase: value => structuredClone(value),
  summoner: account,
  lobby: value => select(value, ['partyId', 'canStartActivity'], {
    gameConfig: value => select(value, ['queueId', 'gameMode', 'mapId', 'gameType', 'gameMutator', 'isCustom', 'customGameName']),
    members: each(lobbyMember), localMember: lobbyMember,
  }),
  matchmaking: value => select(value, ['searchState', 'isCurrentlyInQueue', 'queueId']),
  readyCheck: value => select(value, ['state', 'playerResponse', 'dodgeWarning']),
  champSelect: value => select(value, ['localPlayerCellId', 'isCustomGame', 'isSpectating', 'bans'], {
    myTeam: each(selectionPlayer), theirTeam: each(selectionPlayer), actions: selectionActions,
    timer: value => select(value, ['phase']),
  }),
  gameflow: value => select(value, ['phase', 'gameId'], {
    gameData: value => select(value, ['gameId', 'queueId', 'gameMode', 'mapId', 'gameType', 'gameName', 'isCustomGame'], {
      queue, teamOne: each(player), teamTwo: each(player),
    }), map,
  }),
  live: value => select(value, [], {
    events: eventMetadata,
    gameData: value => select(value, ['gameMode', 'mapNumber', 'mapName', 'gameType', 'isReplay']),
    activePlayer: value => select(value, [...IDENTITY, 'level']), allPlayers: each(player),
  }),
  endgame: value => select(value, ['gameId', 'gameMode', 'gameType', 'queueType', 'queueId', 'gameLength', 'isRanked', 'ranked', 'win', 'winner'], {
    teams: each(resultTeam), players: each(resultPlayer), participants: each(resultPlayer),
  }),
};

export function validateDataMode(mode) {
  if (mode !== 'basic' && mode !== 'detailed') throw new TypeError('dataMode must be basic or detailed');
  return mode;
}

/** Project an already-sanitized snapshot. Missing fields stay absent, never zero-filled. */
export function projectObservation(snapshot, dataMode = 'basic') {
  validateDataMode(dataMode);
  if (dataMode === 'detailed') return { ...omitCumulativeEvents(snapshot), dataMode };
  const sections = Object.fromEntries(Object.entries(snapshot.sections ?? {}).map(([name, section]) => {
    const result = select(section, ['status', 'reason', 'httpStatus']);
    const projector = BASIC_SECTIONS[name];
    if (projector && Object.hasOwn(section, 'data')) result.data = projector(section.data);
    return [name, result];
  }));
  return {
    capturedAt: snapshot.capturedAt, sources: structuredClone(snapshot.sources), sections,
    self: select(snapshot.self, ['status'], { player, activePlayer: value => select(value, [...IDENTITY, 'level']), account }),
    dataMode,
  };
}
