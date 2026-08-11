---
name: sagasu
description: Browse websites through the existing persistent Sagasu preview browser using vision-first full-display screenshots and HumanCursor X-level mouse input, with bounded action sequences and supplemental CDP DOM extraction, visible-element location, direct navigation, and focused Unicode insertion. Use for browser interaction tasks that must reuse the current sagasu-preview profile, prefer a website's own search interface when practical, and pause agent input for the user at any login, verification request, or CAPTCHA.
---

# Browse with Sagasu

Run commands from the Sagasu repository root.

## Use the fixed preview session

- Always target `--container sagasu-preview`.
- Reuse the existing container and the profile volume already attached to it.
- Do not stop, restart, recreate, or replace the container.
- Do not clear cookies, sign out, reset the profile, or create another profile.

Until the host CLI is installed, invoke it as:

```bash
PYTHONPATH=src python3 -m sagasu.cli.main
```

Use only the currently implemented public commands:

```text
session display
session screenshot
session dom
session locate
session navigate
session insert-text
session sequence
session cursor position|move|click|drag|scroll
```

Do not invent `setup`, `config`, session lifecycle, keyboard, CDP wait, upload,
cookie, accessibility, or public handoff commands. The commands in the
login/CAPTCHA section are private executor commands for this preview workflow.

## Check the existing container

At the beginning of a browsing task, first check whether the existing container
for the preview profile is already active:

```bash
docker inspect --format '{{.State.Running}}' sagasu-preview
```

- If the result is `true`, keep using it and do not call `docker start`.
- If the result is `false`, start that existing container:

  ```bash
  docker start sagasu-preview
  ```

- If inspection reports that `sagasu-preview` does not exist, stop and tell
  the user. Do not create a replacement.

Starting the existing container preserves its attached preview profile volume.
After either path, check its health before interacting:

```bash
docker inspect --format '{{.State.Health.Status}}' sagasu-preview
```

Proceed only after the result is `healthy`. Poll briefly if it is still
`starting`. If it exits or does not become healthy, stop and tell the user. Do
not fall back to `docker compose up`, `docker run`, or another container
because that could select a different profile.

## Follow the interaction priority

1. Use the full-display screenshot and vision as the primary observation
   source. It includes page content, browser chrome, overlays, and popups.
2. Use HumanCursor-backed X input for normal pointing, hovering, clicking,
   scrolling, and dragging.
3. Use CDP only for focused utility operations: direct navigation, optional
   DOM extraction, element-to-screen location, and text insertion into an
   already focused field.
4. Capture the DOM only when vision does not provide enough detail or when the
   task requires structured extraction. Do not capture it automatically for
   every screenshot or after every action.

## Account for idle cursor motion

The preview container enables HumanCursor idle motion by default. Five seconds
after the last executor command, the pointer begins moving continuously to
random visible points within 300 pixels of its stopped position; each movement
takes 0.3 to 2 seconds. Idle mode never clicks, scrolls, drags, or types, and a
new executor command takes priority and resets its cooldown.

Do not interpret idle motion as an action result. Idle hover can still open a
menu or change a hover-sensitive page. Treat a coordinate as stale when idle
hover or page motion may have changed what occupies it, and capture a fresh
screenshot before clicking in that case.

## Use the available tools

Query display geometry or the current pointer only when useful:

```bash
PYTHONPATH=src python3 -m sagasu.cli.main session display --container sagasu-preview
PYTHONPATH=src python3 -m sagasu.cli.main session cursor --container sagasu-preview position
```

Capture the primary visual observation:

```bash
PYTHONPATH=src python3 -m sagasu.cli.main session screenshot --container sagasu-preview --out /tmp/sagasu-preview.png --overwrite
```

Open `/tmp/sagasu-preview.png` with the available image-viewing or vision tool
(`view_image` in Codex). Actually inspect the pixels before deciding what to
do; do not infer page state from the command metadata alone. Screenshots
include the pointer by default. Use `--no-pointer` only when pointer pixels
would interfere with inspection.

Screenshot, DOM, and sequence artifacts are validated and atomically published
at `--out`. Use `--overwrite` only for a temporary path owned by this task; do
not overwrite a user file merely to make a command succeed.

Drive the real X cursor. Clicking, moving, dragging, and scrolling use
HumanCursor by default:

```bash
PYTHONPATH=src python3 -m sagasu.cli.main session cursor --container sagasu-preview move X Y
PYTHONPATH=src python3 -m sagasu.cli.main session cursor --container sagasu-preview click X Y
PYTHONPATH=src python3 -m sagasu.cli.main session cursor --container sagasu-preview scroll X Y --steps -4
PYTHONPATH=src python3 -m sagasu.cli.main session cursor --container sagasu-preview drag X1 Y1 X2 Y2
```

Use atomic actions with explicit coordinates. Do not use `--current`, and do
not select the `xdotool` mouse backend unless the user specifically requests
debugging of that fallback. Coordinates refer to the entire screenshot, not
only the page viewport. Positive scroll steps move up and negative steps move
down. A standalone input action does not take a screenshot automatically.

Queue up to three deterministic mutations when no intermediate visual decision
is needed. By default, the command waits one second after the final action and
automatically captures the full display:

```bash
PYTHONPATH=src python3 -m sagasu.cli.main session sequence \
  --container sagasu-preview \
  --actions-json '[
    {"operation":"cursor.click","x":650,"y":312},
    {"operation":"text.insert","text":"apples"},
    {"operation":"cursor.click","x":603,"y":386}
  ]' \
  --out /tmp/sagasu-preview.png --overwrite
```

Immediately open the returned `output` path with `view_image`; the sequence
already took the post-action screenshot, so do not issue a second screenshot
first. Use `--settle-ms N` only when the page needs a different 0-30000 ms
post-action delay.

Queueable operations are `cursor.move`, `cursor.click`, `cursor.drag`,
`cursor.scroll`, `text.insert`, and `page.navigate`. Navigation can only be the
final action. Screenshot, DOM, locate, display, pointer observation, and human
pause/resume are never queue entries and must remain individual commands.

Use these action fields:

- `cursor.move`: `x`, `y`; optional `duration_ms`, `steady`, `backend`.
- `cursor.click`: `x`, `y`; optional `button`, `count`, `hold_ms`, `backend`.
- `cursor.drag`: `x1`, `y1`, `x2`, `y2`; optional `duration_ms`, `steady`,
  `backend`.
- `cursor.scroll`: `x`, `y`, `steps`; optional `backend`.
- `text.insert`: `text`.
- `page.navigate`: absolute HTTP(S) `url`.

HumanCursor is the default backend for queued cursor actions too. Do not put
`null` in optional fields; omit fields that are not being set.

The limit is a maximum, not a target. Use one action whenever an intermediate
click can open a menu, suggestion list, overlay, login, CAPTCHA, or otherwise
move the next target. Only batch coordinates grounded in the same fresh
screenshot whose layout is expected to remain stable throughout the sequence.

Actions are not transactional. Handle sequence failures from their structured
JSON instead of blindly retrying:

- When error details contain `output` and `failed_index`, actions before that
  zero-based index were applied and a diagnostic screenshot was published.
  Inspect `output` before choosing a recovery action.
- When error details contain `sequence_state`, the mutations ran but the final
  screenshot or pointer observation failed. Treat `actions_completed`,
  `results`, `failed_index` when present, and the observation flags in that
  state as authoritative. Do not replay completed actions.
- A `pointer_observation` error attached to an individual successful result is
  supplemental; it does not undo or invalidate that mutation.
- A destination-reservation error occurs before mutations. Choose another
  task-owned output path or explicitly overwrite only the intended temporary
  artifact.

## Follow the vision-first loop

1. Capture and inspect the full display:

   ```bash
   PYTHONPATH=src python3 -m sagasu.cli.main session screenshot --container sagasu-preview --out /tmp/sagasu-preview.png --overwrite
   ```

2. Open the screenshot with vision and identify the visible page state,
   relevant controls, overlays, and likely action coordinates.

3. Decide whether more structured detail is genuinely needed. Capture the DOM
   only for cases such as:

   - extracting a list, table, prices, product attributes, or dense text;
   - obtaining an exact label or selector for an ambiguous visible control;
   - distinguishing visually truncated or very similar results;
   - confirming page state that is not legible or visually exposed.

   When needed, capture it once:

   ```bash
   PYTHONPATH=src python3 -m sagasu.cli.main session dom --container sagasu-preview --out /tmp/sagasu-preview.html --overwrite
   ```

   Inspect only the relevant DOM fragments where practical. DOM presence is
   never proof that an element is visible, unobstructed, or safe to click. The
   artifact contains the active top-level document; it does not flatten
   cross-origin iframe documents.

4. When a visible target has a stable CSS selector, optionally ask CDP to map
   it into the current full-display coordinate space:

   ```bash
   PYTHONPATH=src python3 -m sagasu.cli.main session locate --container sagasu-preview 'button[type=submit]'
   ```

   Read `screen.x` and `screen.y` from the result and compare that point with
   the current screenshot. `locate` does not scroll or click, and it fails when
   the element is missing or geometrically offscreen. Do not use a returned
   coordinate when a visible overlay, browser popup, or page motion makes it
   stale or obstructed.

5. Take one small X-level action, or a bounded sequence when every later action
   is safe without intermediate observation. Clicking uses HumanCursor by
   default:

   ```bash
   PYTHONPATH=src python3 -m sagasu.cli.main session cursor --container sagasu-preview click X Y
   PYTHONPATH=src python3 -m sagasu.cli.main session cursor --container sagasu-preview scroll X Y --steps -4
   PYTHONPATH=src python3 -m sagasu.cli.main session cursor --container sagasu-preview drag X1 Y1 X2 Y2
   ```

6. After a standalone action, capture and visually inspect a fresh screenshot.
   After a sequence, immediately inspect its automatically produced `output`
   image instead. Fetch a new DOM only if the new state requires detailed
   reading or extraction.

Keep only one mutating command in flight for `sagasu-preview`. One display has
one cursor and one keyboard focus. If a command returns `session_busy`, another
actor owns the session lock; do not start a competing action or recreate the
container. Wait for that actor to finish, then capture a fresh screenshot
before deciding what to do.

## Prefer the website's search interface

When asked to search within a website, use its visible search bar when it is
available and convenient:

1. If needed, use CDP navigation only to open the site's homepage or an exact
   landing page supplied by the user.
2. Capture and inspect a screenshot, then visually find the site's search
   field. Use DOM or `locate` only if the control is ambiguous.
3. Click the field through X input and insert the query. Queue the visible
   submit click only when text entry cannot open suggestions or move/cover the
   target; otherwise inspect the intermediate screen first.
4. Inspect the sequence's automatic result screenshot, or capture one after a
   standalone submit action. Use DOM afterward only when the result details
   need structured extraction.

Prefer this over constructing and directly navigating to a search-results URL
merely to skip the site's interface. Direct navigation remains appropriate
when the site has no usable search control, the user supplied an exact URL, or
the native search path is clearly impractical.

## Use supplemental CDP deliberately

Use supplemental CDP navigation when the destination URL is known:

```bash
PYTHONPATH=src python3 -m sagasu.cli.main session navigate --container sagasu-preview 'https://example.com'
```

This accepts only absolute HTTP(S) URLs and returns when Chromium accepts the
navigation, not when loading finishes. Capture and inspect a fresh screenshot
before acting. If navigation is placed in a sequence it must be last, and the
sequence supplies that screenshot after its settle delay. Do not fetch the DOM
unless the destination needs detailed reading or extraction. There is no CDP
load-wait command; if the page is visibly incomplete, wait briefly and observe
it again.

Click a normal text field through X before inserting text into it. Use CDP text
insertion, particularly for Unicode that the X keyboard map cannot represent:

```bash
PYTHONPATH=src python3 -m sagasu.cli.main session insert-text --container sagasu-preview 'search text'
```

CDP inserts into the element that already has focus; it does not choose or
focus a field. Capture and visually inspect a fresh screenshot to verify the
result. Fetch the DOM only if the visible result is insufficient.
Never use DOM JavaScript to click, submit, or bypass the visible interface.

## Pause for login or CAPTCHA

At the first sign of a login page, password or verification-code request,
CAPTCHA, or equivalent human-verification challenge:

1. Immediately pause agent input:

   ```bash
   docker exec --user sagasu sagasu-preview sagasu-session-exec human pause
   ```

2. Stop browsing. Do not click the challenge, attempt to solve it, ask for
   credentials or codes, inspect hidden page data, or continue in another tab.
3. Send the user a message such as:

   > I reached a login/CAPTCHA in the Sagasu preview browser and paused agent
   > input. Please complete it in the existing preview browser and tell me
   > when it is ready.

4. Do not take screenshots, capture DOM, or resume work until the user
   explicitly confirms completion.
5. After confirmation, restore agent input:

   ```bash
   docker exec --user sagasu sagasu-preview sagasu-session-exec human resume
   ```

6. Capture and visually inspect a fresh screenshot, then continue the normal
   loop. Capture the DOM only if the resumed page requires detailed extraction.
