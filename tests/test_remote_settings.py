import json
import tempfile
import unittest
from pathlib import Path

from zvec_webview.remote_settings import (
    REMOTE_OWNED_CONFIG_FILE,
    RemoteSettingsError,
    RemoteSettingsStore,
)

_TOKEN = "SYNTHETIC-SECRET-TOKEN"


def _toml(
    *,
    server_addr: str = "203.0.113.7",
    server_port: str = "7000",
    token: str = _TOKEN,
    top_level: str = "",
    tables: str = "",
    proxy: str = (
        'name = "zvec-api"\n'
        'type = "tcp"\n'
        'localIP = "192.168.10.136"\n'
        "localPort = 38522\n"
        "remotePort = 38522\n"
    ),
) -> str:
    return (
        f'serverAddr = "{server_addr}"\n'
        f"serverPort = {server_port}\n"
        f"{top_level}"
        "[auth]\n"
        'method = "token"\n'
        f'token = "{token}"\n'
        f"{tables}"
        "[[proxies]]\n"
        f"{proxy}"
    )


class RemoteSettingsStoreTest(unittest.TestCase):
    def _store(self, root: Path) -> tuple[RemoteSettingsStore, Path]:
        home = root / "config-home"
        return RemoteSettingsStore(home), home

    def test_import_preserves_source_and_writes_owned_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, home = self._store(root)
            source = root / "frpc.toml"
            original = _toml(
                tables='[log]\nto = "file"\nlevel = "info"\n'
                '[transport]\nprotocol = "tcp"\npoolCount = 3\n'
            )
            source.write_text(original, encoding="utf-8")
            before = source.read_bytes()

            config = store.import_toml(source)

            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(config.server_addr, "203.0.113.7")
            self.assertEqual(config.server_port, 7000)
            self.assertEqual(config.remote_port, 38522)
            self.assertEqual(config.public_origin(), "http://203.0.113.7:38522")
            owned = json.loads((home / REMOTE_OWNED_CONFIG_FILE).read_text("utf-8"))
            self.assertEqual(owned["serverAddr"], "203.0.113.7")
            self.assertEqual(owned["serverPort"], 7000)
            self.assertFalse(owned["loginFailExit"])
            self.assertEqual(owned["log"], {"to": "console", "level": "info"})
            self.assertEqual(owned["auth"], {"method": "token", "token": _TOKEN})
            self.assertEqual(owned["transport"]["poolCount"], 3)
            self.assertEqual(owned["proxies"][0]["localIP"], "192.168.10.136")
            self.assertEqual(owned["proxies"][0]["localPort"], 38522)
            self.assertEqual(owned["proxies"][0]["remotePort"], 38522)

    def test_owned_config_reloads_and_preferences_persist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, home = self._store(root)
            source = root / "frpc.toml"
            source.write_text(_toml(), encoding="utf-8")
            store.import_toml(source)
            self.assertFalse(store.load_preferences().enabled)
            store.save_preferences(True)

            reloaded = RemoteSettingsStore(home)
            self.assertTrue(reloaded.load_preferences().enabled)
            config = reloaded.load_config()
            self.assertIsNotNone(config)
            assert config is not None
            self.assertEqual(config.proxy_name, "zvec-api")
            self.assertEqual(config.remote_port, 38522)
            self.assertEqual(config.token, _TOKEN)

    def test_runtime_config_overrides_listener_address(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, home = self._store(root)
            source = root / "frpc.toml"
            source.write_text(_toml(), encoding="utf-8")
            config = store.import_toml(source)

            store.write_config(config, local_host="10.0.0.9", local_port=38522)

            owned = json.loads((home / REMOTE_OWNED_CONFIG_FILE).read_text("utf-8"))
            self.assertEqual(owned["proxies"][0]["localIP"], "10.0.0.9")
            self.assertEqual(owned["proxies"][0]["localPort"], 38522)
            self.assertEqual(store.load_config().local_ip, "10.0.0.9")

    def test_missing_file_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store, _ = self._store(Path(temporary))
            with self.assertRaises(RemoteSettingsError) as raised:
                store.import_toml(Path(temporary) / "missing.toml")
            self.assertEqual(raised.exception.code, "remote_config_invalid")

    def test_malformed_toml_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _ = self._store(root)
            source = root / "frpc.toml"
            source.write_text("serverAddr = \n", encoding="utf-8")
            with self.assertRaises(RemoteSettingsError) as raised:
                store.import_toml(source)
            self.assertEqual(raised.exception.code, "remote_config_invalid")

    def test_boolean_port_is_rejected(self) -> None:
        self._reject(_toml(server_port="true"), "serverPort")

    def test_out_of_range_port_is_rejected(self) -> None:
        self._reject(_toml(server_port="70000"), "serverPort")
        self._reject(
            _toml(
                proxy=(
                    'name = "zvec-api"\n'
                    'type = "tcp"\n'
                    "localPort = 38522\n"
                    "remotePort = 0\n"
                )
            ),
            "remotePort",
        )

    def test_multiple_proxies_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _ = self._store(root)
            source = root / "frpc.toml"
            source.write_text(
                _toml() + '[[proxies]]\nname = "second"\ntype = "tcp"\n'
                "localPort = 1\nremotePort = 2\n",
                encoding="utf-8",
            )
            with self.assertRaises(RemoteSettingsError) as raised:
                store.import_toml(source)
            self.assertEqual(raised.exception.code, "remote_config_unsupported")

    def test_includes_visitors_and_plugins_are_rejected(self) -> None:
        self._reject(_toml(top_level='includes = ["other.toml"]\n'), "includes")
        self._reject(_toml(top_level="visitors = []\n"), "visitors")
        self._reject(
            _toml(
                proxy=(
                    'name = "zvec-api"\n'
                    'type = "tcp"\n'
                    "localPort = 38522\n"
                    "remotePort = 38522\n"
                    'plugin = "unix_domain_socket"\n'
                )
            ),
            "插件",
        )

    def test_non_ipv4_server_address_is_rejected(self) -> None:
        self._reject(_toml(server_addr="frps.example.com"), "serverAddr")

    def test_relative_certificate_path_is_rejected(self) -> None:
        self._reject(
            _toml(
                tables='[transport.tls]\ncertFile = "./client.crt"\n',
            ),
            "证书",
        )

    def test_non_token_auth_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _ = self._store(root)
            source = root / "frpc.toml"
            source.write_text(
                _toml().replace('method = "token"', 'method = "oidc"'),
                encoding="utf-8",
            )
            with self.assertRaises(RemoteSettingsError):
                store.import_toml(source)

    def test_missing_token_is_rejected(self) -> None:
        self._reject(_toml(token=""), "token")

    def test_errors_never_leak_the_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _ = self._store(root)
            source = root / "frpc.toml"
            # A valid token sits next to an unsupported field.
            source.write_text(_toml(top_level="unknownField = 1\n"), encoding="utf-8")
            with self.assertRaises(RemoteSettingsError) as raised:
                store.import_toml(source)
            self.assertNotIn(_TOKEN, str(raised.exception))
            self.assertNotIn(_TOKEN, raised.exception.message)

    def _reject(self, content: str, fragment: str) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store, _ = self._store(root)
            source = root / "frpc.toml"
            source.write_text(content, encoding="utf-8")
            with self.assertRaises(RemoteSettingsError) as raised:
                store.import_toml(source)
            self.assertIn(fragment, raised.exception.message)
            self.assertNotIn(_TOKEN, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
