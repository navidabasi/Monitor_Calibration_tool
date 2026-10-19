"""The `monitor-calibration` command: ports, data folder, packaged web files."""

import socket
import sys
import tempfile
import unittest
from importlib import resources
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor_calibration import __version__, cli  # noqa: E402


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_busy_port_moves_to_next_free_one(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        busy = blocker.getsockname()[1]
        try:
            server = cli.start_server(busy, self.tmp.name, fixed=False)
            try:
                self.assertNotEqual(server.server_address[1], busy)
                self.assertGreater(server.server_address[1], busy)
            finally:
                server.server_close()
            # A port the user asked for explicitly is never swapped silently.
            with self.assertRaises(SystemExit):
                cli.start_server(busy, self.tmp.name, fixed=True)
        finally:
            blocker.close()

    def test_two_copies_never_share_a_port(self):
        first = cli.start_server(0, self.tmp.name, fixed=True)
        try:
            with self.assertRaises(SystemExit):
                cli.start_server(first.server_address[1], self.tmp.name, fixed=True)
        finally:
            first.server_close()

    def test_user_data_folder_is_outside_the_install(self):
        home = cli.user_data_home()
        package = Path(cli.__file__).resolve().parent
        self.assertNotIn(package, home.parents)
        self.assertIn(home.name, ("MonitorCalibration", "monitor-calibration"))

    def test_web_files_ship_inside_the_package(self):
        web = resources.files("monitor_calibration") / "web"
        for name in ("index.html", "app.js", "style.css"):
            self.assertTrue((web / name).is_file(), name)
        self.assertIn("__MC_TOKEN__", (web / "index.html").read_text(encoding="utf-8"))

    def test_main_wires_port_data_and_browser(self):
        with mock.patch.object(cli, "open_browser") as browser, \
                mock.patch("monitor_calibration.server.ThreadingHTTPServer.serve_forever", side_effect=KeyboardInterrupt):
            self.assertEqual(cli.main(["--port", "0", "--data", self.tmp.name], home=self.tmp.name), 0)
            url, profile = browser.call_args[0]
            self.assertTrue(url.startswith("http://127.0.0.1:"))
            self.assertEqual(Path(profile), Path(self.tmp.name) / ".browser-profile")
            browser.reset_mock()
            cli.main(["--port", "0", "--no-browser"], home=self.tmp.name)
            browser.assert_not_called()

    def test_version_flag(self):
        with mock.patch("sys.stdout") as out, self.assertRaises(SystemExit):
            cli.main(["--version"])
        self.assertIn(__version__, "".join(str(c.args[0]) for c in out.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
