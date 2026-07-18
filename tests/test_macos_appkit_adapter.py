from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from threading import Event, Thread
import tempfile
import unittest
from unittest import mock

from organizer.macos_launcher.appkit_adapter import AppKitAdapter
from organizer.macos_launcher.coordinator import NativeMessage


class _Allocated:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self


class _NSObject(_Allocated):
    def init(self):
        return self


class _URL:
    def __init__(self, path: str) -> None:
        self._path = path

    def path(self) -> str:
        return self._path


class _NSURL:
    created_paths: list[tuple[str, bool]] = []
    browser_urls: list[str] = []

    @classmethod
    def fileURLWithPath_isDirectory_(cls, path: str, is_directory: bool):
        cls.created_paths.append((path, is_directory))
        return _URL(path)

    @classmethod
    def URLWithString_(cls, value: str):
        cls.browser_urls.append(value)
        return value


class _Panel:
    def __init__(self, response: int, urls: list[_URL]) -> None:
        self.response = response
        self.urls = urls
        self.values: dict[str, object] = {}

    def setCanChooseDirectories_(self, value: bool) -> None:
        self.values["directories"] = value

    def setCanChooseFiles_(self, value: bool) -> None:
        self.values["files"] = value

    def setAllowsMultipleSelection_(self, value: bool) -> None:
        self.values["multiple"] = value

    def setResolvesAliases_(self, value: bool) -> None:
        self.values["aliases"] = value

    def setTreatsFilePackagesAsDirectories_(self, value: bool) -> None:
        self.values["packages"] = value

    def setCanCreateDirectories_(self, value: bool) -> None:
        self.values["create"] = value

    def setPrompt_(self, value: str) -> None:
        self.values["prompt"] = value

    def setTitle_(self, value: str) -> None:
        self.values["title"] = value

    def setDirectoryURL_(self, value: _URL) -> None:
        self.values["initial"] = value.path()

    def runModal(self) -> int:
        return self.response

    def URLs(self) -> list[_URL]:
        return self.urls


class _OpenPanel:
    panel: _Panel

    @classmethod
    def openPanel(cls) -> _Panel:
        return cls.panel


class _Application:
    def __init__(self) -> None:
        self.delegate = None
        self.menu = None

    def setActivationPolicy_(self, _policy: object) -> None:
        pass

    def setDelegate_(self, delegate: object) -> None:
        self.delegate = delegate

    def setMainMenu_(self, menu: object) -> None:
        self.menu = menu

    def activateIgnoringOtherApps_(self, _active: bool) -> None:
        pass


class _NSApplication:
    application = _Application()

    @classmethod
    def sharedApplication(cls) -> _Application:
        return cls.application


class _Menu(_Allocated):
    def __init__(self) -> None:
        self.items: list[object] = []

    def initWithTitle_(self, _title: str):
        return self

    def addItem_(self, item: object) -> None:
        self.items.append(item)


class _MenuItem(_Allocated):
    def initWithTitle_action_keyEquivalent_(
        self,
        _title: str,
        _action: str,
        _key: str,
    ):
        return self

    def setSubmenu_(self, _menu: object) -> None:
        pass


class _Alert(_Allocated):
    shown: list[tuple[str, str]] = []

    def __init__(self) -> None:
        self.title = ""
        self.message = ""

    def setMessageText_(self, title: str) -> None:
        self.title = title

    def setInformativeText_(self, message: str) -> None:
        self.message = message

    def setAlertStyle_(self, _style: object) -> None:
        pass

    def runModal(self) -> None:
        self.shown.append((self.title, self.message))


class _Workspace:
    opens: list[str] = []
    result = True

    @classmethod
    def sharedWorkspace(cls):
        return cls()

    def openURL_(self, url: str) -> bool:
        self.opens.append(url)
        return self.result


class _AppHelper:
    callbacks: list[object] = []
    callback_ready = Event()
    run_calls = 0
    stop_calls = 0

    @classmethod
    def callAfter(cls, callback) -> None:
        cls.callbacks.append(callback)
        cls.callback_ready.set()

    @classmethod
    def runConsoleEventLoop(cls, **_kwargs: object) -> None:
        cls.run_calls += 1

    @classmethod
    def stopEventLoop(cls) -> None:
        cls.stop_calls += 1


class MacOSAppKitAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        _NSURL.created_paths = []
        _NSURL.browser_urls = []
        _Workspace.opens = []
        _Workspace.result = True
        _Alert.shown = []
        _AppHelper.callbacks = []
        _AppHelper.callback_ready = Event()
        _AppHelper.run_calls = 0
        _AppHelper.stop_calls = 0
        _NSApplication.application = _Application()

    def adapter(self, panel: _Panel, downloads: Path) -> AppKitAdapter:
        _OpenPanel.panel = panel
        bindings = SimpleNamespace(
            AppHelper=_AppHelper,
            NSAlert=_Alert,
            NSAlertStyleWarning=0,
            NSApplication=_NSApplication,
            NSApplicationActivationPolicyRegular=0,
            NSMenu=_Menu,
            NSMenuItem=_MenuItem,
            NSModalResponseOK=1,
            NSOpenPanel=_OpenPanel,
            NSWorkspace=_Workspace,
            NSURL=_NSURL,
            NSObject=_NSObject,
            objc_super=super,
            NSTerminateCancel="cancel",
            NSTerminateNow="now",
        )
        return AppKitAdapter(bindings=bindings, downloads=downloads)

    def test_chooser_is_one_directory_only_and_returns_raw_lexical_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            downloads = Path(directory) / "Downloads"
            downloads.mkdir()
            raw_path = str(Path(directory) / "selected-link")
            panel = _Panel(1, [_URL(raw_path)])
            adapter = self.adapter(panel, downloads)

            with mock.patch.object(Path, "resolve", autospec=True) as resolver:
                selected = adapter.choose_directory()

            self.assertEqual(selected, raw_path)
            resolver.assert_not_called()
            self.assertEqual(
                panel.values,
                {
                    "directories": True,
                    "files": False,
                    "multiple": False,
                    "aliases": False,
                    "packages": False,
                    "create": False,
                    "prompt": "Choose",
                    "title": "Choose a folder for Loci Flow",
                    "initial": str(downloads),
                },
            )
            self.assertEqual(_NSURL.created_paths, [(str(downloads), True)])

    def test_chooser_cancellation_returns_none_without_alert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.adapter(_Panel(0, []), Path(directory))

            self.assertIsNone(adapter.choose_directory())
            self.assertEqual(_Alert.shown, [])

    def test_browser_request_is_marshaled_once_and_url_is_opaque(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.adapter(_Panel(0, []), Path(directory))
            launch_url = "http://127.0.0.1:48191/launch/" + "t" * 32
            result: list[bool] = []
            worker = Thread(target=lambda: result.append(adapter.open_browser(launch_url)))

            worker.start()
            self.assertTrue(_AppHelper.callback_ready.wait(1))
            callback = _AppHelper.callbacks.pop(0)
            callback()
            worker.join()

            self.assertEqual(result, [True])
            self.assertEqual(_Workspace.opens, [launch_url])
            self.assertEqual(_NSURL.browser_urls, [launch_url])
            self.assertFalse(adapter.open_browser(launch_url))
            self.assertEqual(_Workspace.opens, [launch_url])

    def test_browser_false_result_is_returned_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.adapter(_Panel(0, []), Path(directory))
            _Workspace.result = False

            self.assertFalse(adapter.open_browser("opaque-private-url"))
            self.assertEqual(_Workspace.opens, ["opaque-private-url"])

    def test_running_quit_fails_closed_without_stopping_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.adapter(_Panel(0, []), Path(directory))
            adapter.initialize()
            adapter.set_launch_running(True)

            reply = adapter.running_quit_requested()

            self.assertEqual(reply, "cancel")
            self.assertIn("isn’t connected yet", _Alert.shown[-1][1])
            self.assertEqual(_AppHelper.stop_calls, 0)

    def test_native_delegate_initializes_through_objc_super(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.adapter(_Panel(0, []), Path(directory))

            adapter.initialize()

            self.assertIsNotNone(_NSApplication.application.delegate)

    def test_error_and_event_loop_methods_stay_in_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.adapter(_Panel(0, []), Path(directory))
            adapter.show_error(NativeMessage("Loci Flow", "Safe message"))
            adapter.run_event_loop()
            adapter.stop_event_loop()

            self.assertEqual(_Alert.shown, [("Loci Flow", "Safe message")])
            self.assertEqual(_AppHelper.run_calls, 1)
            self.assertEqual(_AppHelper.stop_calls, 1)


if __name__ == "__main__":
    unittest.main()
