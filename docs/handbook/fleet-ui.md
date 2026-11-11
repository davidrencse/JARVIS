# Computers and servers

The owner console's Connections view and the phone's Manage computers screen
are the fleet UI. They call the fleet API in
[FLEET-API.md](../design/mobile-continuity/FLEET-API.md). They add no new
power: an invite still has to be joined by hand on the other computer.

Exactly one computer is the MAIN. The others are nodes. A cloud server is
inventory-only unless you acknowledge extra permissions. Enrollment does not
create the server, install an OS, or install a gateway service.

## On the MAIN

Open Connections. Under Computers and servers:

1. Pick PC, headless computer, or cloud server. The id is letters, digits, `-`
   and `_`. The operating system only chooses which join command you are shown.
2. Check readiness. Nothing changes. The preview names which computer does
   each step, and which commands run on the new computer.
3. Create the invite. The code is shown once, in that panel only. It is one
   use and it expires. Copy the code or the join command. The console never
   runs the command.
4. On the new computer, in its JARVIS folder, run the join command, then
   `jarvis cluster doctor` (or the Windows equivalent the preview shows).
5. Back on the MAIN, the node becomes Online. Change what it may do from its
   row. Extra permissions need the acknowledgement. Revoke asks you to type
   the node's id. That cuts the credential and the SSH key.

A failed or lost reply retries the same request. If JARVIS says the code was
already issued, or an invite is still pending, tick Replace it. That makes a
new request. It does not rotate a node that has already joined. Revoke that
node first.

The code is cleared when you press Done, Start over, or leave Connections.
It is not put in the address bar, browser storage, or a log.

## What is not available

- Moving the MAIN. Check readiness lists the missing lease, fencing, and
  owner proof. The Move MAIN control does not perform a transfer, and the
  console never calls the transfer endpoint.
- Creating a cloud server or installing an operating system.
- Phone administration. The phone can look, not invite, change permissions,
  revoke, or move the MAIN. Finish those on the MAIN's console.
- A location view. The phone does not show the locate permission.

If this computer is not the MAIN, the invite, permission, and revoke controls
stay disabled and say why.

## Memory archive and Git

These are separate.

- The archive is JARVIS capturing memory, history, and tasks into the vault
  folder. Its own counts sit under Archive diagnostic details.
- Git is the vault folder's commits. Unpublished changes are files not
  committed. Ahead and behind are only as fresh as the last fetch, and this
  screen never fetches. Whether the GitHub repo is private is not checked
  here.
- There is no commit or push button. A configured remote is not a backup and
  not proof the changes were published.

## Phone

Devices stays the home screen. Remote control is on each computer's row.
Manage computers is the entry under that list. It reads `GET /phone/api/fleet`
only. It shows the MAIN, nodes, what each may do, the next step, and who runs
each part of the guide. Adding or changing a computer is finished on the MAIN.


## Verification limits

Grok independently critiqued the design and exercised fake-data desktop/phone flows. Parent
fixed its failed-invite readiness and reversed-tunnel copy findings, clarified pending-invite
replacement and archive backlog, and confirmed phone step actors. The final browser pass
reported no page exceptions or horizontal overflow in measured desktop/320/390/landscape
views. One missing fixture resource returned404. Actual iPhone keyboard and physical safe-area
behavior remain unverified. No real enrollment, permissions change, revoke or MAIN transfer
was run in these tests. Backend Python changes need a service restart before the new UI APIs
are usable; UI assets reload, and the phone shell hash updates its cache.
