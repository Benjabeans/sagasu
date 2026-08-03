# BUGS

Findings from a full-codebase review (2026-07-31). 27 verified candidate issues were consolidated into the 10 distinct defects below, ranked most severe first. Verdicts: **FIXED** = corrected with a passing regression test; **CONFIRMED** = reproduced or proven from code; **PLAUSIBLE** = mechanism verified in code, trigger depends on environment.

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

## 5. `validate_html` rejects legitimate non-HTML documents (SVG/XML) — CONFIRMED

**Where:** `src/sagasu/artifacts/html.py:28`

`validate_html` rejects any captured DOM that lacks an `<html` tag, so DOM capture of legitimately non-HTML documents (SVG or XML pages) always fails after the capture already succeeded.

**Failure scenario:** The active tab shows a standalone SVG or XML document (e.g. the browser navigated to a raw `.svg` URL). `DOM.getOuterHTML` returns `<svg ...>...</svg>` with no `<html>` element; `sagasu session dom SESSION --out page.html` streams the full document, then `validate_html` raises `dom_failed: missing HTML document element`, the temp file is deleted, and the user gets an error with no output file even though the DOM was captured correctly.

## 6. `os.link` publication fails on filesystems without hard-link support — CONFIRMED

**Where:** `src/sagasu/artifacts/atomic.py:121`

The no-overwrite publish path relies on `os.link` for atomic no-clobber publication, so saving a screenshot or DOM without `--overwrite` fails on destination filesystems that do not support hard links even when the destination does not exist.

**Failure scenario:** `sagasu session screenshot SESSION --out /mnt/share/shot.png` where the destination is CIFS/SMB, exFAT/VFAT, or a FUSE mount without hardlink support streams and validates the PNG successfully, then `os.link` raises EPERM/ENOTSUP and the command fails with `output_failed: The screenshot could not be published` and deletes the temp file — the capture is lost and the error message gives no hint that `--overwrite` (which uses `os.replace` instead) would have succeeded.

## 7. Container-side `exit_status` is lost when errors cross the exec boundary — CONFIRMED

**Where:** `src/sagasu/protocol.py:47`

`SagasuError.from_payload` reconstructs container-side errors without their `exit_status` (`as_dict` never serializes it and `from_payload` defaults to 1), so usage errors validated inside the container exit 1 from the host CLI while identical host-validated usage errors exit 2.

**Failure scenario:** `sagasu session cursor click SESSION 5000 5000` (coordinate outside the display) is rejected in-container by `validate_coordinate` with `exit_status=2`, but the host round-trips it through `as_dict`/`from_payload` and `sagasu` exits 1 — whereas `sagasu session cursor click SESSION 100 200 --count 0` (validated host-side) exits 2. A wrapper script that treats exit 2 as permanent usage error and exit 1 as retryable failure will retry a permanently invalid click forever.

## 8. `_viewport_metrics` hard-requires the optional CDP `zoom` field — PLAUSIBLE

**Where:** `src/sagasu/cdp/locate.py:210`

`_viewport_metrics` hard-requires the `zoom` field of `Page.getLayoutMetrics`' `cssVisualViewport`, but `zoom` is an optional field in the CDP `VisualViewport` type.

**Failure scenario:** On a Chromium-family build whose `Page.getLayoutMetrics` omits the optional `zoom` key from `cssVisualViewport` (it is marked optional/experimental in the protocol), `_positive_number(raw, "zoom", ...)` raises `invalid_response: CDP returned an invalid viewport zoom`, making every `sagasu session locate` call fail even though the element is present and visible.

## 9. Strict UTF-8 decode of `docker container ls` output can escape error handling — PLAUSIBLE

**Where:** `src/sagasu/sessions/docker.py:86`

`containers_for_session` decodes `docker container ls` output with `errors="strict"`, and a `UnicodeDecodeError` escapes the `SagasuError` handling as an unstructured `internal_error`.

**Failure scenario:** If the docker CLI emits any non-UTF-8 byte in its `{{json .}}` container listing (e.g. a locale-mangled label or name from an older engine), `completed.stdout.decode("utf-8", errors="strict")` raises `UnicodeDecodeError`, which is not caught by the surrounding `invalid_response` handling; the CLI falls through to the generic `internal_error: Sagasu failed unexpectedly` path instead of the structured `invalid_response` error every other malformed-docker-output branch produces.

## 10. Heavy backend imports run inside the exclusive session lock — CONFIRMED (cleanup)

**Where:** `src/sagasu/cli/session_executor.py:431` (lock taken at line 420; also lines 448, 466, 485)

`create_backend` (which imports pyautogui/humancursor at `HumanCursorBackend.__init__`) runs inside the exclusive session lock.

**Failure scenario:** The heavy imports (PIL, python-xlib, X server connect) execute after `session_lock(exclusive=True)` is taken, extending the exclusive window by the import time on every cursor mutation; a concurrent `sagasu session screenshot` or `display` (shared lock) fails with `session_busy` during that entire window even though nothing has touched the display yet. Constructing the backend (and thereby validating the `--backend` value) before acquiring the lock shrinks the critical section.
