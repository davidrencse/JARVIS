# Computers and servers

*The console Connections view and the phone's Manage computers screen. The API is [docs/design/fleet-api.md](../design/fleet-api.md).*

Exactly one computer is the MAIN. The others are nodes. These screens add no power: an invite is still joined by hand on the other computer. A cloud server is inventory until extra permissions are acknowledged. Enrollment does not create the server, install an OS, or install a gateway.

On the MAIN, open Connections → Computers and servers.

1. Pick PC, headless computer, or cloud server. The id is letters, digits, `-`, and `_`. The operating system only chooses which join command is shown.
2. Check readiness. Nothing changes. The preview names which computer does each step.
3. Create the invite. The code is shown once, in that panel only. It is one use and it expires. Copy the code or the join command. The console never runs it.
4. On the new computer, in its JARVIS folder, run the join command, then `jarvis cluster doctor`.
5. Back on the MAIN, the node becomes Online. Change what it may do from its row. Extra permissions need the acknowledgement. Revoke asks for the node's id and cuts the credential and the SSH key.

A failed or lost reply retries the same request. If the code was already issued, or an invite is still pending, tick Replace it. That is a new request. It does not rotate a node that has already joined. Revoke that node first. The code is cleared on Done, Start over, or leaving Connections. It is not put in the address bar, browser storage, or a log.

If this computer is not the MAIN, invite, permission, and revoke stay disabled and say why.

Not on these screens:

- Moving the MAIN. Readiness lists the missing lease, fencing, and owner proof. Move MAIN does not transfer, and the console does not call the transfer endpoint. The blocked procedure is [continuity.md](continuity.md).
- Creating a cloud server or installing an operating system.
- Phone administration. The phone reads `GET /phone/api/fleet` and can look. Invite, permissions, revoke, and Move MAIN are finished on the MAIN. Devices stays the phone's home screen. Remote control is on each computer's row. Manage computers is the entry under that list. The phone does not show the locate permission.

The archive and Git are separate. The archive is JARVIS capturing memory, history, and tasks into the vault folder. Its counts sit under Archive diagnostic details. Git is that folder's commits. Unpublished changes are files not committed. Ahead and behind are only as fresh as the last fetch, and this screen never fetches. It does not check whether the GitHub repo is private. There is no commit or push button. A configured remote is not a backup and not proof the changes were published.

Backend Python needs a service restart before new fleet APIs answer. UI assets reload with the page. The phone shell hash updates its cache.
