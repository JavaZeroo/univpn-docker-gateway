import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from univpn_gateway import gateway


class FirewallTests(unittest.TestCase):
    def test_restore_preserves_unrelated_chains_and_final_drop(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        config = json.loads(Path("examples/vpn.json").read_text())
        with patch("subprocess.run", side_effect=fake_run):
            gateway.enable(config)
        restores = [(argv, kwargs["input"]) for argv, kwargs in calls
                    if argv[0] == "iptables-restore"]
        self.assertEqual(2, len(restores))
        for argv, rules in restores:
            self.assertIn("--noflush", argv)
            self.assertNotIn("-F FORWARD", rules)
            self.assertIn("-A UVPN_IN -j DROP\n", rules)
            self.assertIn("-A UVPN_OUT -j DROP\n", rules)
        self.assertIn("-s 10.203.0.2/32 -d 10.80.0.11/32 -o cnem_vnic -j ACCEPT",
                      restores[-1][1])
        self.assertTrue(any("MASQUERADE" in argv and "cnem_vnic" in argv
                            for argv, _ in calls))


if __name__ == "__main__":
    unittest.main()
