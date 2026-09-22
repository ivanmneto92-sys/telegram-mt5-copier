import unittest

from telegram_mt5_copier.email_service import (
    EmailSendError,
    NullEmailService,
    email_changed_email,
    email_confirmation_email,
    mt5_account_connected_email,
    mt5_account_removed_email,
    password_changed_email,
    password_reset_email,
)


class TemplateTests(unittest.TestCase):
    """Cada template usa o mesmo shell visual (logo + paleta da marca) e nunca
    hardcoded "Instituto Trader" — a marca vem sempre do brand_name, porque o
    mesmo web_server.py tambem atende a instancia da Robo Braba."""

    def test_password_reset_email_usa_a_marca_recebida(self) -> None:
        subject, html = password_reset_email(brand_name="Robo Braba", reset_url="https://x/y#reset_token=abc")
        self.assertIn("Robo Braba", subject)
        self.assertIn("ROBO BRABA", html)
        self.assertIn("https://x/y#reset_token=abc", html)
        self.assertIn("#FFB100", html)
        self.assertIn("#051F43", html)

    def test_email_confirmation_email_usa_a_marca_recebida(self) -> None:
        subject, html = email_confirmation_email(brand_name="Instituto Trader", confirm_url="https://x/y#confirm_token=abc")
        self.assertIn("Instituto Trader", subject)
        self.assertIn("https://x/y#confirm_token=abc", html)

    def test_password_changed_email(self) -> None:
        subject, html = password_changed_email(
            brand_name="Instituto Trader", security_url="https://app.example.com/esqueci-senha"
        )
        self.assertIn("alterada", subject)
        self.assertIn("https://app.example.com/esqueci-senha", html)
        self.assertIn("sessões ativas foram encerradas", html)

    def test_email_changed_email_mostra_o_novo_email_e_nao_vaza_url_externa_inventada(self) -> None:
        subject, html = email_changed_email(
            brand_name="Instituto Trader",
            new_email="novo@example.com",
            profile_url="https://app.example.com/perfil",
        )
        self.assertIn("alterado", subject)
        self.assertIn("novo@example.com", html)
        self.assertIn("https://app.example.com/perfil", html)

    def test_mt5_account_connected_email(self) -> None:
        subject, html = mt5_account_connected_email(
            brand_name="Instituto Trader",
            broker="HFM",
            server="HFM-Live1",
            masked_login="••••7777",
            accounts_url="https://app.example.com/conta-mt5",
        )
        self.assertIn("conectada", subject)
        self.assertIn("HFM", html)
        self.assertIn("HFM-Live1", html)
        self.assertIn("••••7777", html)
        # Nunca deve haver espaco para vazar a senha de investidor no e-mail.
        self.assertNotIn("password", html.lower())

    def test_mt5_account_removed_email(self) -> None:
        subject, html = mt5_account_removed_email(
            brand_name="Instituto Trader",
            broker="HFM",
            masked_login="••••7777",
            accounts_url="https://app.example.com/conta-mt5",
        )
        self.assertIn("removida", subject)
        self.assertIn("HFM", html)
        self.assertIn("••••7777", html)


class NullEmailServiceTests(unittest.TestCase):
    def test_nunca_levanta_erro_mesmo_sem_credencial(self) -> None:
        service = NullEmailService()
        try:
            service.send(to="cliente@example.com", subject="Assunto", html="<p>Oi</p>")
        except EmailSendError:
            self.fail("NullEmailService nunca deveria levantar EmailSendError.")


if __name__ == "__main__":
    unittest.main()
