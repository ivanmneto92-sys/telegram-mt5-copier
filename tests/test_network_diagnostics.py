from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from telegram_mt5_copier.network_diagnostics import (
    probe_tcp,
    read_telegram_data_center,
)


class FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        return None


class NetworkDiagnosticsTests(unittest.TestCase):
    def test_le_data_center_sem_expor_auth_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session_name = Path(directory) / "telegram_mt5_copier"
            connection = sqlite3.connect(f"{session_name}.session")
            try:
                connection.execute(
                    """
                    CREATE TABLE sessions (
                        dc_id INTEGER PRIMARY KEY,
                        server_address TEXT,
                        port INTEGER,
                        auth_key BLOB,
                        takeout_id INTEGER
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                    (2, "149.154.167.51", 443, b"segredo", None),
                )
                connection.commit()
            finally:
                connection.close()

            data_center = read_telegram_data_center(session_name)

            self.assertEqual(data_center.dc_id, 2)
            self.assertEqual(data_center.server_address, "149.154.167.51")
            self.assertEqual(data_center.port, 443)
            self.assertNotIn("segredo", repr(data_center))

    def test_probe_tcp_resume_sucessos_e_falhas(self) -> None:
        timeout = TimeoutError("timeout")
        timeout.errno = 10060
        responses = [FakeSocket(), timeout, FakeSocket()]

        with patch("socket.create_connection", side_effect=responses):
            summary = probe_tcp(
                "149.154.167.51",
                443,
                attempts=3,
                timeout_seconds=1,
            )

        self.assertEqual(summary.successes, 2)
        self.assertEqual(summary.attempts, 3)
        self.assertEqual(summary.errors, (("TimeoutError:10060", 1),))
        self.assertIsNotNone(summary.average_ms)


if __name__ == "__main__":
    unittest.main()
