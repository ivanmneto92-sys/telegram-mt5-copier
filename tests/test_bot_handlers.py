from __future__ import annotations

from types import SimpleNamespace
import unittest

from telegram_mt5_copier.bot_handlers import callback_handler, is_expired_callback_error


class FakeBadRequest(Exception):
    pass


class ExpiredQuery:
    data = "v1:m"

    async def answer(self) -> None:
        raise FakeBadRequest("Query is too old and response timeout expired")


class ServiceMustNotRun:
    def handle_callback(self, *_args: object) -> None:
        raise AssertionError("Callback vencido não deve executar nenhuma ação.")


class BotHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_vencido_e_descartado_sem_executar_acao(self) -> None:
        update = SimpleNamespace(callback_query=ExpiredQuery())

        await callback_handler(update, None, ServiceMustNotRun())

    def test_identifica_mensagens_de_callback_vencido(self) -> None:
        self.assertTrue(is_expired_callback_error(FakeBadRequest("Query is too old")))
        self.assertFalse(is_expired_callback_error(RuntimeError("network unavailable")))


if __name__ == "__main__":
    unittest.main()
