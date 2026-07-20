from __future__ import annotations

import json
import socket
import unittest

from zvec_lan import (
    DISCOVERY_REQUEST_PREFIX,
    DiscoveryError,
    DiscoveryServer,
    parse_discovery_request,
)


class DiscoveryParsingTests(unittest.TestCase):
    def test_valid_nonce_is_echo_ready(self) -> None:
        payload = f"{DISCOVERY_REQUEST_PREFIX}android_nonce-123".encode()

        self.assertEqual(parse_discovery_request(payload), "android_nonce-123")

    def test_malformed_datagrams_are_ignored(self) -> None:
        invalid = (
            b"",
            b"ZVEC_LAN_DISCOVER/2 nonce",
            b"ZVEC_LAN_DISCOVER/1 ",
            b"ZVEC_LAN_DISCOVER/1 bad\nnonce",
            b"ZVEC_LAN_DISCOVER/1 \xff",
            f"{DISCOVERY_REQUEST_PREFIX}{'x' * 513}".encode(),
        )

        for payload in invalid:
            with self.subTest(payload=payload[:40]):
                self.assertIsNone(parse_discovery_request(payload))


class DiscoveryServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = DiscoveryServer(
            instance_id="stable-instance-id-1234567890",
            name="Zvec on TEST-PC",
            advertised_host="192.168.1.20",
            api_port=38522,
            bind_host="127.0.0.1",
            port=0,
        )

    def tearDown(self) -> None:
        self.server.stop()

    def test_live_udp_response_is_compact_and_echoes_nonce(self) -> None:
        address = self.server.start()
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(1.0)
        self.addCleanup(client.close)

        client.sendto(
            f"{DISCOVERY_REQUEST_PREFIX}nonce-abc".encode(),
            ("127.0.0.1", address.port),
        )
        raw, _peer = client.recvfrom(4096)

        self.assertNotIn(b": ", raw)
        self.assertEqual(
            json.loads(raw),
            {
                "protocol": 1,
                "nonce": "nonce-abc",
                "instance_id": "stable-instance-id-1234567890",
                "name": "Zvec on TEST-PC",
                "host": "192.168.1.20",
                "port": 38522,
            },
        )

    def test_invalid_probe_gets_no_response_and_server_keeps_running(self) -> None:
        address = self.server.start()
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(0.15)
        self.addCleanup(client.close)

        client.sendto(b"not-zvec", ("127.0.0.1", address.port))
        with self.assertRaises(TimeoutError):
            client.recvfrom(4096)
        self.assertTrue(self.server.is_running)

    def test_start_and_stop_are_idempotent_and_restartable(self) -> None:
        first = self.server.start()
        self.assertEqual(self.server.start(), first)

        self.server.stop()
        self.server.stop()
        self.assertFalse(self.server.is_running)
        with self.assertRaises(DiscoveryError):
            _address = self.server.address

        restarted = self.server.start()
        self.assertGreater(restarted.port, 0)
        self.assertTrue(self.server.is_running)


if __name__ == "__main__":
    unittest.main()
