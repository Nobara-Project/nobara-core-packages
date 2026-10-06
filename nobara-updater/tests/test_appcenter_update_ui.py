"""Run with NOBARA_TEST_GTK=1 under Xvfb; backend and network loading are fakes."""
import os
import sys
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from test_frontend_contract import ROOT


@unittest.skipUnless(os.environ.get("NOBARA_TEST_GTK") == "1", "Requires isolated GTK display")
class AppCenterUpdateUiTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT))
        self.addCleanup(lambda: sys.path.remove(str(ROOT)))
        # Import the real entry point before GTK initialization.
        from appcenter import main, ui
        self.ui = ui
        ui.Adw.init()
        self.application = ui.Adw.Application(application_id="org.nobara.AppCenter.UpdateTest.t" + uuid.uuid4().hex, flags=ui.Gio.ApplicationFlags.NON_UNIQUE)
        self.application.register(None)
        for patcher in (patch.object(ui.MainWindow, "_load_async"),
                        patch.object(ui, "load_updater_settings", return_value={"update_flatpaks": False}),
                        patch.object(ui, "get_view_mode", return_value="list"),
                        patch.object(ui.Gio, "bus_get_sync", return_value=Mock())):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window = ui.MainWindow(self.application)
        self.addCleanup(self.window.destroy)
        self.apps = [ui.AppEntry(name, name.title(), "Test update", "", [name], kind="PACKAGE", installed=True,
                                 installed_version="1", candidate_version="2") for name in ("browser", "editor")]
        self.window.backend = Mock(get_upgradable_packages=lambda **_: self.apps)
        self.window.apps = self.apps
        self.window._should_use_nobara_sync = lambda: True
        self.window._refresh_main_page()
        self.window.present()
        self.drain()

    def drain(self, seconds=0.15):
        context = self.ui.GLib.MainContext.default()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.005)

    def test_real_window_uses_requested_renderer(self):
        renderer = self.window.get_renderer().__gtype__.name
        requested = os.environ.get("GSK_RENDERER")
        if requested == "gl":
            self.assertIn("GLRenderer", renderer)
        elif requested == "cairo":
            self.assertIn("CairoRenderer", renderer)
        elif requested == "vulkan":
            self.assertIn("VulkanRenderer", renderer)
        print("App Center renderer:", renderer)

    def assert_shared_progress_bars(self):
        bars = []
        def visit(widget):
            if isinstance(widget, self.ui.Gtk.ProgressBar):
                bars.append(widget)
            child = widget.get_first_child()
            while child is not None:
                visit(child)
                child = child.get_next_sibling()
        visit(self.window)
        self.assertEqual(set(bars), {self.window.queue_progress, self.window.bottom_queue_progress})
        self.assertEqual(len(bars), 2)
        self.assertEqual(self.window.queue_progress.get_text(), self.window.bottom_queue_progress.get_text())
        self.assertEqual(self.window.queue_progress.get_fraction(), self.window.bottom_queue_progress.get_fraction())

    def test_overall_bars_count_all_25_items_and_partial_progress(self):
        window = self.window
        names = [f"package-{index:02}" for index in range(25)]
        item = self.ui.QueueItem(self.apps[0], "system-update", pkg_names=names)
        window.queue_items = [item]
        window._queue_item_started(item)
        self.assert_shared_progress_bars()
        self.assertEqual(window.bottom_queue_progress.get_text(), "0/25 processed items")
        self.assertEqual(window.bottom_queue_progress.get_fraction(), 0)
        records = [dict(id="Upgrade:" + name, name=name, nevra=name + "-2.x86_64", action="Upgrade") for name in names]
        window._handle_queue_event(item, dict(event="update-progress", progress=dict(event="plan", transaction="job", stage="main", packages=records)))
        for index, record in enumerate(records[:12], start=1):
            window._handle_queue_event(item, dict(event="update-progress", progress=dict(event="package", transaction="job", id=record["id"], phase="downloaded", fraction=1)))
            window._refresh_queue_page()
            self.assertEqual(window.bottom_queue_progress.get_text(), f"{index}/25 processed items")
            self.assertAlmostEqual(window.bottom_queue_progress.get_fraction(), index / 25)
            self.assert_shared_progress_bars()
        window._handle_queue_event(item, dict(event="update-progress", progress=dict(event="package", transaction="job", id=records[12]["id"], phase="downloading", fraction=0.5)))
        self.drain()
        self.assertEqual(window.bottom_queue_progress.get_text(), "12/25 processed items")
        self.assertAlmostEqual(window.bottom_queue_progress.get_fraction(), 0.5)
        self.assertEqual(window.bottom_queue_status.get_text(), "Downloading")
        for record in records[12:]:
            window._handle_queue_event(item, dict(event="update-progress", progress=dict(event="package", transaction="job", id=record["id"], phase="downloaded", fraction=1)))
        self.drain()
        self.assertEqual(window.bottom_queue_progress.get_text(), "25/25 processed items")
        self.assertEqual(window.bottom_queue_status.get_text(), "Validating downloads")
        self.assert_shared_progress_bars()
        window.backend.execute_action.assert_not_called()

    def test_large_update_queue_can_redraw_and_resize(self):
        window = self.window
        window.apps = [self.ui.AppEntry(name, name, "Test update", "", [name], kind="PACKAGE", installed=True,
                                       installed_version="1", candidate_version="2")
                       for name in (f"package-{index:04}" for index in range(1000))]
        window.backend.get_upgradable_packages = lambda **_: window.apps
        window._invalidate_page_caches()
        window._refresh_main_page()
        window.update_all_button.emit("clicked")
        with patch.object(window, "_start_queue_worker"):
            window.update_selected_button.emit("clicked")
        # Check before rendering: an unscrolled queue used to request a
        # 57,000-pixel window and crash native GPU buffer allocation.
        self.assertLess(window.measure(self.ui.Gtk.Orientation.VERTICAL, 1100).minimum, 1000)
        item = window.queue_items[0]
        window._queue_item_started(item)
        records = [dict(id="Upgrade:" + app.primary_pkg, name=app.primary_pkg,
                        nevra=app.primary_pkg + "-2.x86_64", action="Upgrade") for app in window.apps]
        window._handle_queue_event(item, dict(event="update-progress", progress=dict(event="plan", transaction="job", stage="main", packages=records)))
        for step in range(4):
            for record in records:
                window._handle_queue_event(item, dict(event="update-progress", progress=dict(event="package", transaction="job", id=record["id"], phase="downloading", fraction=(step + 1) / 4)))
            window.set_default_size(1000 + step * 80, 650 + step * 25)
            self.drain(0.2)
            self.assertLess(window.measure(self.ui.Gtk.Orientation.VERTICAL, window.get_width()).minimum, 1000)
            # A tiling compositor can expand the window to the monitor even
            # when set_default_size requests a smaller size.
            monitor_heights = [monitor.get_geometry().height for monitor in window.get_display().get_monitors()]
            self.assertLessEqual(window.get_height(), max([1200] + monitor_heights))
        self.assert_shared_progress_bars()
        self.assertEqual(window.bottom_queue_progress.get_fraction(), 1)
        self.assertEqual(window.bottom_queue_progress.get_text(), "1000/1000 processed items")
        window.backend.execute_action.assert_not_called()

    def test_select_deselect_and_update_count_required_packages_without_duplicate_execution(self):
        window = self.window
        self.assertEqual(window.update_all_button.get_label(), "Select All")
        self.assertFalse(window.update_selected_button.get_sensitive())
        window.update_all_button.emit("clicked")
        self.assertEqual(window.update_selection, {"editor", "browser"})
        self.assertFalse(window.queue_items)
        window._toggle_update_selection(self.apps[0], False)
        self.assertEqual(window.updates_selected_label.get_text(), "1 selected")
        records = [dict(id="Upgrade:" + name, name=name, nevra=name + "-2.x86_64", action="Upgrade") for name in ("editor", "dependency")]
        def execute(action, args, callback):
            self.assertEqual(action, "system-update")
            self.assertEqual(args, ["--progress", "--package=editor"])
            callback(dict(event="update-progress", progress=dict(event="plan", transaction="job", stage="main", packages=records)))
            for record in records:
                callback(dict(event="update-progress", progress=dict(event="package", transaction="job", id=record["id"], phase="downloaded", fraction=1)))
            callback(dict(event="update-status", status="scheduled", message="Restart to install.", reboot_required=True))
            return True, "Restart to install."
        window.backend.execute_action.side_effect = execute
        window.update_selected_button.emit("clicked")
        self.drain(0.6)
        window.backend.execute_action.assert_called_once()
        self.assertFalse(window.queue_worker_running)
        self.assertFalse(window.updates_options.get_visible())
        self.assert_shared_progress_bars()
        self.assertEqual(window.bottom_queue_progress.get_text(), "2/2 processed items")
        self.assertEqual(window.bottom_queue_progress.get_fraction(), 1)
        self.assertEqual(window.bottom_queue_status.get_text(), "Prepared for restart")
        self.assertTrue(window.update_banner.get_revealed())
        self.assertNotIn("NOBARA_UPDATE_PROGRESS", window._get_queue_log_text())
        self.assertNotIn("NOBARA_UPDATE_RESULT", window._get_queue_log_text())

    def test_full_selection_keeps_complete_sync_and_clear_does_not_update(self):
        window = self.window
        window.update_all_button.emit("clicked")
        window.updates_clear_button.emit("clicked")
        self.assertFalse(window.update_selection)
        window.backend.execute_action.assert_not_called()
        window.update_all_button.emit("clicked")
        with patch.object(window, "_start_queue_worker"):
            window.update_selected_button.emit("clicked")
        self.assertEqual(window.queue_items[0].sync_args, ["--progress"])
        self.assertEqual(window.queue_items[0].pkg_names, ["browser", "editor"])
