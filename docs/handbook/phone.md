# Phone app

*The phone PWA: pairing, Ask, camera, and the screen it draws. Fleet admin is [fleet-ui.md](fleet-ui.md).*

The phone is a remote control for the main, not a second JARVIS. Devices lists every computer. Control is the same remote session as the console. Camera is a live webcam, 15 minutes at most. Ask goes through the normal pipeline. Code: `jarvis/phone.py`, `jarvis/web/phone/`, `jarvis/camera.py`, session kinds in `jarvis/remote_view.py`.

The server binds `127.0.0.1:7787` (`phone.port`; not 7790–7799). It starts inside `jarvis-cluster` only when the link is on. A bind failure does not take the console down. It serves a fixed file list and never falls through to the console on 7777. Nodes do not listen for the phone. Publish with Tailscale Serve, never Funnel:

```text
jarvis phone on
systemctl --user restart jarvis-cluster
sudo tailscale serve --bg --https=443 http://127.0.0.1:7787
jarvis phone status
```

`status` must show a `url`, `serve_points_here` true, `funnel` false, and `refusing` null. On the iPhone, Tailscale on, open that URL in Safari, Share → Add to Home Screen, and pair from the icon. iOS keeps the icon's storage apart from Safari's.

```text
jarvis phone pair [--name "David's iPhone"]
jarvis phone off
jarvis phone revoke <id> | revoke all
jarvis phone list
jarvis camera allow on | off | status
jarvis cluster caps <node> --capability desktop --capability files --capability grokbot --capability inventory --capability camera
```

`caps` replaces the set. Camera consent is per computer, read from disk at every start and every frame. Infrared cameras are skipped. Windows needs `ffmpeg` on the node agent's PATH and Settings → Privacy → Camera → desktop apps. `jarvis control stop` on the watched computer ends screen and camera sessions.

## Checks

A paired phone is owner-level: raw keyboard and mouse, and Ask under David's rules. There is no shell, file, or settings API. Each request fails closed:

1. `Host` is the main's tailnet name, or `127.0.0.1:7787` / `localhost:7787`.
2. `X-Forwarded-For` is a tailnet address `tailscale whois` knows.
3. `Tailscale-User-Login` equals that whois login.
4. `Sec-Fetch-Site` is absent, `same-origin`, or `none`.
5. A POST carries `X-JARVIS-Phone: 1` and a JSON object of at most 256 KiB.
6. `/phone/api/*` carries `__Host-jarvis_phone` (HttpOnly, Secure, SameSite=Strict, 90 days). Only the SHA-256 is stored, and only from the paired Tailscale device and login. A non-frame request after 7 days rotates it; the previous cookie works for 10 minutes. Frames send `X-JARVIS-Session`. `?session=` is accepted until 2026-12-31 for an old cached page.

The server answers 503 while the link is off, while Funnel reaches the port, or while Serve's settings cannot be read. The pairing code is 8 characters, hashed, valid 5 minutes, printed on the main's terminal only. The terminal asks `<device> (<OS>, <login>) wants to pair as '<name>'. Allow? [y/N]`. Only `y` pairs. The main, any cluster computer, and anything that is not iOS or Android are refused before the code is checked (`--allow-desktop` is for tests). Five wrong codes per device per 10 minutes, then a wait. After `y`, the key must arrive within 60 s.

The phone cannot approve. Confirmation words are refused with "Approve on your PC". A request that needs an OK says "Waiting for your OK on the PC". Someone else's session is 409. Another phone's session id is 404. `camera`, `locate`, and `remote` are refused on `/v1/action` and `/v1/control` for every caller, including the owner token. The watched computer shows a notification when control, camera, or locate starts or stops.

Location, Radar, Maps links, and mobile geolocation are removed. The overview does not carry coordinates. `/phone/api/locate` is 404. Other non-mobile location tools are unchanged. Promoting MAIN from the phone is not a config change. It needs the fencing and handover in [docs/design/continuity-design.md](../design/continuity-design.md). Ordinary commands still go through the current MAIN.

Out of scope: unsandboxed code on the main can forge loopback or edit `.jarvis/phone.json`. `gates.hard_deny` refuses `jarvis phone`, `jarvis camera`, webcam and location commands, the phone port, and `tailscale serve` or `funnel` changes. A stolen unlocked phone keeps access until `jarvis phone revoke`. Anyone who controls the Tailscale account or the client on the main is outside this design.

## Limits

| What | Value |
|---|---|
| Connections | 24, then 503; one frame request per phone (429); 30 s socket timeout |
| Session quality from the phone | low |
| Camera presets | low 640×480 @ 5 fps, medium 960×540 @ 8, high 1280×720 @ 10 |
| Camera session | 15 minutes |
| Locate | 15 s on the computer; the main waits up to 25 s for a node |
| Ask | 2,000 characters; first reply waits up to 3 s, then an 8 s long-poll |
| Background | lets go at once; the session ends after 15 s (`MAIN_IDLE` is 20 s) |
| Trace | 48 jobs, 64 steps each |

Camera frames go only to the main's memory (newest wins) and the phone that opened the session. Nothing is recorded. Console events (`phone.paired`, `phone.revoked`, `phone.session`, `camera.on`, `camera.off`, `phone.locate`) never carry codes, keys, session ids, or coordinates.

Ask shows the console's `turn.*` events. Tool steps show the tool name only. `phone.redact` strips tokens, passwords, URL credentials, and deep local paths. Only the phone that sent the request sees the trace. A retry sends the same `key` and finds the first request. A failure on the PC is a new request. `jarvis.phone.ask` keeps a job id for 30 minutes so a reload can resume.

**Send to** chooses the main or an online node. The phone remembers it. An explicit computer name in the request still wins. The main interprets, and execution uses the signed node channel with `origin_device` set to the target. Approvals stay on the main (`topology.approvals_on`). Offline, unenrolled, and outdated nodes return a concrete error. Retries stay on the original target.

Native requests prefer `open_app`, `app_ui`, and the target's `computer` tools, including that machine's keybinds. Spotify instructions prefer native search, then verified selection. They do not require a Chrome search for a track id. A Spotify accessibility snapshot may wait up to 10 s for enabled controls and returns as soon as they are ready. Other apps opt in with `app_ui snapshot` and `wait_ready` from 0 to 15 s. Only read-only snapshots are retried.

**Usage & cost** under a reply is reported tokens, model calls, and an API-equivalent USD figure. It is a comparison, not an extra charge. Missing usage is said to be missing. Local requests are labelled as having no metered calls.

Manage computers reads `GET /phone/api/fleet` only. The phone can look. Invite, permissions, revoke, and Move MAIN stay on the main's console. Devices is the home screen. The phone does not show the locate permission.

## Screen

Look is monochrome, Geist for UI, Geist Mono for numbers, Doto for the wordmark only. Colour means state. Tokens live in `jarvis/web/phone/phone.css`. Do not put hex in screen CSS, do not rename tokens, and do not add a font.

Each computer card has a 3 px left rail from `.ld[data-st]`: green live, amber needs you, grey idle or offline. One `.strong` button per screen. Targets are at least 44 px. Rows are at least 52 px. Press feedback is 120 ms at scale 0.975. State changes are 180 ms. Sheets are 320 ms. `prefers-reduced-motion` removes transforms. Inputs are 16 px so iOS does not zoom. Icons are Lucide, 24 px, 2 px stroke, with a visible word. The session picture owns the screen; Disconnect is one red outline in thumb reach. Ask is a conversation. The composer sits above the tabs. Approvals stay on the PC. Ink and state tokens are at least 4.5:1 on `--bg` and `--surface` in both themes.

## When it fails

| The phone says | Check |
|---|---|
| "Can't reach JARVIS — is Tailscale on?" | Tailscale on the phone, the main awake, `jarvis phone status` |
| "JARVIS's phone link is off" | `jarvis phone on`, then restart `jarvis-cluster` |
| "Tailscale Funnel publishes JARVIS to the internet" | Turn Funnel off, then Serve again |
| "JARVIS can't read Tailscale's serve settings" | `tailscale serve status --json` must work as David, without sudo |
| "Tailscale serve did not forward the client address" | Use the Serve command above. Serve's `Host` and `X-Forwarded-For` behavior was taken from Tailscale's source and still needs one real request after setup |
| "Unexpected Host header" | Open `https://<main>.<tailnet>.ts.net/`, not an IP |
| "That Tailscale login doesn't match this device" | The phone must be David, not a tagged device |
| Pair screen again | Safari instead of the icon, revoked, or Tailscale reinstalled |
| "Only a phone can pair with JARVIS" | Desktop OS. `--allow-desktop` is for tests |
| "Too many wrong codes" | Wait 10 minutes |
| "Someone else is already controlling / watching" | End it on the console or the other phone |
| "The camera is off for JARVIS on this computer" | `jarvis camera allow on` there |
| "The camera is in use by another app" | On the Arch main, Zen browser can hold `/dev/video1` |
| "ffmpeg isn't installed" / "Windows wouldn't open the camera" | ffmpeg on PATH, and desktop-app camera privacy |
| "Runs older JARVIS code" | [release.md](release.md) |
| "Approve on your PC" | By design |
| Session ends after switching apps | By design |
