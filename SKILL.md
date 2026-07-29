---
name: sagasu
description: Check, start when needed, and browse websites through the existing Sagasu preview browser using full-display screenshots, X-level mouse input, supplemental DOM inspection, direct CDP navigation, and focused CDP text insertion. Use for web navigation or browser interaction tasks that should reuse the current sagasu-preview profile and pause for the user at any login or CAPTCHA.
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

## Follow the interaction loop

1. Capture and inspect the full display:

   ```bash
   PYTHONPATH=src python3 -m sagasu.cli.main session screenshot --container sagasu-preview --out /tmp/sagasu-preview.png --overwrite
   ```

2. Capture the live DOM as supplemental context:

   ```bash
   PYTHONPATH=src python3 -m sagasu.cli.main session dom --container sagasu-preview --out /tmp/sagasu-preview.html --overwrite
   ```

3. Reconcile both views. Use the screenshot for visible state and coordinates.
   Use the DOM to confirm labels, text, links, and page state. Do not treat DOM
   presence as proof that an element is visible or safe to click.

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

5. Take one small X-level action. Clicking uses HumanCursor by default:

   ```bash
   PYTHONPATH=src python3 -m sagasu.cli.main session cursor --container sagasu-preview click X Y
   PYTHONPATH=src python3 -m sagasu.cli.main session cursor --container sagasu-preview scroll X Y --steps -4
   PYTHONPATH=src python3 -m sagasu.cli.main session cursor --container sagasu-preview drag X1 Y1 X2 Y2
   ```

6. Capture the screenshot and DOM again before deciding what to do next.

Use atomic actions with explicit coordinates. Do not use `--current`, and do
not select the `xdotool` mouse backend unless the user specifically requests
debugging of that fallback.

Use supplemental CDP navigation when the destination URL is known:

```bash
PYTHONPATH=src python3 -m sagasu.cli.main session navigate --container sagasu-preview 'https://example.com'
```

This accepts only absolute HTTP(S) URLs and returns when Chromium accepts the
navigation, not when loading finishes. Capture a fresh screenshot and DOM
before acting.

Click a normal text field through X before inserting text into it. Use CDP text
insertion, particularly for Unicode that the X keyboard map cannot represent:

```bash
PYTHONPATH=src python3 -m sagasu.cli.main session insert-text --container sagasu-preview 'search text'
```

CDP inserts into the element that already has focus; it does not choose or
focus a field. Capture a fresh screenshot and DOM to verify the result.
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

6. Capture a fresh screenshot and DOM, then continue the normal loop.
