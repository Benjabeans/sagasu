# BUGS

Findings from a full-codebase review (2026-07-31). 27 verified candidate issues were consolidated into the first 10 distinct defects below; a later action-sequence review added issues 11–15. All 15 defects are now fixed and covered by regression tests. Verdicts: **FIXED** = corrected with a passing regression test; **CONFIRMED** = reproduced or proven from code; **PLAUSIBLE** = mechanism verified in code, trigger depends on environment.

---

## 1. Click coordinates shift down when bottom browser chrome is present — FIXED

**Where:** `src/sagasu/cdp/coordinates.py` (`convert_viewport_to_screen`)

The viewport origin is anchored to the very bottom of the browser window, ignoring bottom browser chrome such as a horizontal scrollbar, so the computed Y origin is too low whenever anything sits below the page viewport.

**Failure scenario:** A page is wide enough to show a horizontal scrollbar (~15 px tall at the bottom of the content area). `cssVisualViewport.clientHeight` excludes the scrollbar, so `origin_y = (window.top + window.height - viewport_height*zoom)` places the viewport top ~15 px lower than reality. Every `sagasu session locate` result is shifted ~15 px down, and the follow-up `cursor click` at the returned screen coordinates lands below the element — hitting the scrollbar or the wrong element (same for a download shelf or any bottom chrome).

**Fix:** Coordinate conversion now uses CSSOM inner-window geometry, whose height includes scrollbar space, to calculate a stable page origin. Covered by `test_bug_1_bottom_chrome_does_not_shift_viewport_origin` and `test_horizontal_scrollbar_does_not_shift_screen_coordinates`.

## 2. `__sagasu_explicit_container__` sentinel leaks into positional arguments — FIXED

**Where:** `src/sagasu/cli/main.py` (`ProtocolArgumentParser.parse_args`)

The internal placeholder `__sagasu_explicit_container__` inserted for the `--container` spelling is only erased when it lands in `session_target`; when the subcommand's own positional is omitted, argparse assigns the placeholder to that positional and it is used as a real value.

**Failure scenario:** A user runs `sagasu session insert-text --container sagasu-preview` and forgets TEXT. `parse_args` inserts the placeholder at index 4; argparse gives `session_target=None` and `text='__sagasu_explicit_container__'` (verified by running `build_parser().parse_args`). Instead of a usage error, Sagasu docker-execs insert-text and types the literal string `__sagasu_explicit_container__` into whatever page element is focused — corrupting a live web form. The navigate/locate variants similarly turn a missing-argument mistake into a confusing invalid-URL / element-not-found error.

**Fix:** Post-parse validation now permits the sentinel only in `session_target` and raises a structured usage error if argparse assigns it to an action operand. Covered for locate, navigate, and insert-text by `test_explicit_container_does_not_fill_missing_action_operand`.

## 3. Missing `--` separator when forwarding text to the in-container executor — FIXED

**Where:** `src/sagasu/cli/session.py:58` (same pattern for `locate` at line 52)

User text is forwarded to the in-container executor argv without a `--` separator, so dash-prefixed text is parsed as options by the executor's argparse instead of as the TEXT positional.

**Failure scenario:** A user runs `sagasu session insert-text SESSION -- "-hello"` (the host parser accepts it via `--`). The host builds `["insert-text", "-hello"]` and docker-execs `sagasu-session-exec insert-text -hello`; the executor's argparse treats `-hello` as an option cluster starting with `-h`, prints its help text to stdout and exits 0 (verified: SystemExit 0). The host then fails with `invalid_response: sagasu-session-exec returned invalid JSON` — or, for other dash text, `invalid_arguments` — so valid text starting with `-` can never be inserted.

**Fix:** Host-side forwarding now inserts an option terminator before each opaque CDP action operand, so dash-prefixed selectors, URLs, and text remain positional values in the executor. Covered by `test_bug_3_dash_prefixed_text_is_separated_from_executor_options` and `test_runtime_arguments_expose_supplemental_cdp_actions`.

## 4. Executor's argparse `-h/--help` action corrupts the JSON stdout protocol — FIXED

**Where:** `src/sagasu/cli/session_executor.py` (`ProtocolArgumentParser`)

The private `ProtocolArgumentParser` keeps argparse's default `-h/--help` action enabled, which prints human-readable help to stdout and raises `SystemExit(0)` — bypassing the overridden `error()` and the JSON-only stdout protocol that `DockerCLI.exec_json` relies on.

**Failure scenario:** Verified: `sagasu-session-exec insert-text -hello` (or any stray argument argparse can prefix-match to `-h`) exits 0 with plain help text on stdout. On the host, `exec_json` sees returncode 0, tries `parse_json_object` on the help text, and raises `invalid_response: sagasu-session-exec returned invalid JSON` — the agent gets a misleading transport-corruption error (and a zero exit from the container) instead of a structured `invalid_arguments` error for a bad-input condition.

**Fix:** The private executor disables argparse's default help action for the root parser and every nested subparser. Invalid or help-like input now reaches the protocol-aware error handler, which emits one JSON error on stderr and returns status 2. Covered by `test_bug_4_executor_parse_failures_remain_json_only`.

## 5. `validate_html` rejects legitimate non-HTML documents (SVG/XML) — FIXED

**Where:** `src/sagasu/artifacts/html.py:28`

`validate_html` rejects any captured DOM that lacks an `<html` tag, so DOM capture of legitimately non-HTML documents (SVG or XML pages) always fails after the capture already succeeded.

**Failure scenario:** The active tab shows a standalone SVG or XML document (e.g. the browser navigated to a raw `.svg` URL). `DOM.getOuterHTML` returns `<svg ...>...</svg>` with no `<html>` element; `sagasu session dom SESSION --out page.html` streams the full document, then `validate_html` raises `dom_failed: missing HTML document element`, the temp file is deleted, and the user gets an error with no output file even though the DOM was captured correctly.

**Fix:** DOM validation retains the tolerant HTML check and accepts other well-formed XML document roots, while still rejecting malformed text, invalid UTF-8, empty documents, and oversized captures.

## 6. `os.link` publication fails on filesystems without hard-link support — FIXED

**Where:** `src/sagasu/artifacts/atomic.py:121`

The no-overwrite publish path relies on `os.link` for atomic no-clobber publication, so saving a screenshot or DOM without `--overwrite` fails on destination filesystems that do not support hard links even when the destination does not exist.

**Failure scenario:** `sagasu session screenshot SESSION --out /mnt/share/shot.png` where the destination is CIFS/SMB, exFAT/VFAT, or a FUSE mount without hardlink support streams and validates the PNG successfully, then `os.link` raises EPERM/ENOTSUP and the command fails with `output_failed: The screenshot could not be published` and deletes the temp file — the capture is lost and the error message gives no hint that `--overwrite` (which uses `os.replace` instead) would have succeeded.

**Fix:** The atomic publisher retains the hard-link fast path and falls back to an exclusive destination reservation plus atomic replacement when hard links are unsupported. Concurrent publishers preserve no-clobber semantics, and failure cleanup removes only the reservation owned by that publisher.

## 7. Container-side `exit_status` is lost when errors cross the exec boundary — FIXED

**Where:** `src/sagasu/protocol.py:47`

`SagasuError.from_payload` reconstructs container-side errors without their `exit_status` (`as_dict` never serializes it and `from_payload` defaults to 1), so usage errors validated inside the container exit 1 from the host CLI while identical host-validated usage errors exit 2.

**Failure scenario:** `sagasu session cursor click SESSION 5000 5000` (coordinate outside the display) is rejected in-container by `validate_coordinate` with `exit_status=2`, but the host round-trips it through `as_dict`/`from_payload` and `sagasu` exits 1 — whereas `sagasu session cursor click SESSION 100 200 --count 0` (validated host-side) exits 2. A wrapper script that treats exit 2 as permanent usage error and exit 1 as retryable failure will retry a permanently invalid click forever.

**Fix:** Non-default exit statuses are serialized inside the error object and validated as non-boolean integers from 1–255 when decoded. Existing status-1 payloads retain their original wire shape, and nested action-sequence failures preserve their status too.

## 8. `_viewport_metrics` hard-requires the optional CDP `zoom` field — FIXED

**Where:** `src/sagasu/cdp/locate.py:210`

`_viewport_metrics` hard-requires the `zoom` field of `Page.getLayoutMetrics`' `cssVisualViewport`, but `zoom` is an optional field in the CDP `VisualViewport` type.

**Failure scenario:** On a Chromium-family build whose `Page.getLayoutMetrics` omits the optional `zoom` key from `cssVisualViewport` (it is marked optional/experimental in the protocol), `_positive_number(raw, "zoom", ...)` raises `invalid_response: CDP returned an invalid viewport zoom`, making every `sagasu session locate` call fail even though the element is present and visible.

**Fix:** An omitted zoom now defaults to `1.0`; malformed, non-finite, zero, or negative supplied values remain rejected.

## 9. Strict UTF-8 decode of `docker container ls` output can escape error handling — FIXED

**Where:** `src/sagasu/sessions/docker.py:86`

`containers_for_session` decodes `docker container ls` output with `errors="strict"`, and a `UnicodeDecodeError` escapes the `SagasuError` handling as an unstructured `internal_error`.

**Failure scenario:** If the docker CLI emits any non-UTF-8 byte in its `{{json .}}` container listing (e.g. a locale-mangled label or name from an older engine), `completed.stdout.decode("utf-8", errors="strict")` raises `UnicodeDecodeError`, which is not caught by the surrounding `invalid_response` handling; the CLI falls through to the generic `internal_error: Sagasu failed unexpectedly` path instead of the structured `invalid_response` error every other malformed-docker-output branch produces.

**Fix:** Strict decoding is retained, but `UnicodeDecodeError` is translated into `invalid_response` with safe encoding, byte-offset, and decoder-reason details.

## 10. Heavy backend imports run inside the exclusive session lock — FIXED

**Where:** `src/sagasu/cli/session_executor.py:431` (lock taken at line 420; also lines 448, 466, 485)

`create_backend` (which imports pyautogui/humancursor at `HumanCursorBackend.__init__`) runs inside the exclusive session lock.

**Failure scenario:** The heavy imports (PIL, python-xlib, X server connect) execute after `session_lock(exclusive=True)` is taken, extending the exclusive window by the import time on every cursor mutation; a concurrent `sagasu session screenshot` or `display` (shared lock) fails with `session_busy` during that entire window even though nothing has touched the display yet. Constructing the backend (and thereby validating the `--backend` value) before acquiring the lock shrinks the critical section.

**Fix:** Individual actions and queued sequences prepare each unique cursor backend before taking the exclusive lock. Human-control state is checked before preparation and rechecked under the lock before any input, while coordinate validation remains immediately before the mutation.

## 11. Final observation failure loses completed action status — FIXED

**Where:** `src/sagasu/cli/session_executor.py` (`_execute_sequence`)

If final screenshot capture or pointer observation failed after actions ran, the error omitted authoritative mutation state, making a retry liable to repeat clicks, text insertion, or navigation.

**Fix:** Post-mutation observation errors now include a host-validated `sequence_state` with action counts/results, partial-action failure data, display and settle state, and explicit screenshot/pointer observation flags. The host adds authoritative session and container IDs before surfacing it.

## 12. Sequence output can become unpublishable after browser mutations — FIXED

**Where:** `src/sagasu/artifacts/atomic.py` (`publish_reserved_stream`)

The old flow checked the destination and then ran browser mutations before winning the no-overwrite publication race. A concurrent destination or unsupported final publication could therefore leave changed browser state without a published screenshot.

**Fix:** No-overwrite sequence output is now reserved with `O_EXCL`, with its descriptor and inode retained, before the temporary screenshot is created or Docker is invoked. Publication replaces only the still-owned reservation; foreign replacements are preserved and cleanup is identity-aware.

## 13. Full action sequence transported in one Docker argv element — FIXED

**Where:** `src/sagasu/cli/session.py`, `src/sagasu/sessions/docker.py`, and `src/sagasu/cli/session_executor.py`

Two individually valid 64 KiB text insertions produced an encoded argument larger than Linux's per-argument limit, causing `E2BIG` before the container executor could validate or run it.

**Fix:** The host validates and canonicalizes the sequence, then sends it through Docker stdin. The executor reads an EOF-delimited UTF-8 document with a 64 MiB cap and performs the same authoritative parse, action-limit, and field validation. Screenshot bytes remain on stdout and metadata remains on stderr.

## 14. Pointer observation failure falsely reports an applied mutation as failed — FIXED

**Where:** `src/sagasu/cli/action_sequence.py` (`run_action_sequence`)

Pointer querying was inside the action's mutation try block, so a transient `xdotool getmouselocation` failure after a successful mutation omitted that action from `actions_completed` and stopped the queue.

**Fix:** Mutation success is recorded before supplemental pointer observation. A failed pointer read is represented explicitly by `pointer: null` plus structured `pointer_observation` error metadata and does not prevent later queued actions from running.

## 15. Lone Unicode surrogates escape action validation — FIXED

**Where:** `src/sagasu/cdp/insert_text.py` and `src/sagasu/cdp/navigate.py`

Python's JSON decoder accepts escaped lone surrogates, but UTF-8 encoding them raised `UnicodeEncodeError`, producing `internal_error` rather than a structured usage error.

**Fix:** Text insertion and navigation validators translate non-scalar Unicode into `invalid_arguments` with exit status 2 for standalone commands, queued actions, and the public CLI.
