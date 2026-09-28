from __future__ import annotations

import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qsl

from telegram_mt5_copier.database import connect_database, initialize_database, utc_now
from telegram_mt5_copier.telegram_notifier import TelegramAdminNotifier


def insert_roster_admin(database_path: Path, telegram_user_id: int, *, revoked: bool = False) -> None:
    now = utc_now()
    with connect_database(database_path) as connection:
        connection.execute(
            """
            INSERT INTO admin_roster (
                telegram_user_id, role, label, added_by_telegram_user_id,
                created_at, updated_at, revoked_at, revoked_by_telegram_user_id
            ) VALUES (?, 'regular', NULL, 9001, ?, ?, ?, ?)
            """,
            (telegram_user_id, now, now, now if revoked else None, 9001 if revoked else None),
        ).close()


class FakeResponse:
    def __init__(self, body: dict[str, object]) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class TelegramAdminNotifierRosterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "notifier.sqlite3"
        initialize_database(self.database_path)
        self.logger = logging.getLogger("test_telegram_notifier")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_envia_para_admin_do_env_e_admin_do_roster(self) -> None:
        insert_roster_admin(self.database_path, 202)
        notifier = TelegramAdminNotifier(
            "123:token",
            (9001,),
            database_path=self.database_path,
            logger=self.logger,
        )

        with mock.patch(
            "telegram_mt5_copier.telegram_notifier.request.urlopen",
            return_value=FakeResponse({"ok": True}),
        ) as urlopen:
            delivered = notifier.send("teste")

        self.assertTrue(delivered)
        sent_chat_ids = {
            dict(parse_qsl(call.args[0].data.decode("utf-8")))["chat_id"]
            for call in urlopen.call_args_list
        }
        self.assertEqual(sent_chat_ids, {"9001", "202"})

    def test_nao_envia_para_admin_do_roster_revogado(self) -> None:
        insert_roster_admin(self.database_path, 202, revoked=True)
        notifier = TelegramAdminNotifier(
            "123:token",
            (9001,),
            database_path=self.database_path,
            logger=self.logger,
        )

        with mock.patch(
            "telegram_mt5_copier.telegram_notifier.request.urlopen",
            return_value=FakeResponse({"ok": True}),
        ) as urlopen:
            notifier.send("teste")

        self.assertEqual(urlopen.call_count, 1)

    def test_sem_database_path_continua_funcionando_so_com_env(self) -> None:
        notifier = TelegramAdminNotifier(
            "123:token",
            (9001,),
            logger=self.logger,
        )

        with mock.patch(
            "telegram_mt5_copier.telegram_notifier.request.urlopen",
            return_value=FakeResponse({"ok": True}),
        ) as urlopen:
            delivered = notifier.send("teste")

        self.assertTrue(delivered)
        self.assertEqual(urlopen.call_count, 1)

    def test_falha_ao_ler_roster_nao_impede_envio_para_admins_do_env(self) -> None:
        notifier = TelegramAdminNotifier(
            "123:token",
            (9001,),
            database_path=Path(self.temp_dir.name) / "nao-existe" / "sem-banco.sqlite3",
            logger=self.logger,
        )

        with mock.patch(
            "telegram_mt5_copier.telegram_notifier.request.urlopen",
            return_value=FakeResponse({"ok": True}),
        ) as urlopen:
            delivered = notifier.send("teste")

        self.assertTrue(delivered)
        self.assertEqual(urlopen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
