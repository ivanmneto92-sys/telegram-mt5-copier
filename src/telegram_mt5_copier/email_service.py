"""Envio de e-mails transacionais (recuperacao de senha, confirmacao de e-mail,
alterações de segurança e eventos de conta MT5).

Sem chave configurada (RESEND_API_KEY ausente), o envio fica desabilitado e o
link e apenas registrado no log — o mesmo padrao usado em outras integracoes
opcionais deste projeto (noticias, OCR). Isso permite testar o fluxo inteiro
localmente sem depender de rede nem de credencial real.
"""

from __future__ import annotations

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class EmailSendError(Exception):
    """O provedor de e-mail recusou ou falhou ao enviar a mensagem."""


class EmailService:
    """Interface minima usada pelo restante do app."""

    def send(self, *, to: str, subject: str, html: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class NullEmailService(EmailService):
    """Usado quando RESEND_API_KEY nao esta configurada.

    Nao levanta erro (o fluxo de cadastro/recuperacao de senha continua
    funcionando normalmente) — apenas registra que o e-mail nao foi enviado.
    """

    def send(self, *, to: str, subject: str, html: str) -> None:
        del html
        logger.info("E-mail nao enviado (RESEND_API_KEY ausente): assunto=%r", subject)


class ResendEmailService(EmailService):
    """Envia e-mails via a API HTTP da Resend (https://resend.com/docs/api-reference/emails)."""

    def __init__(self, api_key: str, *, from_address: str, timeout_seconds: float = 10.0) -> None:
        if not api_key:
            raise ValueError("RESEND_API_KEY nao pode ficar vazia.")
        if not from_address:
            raise ValueError("RESEND_FROM_EMAIL nao pode ficar vazio.")
        self.api_key = api_key
        self.from_address = from_address
        self.timeout_seconds = timeout_seconds

    def send(self, *, to: str, subject: str, html: str) -> None:
        payload = json.dumps(
            {"from": self.from_address, "to": [to], "subject": subject, "html": html}
        ).encode("utf-8")
        request = Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                # Sem um User-Agent "de navegador", alguns proxies/WAFs na frente
                # da API (Cloudflare) bloqueiam a requisicao antes de chegar na
                # Resend — o padrao default do urllib ("Python-urllib/x.y") cai
                # nesse bloqueio em algumas redes.
                "User-Agent": "Mozilla/5.0 (compatible; InstitutoTraderBot/1.0)",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                response.read()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise EmailSendError(f"Resend recusou o envio ({exc.code}): {body}") from exc
        except URLError as exc:
            raise EmailSendError(f"Falha de rede ao enviar e-mail: {exc}") from exc


# ---------------------------------------------------------------------------
# Layout visual — mesmo shell (logo + paleta da marca) em todos os e-mails.
# Paleta oficial: #FFB100 (dourado), #051F43 (navy), #7EB1F9 (info), #EFF7FF (fundo).
# HTML em tabelas + estilo inline: e o padrao exigido para renderizar de forma
# consistente em Outlook, Gmail e Apple Mail (CSS solto no <head> e ignorado
# por vários clientes).
# ---------------------------------------------------------------------------

_LOGO_SVG = """<svg width="24" height="29" viewBox="0 0 405 491" xmlns="http://www.w3.org/2000/svg">
<path d="M147.507 279.381V355.337L90.3942 310.711V279.381H111.957V243.677H125.944V279.381H147.507Z" fill="url(#g0)"/>
<path d="M318.242 156.463V307.707L261.118 352.345V156.463H282.681V117.471H296.679V156.463H318.242Z" fill="url(#g0)"/>
<path d="M230.622 228.243V376.165L205.771 395.59L202.394 398.221L173.509 375.65V228.243H195.072V189.678H209.059V228.243H230.622Z" fill="url(#g0)"/>
<path d="M57.1133 146.662V283.305L56.1263 283.941L57.1133 284.709V304.462L202.393 417.964L205.77 415.333L347.017 304.977V146.86L404.13 110.619V332.832L261.414 444.338L261.118 444.097V444.57L205.77 487.814L202.393 490.446L0 332.328V110.433L57.1133 146.662Z" fill="url(#g1)"/>
<path d="M404.13 63.1531L347.017 99.3934V57.1133H230.621V151.245L173.509 187.475V57.1133H57.1133V99.1965L0 62.9669V0H404.13V63.1531Z" fill="url(#g1)"/>
<defs>
<linearGradient id="g0" x1="204.318" y1="178.507" x2="204.318" y2="398.221" gradientUnits="userSpaceOnUse">
<stop stop-color="white"/><stop offset="1" stop-color="#D9E9FF"/>
</linearGradient>
<linearGradient id="g1" x1="404.13" y1="0" x2="10.72" y2="465.199" gradientUnits="userSpaceOnUse">
<stop stop-color="white"/><stop offset="1" stop-color="#FFB100"/>
</linearGradient>
</defs>
</svg>"""


def _info_box(html: str) -> str:
    return f"""
        <tr>
          <td style="padding:20px 40px 32px 40px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#EFF7FF; border-radius:10px;">
              <tr>
                <td style="padding:14px 16px;">
                  {html}
                </td>
              </tr>
            </table>
          </td>
        </tr>"""


def _alt_link_box(url: str) -> str:
    return _info_box(
        '<p style="margin:0 0 4px 0; font-family: Arial, Helvetica, sans-serif; font-size:12px; '
        'color:#5A7CA8; font-weight:700;">O botão não funcionou?</p>'
        '<p style="margin:0; font-family: Arial, Helvetica, sans-serif; font-size:12px; '
        f'line-height:18px; color:#5A7CA8; word-break:break-all;">Copie e cole este link no '
        f'navegador:<br><a href="{url}" style="color:#2B6CB0;">{url}</a></p>'
    )


def _note(text: str) -> str:
    return f"""
        <tr>
          <td style="padding:0 40px 8px 40px;">
            <p style="margin:0 0 8px 0; font-family: Arial, Helvetica, sans-serif; font-size:13px; line-height:20px; color:#8792A2; text-align:center;">
              {text}
            </p>
          </td>
        </tr>"""


def render_email(
    *,
    brand_name: str,
    heading: str,
    intro_html: str,
    cta_label: str,
    cta_url: str,
    note_html: str = "",
    info_box_html: str = "",
    footer_note: str = "",
) -> str:
    """Monta o HTML final de um e-mail transacional, dentro do shell da marca.

    O visual (logo, cores, layout) e o mesmo para qualquer instancia — apenas
    o `brand_name` muda, vindo da config de cada instancia (Instituto Trader,
    Robo Braba, etc.), do mesmo jeito que o restante do app ja faz.
    """
    footer = footer_note or (
        f"Você recebeu este e-mail porque possui uma conta no {brand_name}.<br>"
        "Este é um endereço somente para envio — não responda a esta mensagem."
    )
    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0; padding:0; background-color:#EFF7FF; font-family: 'Inter', Arial, Helvetica, sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#EFF7FF; padding:32px 16px;">
  <tr>
    <td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px; background-color:#FFFFFF; border-radius:16px; overflow:hidden; box-shadow:0 8px 24px rgba(5,31,67,0.08);">
        <tr>
          <td style="background-color:#FFB100; height:5px; line-height:5px; font-size:0;">&nbsp;</td>
        </tr>
        <tr>
          <td align="center" style="padding:32px 32px 16px 32px;">
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td style="padding-right:10px;">
                  <table role="presentation" width="40" height="40" cellpadding="0" cellspacing="0" style="background-color:#051F43; border-radius:9px;">
                    <tr><td align="center" valign="middle" style="width:40px; height:40px;">{_LOGO_SVG}</td></tr>
                  </table>
                </td>
                <td valign="middle">
                  <span style="font-family: Georgia, 'Cormorant Garamond', serif; font-weight:600; letter-spacing:2px; font-size:15px; color:#051F43;">{brand_name.upper()}</span>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="padding:8px 40px 8px 40px;">
            <h1 style="margin:0 0 16px 0; font-family: Arial, Helvetica, sans-serif; font-size:20px; line-height:28px; color:#051F43; font-weight:700; text-align:center;">
              {heading}
            </h1>
            <p style="margin:0 0 20px 0; font-family: Arial, Helvetica, sans-serif; font-size:15px; line-height:24px; color:#3A4A63; text-align:center;">
              {intro_html}
            </p>
          </td>
        </tr>
        <tr>
          <td align="center" style="padding:8px 40px 24px 40px;">
            <table role="presentation" cellpadding="0" cellspacing="0">
              <tr>
                <td align="center" style="background-color:#FFB100; border-radius:10px;">
                  <a href="{cta_url}" style="display:inline-block; padding:14px 32px; font-family: Arial, Helvetica, sans-serif; font-size:15px; font-weight:700; color:#051F43; text-decoration:none;">
                    {cta_label}
                  </a>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        {note_html}
        {info_box_html}
        <tr>
          <td style="background-color:#051F43; padding:24px 40px;">
            <p style="margin:0 0 4px 0; font-family: Arial, Helvetica, sans-serif; font-size:12px; line-height:18px; color:#B9C6DD; text-align:center;">
              {brand_name} — copy trading automatizado em MT5
            </p>
            <p style="margin:0; font-family: Arial, Helvetica, sans-serif; font-size:11px; line-height:16px; color:#6E80A3; text-align:center;">
              {footer}
            </p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body></html>"""


def password_reset_email(*, brand_name: str, reset_url: str) -> tuple[str, str]:
    subject = f"Redefinir sua senha — {brand_name}"
    html = render_email(
        brand_name=brand_name,
        heading="Redefinir sua senha",
        intro_html=(
            f"Recebemos um pedido para redefinir a senha da sua conta no "
            f"<strong>{brand_name}</strong>. Clique no botão abaixo para criar uma nova senha."
        ),
        cta_label="Criar nova senha",
        cta_url=reset_url,
        note_html=_note("Este link expira em 30 minutos por segurança."),
        info_box_html=_alt_link_box(reset_url),
    )
    return subject, html


def email_confirmation_email(*, brand_name: str, confirm_url: str) -> tuple[str, str]:
    subject = f"Confirme seu e-mail — {brand_name}"
    html = render_email(
        brand_name=brand_name,
        heading="Confirme seu e-mail",
        intro_html=(
            f"Falta um passo para ativar sua conta no <strong>{brand_name}</strong>. "
            "Confirme seu e-mail clicando no botão abaixo."
        ),
        cta_label="Confirmar e-mail",
        cta_url=confirm_url,
        note_html=_note("Este link expira em 48 horas."),
        info_box_html=_alt_link_box(confirm_url),
    )
    return subject, html


def password_changed_email(*, brand_name: str, security_url: str) -> tuple[str, str]:
    subject = f"Sua senha foi alterada — {brand_name}"
    html = render_email(
        brand_name=brand_name,
        heading="Sua senha foi alterada",
        intro_html=(
            f"A senha da sua conta no <strong>{brand_name}</strong> foi alterada agora. "
            "Se foi você, nenhuma ação é necessária."
        ),
        cta_label="Não fui eu — proteger minha conta",
        cta_url=security_url,
        note_html=_note("Todas as suas sessões ativas foram encerradas por segurança."),
    )
    return subject, html


def email_changed_email(*, brand_name: str, new_email: str, profile_url: str) -> tuple[str, str]:
    subject = f"O e-mail da sua conta foi alterado — {brand_name}"
    html = render_email(
        brand_name=brand_name,
        heading="O e-mail da sua conta foi alterado",
        intro_html=(
            f"O e-mail de acesso à sua conta no <strong>{brand_name}</strong> foi alterado "
            f"para <strong>{new_email}</strong>."
        ),
        cta_label="Ver meus dados",
        cta_url=profile_url,
        note_html=_note(
            "Se você não fez essa alteração, fale com o suporte pelo Telegram imediatamente."
        ),
    )
    return subject, html


def mt5_account_connected_email(
    *, brand_name: str, broker: str, server: str, masked_login: str, accounts_url: str
) -> tuple[str, str]:
    subject = f"Conta MT5 conectada — {brand_name}"
    html = render_email(
        brand_name=brand_name,
        heading="Conta MT5 conectada com sucesso",
        intro_html=(
            f"Sua conta <strong>{broker} · {server} · {masked_login}</strong> foi conectada ao "
            f"{brand_name} e já está pronta para replicar as operações."
        ),
        cta_label="Ver minhas contas",
        cta_url=accounts_url,
        note_html=_note("Nunca compartilhamos sua senha de investidor com terceiros."),
    )
    return subject, html


def mt5_account_removed_email(
    *, brand_name: str, broker: str, masked_login: str, accounts_url: str
) -> tuple[str, str]:
    subject = f"Conta MT5 removida — {brand_name}"
    html = render_email(
        brand_name=brand_name,
        heading="Conta MT5 removida",
        intro_html=(
            f"A conta <strong>{broker} · {masked_login}</strong> foi desconectada do "
            f"{brand_name} e não recebe mais operações."
        ),
        cta_label="Ver minhas contas",
        cta_url=accounts_url,
        note_html=_note("Se você não fez essa remoção, contate o suporte."),
    )
    return subject, html
