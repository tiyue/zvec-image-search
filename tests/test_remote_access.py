import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from zvec_webview.remote_access import (
    RemoteAccessController,
    RemoteAccessError,
)
from zvec_webview.remote_settings import RemoteSettingsStore

_TOKEN = "SYNTHETIC-SECRET-TOKEN"
_TOML = """
serverAddr = "203.0.113.7"
serverPort = 7000

[auth]
method = "token"
token = "SYNTHETIC-SECRET-TOKEN"

[[proxies]]
name = "zvec-api"
type = "tcp"
localIP = "192.168.10.136"
localPort = 38522
remotePort = 38522
"""


class _FakeStream:
    def __init__(self, lines: list[str], gate: threading.Event) -> None:
        self._lines = list(lines)
        self._gate = gate

    def __iter__(self):
        yield from self._lines
        self._gate.wait(5.0)

    def close(self) -> None:
        return None


class _FakeProcess:
    def __init__(self, lines: list[str], *, block: bool = True) -> None:
        self._gate = threading.Event()
        if not block:
            self._gate.set()
        self._stream = _FakeStream(lines, self._gate)
        self.terminated = False
        self.killed = False

    @property
    def stdout(self) -> _FakeStream:
        return self._stream

    def wait(self, timeout: float | None = None) -> int:
        self._gate.wait(timeout if timeout is not None else 5.0)
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self._gate.set()

    def kill(self) -> None:
        self.killed = True
        self._gate.set()


class _RecordingFactory:
    def __init__(self, lines: list[str], *, block: bool = True) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.processes: list[_FakeProcess] = []
        self._lines = lines
        self._block = block

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), dict(kwargs)))
        process = _FakeProcess(self._lines, block=self._block)
        self.processes.append(process)
        return process


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class RemoteAccessControllerTest(unittest.TestCase):
    def _home(self, root: Path) -> Path:
        home = root / "config-home"
        store = RemoteSettingsStore(home)
        source = root / "frpc.toml"
        source.write_text(_TOML, encoding="utf-8")
        store.import_toml(source)
        return home

    def _executable(self, root: Path) -> Path:
        executable = root / "frpc.exe"
        executable.write_bytes(b"binary")
        return executable

    def _controller(
        self,
        root: Path,
        factory,
        *,
        endpoint=None,
        retry_delays=(0.05,),
    ) -> RemoteAccessController:
        home = self._home(root)
        return RemoteAccessController(
            home,
            lan_endpoint=endpoint or (lambda: ("192.168.10.136", 38522)),
            executable=lambda: self._executable(root),
            popen_factory=factory,
            retry_delays=retry_delays,
        )

    def test_connected_only_after_proxy_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            factory = _RecordingFactory(
                ["login to server success\n", "[zvec-api] start proxy success\n"]
            )
            controller = self._controller(root, factory)

            controller.start()
            self.assertTrue(
                _wait_for(lambda: controller.status()["state"] == "connected")
            )

            self.assertEqual(len(factory.calls), 1)
            args, kwargs = factory.calls[0]
            self.assertEqual(args[1], "-c")
            self.assertTrue(args[2].endswith("frpc-owned.json"))
            self.assertIs(kwargs["shell"], False)
            self.assertEqual(
                kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            controller.stop()

    def test_login_success_alone_is_not_connected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            factory = _RecordingFactory(["login to server success\n"])
            controller = self._controller(Path(temporary), factory)

            controller.start()
            self.assertTrue(
                _wait_for(lambda: controller.status()["state"] == "connecting")
            )
            time.sleep(0.05)
            self.assertEqual(controller.status()["state"], "connecting")
            controller.stop()

    def test_registration_failure_and_disconnect_are_recognized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            factory = _RecordingFactory(
                ["[zvec-api] start proxy error: proxy already in use\n"]
            )
            controller = self._controller(Path(temporary), factory)
            controller.start()
            self.assertTrue(
                _wait_for(lambda: controller.status()["state"] == "reconnecting")
            )
            controller.stop()

        with tempfile.TemporaryDirectory() as temporary:
            factory = _RecordingFactory(["connection to server closed\n"])
            controller = self._controller(Path(temporary), factory)
            controller.start()
            self.assertTrue(
                _wait_for(lambda: controller.status()["state"] == "reconnecting")
            )
            controller.stop()

    def test_retry_backoff_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            factory = _RecordingFactory([])
            controller = RemoteAccessController(
                self._home(Path(temporary)),
                lan_endpoint=lambda: ("192.168.10.136", 38522),
                executable=lambda: self._executable(Path(temporary)),
                popen_factory=factory,
            )
            delays = [controller._next_delay() for _ in range(7)]
            self.assertEqual(delays, [1.0, 2.0, 5.0, 10.0, 30.0, 30.0, 30.0])

    def test_pinned_frpc_0611_real_failure_logs_clear_connected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = self._controller(Path(temporary), _RecordingFactory([]))
            for line in (
                "[zvec-api] start error: port already used",
                "connect to server error: EOF",
                "heartbeat timeout",
                "try to connect to server...",
            ):
                with self.subTest(line=line):
                    controller._handle_line(
                        "[zvec-api] start proxy success", "zvec-api"
                    )
                    controller._handle_line(line, "zvec-api")
                    self.assertEqual(controller.status()["state"], "reconnecting")

    def test_child_exit_retries_without_tight_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            factory = _RecordingFactory([], block=False)
            controller = self._controller(
                Path(temporary), factory, retry_delays=(0.05,)
            )

            controller.start()
            self.assertTrue(_wait_for(lambda: len(factory.calls) >= 3, timeout=2.0))
            controller.stop()
            settled = len(factory.calls)
            time.sleep(0.2)
            self.assertEqual(len(factory.calls), settled)
            self.assertFalse(controller.is_active())

    def test_stop_terminates_only_owned_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            factory = _RecordingFactory(["[zvec-api] start proxy success\n"])
            controller = self._controller(Path(temporary), factory)
            controller.start()
            self.assertTrue(
                _wait_for(lambda: controller.status()["state"] == "connected")
            )

            controller.stop()

            self.assertTrue(factory.processes[0].terminated)
            self.assertEqual(controller.status()["enabled"], False)
            self.assertFalse(controller.is_active())

    def test_close_during_startup_joins_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            factory = _RecordingFactory(["[zvec-api] start proxy success\n"])
            controller = self._controller(Path(temporary), factory)
            controller.start()

            started = time.monotonic()
            controller.close()

            self.assertLess(time.monotonic() - started, 8.0)
            self.assertFalse(controller.is_active())
            # Full close stops the owned child but keeps the persisted choice.
            self.assertTrue(self._preference(controller))

    def test_remote_port_mismatch_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            factory = _RecordingFactory(["[zvec-api] start proxy success\n"])
            controller = self._controller(
                Path(temporary),
                factory,
                endpoint=lambda: ("192.168.10.136", 40000),
            )
            controller.start()
            self.assertTrue(_wait_for(lambda: controller.status()["state"] == "error"))
            status = controller.status()
            self.assertIn("端口", status["message"])
            self.assertEqual(factory.calls, [])

    def test_listener_override_rewrites_runtime_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            factory = _RecordingFactory(["[zvec-api] start proxy success\n"])
            controller = self._controller(
                root,
                factory,
                endpoint=lambda: ("10.0.0.9", 38522),
            )
            controller.start()
            self.assertTrue(
                _wait_for(lambda: controller.status()["state"] == "connected")
            )
            owned = json.loads(
                (root / "config-home" / "frpc-owned.json").read_text("utf-8")
            )
            self.assertEqual(owned["proxies"][0]["localIP"], "10.0.0.9")
            self.assertEqual(owned["proxies"][0]["localPort"], 38522)
            self.assertEqual(owned["proxies"][0]["remotePort"], 38522)
            controller.stop()

    def test_public_url_available_while_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            factory = _RecordingFactory([])
            controller = self._controller(Path(temporary), factory)

            status = controller.status()

            self.assertTrue(status["configured"])
            self.assertFalse(status["enabled"])
            self.assertEqual(status["state"], "disabled")
            self.assertEqual(status["public_url"], "http://203.0.113.7:38522")
            self.assertEqual(controller.public_url, "http://203.0.113.7:38522")

    def test_status_and_errors_never_expose_the_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            factory = _RecordingFactory(["[zvec-api] start proxy success\n"])
            controller = self._controller(Path(temporary), factory)
            controller.start()
            self.assertTrue(
                _wait_for(lambda: controller.status()["state"] == "connected")
            )
            self.assertNotIn(_TOKEN, json.dumps(controller.status()))
            controller.stop()
            self.assertNotIn(_TOKEN, json.dumps(controller.status()))

    def test_unconfigured_start_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            controller = RemoteAccessController(
                Path(temporary) / "config-home",
                lan_endpoint=lambda: ("192.168.10.136", 38522),
                popen_factory=_RecordingFactory([]),
                retry_delays=(0.05,),
            )
            with self.assertRaises(RemoteAccessError) as raised:
                controller.start()
            self.assertEqual(raised.exception.code, "remote_not_configured")
            self.assertEqual(controller.status()["state"], "unconfigured")
            self.assertFalse(controller.status()["configured"])

    def test_invalid_import_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            factory = _RecordingFactory([])
            controller = RemoteAccessController(
                root / "config-home",
                lan_endpoint=lambda: ("192.168.10.136", 38522),
                popen_factory=factory,
                retry_delays=(0.05,),
            )
            broken = root / "broken.toml"
            broken.write_text("serverAddr = \n", encoding="utf-8")
            with self.assertRaises(RemoteAccessError) as raised:
                controller.import_config(broken)
            self.assertEqual(raised.exception.code, "remote_config_invalid")
            self.assertEqual(factory.calls, [])
            self.assertFalse(controller.is_active())

    def test_import_while_enabled_restarts_the_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            factory = _RecordingFactory(["[zvec-api] start proxy success\n"])
            controller = self._controller(root, factory)
            controller.start()
            self.assertTrue(
                _wait_for(lambda: controller.status()["state"] == "connected")
            )
            self.assertTrue(controller.preference_enabled())

            replacement = root / "replacement.toml"
            replacement.write_text(_TOML, encoding="utf-8")
            controller.import_config(replacement)

            self.assertTrue(controller.preference_enabled())
            self.assertTrue(
                _wait_for(
                    lambda: (
                        len(factory.calls) >= 2
                        and controller.status()["state"] == "connected"
                    )
                )
            )
            controller.stop()

    @staticmethod
    def _preference(controller: RemoteAccessController) -> bool:
        return controller.preference_enabled()


class _FakeLanController:
    def __init__(
        self,
        *,
        running: bool = True,
        enabled: bool = True,
        listener: tuple[str, int] = ("192.168.10.136", 38522),
    ) -> None:
        self.running = running
        self.enabled = enabled
        self.listener = listener
        self.calls: list[object] = []

    def status(self) -> dict[str, object]:
        return {"running": self.running, "enabled": self.enabled}

    def start(self) -> dict[str, object]:
        self.calls.append("start")
        self.running = True
        return self.status()

    def running_listener(self) -> tuple[str, int] | None:
        return self.listener if self.running else None

    def update(self, payload) -> dict[str, object]:
        self.calls.append(("update", dict(payload)))
        return self.status()

    def stop(self) -> dict[str, object]:
        self.calls.append("stop")
        self.running = False
        return self.status()

    def close(self) -> None:
        self.calls.append("close")


class _FakeRemoteController:
    def __init__(self, *, configured: bool = True, enabled: bool = False) -> None:
        self.configured = configured
        self.enabled = enabled
        self.calls: list[object] = []

    @property
    def public_url(self) -> str:
        return "http://203.0.113.7:38522" if self.configured else ""

    def status(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "enabled": self.enabled,
            "state": "disabled",
            "message": "",
            "public_url": self.public_url,
        }

    def start(self) -> dict[str, object]:
        self.calls.append("start")
        self.enabled = True
        return self.status()

    def stop(self) -> dict[str, object]:
        self.calls.append("stop")
        self.enabled = False
        return self.status()

    def set_enabled(self, enabled) -> dict[str, object]:
        self.calls.append(("set_enabled", enabled))
        self.enabled = bool(enabled)
        return self.status()

    def pause(self) -> None:
        self.calls.append("pause")

    def restart_if_enabled(self) -> None:
        self.calls.append("restart_if_enabled")

    def import_config(self, source_path) -> dict[str, object]:
        self.calls.append(("import_config", source_path))
        return self.status()

    def start_if_enabled(self) -> None:
        self.calls.append("start_if_enabled")

    def close(self) -> None:
        self.calls.append("close")


class _IdleHost:
    is_running = False

    def stop(self, *, force: bool = False) -> None:
        del force


class FacadeRemoteWiringTest(unittest.TestCase):
    def _facade(self, root: Path, lan, remote):
        from zvec_host.credentials import SessionCredentialStore
        from zvec_webview.facade import PreviewFacade

        return PreviewFacade(
            root / "config.json",
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
            lan_access_controller=lan,  # type: ignore[arg-type]
            remote_access_controller=remote,  # type: ignore[arg-type]
        )

    def test_start_remote_access_ensures_lan_then_starts(self) -> None:
        from zvec_webview.facade import FacadeError

        with tempfile.TemporaryDirectory() as temporary:
            lan = _FakeLanController(running=False, enabled=True)
            remote = _FakeRemoteController()
            facade = self._facade(Path(temporary), lan, remote)
            self.addCleanup(facade.close, force=True)

            facade.start_remote_access()

            self.assertIn("start", lan.calls)
            self.assertIn("start", remote.calls)

        with tempfile.TemporaryDirectory() as temporary:
            lan = _FakeLanController(running=False, enabled=False)
            remote = _FakeRemoteController()
            facade = self._facade(Path(temporary), lan, remote)
            self.addCleanup(facade.close, force=True)

            with self.assertRaises(FacadeError) as raised:
                facade.start_remote_access()

            self.assertEqual(raised.exception.code, "lan_access_disabled")
            self.assertNotIn("start", remote.calls)

    def test_start_remote_access_requires_configuration(self) -> None:
        from zvec_webview.facade import FacadeError

        with tempfile.TemporaryDirectory() as temporary:
            lan = _FakeLanController()
            remote = _FakeRemoteController(configured=False)
            facade = self._facade(Path(temporary), lan, remote)
            self.addCleanup(facade.close, force=True)

            with self.assertRaises(FacadeError) as raised:
                facade.start_remote_access()

            self.assertEqual(raised.exception.code, "remote_not_configured")
            self.assertNotIn("start", remote.calls)
            self.assertNotIn("start", lan.calls)

    def test_update_remote_access_toggles_the_controller(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lan = _FakeLanController(running=True, enabled=True)
            remote = _FakeRemoteController()
            facade = self._facade(Path(temporary), lan, remote)
            self.addCleanup(facade.close, force=True)

            facade.update_remote_access({"enabled": True})
            facade.update_remote_access({"enabled": False})

            self.assertIn(("set_enabled", True), remote.calls)
            self.assertIn(("set_enabled", False), remote.calls)

    def test_lan_update_pauses_then_restarts_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lan = _FakeLanController(running=True, enabled=True)
            remote = _FakeRemoteController()
            facade = self._facade(Path(temporary), lan, remote)
            self.addCleanup(facade.close, force=True)

            payload = {
                "enabled": True,
                "bind_host": "10.0.0.9",
                "port": 38522,
                "display_name": "pc",
            }
            facade.update_lan_access(payload)

            self.assertEqual(remote.calls, ["pause", "restart_if_enabled"])
            self.assertTrue(any(isinstance(call, tuple) for call in lan.calls))

    def test_lan_stop_then_start_resumes_enabled_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lan = _FakeLanController(running=True, enabled=True)
            remote = _FakeRemoteController(enabled=True)
            facade = self._facade(Path(temporary), lan, remote)
            self.addCleanup(facade.close, force=True)

            facade.stop_lan_access()
            facade.start_lan_access()

            self.assertTrue(remote.enabled)
            self.assertEqual(remote.calls, ["pause", "restart_if_enabled"])

    def test_close_closes_remote_before_lan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            order: list[str] = []
            lan = _FakeLanController()
            remote = _FakeRemoteController()
            lan.close = lambda: order.append("lan")  # type: ignore[method-assign]
            remote.close = lambda: order.append("remote")  # type: ignore[method-assign]
            facade = self._facade(Path(temporary), lan, remote)

            facade.close(force=True)

            self.assertEqual(order, ["remote", "lan"])


if __name__ == "__main__":
    unittest.main()
