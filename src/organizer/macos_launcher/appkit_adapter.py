from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, current_thread, main_thread
from typing import Any

from organizer.macos_launcher.coordinator import NativeAdapterError, NativeMessage
from organizer.macos_launcher.shutdown_coordinator import (
    SAFE_SHUTDOWN_FAILURE_MESSAGE,
)


@dataclass(frozen=True)
class _AppKitBindings:
    AppHelper: Any
    NSAlert: Any
    NSAlertStyleWarning: Any
    NSApplication: Any
    NSApplicationActivationPolicyRegular: Any
    NSEventModifierFlagCommand: Any
    NSMenu: Any
    NSMenuItem: Any
    NSModalResponseOK: Any
    NSOpenPanel: Any
    NSPanel: Any
    NSTextAlignmentCenter: Any
    NSTextField: Any
    NSWindowStyleMaskTitled: Any
    NSBackingStoreBuffered: Any
    NSMakeRect: Any
    NSWorkspace: Any
    NSURL: Any
    NSObject: Any
    objc_super: Any
    NSTerminateCancel: Any
    NSTerminateLater: Any
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
        self._termination_handler: Callable[[], bool] | None = None
        self._diagnostic_handler: Callable[[str], None] | None = None
        self._active_directory_panel: Any = None
        self._directory_panel_abort_requested = False
        self._finishing_panel: Any = None
        self._finishing_label: Any = None

    def initialize(self) -> None:
        self._require_main_thread()
        bindings = self._bindings
        self._app = bindings.NSApplication.sharedApplication()
        self._emit_diagnostic("native_app_initialized")
        self._app.setActivationPolicy_(
            bindings.NSApplicationActivationPolicyRegular
        )
        self._delegate = _delegate_type(bindings).alloc().initWithAdapter_(self)
        self._app.setDelegate_(self._delegate)
        self._emit_diagnostic("delegate_installed")
        self._install_application_menu()
        self._emit_diagnostic("application_menu_installed")
        self._app.activateIgnoringOtherApps_(True)

    def choose_directory(self) -> str | None:
        self._require_main_thread()
        if self._active_directory_panel is not None:
            raise NativeAdapterError("selection_failed")
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

        self._active_directory_panel = panel
        self._directory_panel_abort_requested = False
        self._emit_diagnostic("chooser_presenting")
        try:
            if panel.runModal() != bindings.NSModalResponseOK:
                return None
            selected_urls = list(panel.URLs())
            if len(selected_urls) != 1:
                raise NativeAdapterError("selection_failed")
            selected_path = selected_urls[0].path()
            if not isinstance(selected_path, str) or not selected_path:
                raise NativeAdapterError("selection_failed")
            return selected_path
        finally:
            if self._active_directory_panel is panel:
                self._active_directory_panel = None
                self._directory_panel_abort_requested = False

    def cancel_active_directory_selection(self) -> bool:
        self._require_main_thread()
        panel = self._active_directory_panel
        if panel is None or self._directory_panel_abort_requested:
            return False
        if self._app is None:
            raise RuntimeError("AppKit application is unavailable")
        self._directory_panel_abort_requested = True
        self._emit_diagnostic("chooser_cancel_requested")
        self._app.abortModal()
        panel.orderOut_(None)
        self._emit_diagnostic("chooser_modal_aborted")
        return True

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

    def set_termination_handler(self, handler: Callable[[], bool]) -> None:
        self._termination_handler = handler

    def set_diagnostic_handler(self, handler: Callable[[str], None]) -> None:
        self._diagnostic_handler = handler

    def running_quit_requested(self) -> Any:
        self._require_main_thread()
        if self._active_directory_panel is not None:
            if self._termination_handler is not None:
                self._termination_handler()
            else:
                self.cancel_active_directory_selection()
            self._emit_diagnostic("chooser_quit_requested")
            return self._bindings.NSTerminateCancel
        if not self._launch_running:
            return self._bindings.NSTerminateNow
        if self._termination_handler is None:
            return self._bindings.NSTerminateCancel
        if not self._termination_handler():
            return self._bindings.NSTerminateCancel
        self._emit_diagnostic("delayed_shutdown_started")
        return self._bindings.NSTerminateLater

    def show_finishing_status(self) -> None:
        self._require_main_thread()
        if self._finishing_panel is not None:
            return
        bindings = self._bindings
        text = "Finishing the current operation…"
        panel = bindings.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            bindings.NSMakeRect(0, 0, 380, 112),
            bindings.NSWindowStyleMaskTitled,
            bindings.NSBackingStoreBuffered,
            False,
        )
        panel.setTitle_("Loci Flow")
        panel.setReleasedWhenClosed_(False)
        panel.setHidesOnDeactivate_(False)
        label = bindings.NSTextField.labelWithString_(text)
        label.setFrame_(bindings.NSMakeRect(24, 34, 332, 42))
        label.setAlignment_(bindings.NSTextAlignmentCenter)
        label.setAccessibilityLabel_(text)
        panel.contentView().addSubview_(label)
        panel.center()
        panel.makeKeyAndOrderFront_(None)
        self._finishing_panel = panel
        self._finishing_label = label

    def close_finishing_status(self) -> None:
        self._require_main_thread()
        panel = self._finishing_panel
        self._finishing_panel = None
        self._finishing_label = None
        if panel is not None:
            panel.orderOut_(None)
            panel.close()

    def show_shutdown_failure(self) -> None:
        self.show_error(
            NativeMessage(
                title="Loci Flow",
                message=SAFE_SHUTDOWN_FAILURE_MESSAGE,
            )
        )

    def reply_to_termination(self, allow: bool) -> None:
        self._require_main_thread()
        if self._app is None:
            raise RuntimeError("AppKit application is unavailable")
        self._emit_diagnostic("appkit_termination_reply")
        self._app.replyToApplicationShouldTerminate_(allow)

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
        quit_item.setTarget_(self._app)
        quit_item.setKeyEquivalentModifierMask_(
            bindings.NSEventModifierFlagCommand
        )
        application_menu.addItem_(quit_item)
        application_item.setSubmenu_(application_menu)
        self._app.setMainMenu_(main_menu)

    def _emit_diagnostic(self, event: str) -> None:
        if self._diagnostic_handler is None:
            return
        self._diagnostic_handler(event)

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
            self._adapter._emit_diagnostic("quit_action_received")
            self._adapter._emit_diagnostic("application_should_terminate")
            return self._adapter.running_quit_requested()

    return ApplicationDelegate


def _load_bindings() -> _AppKitBindings:
    import objc
    from AppKit import (
        NSAlert,
        NSAlertStyleWarning,
        NSApplication,
        NSApplicationActivationPolicyRegular,
        NSEventModifierFlagCommand,
        NSMenu,
        NSMenuItem,
        NSModalResponseOK,
        NSOpenPanel,
        NSPanel,
        NSTextAlignmentCenter,
        NSTextField,
        NSWindowStyleMaskTitled,
        NSBackingStoreBuffered,
        NSTerminateCancel,
        NSTerminateLater,
        NSTerminateNow,
        NSWorkspace,
    )
    from Foundation import NSMakeRect, NSObject, NSURL
    from PyObjCTools import AppHelper

    return _AppKitBindings(
        AppHelper=AppHelper,
        NSAlert=NSAlert,
        NSAlertStyleWarning=NSAlertStyleWarning,
        NSApplication=NSApplication,
        NSApplicationActivationPolicyRegular=NSApplicationActivationPolicyRegular,
        NSEventModifierFlagCommand=NSEventModifierFlagCommand,
        NSMenu=NSMenu,
        NSMenuItem=NSMenuItem,
        NSModalResponseOK=NSModalResponseOK,
        NSOpenPanel=NSOpenPanel,
        NSPanel=NSPanel,
        NSTextAlignmentCenter=NSTextAlignmentCenter,
        NSTextField=NSTextField,
        NSWindowStyleMaskTitled=NSWindowStyleMaskTitled,
        NSBackingStoreBuffered=NSBackingStoreBuffered,
        NSMakeRect=NSMakeRect,
        NSWorkspace=NSWorkspace,
        NSURL=NSURL,
        NSObject=NSObject,
        objc_super=objc.super,
        NSTerminateCancel=NSTerminateCancel,
        NSTerminateLater=NSTerminateLater,
        NSTerminateNow=NSTerminateNow,
    )
