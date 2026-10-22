import base64
import hashlib
import unittest
from unittest.mock import patch

from jarvis.desktop import lan_mouse_config, pem_fingerprint, remote_fingerprint

DER = b"\x30\x03\x02\x01\x01"
PEM = ("-----BEGIN PRIVATE_KEY-----\nc2VjcmV0\n-----END PRIVATE_KEY-----\n"
       "-----BEGIN CERTIFICATE-----\n" + base64.b64encode(DER).decode() +
       "\n-----END CERTIFICATE-----\n")
FP = ":".join(hashlib.sha256(DER).hexdigest()[i:i + 2] for i in range(0, 64, 2))


class DesktopTests(unittest.TestCase):
    def test_fingerprint_hashes_certificate_not_key(self):
        self.assertEqual(pem_fingerprint(PEM), FP)
        with self.assertRaises(ValueError):
            pem_fingerprint("-----BEGIN PRIVATE_KEY-----\nc2VjcmV0\n-----END PRIVATE_KEY-----\n")

    def test_config_trusts_only_peer(self):
        text = lan_mouse_config("archmain", ["172.1.42.215"], "right", FP)
        self.assertIn(f'"{FP}" = "archmain"', text)
        self.assertIn('position = "right"', text)
        self.assertIn('ips = [ "172.1.42.215" ]', text)
        self.assertNotIn("capture_backend", text)
        text = lan_mouse_config("dell", ["1.2.3.4"], "left", FP, capture_backend="layer-shell")
        self.assertTrue(text.splitlines()[1] == 'capture_backend = "layer-shell"')

    def test_config_rejects_injection(self):
        for args in (("bad\"name", ["1.2.3.4"], "right", FP),
                     ("peer", ["1.2.3.4\"]"], "right", FP),
                     ("peer", ["1.2.3.4"], "middle", FP),
                     ("peer", ["1.2.3.4"], "right", "zz")):
            with self.assertRaises(ValueError):
                lan_mouse_config(*args)

    @patch("jarvis.desktop.shutil.which", return_value="ssh")
    @patch("jarvis.desktop.subprocess.run")
    def test_remote_fingerprint_validates_output(self, run, _):
        run.return_value.returncode = 0
        run.return_value.stdout = FP + "\n"
        self.assertEqual(remote_fingerprint("jdoe@host"), FP)
        cmd = run.call_args.args[0]
        self.assertIn("StrictHostKeyChecking=yes", cmd)
        self.assertNotIn("PRIVATE", run.call_args.kwargs["input"])
        run.return_value.stdout = "-----BEGIN PRIVATE_KEY-----"
        with self.assertRaises(RuntimeError):
            remote_fingerprint("jdoe@host")


if __name__ == "__main__":
    unittest.main()
