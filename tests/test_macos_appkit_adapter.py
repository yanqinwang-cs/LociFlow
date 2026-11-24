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
        self.on_run = None
        self.order_outs = 0

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
        if self.on_run is not None:
            self.on_run()
        return self.response

    def orderOut_(self, _sender: object) -> None:
        self.order_outs += 1

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
        self.termination_replies: list[bool] = []
        self.termination_requests = 0
        self.last_termination_result = None
        self.modal_aborts = 0

    def setActivationPolicy_(self, _policy: object) -> None:
        pass

    def setDelegate_(self, delegate: object) -> None:
        self.delegate = delegate

    def setMainMenu_(self, menu: object) -> None:
        self.menu = menu

    def activateIgnoringOtherApps_(self, _active: bool) -> None:
        pass

    def replyToApplicationShouldTerminate_(self, allow: bool) -> None:
        self.termination_replies.append(allow)

    def terminate_(self, _sender: object) -> object:
        self.termination_requests += 1
        self.last_termination_result = self.delegate.applicationShouldTerminate_(self)
        return self.last_termination_result

    def abortModal(self) -> None:
        self.modal_aborts += 1


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
    def __init__(self) -> None:
        self.title = ""
        self.action = ""
        self.key = ""
        self.target = None
        self.modifier = None
        self.submenu = None

    def initWithTitle_action_keyEquivalent_(
        self,
        title: str,
        action: str,
        key: str,
    ):
        self.title = title
        self.action = action
        self.key = key
        return self

    def setSubmenu_(self, menu: object) -> None:
        self.submenu = menu

    def setTarget_(self, target: object) -> None:
        self.target = target

    def setKeyEquivalentModifierMask_(self, modifier: object) -> None:
        self.modifier = modifier

    def activate(self) -> object:
        if self.action != "terminate:" or self.target is None:
            raise AssertionError("Quit action is not wired to NSApplication")
        return self.target.terminate_(self)


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


class _ContentView:
    def __init__(self) -> None:
        self.subviews: list[object] = []

    def addSubview_(self, view: object) -> None:
        self.subviews.append(view)


class _FinishingPanel(_Allocated):
    created: list["_FinishingPanel"] = []

    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.content = _ContentView()
        self.visible = False
        self.closed = False

    def initWithContentRect_styleMask_backing_defer_(
        self, rect: object, style: object, backing: object, deferred: bool
    ):
        self.values.update(
            rect=rect,
            style=style,
            backing=backing,
            deferred=deferred,
        )
        self.created.append(self)
        return self

    def setTitle_(self, value: str) -> None:
        self.values["title"] = value

    def setReleasedWhenClosed_(self, value: bool) -> None:
        self.values["released"] = value

    def setHidesOnDeactivate_(self, value: bool) -> None:
        self.values["hides"] = value

    def contentView(self) -> _ContentView:
        return self.content

    def center(self) -> None:
        self.values["centered"] = True

    def makeKeyAndOrderFront_(self, _sender: object) -> None:
        self.visible = True

    def orderOut_(self, _sender: object) -> None:
        self.visible = False

    def close(self) -> None:
        self.closed = True


class _TextLabel:
    def __init__(self, text: str) -> None:
        self.text = text
        self.values: dict[str, object] = {}

    def setFrame_(self, value: object) -> None:
        self.values["frame"] = value

    def setAlignment_(self, value: object) -> None:
        self.values["alignment"] = value

    def setAccessibilityLabel_(self, value: str) -> None:
        self.values["accessibility"] = value


class _TextField:
    labels: list[_TextLabel] = []

    @classmethod
    def labelWithString_(cls, text: str) -> _TextLabel:
        label = _TextLabel(text)
        cls.labels.append(label)
        return label


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
        _FinishingPanel.created = []
        _TextField.labels = []
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
            NSEventModifierFlagCommand="command",
            NSMenu=_Menu,
            NSMenuItem=_MenuItem,
            NSModalResponseOK=1,
            NSOpenPanel=_OpenPanel,
            NSPanel=_FinishingPanel,
            NSTextAlignmentCenter="center",
            NSTextField=_TextField,
            NSWindowStyleMaskTitled="titled",
            NSBackingStoreBuffered="buffered",
            NSMakeRect=lambda *values: values,
            NSWorkspace=_Workspace,
            NSURL=_NSURL,
            NSObject=_NSObject,
            objc_super=super,
            NSTerminateCancel="cancel",
            NSTerminateLater="later",
            NSTerminateNow="now",
        )
        return AppKitAdapter(bindings=bindings, downloads=downloads)

    @staticmethod
    def quit_item() -> _MenuItem:
        application_item = _NSApplication.application.menu.items[0]
        return application_item.submenu.items[0]

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

    def test_external_chooser_cancellation_aborts_and_hides_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            panel = _Panel(0, [])
            adapter = self.adapter(panel, Path(directory))
            adapter.initialize()
            results: list[bool] = []

            def cancel_twice() -> None:
                results.append(adapter.cancel_active_directory_selection())
                results.append(adapter.cancel_active_directory_selection())

            panel.on_run = cancel_twice

            self.assertIsNone(adapter.choose_directory())
            self.assertEqual(results, [True, False])
            self.assertEqual(_NSApplication.application.modal_aborts, 1)
            self.assertEqual(panel.order_outs, 1)
            self.assertIsNone(adapter._active_directory_panel)
            self.assertFalse(adapter.cancel_active_directory_selection())

    def test_chooser_cancellation_rejects_non_main_thread_appkit_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            panel = _Panel(0, [])
            adapter = self.adapter(panel, Path(directory))
            adapter.initialize()
            adapter._active_directory_panel = panel
            errors: list[BaseException] = []
            worker = Thread(
                target=lambda: self._capture_error(
                    adapter.cancel_active_directory_selection,
                    errors,
                )
            )

            worker.start()
            worker.join()

            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], RuntimeError)
            self.assertEqual(_NSApplication.application.modal_aborts, 0)
            self.assertEqual(panel.order_outs, 0)

    @staticmethod
    def _capture_error(callback, errors: list[BaseException]) -> None:
        try:
            callback()
        except BaseException as error:
            errors.append(error)

    def test_quit_during_chooser_aborts_modal_without_delayed_termination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            panel = _Panel(0, [])
            adapter = self.adapter(panel, Path(directory))
            adapter.initialize()
            handler = mock.Mock(
                side_effect=adapter.cancel_active_directory_selection
            )
            adapter.set_termination_handler(handler)
            panel.on_run = lambda: _NSApplication.application.terminate_(None)

            self.assertIsNone(adapter.choose_directory())

            handler.assert_called_once_with()
            self.assertEqual(
                _NSApplication.application.last_termination_result,
                "cancel",
            )
            self.assertEqual(_NSApplication.application.modal_aborts, 1)
            self.assertEqual(panel.order_outs, 1)
            self.assertEqual(_NSApplication.application.termination_replies, [])

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

    def test_running_quit_starts_one_delayed_termination_flow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.adapter(_Panel(0, []), Path(directory))
            adapter.initialize()
            adapter.set_launch_running(True)
            handler = mock.Mock(return_value=True)
            adapter.set_termination_handler(handler)

            first = adapter.running_quit_requested()
            second = adapter.running_quit_requested()

            self.assertEqual((first, second), ("later", "later"))
            self.assertEqual(handler.call_count, 2)
            self.assertEqual(_Alert.shown, [])
            self.assertEqual(_AppHelper.stop_calls, 0)

    def test_menu_and_dock_termination_reach_the_retained_delegate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.adapter(_Panel(0, []), Path(directory))
            starts: list[str] = []

            def start_or_join_shutdown() -> bool:
                if not starts:
                    starts.append("started")
                return True

            adapter.set_termination_handler(start_or_join_shutdown)
            adapter.initialize()
            adapter.set_launch_running(True)
            app = _NSApplication.application
            quit_item = self.quit_item()

            self.assertIs(app.delegate, adapter._delegate)
            self.assertIsNotNone(app.menu)
            self.assertTrue(callable(app.delegate.applicationShouldTerminate_))
            self.assertEqual(quit_item.title, "Quit Loci Flow")
            self.assertEqual(quit_item.action, "terminate:")
            self.assertEqual(quit_item.key, "q")
            self.assertEqual(quit_item.modifier, "command")
            self.assertIs(quit_item.target, app)

            menu_result = quit_item.activate()
            dock_result = app.terminate_(None)

            self.assertEqual((menu_result, dock_result), ("later", "later"))
            self.assertEqual(app.termination_requests, 2)
            self.assertEqual(starts, ["started"])

    def test_appkit_debug_events_are_opt_in_and_contain_only_safe_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.adapter(_Panel(0, []), Path(directory))
            adapter.set_termination_handler(lambda: True)
            adapter.initialize()
            adapter.set_launch_running(True)

            self.quit_item().activate()

            self.assertFalse(hasattr(adapter, "_debug_events"))

        with tempfile.TemporaryDirectory() as directory:
            events: list[str] = []
            adapter = self.adapter(_Panel(0, []), Path(directory))
            adapter.set_diagnostic_handler(events.append)
            adapter.set_termination_handler(lambda: True)
            adapter.initialize()
            adapter.set_launch_running(True)

            self.quit_item().activate()
            adapter.reply_to_termination(True)

            self.assertEqual(
                events,
                [
                    "native_app_initialized",
                    "delegate_installed",
                    "application_menu_installed",
                    "quit_action_received",
                    "application_should_terminate",
                    "delayed_shutdown_started",
                    "appkit_termination_reply",
                ],
            )
            serialized = repr(events)
            for forbidden in ("/Users/", "/launch/", "token", "0x"):
                self.assertNotIn(forbidden, serialized)

    def test_running_quit_cancels_when_shutdown_flow_has_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.adapter(_Panel(0, []), Path(directory))
            adapter.initialize()
            adapter.set_launch_running(True)
            adapter.set_termination_handler(lambda: False)

            self.assertEqual(adapter.running_quit_requested(), "cancel")

    def test_finishing_panel_is_nonmodal_accessible_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.adapter(_Panel(0, []), Path(directory))

            adapter.show_finishing_status()
            adapter.show_finishing_status()

            self.assertEqual(len(_FinishingPanel.created), 1)
            panel = _FinishingPanel.created[0]
            self.assertTrue(panel.visible)
            self.assertEqual(panel.values["style"], "titled")
            self.assertEqual(len(_TextField.labels), 1)
            label = _TextField.labels[0]
            self.assertEqual(label.text, "Finishing the current operation…")
            self.assertEqual(label.values["accessibility"], label.text)

            adapter.close_finishing_status()

            self.assertFalse(panel.visible)
            self.assertTrue(panel.closed)

    def test_termination_reply_and_safe_failure_stay_in_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.adapter(_Panel(0, []), Path(directory))
            adapter.initialize()

            adapter.reply_to_termination(True)
            adapter.show_shutdown_failure()

            self.assertEqual(
                _NSApplication.application.termination_replies,
                [True],
            )
            self.assertEqual(
                _Alert.shown[-1][1],
                "Loci Flow couldn’t close safely. Keep the application open.",
            )

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
