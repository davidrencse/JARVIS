# Cross-machine file drag: design spike

## Decision and evidence conventions
- VERIFIED: Scope is Arch/Hyprland main ↔ Windows 11 Dell; this spike may create only this document (owner brief).
- VERIFIED: Inspected JARVIS HEAD `6233803c457982474764c4e69f16f245a6a7966b`; working tree was clean before this document.
- VERIFIED: `LM/` below means `/home/jdoe/src/lan-mouse-jarvis/`; Dell twin was not accessed (owner brief).
- VERIFIED means inspected code or an API contract, never an assertion that an unbuilt integration works.
- ASSUMED means a proposed design, bound, inference, or behavior requiring an experiment; this includes every implementation slice.
- ASSUMED: Verdict main→node: conditionally feasible; Linux source grab termination is the first blocking experiment.
- ASSUMED: Verdict node→main: conditionally feasible; Windows OLE interception and Linux target serial acquisition are blocking experiments.
- ASSUMED: Build two local native drags linked by one transfer, rather than pretending to continue one OS session.
- ASSUMED: Priority order is source-file safety, reuse, then seamless interaction; enable neither direction until its native gates pass.
- ASSUMED: MVP copies one regular local file, including a photo file; no directories, virtual/cloud-only objects, or image-only offers.
- ASSUMED: No move semantics: leave the original and staging copy intact; the destination file manager performs COPY.
- ASSUMED: Use Lan Mouse for crossing/held-input authority, JARVIS for transfer coordination, OS adapters for native DnD only.
- ASSUMED: Rejected: Python edge detector, clipboard polling as drag detection, new network transport, destination-path RPC, and compositor patch as an implicit dependency.
- VERIFIED: Existing console/send behavior remains useful fallback; transfer docstring's impossibility claim describes the current implementation (`jarvis/transfer.py:1–15`).
- ASSUMED: Update that docstring only after implementation passes both direction tests; it should describe bridging, not shared drag memory.
- ASSUMED: System-design methodology is narrowed to this single requested artifact; no extra reports, JSON, ADR, or implementation files.

## Existing pieces and boundaries
- VERIFIED: `push()` uses sequential chunk calls and returns the final receiver result (`jarvis/transfer.py:374–386`).
- VERIFIED: `call()` dispatches main→node or node→main `/v1/control` using existing cluster transport (`jarvis/transfer.py:361–371`).
- VERIFIED: Node capability `files` permits `transfer` to main, not arbitrary main actions (`jarvis/cluster.py:856–866`).
- VERIFIED: Validation/execution already delegate transfer jobs to this module (`jarvis/cluster.py:351–353,373–375`).
- VERIFIED: Receiver uses fixed home drop folder, offset checks, limits, collision names, then final publication (`jarvis/transfer.py:184–187,242–324`).
- VERIFIED: `_land` uses link/unlink or checked rename fallback, not a general destination writer (`jarvis/transfer.py:242–260`).
- VERIFIED: Publication is not an fsync durability guarantee, and the no-hardlink fallback has a check/rename race (`jarvis/transfer.py:247–259,309–324`).
- VERIFIED: Clipboard already calls `push`; reuse its transport pattern, not another clipboard implementation (`jarvis/clipsync.py:531–577`).
- VERIFIED: `sharedinput` reads config/process/monitor state; it does not currently subscribe to crossings (`jarvis/sharedinput.py:1–20,64–79,111–137`).
- VERIFIED: Lan Mouse IPC is JSON-lines; Unix socket is `$XDG_RUNTIME_DIR/lan-mouse-socket.sock` (`LM/lan-mouse-ipc/src/connect.rs:29–44`; `src/lib.rs:287–291`).
- VERIFIED: Windows IPC already uses `127.0.0.1:5252`; no additional port is needed (`LM/lan-mouse-ipc/src/listen.rs:73–81`).
- VERIFIED: Incoming `DeviceEntered` exists; outgoing `ClientEntered` currently runs a hook command (`LM/lan-mouse-ipc/src/lib.rs:211–215`; `LM/src/service.rs:342–345`).
- ASSUMED: Extend this IPC in `sharedinput.py`; do not shell out an enter-hook per drag or poll desktop geometry.
- ASSUMED: Attach the coordinator to existing hub/node lifecycles; Windows OLE runs on an STA thread inside the existing interactive node process.
- VERIFIED: Existing hub/node already own threads and shutdown points (`jarvis/cluster.py:663–703,1182,1228–1231,1266–1274`).

## Native feasibility: source on Hyprland
- VERIFIED: Existing edge is a one-pixel overlay surface with a full input region (`LM/input-capture/src/layer_shell.rs:167–169,186–208`).
- VERIFIED: Current Begin requires `wl_pointer.enter`, immediately grabs/locks, and discards button serials (`LM/input-capture/src/layer_shell.rs:739–795`).
- VERIFIED: Upstream Hyprland clears ordinary pointer focus for DnD and routes offers via a separate DnD focus ([H1], `DataDevice.cpp:602–625`).
- VERIFIED: Its hit testing permits refocus during DnD despite held buttons ([H2], `InputManager.cpp:356–359`).
- ASSUMED: Thus the pointer can hit the overlay geometrically, but the existing ordinary pointer-enter path cannot be relied upon to cross.
- VERIFIED: A seat data device supplies DnD `enter`; its offer can be read through `receive`; `start_drag` requires a matching implicit-grab serial ([W1]).
- ASSUMED: Bind the manager/device on the SAME connection/seat as the layer surfaces and handle data-device enter/motion as an alternative Begin producer.
- ASSUMED: Accept/read `text/uri-list` into a bounded pipe before drop, asynchronously; do not block Wayland dispatch or call `finish` early.
- ASSUMED: An active DnD must not execute the current pointer-lock path until the original drag has ended.
- ASSUMED: Installed Hyprland, file manager, and XWayland interoperability remain unverified; upstream main is evidence, not the installed version.
- ASSUMED: First live probe must log data-enter, offer, pre-drop receive, action, local termination, fresh pointer enter, and physical release separately.

## Native feasibility: source on Windows
- VERIFIED: Low-level hooks run when mouse events are about to enter a thread queue; the installer must pump messages and return promptly ([M1]).
- VERIFIED: `DoDragDrop` tracks pointer/button changes and calls the target interface ([M2]); its contract does not disable low-level hooks.
- ASSUMED: Hooks should keep firing during Explorer OLE drag; exact Dell behavior and third-party hook interference require measurement.
- VERIFIED: Current hook captures/parks at crossing before forwarding movement (`LM/input-capture/src/windows/event_thread.rs:408–429`).
- ASSUMED: While left-held at the edge, defer this capture briefly so OLE can hit an actual topmost, nonactivating, narrow target HWND.
- VERIFIED: RegisterDragDrop registers HWND/IDropTarget; OleInitialize and message pumping are required ([M3]).
- VERIFIED: DragEnter supplies IDataObject; CF_HDROP provides existing paths via DROPFILES/DragQueryFile, not clipboard polling ([M4], [M5]).
- ASSUMED: Implement OLE only once in `jarvis/dragdrop_windows.py`, on the node's medium-integrity STA; the Rust hook supplies edge/input authority.
- ASSUMED: Strip geometry comes from Lan Mouse's configured active edge; strip callbacks feed the same state machine, not a second crossing detector.
- ASSUMED: GetData(CF_HDROP, TYMED_HGLOBAL), copy bounded UTF-16 paths, ReleaseStgMedium, return immediately; file reads happen off the STA.
- ASSUMED: Hook delays capture at most 150 ms; absent a valid OLE offer, resume ordinary crossing; test window drags and edge snapping for regressions.
- VERIFIED: Windows Lan Mouse is elevated through protected scheduled-task binaries (`CLAUDE.md:151–156`).
- ASSUMED: Do not host the strip in that elevated process: ordinary Explorer→elevated OLE interoperability is unsafe to assume.
- ASSUMED: Verify the node is interactive and medium integrity; if not, disable Windows DnD rather than introduce another daemon or silently relaunch.
- ASSUMED: Synergy/Input Leap are inspiration only; no claims about their implementation were verified or used as a dependency.

## Leaving A without a local move
- VERIFIED: A Wayland target cannot destroy another client's data source; source destruction cancels its own drag ([W1]).
- ASSUMED: After reading/validating the list and opening the file, reject the source offer (accept NULL/actions none), then synthesize button-up while still over the strip.
- ASSUMED: Wait for local drag termination; no `finish` success, MOVE action, source deletion, or local destination copy is reported.
- ASSUMED: The synthetic release uses existing wlroots emulation; whether it terminates a physical-device grab on the installed seat is the BIGGEST risk.
- ASSUMED: If it does not, stock protocol alone has not solved main→node; stop this direction's rollout and report the gate failed.
- ASSUMED: After termination, retain a short mapped capture pad under the cursor, establish pointer capture, and observe the later real release.
- ASSUMED: An internal synthetic down may be needed to acquire that capture; tag it as bookkeeping and never confuse it with physical held state.
- ASSUMED: Windows first extracts paths, then synthesizes source-local up over the rejecting strip; effect remains NONE and OLE must unwind before cursor parking.
- VERIFIED: Source cancellation/drop decisions belong to IDropSource.QueryContinueDrag, not IDropTarget ([M6]).
- ASSUMED: Therefore synthetic release plus NONE is a tested termination strategy, not a claim that DragLeave or ReleaseCapture cancels OLE.
- ASSUMED: If OLE does not unwind safely, fail the Windows source gate; do not inject Escape into an arbitrary focused application.
- ASSUMED: Reuse local emulation API, route these bookkeeping events past local capture but never over peer input; preserve PEER_TAG/WARP_TAG behavior.
- VERIFIED: Windows ignores injected edge moves except PEER_TAG (`LM/input-capture/src/windows/event_thread.rs:275–307`; `LM/input-emulation/src/windows.rs:236–237`).

## Target native drag and input ownership
- ASSUMED: During staging, B receives cursor motion but no left-down/up into arbitrary applications; Lan Mouse holds a logical physical-button lease.
- ASSUMED: Snapshot button-held at crossing: the original down predates capture and cannot be reconstructed from later captured events alone.
- ASSUMED: Windows: when paths exist and hold is current, inject down onto a temporary helper surface, start DoDragDrop with IDataObject CF_HDROP and COPY only.
- VERIFIED: DoDragDrop requires OleInitialize and IDataObject/IDropSource ([M2]); CF_HDROP contains fully qualified local file paths ([M5]).
- ASSUMED: Keep the hook thread independent of the STA modal loop; allow emulated peer moves to reach it; release invokes normal native drop.
- ASSUMED: Helper QueryContinueDrag cancels on lease loss/Escape, drops only on ordered release while armed; never reports MOVE.
- ASSUMED: Hyprland: map a small temporary overlay pad at B's current cursor, await configure/commit and wl_pointer.enter; disable edge recapture for this session.
- ASSUMED: With left logically held but not yet injected, send one wlroots virtual-pointer down onto the pad; capture THAT wl_pointer.button serial on its owning connection.
- ASSUMED: Call start_drag immediately using the pad origin and new COPY-only data source; never reuse A's serial, enter serial, or a synthetic integer.
- VERIFIED: Existing wlroots backend injects buttons with its clock; pointer frame completes the batch (`LM/input-emulation/src/wlroots.rs:221–247`).
- ASSUMED: Leave origin mapped through the drag, shrink/clear its input region after start, and expose only percent-encoded local staged-file URI.
- ASSUMED: If focus/serial fails within 500 ms, cancel, release injected state and notify; never click the underlying folder as a fallback.
- ASSUMED: Freeze motion only during bounded pad acquisition, then replay bounded coalesced motion; a release arriving during arming wins and prevents late start.
- ASSUMED: Both native paths require real file-manager tests; fake compositors prove ordering, not serial validity or inter-device seat behavior.

## Sequence (ASSUMED proposal; applies in both directions)
```text
Owner       A file manager   A Lan Mouse/JARVIS     Existing cluster      B JARVIS/Lan Mouse   B file manager
hold/drag -> native drag
edge ----------------------> offer+physical hold
                            read/open source
                            end A native drag
                            Enter/Ack + Dnd begin ---------------------> logical hold; cursor moves
                            DragUpdate -> push() -> transfer put jobs -> .part -> final drop file
                            <- final receiver result ------------------ landed receipt
                            transfer offer_drag ----------------------> validate receipt+lease
                                                                         pad/down/serial or OLE
                                                                         native COPY drag --------> hover folder
release -------------------> Dnd release (ordered/retried) ------------> native up ---------------> OS copy
                            <---------------------------- done/cancel; release all held state
```

## One local interface, exact proposed message shapes
- VERIFIED: IPC enums use Serde's external tagging (`LM/lan-mouse-ipc/src/lib.rs:180–223`; `src/connect.rs:29–44`).
- ASSUMED: Add one event variant: `{"DragUpdate":{"v":1,"id":"<uuid32>","epoch":7,"seq":1,"phase":"begin","peer":"<fingerprint>","edge":"right","fraction":0.4,"held":true,"paths":["<local absolute file>"]}}`.
- ASSUMED: Same variant phases are `begin|entered|release|cancel|armed|done`; `paths` exists ONLY on authenticated source-coordinator begin events.
- ASSUMED: One request variant: `{"DragControl":{"v":1,"id":"<uuid32>","epoch":7,"op":"offer","receipt":"<uuid32>"}}`.
- ASSUMED: Allowed ops are `capabilities|register_offer|terminate_source|offer|cancel|status`; register_offer includes bounded local `paths` from the native adapter.
- ASSUMED: Capability reply advertises `file_drag_v1`; lifecycle/status replies use DragUpdate; all fields have closed schemas and bounded sizes.
- ASSUMED: Connect/authenticate roles once over existing IPC: `{"DragAuth":{"v":1,"role":"coordinator","token":"<local capability>"}}`; never log token.
- ASSUMED: Protect capability with user-only Unix permissions / Windows owner+SYSTEM ACL; isolate authenticated extension traffic from ordinary frontend broadcasts.
- VERIFIED: Existing listener broadcasts JSON to frontends and accepts loopback TCP; loopback alone is not a source-path authorization boundary (`LM/lan-mouse-ipc/src/listen.rs:73–100`).
- ASSUMED: Add authenticated per-connection replies/private events for extension messages; do not expose paths/tokens to unrelated GUI clients.
- ASSUMED: In existing DTLS protocol add `Dnd{v,id,epoch,seq,phase,button,held}` plus `DndAck{id,epoch,seq}`; phases `begin|release|cancel|done`.
- ASSUMED: `begin` is installed on B before A emits begin to its coordinator; it binds id to the actual authorized peer/session, not a claimed fingerprint.
- ASSUMED: Repeat until ack at 40 ms, dedupe by epoch/seq, tombstone terminal states; renew held lease every 100 ms, expire after 1 s silence.
- ASSUMED: This adds drag lifecycle control only; all file bytes and metadata completion ride existing cluster jobs, no alternate file protocol.
- VERIFIED: Existing Enter retries/Ack buffering and Leave cleanup already occupy this boundary (`LM/src/capture.rs:277–332,628–658,739–790`; `LM/src/emulation.rs:152–172`).
- VERIFIED: Held refresh currently tracks keyboard events only; it does not solve drag-button reliability (`LM/src/capture.rs:226–260`; `LM/src/emulation.rs:344–366,489–495`).
- ASSUMED: Extend that single held-input mechanism to mouse buttons/drag lease; do not create a Python input watchdog or a second timer implementation.
- ASSUMED: Leave, fail-safe, link loss, service stop, manual return crossing, or source cancel terminates the drag and releases native state once.
- ASSUMED: Cancel native drag BEFORE injecting cleanup up: destroy our Wayland source or set OLE cancellation flag; otherwise cleanup could accidentally perform a drop.
- ASSUMED: Deploy extension readers on both peers before writers; advertise capability/version over existing IPC and DTLS negotiation, default off, and reject unsupported peers without swallowing their drag.

## Transfer extension and final-path authority
- ASSUMED: Add optional `drag={"id":"<uuid32>","epoch":7}` context to push(), carried on its existing put jobs; ordinary callers are unchanged.
- ASSUMED: On final put, run_here records `{receipt, authenticated_sender, drag_id, epoch, final_name, size, file_identity, expires}` locally and returns `{sent,bytes,receipt}`.
- ASSUMED: Receipt is random, single-drag, 60 s lifetime; persist a user-only bounded receipt map under existing JARVIS state, never trust a sender-supplied final name.
- ASSUMED: Reuse cluster's authenticated node/job target identity as sender; thread trusted execution context into transfer.run_here, not a payload field.
- ASSUMED: New action: `{"action":"offer_drag","drag":{"id":"<uuid32>","epoch":7},"receipt":"<uuid32>"}` in a normal `transfer` job through transfer.call().
- ASSUMED: Result: `{"drag":"<uuid32>","status":"armed|staged_only|rejected","reason":"<bounded-code>"}`; OS completion is asynchronous DragUpdate.
- ASSUMED: offer_drag resolves receipt locally and requires matching active inbound Lan Mouse peer+epoch+hold, fixed-folder regular file and unchanged identity.
- ASSUMED: JARVIS maps configured device↔authorized fingerprint explicitly; neither hostname nor last IP is sufficient identity.
- ASSUMED: Repeated offer_drag returns stored outcome without restarting native drag; expired/consumed receipt or released epoch can never arm.
- ASSUMED: Extend transfer.validate/run_here only for this action/context; keep files capability scope and cluster signing; no new desktop action available to nodes.
- VERIFIED: Node replay/signature checks exist (`jarvis/cluster.py:1136–1164`); outgoing Lan Mouse fingerprint verification exists (`LM/src/connect.rs:50–64`).

## Timing, bounds, security and recovery
- VERIFIED: MAX_BYTES=2 GiB, CHUNK=2 MiB, disk reserve=2 GiB (`jarvis/transfer.py:27–33,232–239`).
- ASSUMED: Enforce file size ≤MAX_BYTES before push; MVP one item/session, one active drag/seat, URI offer ≤64 KiB; never buffer file contents in RAM.
- VERIFIED: Arithmetic: a maximum file needs 1024 sequential chunks and about 2.67 GiB base64 payload (derived from transfer constants/push).
- ASSUMED: Ideal staging time is ≥S/bandwidth + 1024×job round-trip for a maximum file; no credible seamless-large-file guarantee without measured chunk latency.
- ASSUMED: First release before ready irreversibly marks staged_only; finish transfer, retain file in JARVIS-Drop, notify owner on B; never defer a drop into a remembered folder.
- ASSUMED: Actual transfer failure instead reports incomplete/failed; a .part is not called a landed file; existing sweep handles abandoned parts.
- ASSUMED: Release just after arming is a normal drop; release during pad acquisition cancels; terminal seq/tombstone wins over every late ready/offer.
- ASSUMED: Use a 60 s interactive session deadline; transfer may continue after expiry, but cannot rearm; canceled/released sessions retain landed files.
- ASSUMED: Restart reconstructs receipts for staged-only reporting, never resurrects active input; bounded 256 terminal records/10 min dedupe, then reject unknown sessions.
- ASSUMED: Unknown/expired receipts fail closed; no blind whole-file retry after ambiguous final response, because fresh push upload IDs could create duplicate files.
- ASSUMED: Recovery from ambiguous publication is manual inbox inspection; later idempotent upload recovery belongs inside transfer.py, never a duplicate transport.
- ASSUMED: No destination pathname crosses JARVIS: it writes file contents only in fixed drop folder; OS file manager owns final copy/collision/permission decisions.
- ASSUMED: URI parser permits only local file URIs (empty/localhost authority), strict decoding, absolute paths, no NUL, traversal tricks, network mounts or directories.
- ASSUMED: Windows rejects UNC/device paths, reparse points, alternate streams, offline placeholders; Linux rejects symlinks and nonregular/network-backed files.
- ASSUMED: Open source once with no-follow semantics, inspect handle, stream that handle; detect mutation and fail/notify instead of presenting corrupt success.
- ASSUMED: Validate receipt file identity again before native offer; retaining staged copies cannot authorize symlink swaps or arbitrary local reads.
- VERIFIED: Same-LAN exclusion, PEER_TAG and protected elevation requirements are explicit (`CLAUDE.md:143–156,158–184`).
- ASSUMED: Reuse sharedinput's nearby/ControllerPause policy in the coordinator; explicit together, active authorized peer and no remote-control pause are required at begin and arm.
- ASSUMED: No SSH fallback for drag: call push directly; no new daemon, port, task, or paid API; existing transports and signing remain authoritative.

## Minimal implementation slices in dependency order (all ASSUMED)
1. **Arch alone, headless contract/state gate — first buildable slice.**
   Owner files: `jarvis/sharedinput.py`, `jarvis/transfer.py`, `tests/test_dragdrop.py`, `tests/test_transfer.py`; LM `lan-mouse-ipc/src/lib.rs`, `lan-mouse-proto/src/lib.rs`, `src/capture.rs`, `src/emulation.rs`.
   Acceptance: fake IPC/hook/compositor/transfer clocks; held-at-entry snapshot, duplicate Begin/Ack, reordered release/ready, early release, timeout, Leave, peer mismatch; zero late starts/stuck buttons.
   Acceptance: existing push/run_here fakes both directions, exact collision receipt, rejected traversal and oversized source, unchanged clipboard callers; no OS input injection or installed-binary changes.
   Hands/hardware: none; this validates contracts only, not either native direction.
2. **Arch alone, Linux native gate (block rollout on failure).**
   Owner files: LM `input-capture/src/layer_shell.rs`, `input-emulation/src/wlroots.rs`, `src/capture.rs`, `src/emulation.rs`; isolated native probe tests beside these modules.
   Acceptance: fake data-device pre-drop pipe, bounded EOF/timeout, rejected offer then release, no early finish, fresh button serial only, serial timeout cancels and restores cursor.
   Hands: owner-approved scratch build/session, actual Hyprland + file manager; verify edge receives uri-list mid-drag, synthetic release ends A safely, real held release remains observable, target pad gets valid serial.
   Dell: unnecessary; use two test native windows on Arch for source and target separately; never infer cross-PC success from this.
3. **Arch alone, authenticated IPC and staging integration.**
   Owner files: LM `lan-mouse-ipc/src/{lib,listen,connect_async}.rs`, `src/service.rs`; JARVIS `sharedinput.py`, `transfer.py`, `cluster.py`, `platforms.py`, corresponding tests.
   Acceptance: fake hub/node both roles, trusted sender propagation, receipt replay/expiry, private events, token rejection, partial transfer, publication-response loss, paused/apart; no destination path accepted.
   Acceptance: coordinator attached once to existing lifecycle, bounded work queue, shutdown cancels once; use isolated temp drops/fake Lan Mouse endpoints, never live input.
   Hands: none for headless suite; Arch live staging only after slice 2 approval; no Windows compatibility claim yet.
4. **Dell REQUIRED, Windows source and target adapters.**
   Owner files: `jarvis/dragdrop_windows.py`, `jarvis/platforms.py`, `jarvis/sharedinput.py`; Dell twin `input-capture/src/windows/event_thread.rs`, `input-emulation/src/windows.rs`, shared LM IPC/proto/session files.
   Acceptance: headless fake hook/COM tests: STA lifetime, GetData/ReleaseStgMedium, hook callback nonblocking, deferred edge capture, NONE unwind, COPY-only offer, PEER_TAG, UIPI/SendInput failure cleanup.
   Hands: Windows build/test on Dell, medium Explorer plus protected elevated Lan Mouse; actual CF_HDROP enter while hook fires, no park-before-offer race, release lands in chosen folder.
   Hands: test elevated Explorer separately; unsupported integrity/secure-desktop cases disable feature and notify without changing scheduled-task paths.
5. **Dell REQUIRED, two-PC acceptance and guarded rollout.**
   Owner files: existing tests plus `tests/test_dragdrop.py`, handbook input-sharing docs and transfer docstring in a later implementation task.
   Acceptance: headless permutations of loss/reorder, transfer failure, both roles, cancel/return crossing, shutdown, fresh-press after cancellation; existing transfer/sharedinput/clipsync and full Python suites.
   Hands: owner drags small photo both ways, releases early on a large file, drops on folder/desktop/invalid target, tests duplicate name, disconnected peer, sleep, fail-safe and ordinary non-file drags.
   Pass only if originals survive, no unintended local move/click occurs, fixed staging is complete, final COPY works, and pointer/buttons always recover; no deployment/restarts authorized by this spike.

## Remaining risks and falsification gates
- ASSUMED: Highest risk is source physical-grab termination on Hyprland; failure falsifies the main→node plan under current dependencies.
- ASSUMED: Second risk is target virtual-pointer serial/seat acceptance; failure falsifies node→main without an explicitly approved compositor-specific dependency.
- ASSUMED: Windows medium-integrity node availability and the 150 ms strip/hook race are open; test both before widening supported applications.
- ASSUMED: Prove actual file manager offered actions and XWayland behavior; initially support only named, tested native managers and one real file.
- ASSUMED: Measure cross-host chunk RTT and typical photo completion time; tune UX from measurements, preserving early-release staged-only behavior.
- ASSUMED: Receipt publication hardening on filesystems without hard links and source mutation semantics need targeted tests before calling storage guarantees stronger than existing transfer.

## Primary protocol/API references (VERIFIED: retrieved 2026-11-08)
[W1]: https://wayland.freedesktop.org/docs/html/apa.html#protocol-spec-wl_data_device
[H1]: https://github.com/hyprwm/Hyprland/blob/main/src/protocols/core/DataDevice.cpp
[H2]: https://github.com/hyprwm/Hyprland/blob/main/src/managers/input/InputManager.cpp
[M1]: https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelmouseproc
[M2]: https://learn.microsoft.com/en-us/windows/win32/api/ole2/nf-ole2-dodragdrop
[M3]: https://learn.microsoft.com/en-us/windows/win32/api/ole2/nf-ole2-registerdragdrop
[M4]: https://learn.microsoft.com/en-us/windows/win32/api/oleidl/nf-oleidl-idroptarget-dragenter
[M5]: https://learn.microsoft.com/en-us/windows/win32/shell/clipboard#cf_hdrop
[M6]: https://learn.microsoft.com/en-us/windows/win32/api/oleidl/nf-oleidl-idropsource-querycontinuedrag
