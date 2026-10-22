"""The AppKit shell behind `./nexus menubar`. Optional; nothing else imports it.

pyobjc is an optional extra, so this file is only imported once someone runs the
menu-bar subcommand. It draws its own status item and menu and never reads the
screen or sends input to other apps, so it needs no Accessibility grant and no
Screen Recording grant. The only thing it can do to the door is call
`./nexus stop` and `./nexus start`.

`run()` blocks until the user picks Quit. The door does not depend on this file.
"""

from __future__ import annotations

import threading

from nexus.menubar import (
    DoorPoller,
    DoorState,
    MenuBarUnavailable,
    poll_interval,
    restart_door,
    status_title,
)

_STATUS_TAG = 1001
_BUSY_TAG = 1002


def _appkit() -> object:
    try:
        import AppKit
    except ImportError as exc:  # pragma: no cover
        raise MenuBarUnavailable(str(exc)) from exc
    return AppKit


class MenuBarController:
    """AppKit target object. Owns the status item and the menu behind it."""

    def __init__(self, AppKit: object, interval: float | None = None) -> None:
        self.AppKit = AppKit
        self._status_bar_item = None
        self._busy = False
        self._build()
        self.poller = DoorPoller(interval=interval or poll_interval(), on_state=self._publish)

    def _build(self) -> None:
        AppKit = self.AppKit
        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

        self._menu = AppKit.NSMenu.alloc().init()
        self._menu.setAutoenablesItems_(False)

        status_item = self._menu.addItemWithTitle_action_keyEquivalent_(
            f"{status_title(DoorState(up=False, detail='checking…'))}  checking…", None, ""
        )
        status_item.setTag_(_STATUS_TAG)
        status_item.setEnabled_(False)

        self._menu.addItem_(AppKit.NSMenuItem.separatorItem())

        restart = self._menu.addItemWithTitle_action_keyEquivalent_(
            "Restart door", "restartDoor:", ""
        )
        restart.setTarget_(self)
        restart.setEnabled_(True)

        self._busy_item = self._menu.addItemWithTitle_action_keyEquivalent_("Restarting…", None, "")
        self._busy_item.setTag_(_BUSY_TAG)
        self._busy_item.setHidden_(True)

        refresh = self._menu.addItemWithTitle_action_keyEquivalent_("Check now", "checkNow:", "")
        refresh.setTarget_(self)
        refresh.setEnabled_(True)

        self._menu.addItem_(AppKit.NSMenuItem.separatorItem())

        quit_item = self._menu.addItemWithTitle_action_keyEquivalent_(
            "Quit menu bar", "terminate:", ""
        )
        quit_item.setTarget_(app)
        quit_item.setEnabled_(True)

        self._status_bar_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength
        )
        button = self._status_bar_item.button()
        button.setTitle_(status_title(DoorState(up=False, detail="checking…")))
        button.setToolTip_("nexus door status")
        button.setMenu_(self._menu)

    def _on_main(self, block) -> None:
        self.AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(block)

    def _publish(self, state: DoorState) -> None:
        self._on_main(lambda: self._apply(state))

    def _apply(self, state: DoorState) -> None:
        button = self._status_bar_item.button()
        button.setTitle_(status_title(state))
        button.setToolTip_(f"nexus door: {state.detail}")
        item = self._menu.itemWithTag_(_STATUS_TAG)
        if item is not None:
            item.setTitle_(f"{status_title(state)}  {state.detail}")

    def _set_busy(self, busy: bool) -> None:
        def apply() -> None:
            self._busy = busy
            self._menu.itemWithTag_(_BUSY_TAG).setHidden_(not busy)
            self._menu.itemWithTitle_("Restart door").setEnabled_(not busy)

        self._on_main(apply)

    def checkNow_(self, _sender: object) -> None:
        if self._busy:
            return
        threading.Thread(target=self.poller.check, name="os3-menubar-check", daemon=True).start()

    def restartDoor_(self, _sender: object) -> None:
        if self._busy:
            return
        self._set_busy(True)
        threading.Thread(target=self._restart, name="os3-menubar-restart", daemon=True).start()

    def _restart(self) -> None:
        try:
            state = restart_door()
        finally:
            self._set_busy(False)
        self._publish(state)


def run(interval: float | None = None) -> None:
    """Show the status item and hand control to AppKit. Blocks until Quit."""
    AppKit = _appkit()
    controller = MenuBarController(AppKit, interval)
    controller.poller.start()
    try:
        AppKit.NSApplication.sharedApplication().run()
    finally:
        controller.poller.close()
