# Problems.md — implementation challenges

The [README's problem section](./README.md#the-problem) states the problems Sagasu solves for its users. This document is the other side: the problems *building* Sagasu has to work through. Living document — sections get added as challenges surface and updated as they're resolved.

## The re-challenge loop

The README promises that persistent profiles and solve-in-place handoff tame the "CAPTCHA comes back" treadmill. Delivering that means designing around how modern anti-bot systems (Cloudflare, DataDome, GeeTest, reCAPTCHA v3) actually score sessions — several independent layers, where passing one and failing another still means a challenge. The situations that produce the loop:

1. **Datacenter IP reputation.** IP reputation is scored before the browser even renders — a datacenter ASN gets challenged on nearly every fresh session no matter how the browser looks. The human solves; the next session from the same ASN is challenged again.
2. **Throwaway profiles.** A cookie-less, history-less browser is the profile of a first-time anonymous visitor — exactly what challenge systems are tuned to gate. Discarding the profile after a solve throws away the one signal ("returning visitor") that would have prevented the next challenge.
3. **Bound clearance tokens.** Passing a challenge issues a token (e.g. Cloudflare's `cf_clearance`) bound to the IP and browser fingerprint that earned it. If the human solves in a *different* context — their own device, a different egress IP — the token never attaches to the agent's session. "Just log in on your machine and export the cookie" fails here.
4. **Headless tells.** Truly headless browser modes leak signals (headless UA variants, `navigator.webdriver`, missing GPU/codec surfaces) that keep the session's score bad after the solve, so continuous re-scoring brings the challenge back.
5. **Post-handoff behavior.** The human solves, hands back, and the agent immediately navigates at machine speed with zero mouse movement. Behavioral scoring runs continuously — a session that stops behaving like the human who solved it can be re-challenged mid-flow.
6. **Parallel velocity.** Many concurrent sessions from one IP trip rate heuristics, and the whole fleet starts getting challenged — flooding the queue.

Design consequences (all within the handoff-not-bypass line — no spoofing, no solvers):

- **Solve in place** — the shared-browser model already guarantees the solve happens in the agent's browser over the agent's IP (answers 3).
- **Persistent profiles are a CAPTCHA-reduction mechanism**, not just login convenience (answers 2).
- **Headful rendering** on a real virtual display comes free with the noVNC architecture (answers 4).
- **Residential self-hosting recommendation** — the same stack on a VPS gets challenged an order of magnitude more often (answers 1); state this in the docs.
- **Surface repeat-challenge sessions in the panel** instead of silently re-queuing them (answers 5, 6) — a session challenged over and over is a site saying no, and the human should decide to stop.

## Getting the security defaults right

Sagasu containers carry live authenticated sessions, so a leaked port is account takeover, not a privacy leak. The defaults must be safe without the operator reading a hardening guide — and this is exactly where comparable projects go wrong (viewers bound to `0.0.0.0`, URLs as the only credential, CDP reachable off-host, cookies in world-readable volumes). Concretely to enforce:

- VNC and CDP bind to loopback/container networks only, in every configuration, with no flag to expose them.
- The panel binds to the configured boundary (LAN default) and always carries its own access credential.
- Profile volumes are created with tight permissions, documented as sensitive, easy to enumerate and destroy (`sagasu` should make deleting a profile first-class).
- `setup` verifies the binding it just configured (can the panel be reached only where intended?) rather than assuming.

## Resume signaling

"Mark it done and the owning agent resumes" needs an actual state machine: `requested → claimed → resolved` (plus `abandoned`/`expired`). Open questions:

- Does `sagasu handoff request` block until resolution or return immediately for polling? Blocking is simpler for agents; polling survives agent restarts. Probably both (`--wait`).
- What determines "resolved" — only the human clicking Done in the panel, or auto-detection (challenge iframe gone)? Human-click is unambiguous and keeps the human accountable; start there.
- Timeouts: a request nobody services shouldn't block an agent forever. Expiry with a status the agent can read and act on.

## Sharing one browser between agent and human

- **Profile locking.** A Chromium profile directory can only be open in one browser instance at a time. Two agents requesting sessions on the same named profile must either queue, share the running instance (separate tabs — with which isolation guarantees?), or fail loudly. Needs an explicit policy in the CLI.
- **Agent harness attach limitations.** Some agent frameworks' built-in browser tools can't attach to an external CDP endpoint and insist on launching their own browser. The skill must be explicit that the agent drives Sagasu's browser via raw CDP (or a CDP-capable library), not via its harness's browser tool.
- **Input collision.** Agent input landing while the human is mid-drag on a slider captcha would corrupt the very trajectory being scored — and with the X-level interaction model the agent and the human share the literal cursor, so this is physical, not just a protocol quirk. Handoff must imply the agent freezes all driving until resolution — a hard rule in skill.md at minimum, enforcement (queue state gates input?) if it proves necessary. See "One cursor per display" below.

## Blocker detection is iframe-blind in the DOM

Observed in a live Taobao run: an image-select captcha covered the entire results page while `document.body.innerText` in the main frame reported no captcha at all — the overlay's content lives in a child iframe (cross-origin ones are separate CDP targets entirely), which `Runtime.evaluate` in the main frame cannot see into. A DOM-only "am I blocked?" check returns a false negative at exactly the moment it matters. Consequences:

- **The screenshot is the authoritative blocker detector** — vision sees what the main-frame DOM hides. skill.md should teach: when deciding whether the page is blocked, trust the pixels over the DOM.
- DOM-side detection, where used, must enumerate frames (`Page.getFrameTree`, and `Target.getTargets` for out-of-process iframes) rather than querying the main document.
- The same caveat applies to auto-resolve heuristics in [resume signaling](#resume-signaling): "challenge iframe gone" has to be checked at the frame level, not against the main document.

## One cursor per display — the interaction and concurrency model

The agent's primary interface is computer-use style: full-display screenshots (browser chrome included) and OS-level X input, with the DOM as an information supplement and CDP verbs for navigation and machine operations. Cheap to build — `xdotool` + `scrot` add under 1MB to the image — but the choice has consequences that shape the session model:

- **X input is a shared resource.** A display has one cursor and one keyboard focus. Within a container, exactly one actor can drive at a time — agent, human, or another agent. Parallelism therefore lives at the container level (one task per container, each with its own display and cursor), never inside a session.
- **Two coordinate spaces.** Display coordinates (X screenshots, X input) and viewport coordinates (CDP screenshots, CDP input, DOM boxes) are each internally 1:1 — the container runs at `devicePixelRatio` 1 — but differ by a fixed offset: window position plus the browser-chrome band (`window.screenX/screenY`, `outerHeight − innerHeight`). Mixing a screenshot from one space with a click in the other silently misclicks. The skill must keep the pairings straight and supply the offset for the best targeting pattern: locate the element in the DOM, translate its box to display coordinates, click it with real X input.
- **CJK text entry.** `xdotool type` is keymap-bound and unreliable for CJK — a real gap given the Chinese-web goal. Rule: establish focus with an X click, then insert text via CDP `Input.insertText` (verified working with 中文 in the built image).
- **Focus follows input.** Keyboard events go to the X-focused window, and clicks establish focus — the agent must click before typing, like a human does.

### The shared-login multi-window mode (assessed, deferred)

For subagents that need the *same logged-in site simultaneously*, a second session mode was verified empirically in the built image: one container, one browser, N windows (`Target.createTarget` with `newWindow: true`), each window driven by its own CDP session over the single CDP endpoint. Findings from the live experiment:

- **Live login sharing works.** A cookie set in one window is instantly visible in every other — one instance, one jar. This also sidesteps the profile-lock problem by funneling concurrency into one browser, and avoids sites that invalidate concurrent logins to the same account.
- **Occlusion is the trap.** A fully covered window goes `visibilityState: hidden`, stops painting (stale screenshots), and drops CDP mouse events. Three flags fix all of it — verified: fresh screenshots and `isTrusted` clicks landed in a fully occluded window — `--disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling` (they ride the image's `BROWSER_EXTRA_ARGS`).
- **Browser-internal focus flips.** Driving window A makes other windows see `document.hasFocus() === false` even though X focus is untouched. Harmless between agents; one more reason the handoff freeze rule is fleet-wide within a session.
- **"One window per noVNC view" is possible without multiple displays** (a browser instance cannot span X displays): run one large display, tile windows into fixed slots (`Browser.setWindowBounds` is pixel-exact), and export each slot as its own view with `x11vnc -clip` behind its own websockify path — spawned lazily only while a handoff on that window is active, so steady-state cost stays zero. Keyboard focus remains global: one human at a time per session container.
- **Why it stays opt-in:** one crash domain and one identity for N tasks — per-*account* velocity becomes the limiting anti-bot factor — and input must be CDP-scoped per window, giving up the X-level interaction model, because N cursors don't exist on one display.

## Human input fidelity through VNC

Challenges that score mouse trajectory (GeeTest sliders) meet an input path that is VNC-relayed: throttled frame rates and coarse pointer event timing could make a real human's drag look synthetic. Risks and options:

- Measure first: does a human solving GeeTest through noVNC on a LAN actually fail trajectory checks? LAN latency may be low enough that this is a non-issue.
- If it is an issue: noVNC/websockify tuning (frame rate, compression) before architecture changes.
- Longer-term option: a WebRTC-based viewer (neko-style) for the panel's embedded view — much smoother input and audio support, at the cost of a significantly more complex stack.

## Panel plumbing

Embedding per-session noVNC views in one dashboard means the panel proxies or routes to each session's websockify endpoint: one origin, per-session paths, panel auth passing through to the VNC layer (the human shouldn't juggle per-session VNC passwords — that's the sprawl problem again). Session lifecycle events (started, handoff requested, resolved, stopped) need to reach the panel — polling the CLI's state vs. a small event stream.

## Browser packaging

Helium as the default browser means packaging a niche Chromium fork in the image: keeping up with its releases, verifying its CDP surface matches stock Chromium (the CLI and agents assume it), multi-arch builds (amd64/arm64), and a tested fallback path to stock Chromium when Helium breaks or lags upstream security fixes.
