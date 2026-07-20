from __future__ import annotations

import base64
import itertools
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from zvec_lan import (
    CredentialStoreError,
    InvalidPairingRequest,
    JsonCredentialStore,
    MemoryCredentialStore,
    PairingCapacityExceeded,
    PairingError,
    PairingExpired,
    PairingManager,
    PairingSecretRejected,
    PairingTransitionRejected,
)


def _secret(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode().rstrip("=")


CLIENT_SECRET = _secret(17)
OTHER_SECRET = _secret(18)
DEVICE_TOKEN = _secret(99)


class _Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _manager(
    store: MemoryCredentialStore | JsonCredentialStore | None = None,
    *,
    clock: _Clock | None = None,
) -> PairingManager:
    return PairingManager(
        store,
        clock=clock or _Clock(),
        token_factory=lambda: DEVICE_TOKEN,
        pairing_id_factory=lambda: "pairing-id-1234567890",
        code_factory=lambda: "003721",
    )


class PairingStateMachineTests(unittest.TestCase):
    def test_six_digit_code_and_five_minute_expiry(self) -> None:
        clock = _Clock()
        manager = _manager(clock=clock)

        view = manager.create_request(
            device_id="android-install-1",
            device_name="Pixel Tablet",
            client_secret=CLIENT_SECRET,
        )

        self.assertEqual(view.comparison_code, "003721")
        self.assertEqual(view.status, "pending")
        self.assertEqual(view.expires_at - view.created_at, 300)
        self.assertEqual(view.expires_in_seconds(clock()), 300)

    def test_approval_returns_token_exactly_once_even_with_concurrent_polls(
        self,
    ) -> None:
        manager = _manager()
        view = manager.create_request(
            device_id="android-install-1",
            device_name="Pixel Tablet",
            client_secret=CLIENT_SECRET,
        )
        manager.approve(view.pairing_id)
        barrier = threading.Barrier(12)

        def poll() -> str | None:
            barrier.wait()
            return manager.poll(
                view.pairing_id,
                client_secret=CLIENT_SECRET,
            ).token

        with ThreadPoolExecutor(max_workers=12) as executor:
            tokens = list(executor.map(lambda _index: poll(), range(12)))

        self.assertEqual(tokens.count(DEVICE_TOKEN), 1)
        self.assertEqual(tokens.count(None), 11)
        self.assertIsNotNone(manager.authenticate(DEVICE_TOKEN))

    def test_secret_and_token_are_never_retained_in_plaintext(self) -> None:
        store = MemoryCredentialStore()
        manager = _manager(store)
        view = manager.create_request(
            device_id="android-install-1",
            device_name="Pixel Tablet",
            client_secret=CLIENT_SECRET,
        )
        self.assertNotIn(CLIENT_SECRET, repr(manager.__dict__))

        manager.approve(view.pairing_id)
        token = manager.poll(view.pairing_id, client_secret=CLIENT_SECRET).token

        self.assertEqual(token, DEVICE_TOKEN)
        records = store.list_clients()
        self.assertEqual(len(records), 1)
        self.assertNotIn(CLIENT_SECRET, repr(records))
        self.assertNotIn(DEVICE_TOKEN, repr(records))
        self.assertEqual(len(records[0].token_hash), 64)
        self.assertEqual(len(records[0].client_secret_hash), 64)

    def test_reject_wrong_secret_and_expiry_are_distinct_states(self) -> None:
        clock = _Clock()
        manager = _manager(clock=clock)
        view = manager.create_request(
            device_id="android-install-1",
            device_name="Pixel Tablet",
            client_secret=CLIENT_SECRET,
        )

        with self.assertRaises(PairingSecretRejected):
            manager.poll(view.pairing_id, client_secret=OTHER_SECRET)
        manager.reject(view.pairing_id)
        self.assertEqual(
            manager.poll(view.pairing_id, client_secret=CLIENT_SECRET).status,
            "rejected",
        )
        with self.assertRaises(PairingTransitionRejected):
            manager.approve(view.pairing_id)

        second = PairingManager(
            clock=clock,
            pairing_id_factory=lambda: "pairing-id-abcdefghij",
            code_factory=lambda: "123456",
        ).create_request(
            device_id="android-install-2",
            device_name="Phone",
            client_secret=CLIENT_SECRET,
        )
        expiring_manager = PairingManager(
            clock=clock,
            pairing_id_factory=lambda: second.pairing_id,
            code_factory=lambda: second.comparison_code,
        )
        expiring = expiring_manager.create_request(
            device_id="android-install-2",
            device_name="Phone",
            client_secret=CLIENT_SECRET,
        )
        clock.now += 300
        with self.assertRaises(PairingExpired):
            expiring_manager.approve(expiring.pairing_id)
        with self.assertRaises(PairingExpired):
            expiring_manager.poll(expiring.pairing_id, client_secret=CLIENT_SECRET)

    def test_client_secret_must_encode_exactly_256_bits(self) -> None:
        manager = _manager()

        for invalid in ("", "plain-text", _secret(1)[:-3]):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(InvalidPairingRequest),
            ):
                manager.create_request(
                    device_id="android-install-1",
                    device_name="Pixel Tablet",
                    client_secret=invalid,
                )

    def test_pending_capacity_is_bounded_and_expired_records_are_pruned(self) -> None:
        clock = _Clock()
        ids = itertools.count()
        codes = itertools.count()
        manager = PairingManager(
            clock=clock,
            pairing_id_factory=lambda: f"pairing-id-{next(ids):020d}",
            code_factory=lambda: f"{next(codes):06d}",
        )
        for index in range(32):
            manager.create_request(
                device_id=f"android-install-{index}",
                device_name=f"Device {index}",
                client_secret=CLIENT_SECRET,
            )

        with self.assertRaises(PairingCapacityExceeded):
            manager.create_request(
                device_id="android-install-overflow",
                device_name="Overflow",
                client_secret=CLIENT_SECRET,
            )

        clock.now += 300
        replacement = manager.create_request(
            device_id="android-install-after-expiry",
            device_name="Replacement",
            client_secret=CLIENT_SECRET,
        )
        self.assertEqual(replacement.status, "pending")
        self.assertEqual(manager.pending_requests(), (replacement,))

    def test_local_ui_view_has_no_hashes_and_can_revoke_by_device_id(self) -> None:
        manager = _manager()
        view = manager.create_request(
            device_id="android-install-1",
            device_name="Pixel Tablet",
            client_secret=CLIENT_SECRET,
        )
        manager.approve(view.pairing_id)
        manager.poll(view.pairing_id, client_secret=CLIENT_SECRET)

        clients = manager.paired_clients()

        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].device_id, "android-install-1")
        authenticated = manager.authenticate(DEVICE_TOKEN)
        assert authenticated is not None
        self.assertEqual(clients[0].session_id, authenticated.session_id)
        self.assertNotIn(DEVICE_TOKEN, clients[0].session_id)
        self.assertFalse(hasattr(clients[0], "token_hash"))
        self.assertFalse(hasattr(clients[0], "client_secret_hash"))
        self.assertTrue(manager.revoke_client("android-install-1"))
        self.assertIsNone(manager.authenticate(DEVICE_TOKEN))
        self.assertFalse(manager.revoke_client("android-install-1"))

    def test_repairing_same_device_gets_a_new_internal_session_scope(self) -> None:
        tokens = iter((_secret(70), _secret(71)))
        pairing_ids = iter(("pairing-id-first-12345", "pairing-id-second-1234"))
        codes = iter(("111111", "222222"))
        manager = PairingManager(
            token_factory=lambda: next(tokens),
            pairing_id_factory=lambda: next(pairing_ids),
            code_factory=lambda: next(codes),
        )

        first = manager.create_request(
            device_id="android-install-1",
            device_name="Pixel Tablet",
            client_secret=CLIENT_SECRET,
        )
        manager.approve(first.pairing_id)
        first_token = manager.poll(
            first.pairing_id,
            client_secret=CLIENT_SECRET,
        ).token
        assert first_token is not None
        first_client = manager.authenticate(first_token)
        assert first_client is not None

        second = manager.create_request(
            device_id="android-install-1",
            device_name="Pixel Tablet",
            client_secret=OTHER_SECRET,
        )
        manager.approve(second.pairing_id)
        second_token = manager.poll(
            second.pairing_id,
            client_secret=OTHER_SECRET,
        ).token
        assert second_token is not None
        second_client = manager.authenticate(second_token)
        assert second_client is not None

        self.assertNotEqual(first_client.session_id, second_client.session_id)
        self.assertIsNone(manager.authenticate(first_token))

    def test_failed_replacement_cleanup_is_retried_before_token_delivery(self) -> None:
        tokens = iter((_secret(72), _secret(73), _secret(74)))
        pairing_ids = iter(("pairing-id-first-54321", "pairing-id-second-4321"))
        codes = iter(("333333", "444444"))
        cleanup_calls: list[str] = []
        fail_cleanup = True

        def cleanup(session_id: str) -> None:
            cleanup_calls.append(session_id)
            if fail_cleanup:
                raise RuntimeError("cleanup failed")

        manager = PairingManager(
            token_factory=lambda: next(tokens),
            pairing_id_factory=lambda: next(pairing_ids),
            code_factory=lambda: next(codes),
            session_cleanup_callback=cleanup,
        )
        first = manager.create_request(
            device_id="android-install-1",
            device_name="Pixel Tablet",
            client_secret=CLIENT_SECRET,
        )
        manager.approve(first.pairing_id)
        first_token = manager.poll(
            first.pairing_id,
            client_secret=CLIENT_SECRET,
        ).token
        assert first_token is not None
        first_client = manager.authenticate(first_token)
        assert first_client is not None
        second = manager.create_request(
            device_id="android-install-1",
            device_name="Pixel Tablet",
            client_secret=OTHER_SECRET,
        )
        manager.approve(second.pairing_id)

        with self.assertRaises(PairingError):
            manager.poll(second.pairing_id, client_secret=OTHER_SECRET)
        self.assertIsNone(manager.authenticate(first_token))

        fail_cleanup = False
        replacement_token = manager.poll(
            second.pairing_id,
            client_secret=OTHER_SECRET,
        ).token
        self.assertIsNotNone(replacement_token)
        self.assertEqual(
            cleanup_calls,
            [first_client.session_id, first_client.session_id],
        )


class JsonCredentialStoreTests(unittest.TestCase):
    def test_only_hashes_are_persisted_and_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lan-devices.json"
            manager = _manager(JsonCredentialStore(path))
            view = manager.create_request(
                device_id="android-install-1",
                device_name="Pixel Tablet",
                client_secret=CLIENT_SECRET,
            )
            manager.approve(view.pairing_id)
            manager.poll(view.pairing_id, client_secret=CLIENT_SECRET)

            raw = path.read_text(encoding="utf-8")
            self.assertNotIn(CLIENT_SECRET, raw)
            self.assertNotIn(DEVICE_TOKEN, raw)
            payload = json.loads(raw)
            self.assertEqual(len(payload["clients"][0]["token_hash"]), 64)

            restarted = PairingManager(JsonCredentialStore(path))
            self.assertIsNotNone(restarted.authenticate(DEVICE_TOKEN))
            self.assertTrue(restarted.revoke_client("android-install-1"))
            self.assertEqual(json.loads(path.read_text())["clients"], [])

    def test_failed_persistent_revoke_remains_fail_closed_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "devices.json"
            store = JsonCredentialStore(path)
            manager = _manager(store)
            view = manager.create_request(
                device_id="android-install-1",
                device_name="Pixel Tablet",
                client_secret=CLIENT_SECRET,
            )
            manager.approve(view.pairing_id)
            manager.poll(view.pairing_id, client_secret=CLIENT_SECRET)

            with (
                patch.object(
                    store,
                    "_persist_locked",
                    side_effect=CredentialStoreError("disk unavailable"),
                ),
                self.assertRaises(CredentialStoreError),
            ):
                manager.revoke_client("android-install-1")

            self.assertIsNone(manager.authenticate(DEVICE_TOKEN))
            restarted = PairingManager(JsonCredentialStore(path))
            self.assertIsNone(restarted.authenticate(DEVICE_TOKEN))


if __name__ == "__main__":
    unittest.main()
