# Cross-machine file drag

*Design for a native drag between the Arch main and the Windows node. It is not built. `jarvis/web` has no drag protocol, and there is no `dragdrop` module.*

What exists today is different. `dropzone.py` is a Wayland strip inside the Lan Mouse edge. A drop on it is `transfer.push` with COPY only, so the file manager does not delete the original. `jarvis send` and the console drop remain the fallback. The transfer docstring's impossibility claim describes that current path. Change it only after both directions below pass on the real machines.

## Decisions

Build two local native drags linked by one transfer. Do not pretend to continue one OS drag session. Order is source-file safety, then reuse, then how seamless it feels. Neither direction turns on until its native gate passes.

The first version copies one regular local file, including a photo. No directories, no virtual or cloud-only objects, no image-only offers, no move. The original and the staging copy stay. The destination file manager performs COPY. Lan Mouse owns the crossing and the held-input lease. JARVIS owns transfer coordination. OS adapters own native drag and drop. Rejected: a Python edge detector, clipboard polling as drag detection, a new network transport, a destination-path RPC, and a compositor patch as a silent dependency. No new daemon, port, task, or paid API.

File bytes stay on the existing cluster transfer. `MAX_BYTES` is 2 GiB, `CHUNK` is 2 MiB, and the disk reserve is 2 GiB. Enforce the size before `push`. One item and one active drag per seat. Never buffer the file in RAM. JARVIS writes only into the fixed drop folder. The file manager owns the final copy, collision, and permissions. No destination path crosses the cluster.

A receipt is random, single-drag, and lives 60 s. The sender's final name is not trusted. Identity is the enrolled device and fingerprint, not the hostname or the last IP. A repeated offer returns the stored outcome and does not start another native drag. An expired or consumed receipt cannot arm. An ambiguous final response is not retried as a new upload, because a fresh upload id can create a second file. Recovery is looking in the inbox. A release before the file is ready leaves it in JARVIS-Drop and tells the owner. A `.part` is not a landed file.

The drag also requires the machines together, an authorized peer, and no remote-control pause, both at begin and at arm. Same-LAN exclusion and the elevated Windows tasks stay as they are. Do not host the Windows drop strip inside the elevated Lan Mouse process. If the node is not an interactive medium-integrity session, Windows drag stays off.

## Gates that are still experiments

Main → node is blocked on ending the Hyprland source drag. A Wayland target cannot destroy another client's data source. The plan is to read `text/uri-list`, reject the offer, and synthesize a button-up over the strip, using the existing wlroots clock. If that does not end a physical grab on the installed seat, this direction stops. Do not inject Escape into whatever app is focused.

Node → main is blocked on a fresh `wl_pointer` serial on the target seat, and on Windows OLE seeing `CF_HDROP` while the low-level hook is still firing. The hook may delay capture at most 150 ms so Explorer can hit a narrow strip. If no offer arrives, ordinary crossing resumes. The hook thread stays off the OLE modal loop. If OLE does not unwind, this direction stops.

Pad acquisition has 500 ms. If the serial or focus fails, cancel and release. Never click the folder underneath as a fallback. A release during arming wins and prevents a late start. Cancel the native drag before injecting a cleanup button-up, or the cleanup can become a drop.

IPC would be one authenticated `DragUpdate` / `DragControl` family on the existing Lan Mouse socket (`$XDG_RUNTIME_DIR/lan-mouse-socket.sock` on Linux, `127.0.0.1:5252` on Windows). Paths and tokens stay off the frontend broadcast. Loopback is not authorization. Held-input refresh has to cover the drag button. It currently tracks keyboard events only. Deploy readers before writers. Unsupported peers are rejected and their drag is left alone.

Build order, each one blocking the next: a headless contract test with no OS injection; the Linux native gate on Arch alone; authenticated IPC and staging; the Windows source and target on the Dell; then one photo each way on the two PCs. Pass only if the originals survive, nothing moves or clicks by accident, the staged file is complete, the final COPY lands, and the pointer and buttons always recover.
