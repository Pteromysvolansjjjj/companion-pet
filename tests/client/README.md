# Chat presentation verification

Run `node scripts/build-client.mjs` after editing `src/client/chat-filter.mjs`, then `node --test tests/client/*.test.mjs`.

The tests read the installed DSH 0.2.1-rc.2 archive at `E:/deepseek/dsh/resources/app.asar` (override with `DSH_ASAR`). They execute its real Conversation registries, assembler, Chat Definitions/Builder/Group, and Trajectory Definitions/Builder in a VM. Only additional test exports expose the SDK's internal registration functions in memory; installed files are never changed. React rendering and unrelated browser services are stubs, so these are SDK projection integration tests, not a claim of a completed browser visual test.

Covered behavior:

- A non-human followup wake notice, reasoning/text chunks, final answer, `<silent/>`, and completed-turn footer produce no Chat nodes for a game-only turn.
- The SDK listener is registered before the filter listener, exercising live-event ordering and assistant settlement.
- Manual turns, unknown-source inputs, and mixed turns remain visible. A human input overrides a temporarily stale wire projection.
- Cold history tails, prepends, cloned replacement windows, different sessions with the same turn number, and a reopened session binding retain correct attribution.
- Trajectory snapshots and the original events remain unchanged; unloading the filter restores ordinary Chat output.
- The generated browser bundle uses the formal lazy module factory and declared services.

The host's `lolCompanionChat` projection contains only turn numbers and sequence ranges. It replays complete Session history, so the client need not guess provenance from message text or from a truncated visible window. Its only neutral user-message source is `runtime-context`; every other non-game source makes the turn visible.

This plugin does not suppress global running indicators, error notifications, or the separate queue dock. It filters Chat's automatic turn nodes and preserves the underlying activity record.
