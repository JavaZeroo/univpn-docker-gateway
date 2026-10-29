import json
from pathlib import Path
import tempfile
import unittest

from univpn_gateway.config import load


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.sample = json.loads(Path("examples/vpn.json").read_text())

    def read(self, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vpn.json"
            path.write_text(json.dumps(self.sample))
            return load(path, **kwargs)

    def test_example(self):
        self.assertEqual(self.read()["port"], 443)

    def test_lite_does_not_need_or_accept_wireguard_client(self):
        self.sample["edition"] = "lite"
        with self.assertRaises(ValueError):
            self.read(edition="lite")
        del self.sample["wireguard_client"]
        self.assertEqual(self.read(edition="lite")["edition"], "lite")

    def test_image_and_configuration_edition_must_agree(self):
        self.sample["edition"] = "lite"
        with self.assertRaises(ValueError):
            self.read(edition="full")

    def test_lite_requires_real_probe(self):
        self.sample.pop("wireguard_client")
        self.sample.pop("probe")
        with self.assertRaises(ValueError):
            self.read(edition="lite")

    def test_default_route_rejected(self):
        self.sample["allowed_cidrs"] = ["0.0.0.0/0"]
        with self.assertRaises(ValueError):
            self.read()

    def test_interface_injection_rejected(self):
        self.sample["vpn_interface"] = "tun0\n-A FORWARD -j ACCEPT"
        with self.assertRaises(ValueError):
            self.read()

    def test_outside_probe_rejected(self):
        self.sample["probe"]["host"] = "1.1.1.1"
        with self.assertRaises(ValueError):
            self.read()

    def test_overlap_rejected(self):
        self.sample["wireguard_client"] = "10.80.0.11/32"
        with self.assertRaises(ValueError):
            self.read()

    def test_client_subnet_rejected(self):
        self.sample["wireguard_client"] = "10.203.0.2/24"
        with self.assertRaises(ValueError):
            self.read()

    def test_noncanonical_network_rejected(self):
        self.sample["allowed_cidrs"] = ["10.80.0.11/24"]
        with self.assertRaises(ValueError):
            self.read()


if __name__ == "__main__":
    unittest.main()
