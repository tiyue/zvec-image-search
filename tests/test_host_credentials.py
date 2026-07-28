from __future__ import annotations

import unittest

from zvec_host.credentials import (
    DEFAULT_DASHSCOPE_CREDENTIAL_TARGET,
    CredentialError,
    SessionCredentialStore,
    WindowsCredentialStore,
)


class FakeNativeCredentialApi:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.writes: list[tuple[str, str, bytes]] = []

    def read(self, target: str) -> bytes | None:
        return self.values.get(target)

    def write(self, target: str, username: str, secret: bytes) -> None:
        self.writes.append((target, username, secret))
        self.values[target] = secret

    def delete(self, target: str) -> None:
        self.values.pop(target, None)


class HostSessionCredentialStoreTest(unittest.TestCase):
    def test_session_store_is_explicitly_non_persistent(self) -> None:
        store = SessionCredentialStore()
        self.assertFalse(store.persistent)
        self.assertFalse(store.has_secret())
        store.save_secret(" sk-example ")
        self.assertEqual(store.read_secret(), "sk-example")
        store.delete_secret()
        self.assertIsNone(store.read_secret())

    def test_invalid_secret_is_rejected(self) -> None:
        store = SessionCredentialStore()
        for invalid in ("", "  ", "abc\ndef"):
            with self.subTest(invalid=repr(invalid)), self.assertRaises(ValueError):
                store.save_secret(invalid)


class HostWindowsCredentialStoreContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.native = FakeNativeCredentialApi()
        self.store = WindowsCredentialStore(native_api=self.native)

    def test_uses_same_target_as_wpf_and_round_trips_utf8(self) -> None:
        self.assertTrue(self.store.persistent)
        self.assertEqual(
            self.store.target_name,
            "Zvec.ImageSearch/DashScopeApiKey",
        )
        self.assertEqual(
            self.store.target_name,
            DEFAULT_DASHSCOPE_CREDENTIAL_TARGET,
        )
        self.store.save_secret("sk-中文-key")
        self.assertTrue(self.store.has_secret())
        self.assertEqual(self.store.read_secret(), "sk-中文-key")
        target, username, secret = self.native.writes[0]
        self.assertEqual(target, DEFAULT_DASHSCOPE_CREDENTIAL_TARGET)
        self.assertTrue(username)
        self.assertEqual(secret, "sk-中文-key".encode())

    def test_delete_is_idempotent(self) -> None:
        self.store.delete_secret()
        self.store.save_secret("sk-example")
        self.store.delete_secret()
        self.store.delete_secret()
        self.assertFalse(self.store.has_secret())

    def test_invalid_utf8_is_reported_without_returning_bytes(self) -> None:
        self.native.values[self.store.target_name] = b"\xff\xfe"
        with self.assertRaises(CredentialError):
            self.store.read_secret()

    def test_windows_blob_limit_is_checked_before_native_write(self) -> None:
        with self.assertRaisesRegex(ValueError, "大小限制"):
            self.store.save_secret("x" * 2561)
        self.assertEqual(self.native.writes, [])


if __name__ == "__main__":
    unittest.main()
