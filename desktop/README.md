# Windows desktop renderer

`build.ps1` uses the installed .NET Framework 4 compiler and WPF assemblies to
create `../dist/LolCompanion-0.7.1.exe` from `LolCompanion.cs` and `ChatInput.cs`; no downloaded packages are required.
The versioned filename leaves a running older renderer untouched. The executable
derives the project root from its `dist` directory, or accepts `--root <directory>`.

The parent spawns it with redirected stdin/stdout/stderr and a hidden console
(`windowsHide: true`). UTF-8 JSON-lines on stdin:

```json
{"type":"reply","commandId":"2","lines":["这波击杀，我看见啦。","下一波也陪着你。","长回复继续逐页显示。"],"holdMs":5000}
{"type":"reply","text":"Legacy short reply"}
{"type":"status","text":"Waiting for game"}
{"type":"clear","commandId":"3"}
{"type":"show"}
{"type":"hide","commandId":"4"}
{"type":"shutdown"}
```

The renderer does not select a conversation, read game state or call an AI service.
`status` changes only the tray tooltip. A reply that trims to `<silent/>` clears the
bubble. Hiding cancels the timeline and all pending pages; replies received while
hidden are discarded and never replayed on show. EOF on stdin exits. A new process
is required after an explicit exit.

`lines` contains one or more nonempty single-line strings, without a fixed page
count limit. The parent uses `Intl.Segmenter` to validate at most 20 modern Unicode
graphemes per page. The renderer checks array shape, control characters, line
separators, well-formed UTF-16 and a 4096-code-unit transport ceiling per page;
it does not repeat segmentation using Framework's older Unicode tables. The full
JSON message has a 16 Mi UTF-16-code-unit limit. Optional `holdMs` defaults to 5000
and accepts integers from 500 through 30000. Each page fades in for 250 ms, stays
visible for `holdMs`, then fades out for 250 ms before the next page starts.

Legacy `text` accepts one short line, using `StringInfo` plus common emoji ZWJ,
modifier and flag grouping. Overlong text is rejected, never truncated. Do not send
both `text` and `lines`. Malformed input preserves the existing display. A new valid
reply replaces the current sequence; clear, silence, hide and exit cancel it.
The character remains visible after playback. The vanished bubble area no longer
accepts clicks. The speech bubble has no ScrollViewer, ellipsis or truncation.

Stdout contains JSON events only. `ready` reports `protocolVersion: 3`; other events
are `playback`, `invalid`, `preferences:set`, `chat:submit` and `exit`. Diagnostics on stderr omit
reply text. `--smoke-test` opens no native window or tray icon and also emits
`handled` acknowledgements with display state and accepted conversation settings.

Every reply start emits `{"type":"playback","active":true,"commandId":"2"}`.
Manual replacement stays active without an intermediate false. Natural completion,
clear, hide, silence and exit emit false. A reply discarded while hidden also emits
false so the parent can release an optimistic busy state. There are no per-page
events. The host waits for the whole reply to finish before generating the next
automatic reply, using latest state plus accumulated events rather than one queued
model response per event.

The parent attaches a distinct string `commandId` to each reply, clear or hide.
Only fully validated display commands update the current ID. Natural completion,
menu hiding and exit retain it. The bridge ignores acknowledgements older than its
newest submitted display command. A rejected command reports its ID and the actual
unchanged state, for example:

```json
{"type":"invalid","code":"INVALID_HOLD_MS","commandId":"3","active":true,"currentCommandId":"2"}
```

The matching rejection lets the bridge restore the actual command and state.
Legacy untagged commands use `commandId:null`; unparseable JSON also has a null
rejected ID. Stderr diagnostics alone do not indicate that playback stopped.

Three completed clicks on the character, or the chat context/tray menu action,
open an activating input window below the non-activating pet. A drag crossing
the system threshold cancels the click sequence. No global hotkey is registered.
The composer is a compact 64-DIP-high translucent strip, without a title, buttons
or persistent help text. Its fill has alpha 64/255 and the TextBox background is
transparent. Only errors or an unbound state show a short message; timeout and
failed-send feedback tell the user to press Enter to retry. Such feedback expands
the strip to 86 DIPs. Sending, IME handling and acknowledgement logic are unchanged.
The input follows pet position and scale; at the screen edge the pair moves up
to fit, with overlap only when an unusually small work area cannot fit both.

Enter submits, Shift+Enter inserts a newline, and Escape dismisses while retaining
an unsent draft. IME composition and candidate-confirmation Enter are left to WPF;
submission requires a subsequent ordinary Enter. The previous foreground window
is restored on dismissal when possible, only if the composer still owns focus;
a delayed acknowledgement cannot steal focus from another app.

The renderer uses the active `bindingId` from preferences and emits:

```json
{"type":"chat:submit","bindingId":"binding-uuid","requestId":"unique-request-id","text":"刚才那波你看见了吗？"}
```

The host enqueues exact text as `source.kind: user` in the existing bound Agent
and responds with `chat:result`, the same binding/request IDs, `accepted: true`
or `accepted: false` and an optional safe error. Acceptance means queued, not
model completion. Manual messages and replies remain visible in the original
workspace Chat. No new conversation, model service or desktop transcript is made.
Input has a 65536 UTF-16 code-unit transport ceiling, independent of speech pages.

While pending, the text is read-only and repeated sends are disabled. After 15
seconds without confirmation, retry uses the same request ID and original text;
the host caches accepted requests per binding to avoid duplicate enqueue. A matching
success clears and hides; a rejection keeps the draft editable. Stale confirmations
are ignored. Changing or clearing the binding discards drafts and pending UI state.
Incoming messages never open or activate the composer. These operations do not
change bubble playback IDs, and ordinary replies still use the existing output path.

Only `--smoke-test` accepts `chat:test` actions `open`, `text`, `submit`, `timeout`
and `dismiss`; `text` supplies a `text` field. This exercises real controls without
native windows. Normal desktop mode rejects these test commands. Smoke `handled`
events report `chatVisible`, `chatPending` and `chatTextLength`, never draft text.

Conversation preferences are owned and persisted by the host. It sends snapshots:

```json
{"type":"preferences","bindingId":"session-A:binding-1","values":{"decisionIntervalMs":15000,"pollIntervalMs":1000,"bubbleHoldMs":5000,"replyLength":20,"dataMode":"detailed"}}
{"type":"preferences","bindingId":null}
```

Applying an edit emits only the changed fields:

```json
{"type":"preferences:set","bindingId":"session-A:binding-1","requestId":"unique-request-id","changes":{"decisionIntervalMs":500,"dataMode":"basic"}}
```

The host validates the active binding, saves that conversation's preferences,
applies any sampling adjustment, and returns a full preferences snapshot with the
matching `requestId`. Optional `error` marks a rejection; `values` still supplies
the authoritative current values. The UI waits for confirmation and never writes
conversation preferences to its local settings file. Matching errors restore the
confirmed values. Unsolicited snapshots omit `requestId`. A changed `bindingId`
discards unsaved edits and pending requests; old acknowledgements are ignored.
Unbinding disables the conversation controls. Resend snapshots when binding
changes, after model-tool adjustments, and when a renderer starts. Preferences
messages do not change playback command IDs.

The conversation panel provides:

- `basic` (主要数据): phase, mode, champion selection, champions, levels, KDA,
  life/death state and game events. New settings default to this mode.
- `detailed` (详细数据): also all available state fields such as health, gold and
  items, plus bounded sampled changes. Both modes retain only recent state changes
  (by default at most 20 steps and 64 KiB), explicitly mark omitted history, and
  keep new event records separately. The initial placeholder is basic until host
  hydration; explicit host settings, including detailed mode, remain authoritative.
- Editable delivery presets of 15/10/5/2/1/0.5 seconds, plus custom values with
  millisecond precision and a minimum of 0.5 seconds.
- Read-only actual sampling interval, per-page hold duration, and suggested total
  reply length; `0` means unrestricted preference, not a hard display length limit.

There is no option to bypass automatic playback waiting. The UI explicitly says
the interval is not model latency: after the previous reply finishes, latest data
determines the next response; events are not replayed as separate queued replies.

Drag the character or visible bubble to move it; Ctrl+wheel scales between 240 and
640 logical pixels. Global appearance settings include preferred font size (shrunk
when needed to fit a full page), edge snapping and saved position. These alone are
stored in `data/desktop-settings.json`. The settings window is limited to the screen
work-area height and scrolls when needed; this does not add a scrollbar to the bubble.
The tray restores a hidden pet or an off-screen position.

The supplied `assets/DSniang02.png` remains byte-for-byte unchanged. A hand-traced
alpha mask produces `assets/DSniang02-character.png`, removing the original thought
bubble and circles without cutting horizontally through the hair or generating
hidden artwork. `prepare_character.py` reproduces it with Pillow and NumPy, which
are asset-generation dependencies only. Visible original RGB remains unchanged;
fully transparent RGB is cleared. Hashes and the contour are in the provenance JSON.

The pet is a transparent, non-activating topmost WPF window. Transparent asset
pixels do not accept clicks. Settings and chat input activate their own windows only
after an explicit action. There is no game injection or global hook. Exclusive fullscreen overlays
are not guaranteed; use borderless/windowed mode or another monitor.

Offline verification:

```powershell
& .\desktop\build.ps1
& .\desktop\Test-Desktop.ps1
```

Tests save active, idle, compact, 20-ZWJ-emoji and conversation-settings previews,
including explicit opaque-black composites for viewers that mishandle transparent
RGB. Checks cover character alpha, absence of old artwork bubbles, independent
opacity, all 257 pages in order, exact default and custom hold boundaries, real timer
completion, playback correlation, cancellation, click-through, zero bubble
ScrollViewers, compact text fit, malformed input, Unicode and parent EOF.

Settings tests use the actual controls to verify disabled unbound state, hydration,
changed-field requests, custom intervals, confirmation-only commit, rejected-write
restoration, stale acknowledgement rejection, dropping drafts and pending requests
on rebinding, and unchanged playback. The preview is `test-output/preview-settings.png`.
Tests do not write user settings or open native windows. OS dragging, tray interaction,
mixed-monitor DPI and game overlay behavior still require normal UI acceptance.

Composer verification adds real WPF controls/routed events for unbound behavior,
exact text, IME candidate guarding, pending sends, timeout same-ID retries, failed
and successful acknowledgements, stale binding, focus-return guards, triple clicks
versus dragging and screen-edge geometry. `test-output/preview-chat.png` is the
rendered input panel. Routed events do not replace checking an installed Chinese
IME or native game focus behavior; OS focus restoration remains best effort.
