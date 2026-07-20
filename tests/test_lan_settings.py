from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zvec_webview.lan_settings import (
    DEFAULT_LAN_PORT,
    LanAccessSettings,
    LanSettingsError,
    LanSettingsStore,
    available_private_ipv4_hosts,
)


class LanSettingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = LanSettingsStore(self.root)

    def test_missing_file_defaults_off_and_never_selects_loopback(self) -> None:
        with patch(
            "zvec_webview.lan_settings.available_private_ipv4_hosts",
            return_value=(),
        ):
            settings = self.store.load()

        self.assertFalse(settings.enabled)
        self.assertEqual(settings.bind_host, "")
        self.assertEqual(settings.port, DEFAULT_LAN_PORT)
        self.assertTrue(settings.display_name.startswith("Zvec on "))

    def test_round_trip_keeps_only_non_secret_listener_preferences(self) -> None:
        written = self.store.save(
            LanAccessSettings(
                enabled=True,
                bind_host="192.168.1.20",
                port=39_000,
                display_name="我的 Zvec",
            )
        )

        self.assertEqual(self.store.load(), written)
        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertNotIn("token", json.dumps(payload).lower())
        self.assertNotIn("secret", json.dumps(payload).lower())

    def test_enabled_listener_rejects_public_loopback_and_empty_hosts(self) -> None:
        for host in (
            "",
            "127.0.0.1",
            "8.8.8.8",
            "169.254.1.2",
            "192.0.0.1",
            "198.18.0.1",
            "::1",
        ):
            with self.subTest(host=host), self.assertRaises(LanSettingsError):
                LanAccessSettings(enabled=True, bind_host=host).normalized()

    def test_unknown_fields_fail_closed(self) -> None:
        self.store.path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "enabled": False,
                    "bind_host": "",
                    "port": DEFAULT_LAN_PORT,
                    "display_name": "Zvec",
                    "bearer_token": "must-not-be-accepted",
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(LanSettingsError, "unsupported fields"):
            self.store.load()

    def test_private_host_discovery_deduplicates_and_filters(self) -> None:
        values = [
            (2, 1, 6, "", ("192.168.1.20", 0)),
            (2, 1, 6, "", ("127.0.0.1", 0)),
            (2, 1, 6, "", ("192.168.1.20", 0)),
            (2, 1, 6, "", ("10.0.0.5", 0)),
        ]
        with (
            patch("zvec_webview.lan_settings.socket.gethostname", return_value="pc"),
            patch("zvec_webview.lan_settings.platform.node", return_value="pc"),
            patch("zvec_webview.lan_settings.socket.getaddrinfo", return_value=values),
        ):
            hosts = available_private_ipv4_hosts()

        self.assertEqual([item.address for item in hosts], ["10.0.0.5", "192.168.1.20"])


if __name__ == "__main__":
    unittest.main()
