from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, current_thread, main_thread
from typing import Any

from organizer.macos_launcher.coordinator import NativeAdapterError, NativeMessage


@dataclass(frozen=True)
class _AppKitBindings:
    AppHelper: Any
    NSAlert: Any
    NSAlertStyleWarning: Any
    NSApplication: Any
    NSApplicationActivationPolicyRegular: Any
    NSMenu: Any
    NSMenuItem: Any
    NSModalResponseOK: Any
    NSOpenPanel: Any
    NSWorkspace: Any
    NSURL: Any
    NSObject: Any
    objc_super: Any
    NSTerminateCancel: Any
    NSTerminateNow: Any


class AppKitAdapter:
    def __init__(
        self,
        *,
        bindings: _AppKitBindings | None = None,
        downloads: Path | None = None,
    ) -> None:
        self._bindings = bindings or _load_bindings()
        self._downloads = downloads or Path.home() / "Downloads"
        self._app: Any = None
        self._delegate: Any = None
        self._launch_running = False
        self._browser_requested = False
        self._browser_lock = Lock()

    def initialize(self) -> None:
        self._require_main_thread()
        bindings = self._bindings
        self._app = bindings.NSApplication.sharedApplication()
        self._app.setActivationPolicy_(
            bindings.NSApplicationActivationPolicyRegular
        )
        self._delegate = _delegate_type(bindings).alloc().initWithAdapter_(self)
        self._app.setDelegate_(self._delegate)
        self._install_application_menu()
        self._app.activateIgnoringOtherApps_(True)

    def choose_directory(self) -> str | None:
        self._require_main_thread()
        bindings = self._bindings
        panel = bindings.NSOpenPanel.openPanel()
        panel.setCanChooseDirectories_(True)
        panel.setCanChooseFiles_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setResolvesAliases_(False)
        panel.setTreatsFilePackagesAsDirectories_(False)
        panel.setCanCreateDirectories_(False)
        panel.setPrompt_("Choose")
        panel.setTitle_("Choose a folder for Loci Flow")
        if self._downloads.exists() and self._downloads.is_dir():
            panel.setDirectoryURL_(
                bindings.NSURL.fileURLWithPath_isDirectory_(
                    str(self._downloads),
                    True,
                )
            )

        if panel.runModal() != bindings.NSModalResponseOK:
            return None
        selected_urls = list(panel.URLs())
        if len(selected_urls) != 1:
            raise NativeAdapterError("selection_failed")
        selected_path = selected_urls[0].path()
        if not isinstance(selected_path, str) or not selected_path:
            raise NativeAdapterError("selection_failed")
        return selected_path

    def open_browser(self, launch_url: str) -> bool:
        with self._browser_lock:
            if self._browser_requested:
                return False
            self._browser_requested = True

        completed = Event()
        outcome = {"opened": False}

        def request_open() -> None:
            try:
                url = self._bindings.NSURL.URLWithString_(launch_url)
                outcome["opened"] = bool(
                    url is not None
                    and self._bindings.NSWorkspace.sharedWorkspace().openURL_(url)
                )
            finally:
                completed.set()

        if current_thread() is main_thread():
            request_open()
        else:
            self.dispatch_to_main(request_open)
        completed.wait()
        return outcome["opened"]

    def show_error(self, message: NativeMessage) -> None:
        self._require_main_thread()
        alert = self._bindings.NSAlert.alloc().init()
        alert.setMessageText_(message.title)
        alert.setInformativeText_(message.message)
        alert.setAlertStyle_(self._bindings.NSAlertStyleWarning)
        alert.runModal()

    def dispatch_to_main(self, callback: Callable[[], None]) -> None:
        self._bindings.AppHelper.callAfter(callback)

    def run_event_loop(self) -> None:
        self._require_main_thread()
        self._bindings.AppHelper.runConsoleEventLoop(
            installInterrupt=False,
            maxTimeout=0.1,
        )

    def stop_event_loop(self) -> None:
        self._require_main_thread()
        self._bindings.AppHelper.stopEventLoop()

    def set_launch_running(self, running: bool) -> None:
        self._launch_running = running

    def running_quit_requested(self) -> Any:
        if not self._launch_running:
            return self._bindings.NSTerminateNow
        self.show_error(
            NativeMessage(
                title="Loci Flow",
                message=(
                    "Safe shutdown while Loci Flow is running isn’t connected yet."
                ),
            )
        )
        return self._bindings.NSTerminateCancel

    def _install_application_menu(self) -> None:
        bindings = self._bindings
        main_menu = bindings.NSMenu.alloc().initWithTitle_("Loci Flow")
        application_item = bindings.NSMenuItem.alloc().init()
        main_menu.addItem_(application_item)
        application_menu = bindings.NSMenu.alloc().initWithTitle_("Loci Flow")
        quit_item = bindings.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit Loci Flow",
            "terminate:",
            "q",
        )
        application_menu.addItem_(quit_item)
        application_item.setSubmenu_(application_menu)
        self._app.setMainMenu_(main_menu)

    @staticmethod
    def _require_main_thread() -> None:
        if current_thread() is not main_thread():
            raise RuntimeError("AppKit operation requires the main thread")


def _delegate_type(bindings: _AppKitBindings) -> Any:
    class ApplicationDelegate(bindings.NSObject):
        def initWithAdapter_(self, adapter: AppKitAdapter) -> Any:
            self = bindings.objc_super(ApplicationDelegate, self).init()
            if self is not None:
                self._adapter = adapter
            return self

        def applicationShouldTerminate_(self, _application: Any) -> Any:
            return self._adapter.running_quit_requested()

    return ApplicationDelegate


def _load_bindings() -> _AppKitBindings:
    import objc
    from AppKit import (
        NSAlert,
        NSAlertStyleWarning,
        NSApplication,
        NSApplicationActivationPolicyRegular,
        NSMenu,
        NSMenuItem,
        NSModalResponseOK,
        NSOpenPanel,
        NSTerminateCancel,
        NSTerminateNow,
        NSWorkspace,
    )
    from Foundation import NSObject, NSURL
    from PyObjCTools import AppHelper

    return _AppKitBindings(
        AppHelper=AppHelper,
        NSAlert=NSAlert,
        NSAlertStyleWarning=NSAlertStyleWarning,
        NSApplication=NSApplication,
        NSApplicationActivationPolicyRegular=NSApplicationActivationPolicyRegular,
        NSMenu=NSMenu,
        NSMenuItem=NSMenuItem,
        NSModalResponseOK=NSModalResponseOK,
        NSOpenPanel=NSOpenPanel,
        NSWorkspace=NSWorkspace,
        NSURL=NSURL,
        NSObject=NSObject,
        objc_super=objc.super,
        NSTerminateCancel=NSTerminateCancel,
        NSTerminateNow=NSTerminateNow,
    )
