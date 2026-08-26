import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moderation_bot import command_handler
from moderation_bot import watchdog_listener


class CommandTests(unittest.TestCase):
    def run_command(self, text):
        with patch.object(command_handler, "reply", return_value={"ok": True}) as reply:
            result = command_handler.handle_command(text, "oc_test", "om_test")
        return result, reply

    def test_help_uses_current_operator_commands(self):
        _, reply = self.run_command("/help")
        body = reply.call_args.args[2]
        for command in ("/testcard", "/resendpending", "/wake", "/diagnose", "/scan", "/watches"):
            self.assertIn(command, body)

    @patch.object(command_handler, "_run_card_sender", return_value='{"sent": 1}')
    def test_testcard_forces_latest_including_reacted(self, sender):
        _, reply = self.run_command("/testcard")
        sender.assert_called_once_with("--latest", "--force", "--include-reacted")
        self.assertEqual(reply.call_count, 2)

    @patch.object(command_handler, "_run_card_sender", return_value='{"sent": 3}')
    def test_resendpending_forces_unacted_cards(self, sender):
        _, reply = self.run_command("/resendpending")
        sender.assert_called_once_with("--force")
        self.assertEqual(reply.call_count, 2)

    @patch.object(command_handler, "_run_card_sender", return_value='{"sent": 0}')
    def test_scan_only_sends_new_cards(self, sender):
        self.run_command("/scan")
        sender.assert_called_once_with()

    @patch.object(command_handler, "fetch_unreacted_candidates")
    def test_pending_command(self, fetch):
        fetch.return_value = ([], {
            "total_messages": 0,
            "skipped_bot": 0,
            "skipped_filipe": 0,
            "skipped_non_request": 0,
            "already_had_reactions": 0,
            "pending": 0,
        })
        _, reply = self.run_command("/pending")
        self.assertIn("No pending", reply.call_args.args[2])

    @patch.object(command_handler, "_health_report", return_value="Daemon: ok")
    def test_diagnose_command(self, _health):
        _, reply = self.run_command("/diagnose")
        self.assertIn("Daemon: ok", reply.call_args.args[2])

    @patch.object(command_handler, "active_watches", return_value={})
    def test_watches_command(self, _watches):
        _, reply = self.run_command("/watches")
        self.assertIn("No active", reply.call_args.args[2])

    @patch("moderation_bot.watchdog.ensure_daemon", return_value=("ok", 1234))
    def test_checkbot_command(self, _ensure):
        _, reply = self.run_command("/checkbot")
        self.assertIn("Bot is healthy", reply.call_args.args[2])

    @patch("moderation_bot.watchdog.ensure_daemon", return_value=("restarted", 5678))
    def test_wake_restarts_through_main_handler_when_it_receives_command(self, _ensure):
        _, reply = self.run_command("/wake")
        self.assertIn("has been restarted", reply.call_args.args[2])

    @patch.object(command_handler, "format_lookup", return_value="lookup result")
    @patch.object(command_handler, "lookup", return_value={"found": True})
    def test_admin_command(self, _lookup, _format):
        _, reply = self.run_command("/admin 795005370745")
        self.assertEqual(reply.call_args.args[2], "lookup result")

    def test_legend_and_status_commands(self):
        _, legend_reply = self.run_command("/legend")
        self.assertIn("Reaction legend", legend_reply.call_args.args[2])
        _, status_reply = self.run_command("/status")
        self.assertIn("Monitored groups", status_reply.call_args.args[2])


class WatchdogCommandTests(unittest.TestCase):
    @patch.object(watchdog_listener, "reply")
    @patch.object(watchdog_listener, "_main_daemon_pid", return_value=4321)
    def test_wake_reports_alive_daemon(self, _pid, reply):
        watchdog_listener.handle_restart("oc_test", "om_test", "/wake")
        self.assertIn("already running", reply.call_args.args[2])

    @patch.object(watchdog_listener, "reply")
    @patch.object(watchdog_listener, "_restart_main", return_value=9876)
    @patch.object(watchdog_listener, "_main_daemon_pid", return_value=None)
    def test_wake_restarts_down_daemon(self, _pid, _restart, reply):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            watchdog_listener,
            "MAIN_PIDFILE",
            Path(directory) / "persistent_callback.pid",
        ):
            watchdog_listener.handle_restart("oc_test", "om_test", "/wake")
        self.assertIn("has been restarted", reply.call_args.args[2])
if __name__ == "__main__":
    unittest.main()
