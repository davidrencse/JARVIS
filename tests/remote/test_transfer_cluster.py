"""Files and clipboard copies over the cluster link (transfer kind), and Lan Mouse control."""

import base64
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import clipsync, sharedinput, transfer
from jarvis.cluster import validate_action
from jarvis.cluster_store import Broker


def cluster(local="main"):
    return {"devices": {"main": {"hostname": "main-host", "transport": "local"},
                        "node": {"hostname": "node-host", "transport": "cluster"}},
            "cluster": {"enabled": True, "main_device": "main", "local_device": local}}


class Homes(unittest.TestCase):
    """A fake home folder per side, so drop folders never touch the real ~/JARVIS-Drop."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.remote_home, self.local_home = self.root / "remote", self.root / "local"
        self.remote_home.mkdir()
        self.local_home.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.calls = []

    def remote(self, config, state, device, payload, timeout=120):
        """transfer.call stand-in: the payload crosses as JSON and runs in the remote home."""
        self.calls.append(payload)
        payload = json.loads(json.dumps(payload))
        validate_action("transfer", payload)
        with mock.patch.object(Path, "home", return_value=self.remote_home):
            return json.loads(json.dumps(transfer.run_here(payload, self.state)))


class TransferTests(Homes):
    def test_desktop_drop_lands_on_desktop_without_exposing_it_in_inbox(self):
        desktop = self.remote_home / "Desktop"
        desktop.mkdir()
        (desktop / "a.txt").write_bytes(b"existing")
        with mock.patch.object(transfer, "call", self.remote), \
                mock.patch.object(transfer.sys, "platform", "linux"):
            result = transfer.push(cluster(), self.state, "node", io.BytesIO(b"new"), 3,
                                   name="a.txt", dest="desktop")
        self.assertEqual(result["sent"], "a (2).txt")
        self.assertEqual((desktop / "a.txt").read_bytes(), b"existing")
        self.assertEqual((desktop / "a (2).txt").read_bytes(), b"new")
        with mock.patch.object(Path, "home", return_value=self.remote_home):
            self.assertEqual(transfer.run_here({"action": "inbox"}), {"entries": []})
            self.assertEqual(transfer.run_here({"action": "inbox", "dest": "desktop"}),
                             {"entries": []})
            with self.assertRaises(ValueError):
                transfer.run_here({"action": "get", "name": "a (2).txt", "offset": 0})

    def test_windows_desktop_uses_shell_redirected_folder(self):
        redirected = self.remote_home / "OneDrive" / "Desktop"
        def resolve(_window, folder_id, _token, _flags, output):
            self.assertEqual(folder_id, 0x10)
            output.value = str(redirected)
            return 0
        shell = mock.Mock()
        shell.SHGetFolderPathW.side_effect = resolve
        with mock.patch.object(transfer.sys, "platform", "win32"), \
                mock.patch("ctypes.windll", create=True) as windll:
            windll.shell32 = shell
            self.assertEqual(transfer.desktop_folder(), redirected)
        self.assertTrue(redirected.is_dir())

    def test_send_streams_in_chunks_and_lands_whole(self):
        src = self.root / "photo.png"
        src.write_bytes(b"0123456789abc")
        with mock.patch.object(transfer, "CHUNK", 4), mock.patch.object(transfer, "call", self.remote):
            result = transfer.send_file(cluster(), self.state, src)
        self.assertEqual(result["sent"], "photo.png")
        self.assertEqual((self.remote_home / "JARVIS-Drop" / "photo.png").read_bytes(), b"0123456789abc")
        self.assertEqual(len(self.calls), 4)
        self.assertEqual([c["last"] for c in self.calls], [False, False, False, True])
        self.assertEqual(list((self.remote_home / "JARVIS-Drop").glob(".jarvis-upload-*")), [])

    def test_send_never_replaces_an_existing_file(self):
        drop = self.remote_home / "JARVIS-Drop"
        drop.mkdir()
        (drop / "a.txt").write_bytes(b"old")
        src = self.root / "a.txt"
        src.write_bytes(b"new")
        with mock.patch.object(transfer, "call", self.remote):
            result = transfer.send_file(cluster(), self.state, src)
        self.assertEqual(result["sent"], "a (2).txt")
        self.assertEqual((drop / "a.txt").read_bytes(), b"old")
        self.assertEqual((drop / "a (2).txt").read_bytes(), b"new")

    def test_node_sends_to_the_main_only(self):
        self.assertEqual(transfer.peer(cluster("node")), "main")
        self.assertEqual(transfer.peer(cluster("main")), "node")
        with self.assertRaisesRegex(ValueError, "this computer"):
            transfer.peer(cluster("main"), "main")
        self.assertIsNone(transfer.peer({"cluster": {"enabled": False}, "devices": {}}))

    def test_inbox_lists_files_not_folders_or_parts(self):
        drop = self.remote_home / "JARVIS-Drop"
        (drop / "folder").mkdir(parents=True)
        (drop / "note.txt").write_text("x")
        (drop / ".jarvis-upload-0123.part").write_text("x")
        with mock.patch.object(transfer, "call", self.remote):
            self.assertEqual(transfer.list_inbox(cluster(), self.state), ["note.txt"])

    def test_fetch_pulls_chunks_into_a_free_local_name(self):
        drop = self.remote_home / "JARVIS-Drop"
        drop.mkdir()
        (drop / "big.bin").write_bytes(bytes(range(10)))
        dest = self.local_home / "in"
        dest.mkdir()
        (dest / "big.bin").write_bytes(b"keep")
        with mock.patch.object(transfer, "CHUNK", 3), mock.patch.object(transfer, "call", self.remote):
            result = transfer.fetch_file(cluster(), self.state, "big.bin", dest=dest)
        self.assertEqual(result["fetched"], "big (2).bin")
        self.assertEqual((dest / "big (2).bin").read_bytes(), bytes(range(10)))
        self.assertEqual((dest / "big.bin").read_bytes(), b"keep")
        self.assertEqual(list(dest.glob(".jarvis-upload-*")), [])

    def test_out_of_order_chunk_is_refused_and_the_part_dropped(self):
        upload = "a" * 32
        first = {"action": "put", "upload": upload, "offset": 0, "last": False, "name": "x",
                 "data": base64.b64encode(b"abcd").decode()}
        self.remote(None, None, "node", first)
        with self.assertRaisesRegex(ValueError, "went missing"):
            self.remote(None, None, "node", {**first, "offset": 8})
        self.assertFalse((self.remote_home / "JARVIS-Drop" / f".jarvis-upload-{upload}.part").exists())

    def test_offline_node_falls_back_to_ssh_when_the_peer_has_a_target(self):
        config = cluster("node")
        config["devices"]["main"].update(transport="ssh", target="me@main-host")
        src = self.root / "f.txt"
        src.write_bytes(b"x")
        with mock.patch.object(transfer, "call", side_effect=ConnectionError("down")), \
                mock.patch.object(transfer, "send", return_value={"sent": "f.txt"}) as ssh:
            self.assertEqual(transfer.send_file(config, self.state, src), {"sent": "f.txt"})
        self.assertEqual(ssh.call_args.args[0], "me@main-host")

    def test_offline_node_without_ssh_reports_it(self):
        src = self.root / "f.txt"
        src.write_bytes(b"x")
        with mock.patch.object(transfer, "call", side_effect=ConnectionError("Node node is offline")):
            with self.assertRaisesRegex(ConnectionError, "offline"):
                transfer.send_file(cluster(), self.state, src)


class SafeNameTests(unittest.TestCase):
    def test_every_safe_name_is_a_valid_inbox_name(self):
        for raw in ("(draft).txt", "+1 notes.md", "@home.png", ",x", "-rf", ".hidden", "a b", "", "CON"):
            with self.subTest(raw=raw):
                self.assertTrue(transfer.NAME.fullmatch(transfer.safe_name(raw)), transfer.safe_name(raw))


class ValidateTests(unittest.TestCase):
    def test_refuses_anything_off_the_fixed_operations(self):
        good = {"action": "put", "upload": "b" * 32, "offset": 0, "data": "", "name": "n"}
        validate_action("transfer", good)
        for bad in ({"action": "rm"}, {**good, "upload": "../x"}, {**good, "offset": -1},
                    {**good, "offset": True}, {**good, "data": "A" * (transfer.CHUNK * 2)},
                    {**good, "dest": "shell"}, {**good, "dest": "clipboard", "kind": "exe"},
                    {"action": "get", "name": "../etc/passwd", "offset": 0},
                    {"action": "get", "name": ".hidden", "offset": 0}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_action("transfer", bad)


class BrokerBulkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.broker = Broker(tmp.name)
        self.broker.enroll("node")

    def test_transfer_chunks_may_exceed_the_control_limit(self):
        big = {"data": "A" * 200_000}
        job = self.broker.enqueue("node", "main", "transfer", big)
        with self.assertRaisesRegex(ValueError, "too large"):
            self.broker.enqueue("node", "main", "desktop", big)
        with self.assertRaisesRegex(ValueError, "too large"):
            self.broker.enqueue("node", "main", "transfer", {"data": "A" * 3_100_000})
        claimed = self.broker.claim("node", {"transfer"})
        self.assertEqual(claimed["id"], job)
        self.broker.finish(job, "node", {"data": "B" * 100})
        self.broker.compact(job)
        with self.broker.db() as db:
            row = db.execute("SELECT payload, result FROM jobs WHERE id=?", (job,)).fetchone()
        self.assertEqual((row["payload"], row["result"]), ("{}", "{}"))


    def test_chunk_bytes_never_reach_sqlite(self):
        job = self.broker.enqueue("node", "main", "transfer", {"data": "QUJD" * 50_000})
        claimed = self.broker.claim("node", {"transfer"})
        self.assertEqual(claimed["payload"], {"data": "QUJD" * 50_000})
        self.broker.finish(job, "node", {"data": "WFla" * 50_000})
        self.broker.finish(job, "node", {"data": "WFla" * 50_000})  # lost ack: repeat is accepted
        self.assertEqual(self.broker.get(job)["result"], {"data": "WFla" * 50_000})
        raw = b"".join(p.read_bytes() for p in Path(self.broker.path).parent.glob("cluster.sqlite3*"))
        self.assertNotIn(b"QUJDQUJD", raw)
        self.assertNotIn(b"WFlaWFla", raw)
        self.assertLess(len(raw), 100_000)
        self.broker.compact(job)
        self.assertEqual(self.broker.get(job)["result"], {})

    def test_result_after_a_timed_out_compact_is_not_held(self):
        # Audit cluster-11: a late result re-created a ~MB entry nobody would read.
        job = self.broker.enqueue("node", "main", "transfer", {"data": "x"})
        self.broker.claim("node", {"transfer"})
        self.broker.compact(job)  # the requester timed out; the row is still running (GRACE)
        self.broker.finish(job, "node", {"data": "Y" * 100_000})
        self.assertNotIn(job, self.broker._bulk)
        self.assertEqual(self.broker.get(job)["state"], "done")
        self.broker.finish(job, "node", {"data": "Y" * 100_000})  # a lost-ack repeat stays quiet
        self.assertNotIn(job, self.broker._bulk)

    def test_queued_chunk_is_expired_not_replayed_after_a_hub_restart(self):
        job = self.broker.enqueue("node", "main", "transfer", {"data": "x"})
        restarted = Broker(self.broker.path.parent)
        self.assertIsNone(restarted.claim("node", {"transfer"}))
        self.assertEqual(restarted.get(job)["state"], "expired")


class FakeBoard:
    def __init__(self, items):
        self.items, self.written = list(items), []

    def read(self):
        return self.items[0] if self.items else None

    def write(self, kind, data):
        self.written.append((kind, data))

    def watch(self, changed):
        while len(self.items) > 1:
            self.items.pop(0)
            changed()
        raise KeyboardInterrupt  # ends run_cluster's forever loop in the test


class ClipboardTests(Homes):
    def test_copies_go_out_once_and_pasted_ones_dont_bounce_back(self):
        board = FakeBoard([("text", b"before"), ("text", b"mine"), ("text", b"mine"),
                           ("text", b"theirs")])
        with mock.patch.object(clipsync, "local_clipboard", return_value=FakeBoard([])):
            clipsync.apply(self.state, "text", b"theirs")  # the node agent pasting a peer copy
        pushed = []
        with mock.patch("jarvis.transfer.push", lambda config, state, target, source, size, **kw:
                        pushed.append((target, source.read(), kw))):
            with self.assertRaises(KeyboardInterrupt):
                clipsync.run_cluster(cluster(), str(self.state), board=board, log=lambda _: None)
        self.assertEqual(pushed, [("node", b"mine", {"dest": "clipboard", "kind": "text"})])

    def test_clipboard_transfer_pastes_on_the_receiver(self):
        board = FakeBoard([])
        with mock.patch.object(transfer, "call", self.remote), \
                mock.patch.object(clipsync, "local_clipboard", return_value=board):
            result = transfer.push(cluster(), self.state, "node", io.BytesIO(b"hi"), 2,
                                   dest="clipboard", kind="text")
        self.assertEqual(result, {"pasted": "text", "bytes": 2})
        self.assertEqual(board.written, [("text", b"hi")])
        self.assertEqual(clipsync._applied(str(self.state)), clipsync.digest("text", b"hi"))


class LanMouseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "config.toml"
        self.fp = ":".join(["ab"] * 32)

    def test_pairing_keeps_tuning_and_backs_up_the_old_config(self):
        self.path.write_text('capture_backend = "layer-shell"\nrelease_bind = [ "KeyLeftAlt" ]\n'
                             "port = 4300\nmotion_scale = 1.5\n", encoding="utf-8")
        with mock.patch.object(sharedinput, "lan_mouse_status", return_value={"running": False}):
            out = sharedinput.write_pairing("WinLaptop", ["10.0.0.2"], "right", self.fp, self.path)
        written = sharedinput.read_config(self.path)
        self.assertEqual((written["port"], written["motion_scale"], written["release_bind"],
                          written["capture_backend"]), (4300, 1.5, ["KeyLeftAlt"], "layer-shell"))
        self.assertEqual(written["clients"][0]["ips"], ["10.0.0.2"])
        self.assertIn("motion_scale = 1.5", Path(out["backup"]).read_text(encoding="utf-8"))

    def test_pairing_waits_for_the_old_daemon_to_exit_before_starting(self):
        self.path.write_text("port = 4300\n", encoding="utf-8")
        states = iter([True, True, True, False, False])  # running, then still dying, then gone
        with mock.patch.object(sharedinput, "lan_mouse_status",
                               side_effect=lambda: {"running": next(states, False)}), \
                mock.patch.object(sharedinput, "stop") as stop, \
                mock.patch.object(sharedinput, "start", return_value={"started": True, "running": True}) as start, \
                mock.patch("time.sleep"):
            out = sharedinput.write_pairing("WinLaptop", ["10.0.0.2"], "right", self.fp, self.path)
        stop.assert_called_once()
        start.assert_called_once()
        self.assertTrue(out["restarted"])

    def test_pairing_does_not_restart_lan_mouse_while_apart(self):
        self.path.write_text("port = 4300\n", encoding="utf-8")
        state = self.path.parent
        (state / sharedinput.GUARD_FILE).write_text(json.dumps(
            {"apart_now": True, "read_at": time.time(), "override": False, "why": "apart"}), encoding="utf-8")
        with mock.patch.object(sharedinput, "lan_mouse_status", return_value={"running": True}), \
                mock.patch.object(sharedinput, "stop") as stop, \
                mock.patch.object(sharedinput, "start") as start:
            out = sharedinput.write_pairing("WinLaptop", ["10.0.0.2"], "right", self.fp,
                                            self.path, state=state, config={})
        stop.assert_not_called()
        start.assert_not_called()
        self.assertFalse(out["restarted"])
        self.assertTrue(self.path.is_file())

    def test_pairing_does_not_claim_a_restart_that_did_not_happen(self):
        self.path.write_text("port = 4300\n", encoding="utf-8")
        with mock.patch.object(sharedinput, "lan_mouse_status", return_value={"running": True}), \
                mock.patch.object(sharedinput, "stop"), \
                mock.patch.object(sharedinput, "start", return_value={"started": False, "running": True}), \
                mock.patch("time.sleep"):
            out = sharedinput.write_pairing("WinLaptop", ["10.0.0.2"], "right", self.fp, self.path)
        self.assertFalse(out["restarted"])

    def test_control_refuses_anything_but_its_commands(self):
        for arg in ("rm -rf", '{"pair": {"peer_name": "x"}}', "[]"):
            with self.subTest(arg=arg):
                ok, _ = sharedinput.control(arg)
                self.assertFalse(ok)

    def test_pair_exchanges_fingerprints_and_opposite_sides(self):
        theirs = {"hostname": "WinLaptop", "addresses": ["10.0.0.2"],
                  "lan_mouse": {"fingerprint": self.fp}}
        mine = {"hostname": "archmain.local", "addresses": ["10.0.0.1"],
                "lan_mouse": {"fingerprint": ":".join(["cd"] * 32)}}
        remote = mock.Mock()
        remote.call.side_effect = [(True, json.dumps(theirs)), (True, json.dumps({"written": "w"}))]
        with mock.patch.object(sharedinput, "local_info", return_value=mine), \
                mock.patch.object(sharedinput, "write_pairing", return_value={"written": "here"}) as write:
            out = sharedinput.pair({}, None, "dell", "right", remote)
        sent = json.loads(remote.call.call_args_list[1].args[2])["pair"]
        self.assertEqual(sent, {"peer_name": "archmain", "peer_ips": ["10.0.0.1"],
                                "position": "left", "peer_fingerprint": ":".join(["cd"] * 32)})
        write.assert_called_once_with("WinLaptop", ["10.0.0.2"], "right", self.fp, state=None, config={})
        self.assertEqual(out["there"], {"written": "w"})

    def test_pair_needs_a_certificate_on_both_sides(self):
        remote = mock.Mock()
        remote.call.return_value = (True, json.dumps({"hostname": "d", "addresses": [],
                                                      "lan_mouse": {"fingerprint": None}}))
        with mock.patch.object(sharedinput, "local_info", return_value={"lan_mouse": {"fingerprint": self.fp}}):
            with self.assertRaisesRegex(ValueError, "no certificate on dell"):
                sharedinput.pair({}, None, "dell", "right", remote)


if __name__ == "__main__":
    unittest.main()


class FileClipboardFlowTests(Homes):
    def frame(self, names):
        return json.dumps({'kind': 'files', 'names': names}).encode()

    def test_receiver_rejects_traversal_absolute_reserved_missing_and_link_names(self):
        drop = self.local_home / 'JARVIS-Drop'
        drop.mkdir()
        outside = self.local_home / 'outside'
        outside.write_text('keep')
        (drop / 'link').symlink_to(outside)
        with mock.patch.object(Path, 'home', return_value=self.local_home), \
                mock.patch.object(clipsync, 'local_clipboard') as board, \
                mock.patch.object(clipsync, '_paused', return_value=None):
            for name in ('../outside', str(outside), r'C:\outside', 'missing', 'link', 'CON'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    clipsync.apply(self.state, 'files', self.frame([name]))
            board.assert_not_called()

    def test_receiver_sets_paths_and_names_digest(self):
        drop = self.local_home / 'JARVIS-Drop'
        drop.mkdir()
        (drop / 'a.txt').write_bytes(b'abc')
        board = FakeBoard([])
        with mock.patch.object(Path, 'home', return_value=self.local_home), \
                mock.patch.object(clipsync, 'local_clipboard', return_value=board), \
                mock.patch.object(clipsync, '_paused', return_value=None):
            self.assertTrue(clipsync.apply(self.state, 'files', self.frame(['a.txt'])))
        self.assertEqual(board.written, [('files', [drop / 'a.txt'])])
        self.assertEqual(clipsync._applied(str(self.state)), clipsync.digest('files', ['a.txt']))

    def test_receiver_pause_still_applies_to_files(self):
        with mock.patch.object(clipsync, '_paused', return_value='apart'), \
                mock.patch.object(clipsync, 'local_clipboard') as board:
            self.assertFalse(clipsync.apply(self.state, 'files', self.frame(['a.txt'])))
        board.assert_not_called()

    def run_watcher(self, board, config=None, **kwargs):
        with self.assertRaises(KeyboardInterrupt):
            clipsync.run_cluster(config or cluster(), str(self.state), board=board, log=lambda _: None,
                                 **kwargs)

    def test_size_cap_sends_nothing_and_notifies_once_per_copy(self):
        src = self.root / 'large'
        src.write_bytes(b'12345')
        config = cluster()
        config['clipboard'] = {'files_max_bytes': 4}
        board = FakeBoard([None, ('files', [str(src)]), ('files', [str(src)])])
        with mock.patch.object(transfer, 'push') as push, \
                mock.patch.object(clipsync, '_notify_files_limit') as notify, \
                mock.patch.object(clipsync, '_paused', return_value=None):
            self.run_watcher(board, config)
        push.assert_not_called()
        notify.assert_called_once()

    def test_drop_folder_bounce_rule_survives_marker_expiry(self):
        drop = self.local_home / 'JARVIS-Drop'
        drop.mkdir()
        src = drop / 'a.txt'
        src.write_bytes(b'abc')
        board = FakeBoard([None, ('files', [str(src)])])
        with mock.patch.object(Path, 'home', return_value=self.local_home), \
                mock.patch.object(transfer, 'push') as push, \
                mock.patch.object(clipsync, '_applied', return_value=None), \
                mock.patch.object(clipsync, '_paused', return_value=None):
            self.run_watcher(board)
        push.assert_not_called()
        self.assertTrue(clipsync.files_in_drop([src], drop))
        self.assertFalse(clipsync.files_in_drop([src, self.root / 'elsewhere'], drop))
        self.assertFalse(clipsync.files_in_drop([self.local_home / 'JARVIS-Drop-other' / 'x'], drop))

    def test_paused_files_are_never_pushed(self):
        src = self.root / 'a.txt'
        src.write_text('keep')
        with mock.patch.object(clipsync, '_paused', return_value='apart'), \
                mock.patch.object(transfer, 'push') as push:
            self.run_watcher(FakeBoard([None, ('files', [str(src)])]))
        push.assert_not_called()

    def test_end_to_end_both_roles_chunked_with_collision_and_bounce(self):
        for local in ('main', 'node'):
            with self.subTest(local=local):
                self.calls.clear()
                src = self.root / f'{local}.txt'
                src.write_bytes(b'hello file')
                drop = self.remote_home / 'JARVIS-Drop'
                drop.mkdir(exist_ok=True)
                (drop / src.name).write_bytes(b'old')
                receiver = FakeBoard([])
                sender = FakeBoard([None, ('files', [str(src)])])
                targets = []
                def remote(config, state, device, payload, timeout=120):
                    targets.append(device)
                    return self.remote(config, state, device, payload, timeout)
                with mock.patch.object(Path, 'home', return_value=self.local_home), \
                        mock.patch.object(transfer, 'CHUNK', 4), \
                        mock.patch.object(transfer, 'call', remote), \
                        mock.patch.object(clipsync, 'local_clipboard', return_value=receiver), \
                        mock.patch.object(clipsync, '_paused', return_value=None):
                    self.run_watcher(sender, cluster(local))
                final = f'{local} (2).txt'
                self.assertEqual(receiver.written, [('files', [drop / final])])
                self.assertEqual((drop / final).read_bytes(), src.read_bytes())
                self.assertEqual((drop / src.name).read_bytes(), b'old')
                self.assertEqual(src.read_bytes(), b'hello file')
                self.assertEqual(set(targets), {'node' if local == 'main' else 'main'})
                clipboard = [c for c in self.calls if c.get('dest') == 'clipboard']
                frame = json.loads(b''.join(base64.b64decode(c['data']) for c in clipboard))
                self.assertEqual(frame, {'kind': 'files', 'names': [final]})
                self.assertTrue(all(c.get('kind') == 'files' for c in clipboard))
                bounce = FakeBoard([None, ('files', [str(drop / final)])])
                with mock.patch.object(Path, 'home', return_value=self.remote_home), \
                        mock.patch.object(transfer, 'push') as push, \
                        mock.patch.object(clipsync, '_paused', return_value=None):
                    self.run_watcher(bounce)
                push.assert_not_called()

    def test_failed_file_transfer_never_publishes_clipboard(self):
        src = self.root / 'a.txt'
        src.write_bytes(b'abc')
        with mock.patch.object(transfer, 'push', side_effect=ConnectionError('offline')) as push, \
                mock.patch.object(clipsync, '_paused', return_value=None):
            self.run_watcher(FakeBoard([None, ('files', [str(src)])]))
        self.assertEqual(push.call_count, 1)
        self.assertEqual(push.call_args.kwargs['dest'], 'drop')
