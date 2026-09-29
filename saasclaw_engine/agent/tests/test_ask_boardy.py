"""Tests for the ask_boardy tool: gating, hardcoded recipient, send path."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from saasclaw_engine.agent import tools

ENV_KEYS = ("BOARDY_SMTP_HOST", "BOARDY_SMTP_PORT", "BOARDY_SMTP_USER",
            "BOARDY_SMTP_PASSWORD", "BOARDY_FROM", "EMAIL_HOST",
            "EMAIL_PORT", "EMAIL_HOST_USER", "EMAIL_HOST_PASSWORD")


class AskBoardyTests(unittest.TestCase):
    def _clean_env(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        return patcher

    def test_short_brief_rejected(self):
        with self._clean_env():
            out = tools.ask_boardy("/tmp/ws", "hi")
        self.assertTrue(out.startswith("Error: the brief is too short"))

    def test_missing_credentials_reported(self):
        with self._clean_env():
            out = tools.ask_boardy("/tmp/ws", "x" * 200)
        self.assertIn("not configured", out)

    def test_recipient_hardcoded(self):
        self.assertEqual(tools.BOARDY_TO_EMAIL, "boardy@boardy.ai")

    def test_send_path(self):
        with self._clean_env():
            os.environ["EMAIL_HOST_USER"] = "wizard@example.com"
            os.environ["EMAIL_HOST_PASSWORD"] = "secret"
            # ask_boardy imports smtplib inside the function, so patch the module.
            with mock.patch("smtplib.SMTP") as smtp_cls:
                instance = smtp_cls.return_value.__enter__.return_value
                out = tools.ask_boardy("/tmp/some-workspace", "B" * 300)
        self.assertIn("Email sent to Boardy", out)
        smtp_cls.assert_called_once_with("smtp.gmail.com", 587, timeout=30)
        instance.starttls.assert_called_once()
        instance.login.assert_called_once_with("wizard@example.com", "secret")
        sent = instance.send_message.call_args[0][0]
        self.assertEqual(sent["To"], "boardy@boardy.ai")
        self.assertIn("SaaSClaw Wizard", sent["From"])
        # set_content() may use quoted-printable soft line breaks for long lines.
        payload = sent.get_payload().replace("=\n", "")
        self.assertEqual(payload.strip(), "B" * 300)
        self.assertIn("SaaSClaw wizard", sent["Subject"])


if __name__ == "__main__":
    unittest.main()
