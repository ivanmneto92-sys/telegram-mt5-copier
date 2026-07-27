from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from html import escape
from pathlib import Path

from .database import connect_database, initialize_database
from .mt5.models import mask_login
from .users import USER_STATUS_ACTIVE, USER_STATUS_PAUSED, UserRepository
from .web_app import TelegramWebAppInitData, WebAppValidationError, validate_telegram_web_app_init_data


@dataclass(frozen=True)
class AdminIdentity:
    telegram_user_id: int
    username: str | None


class AdminPanelService:
    def __init__(
        self,
        database_path: Path,
        *,
        bot_token: str,
        admin_ids: tuple[int, ...],
    ) -> None:
        self.database_path = database_path
        self.bot_token = bot_token
        self.admin_ids = frozenset(admin_ids)
        initialize_database(database_path)

    def authenticate(self, init_data: str) -> AdminIdentity:
        parsed = validate_telegram_web_app_init_data(init_data, self.bot_token)
        if parsed.user.id not in self.admin_ids:
            raise WebAppValidationError("Administrador não autorizado.")
        return AdminIdentity(
            telegram_user_id=parsed.user.id,
            username=parsed.user.username,
        )

    def dashboard(self) -> dict[str, object]:
        with connect_database(self.database_path) as connection:
            summary = connection.execute(
                """
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'paused' THEN 1 ELSE 0 END)
                FROM users
                """
            ).fetchone()
            connected_accounts = connection.execute(
                "SELECT COUNT(*) FROM mt5_accounts WHERE connection_status = 'connected'"
            ).fetchone()[0]
            attention_accounts = connection.execute(
                """
                SELECT COUNT(*) FROM mt5_accounts
                WHERE connection_status != 'connected' OR last_error IS NOT NULL
                """
            ).fetchone()[0]
            active_groups = connection.execute(
                """
                SELECT COUNT(*) FROM execution_groups
                WHERE status IN ('pending_submission', 'pending_active')
                """
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT
                    u.id, u.telegram_user_id, u.telegram_username, u.status,
                    u.created_at, u.updated_at,
                    a.id, a.account_alias, a.login, a.server_name, a.account_type,
                    a.connection_status, a.balance, a.equity, a.worker_heartbeat_at,
                    a.last_error,
                    p.risk_mode, p.fixed_lot, p.risk_percent,
                    p.daily_profit_target, p.daily_loss_limit, p.max_open_signals,
                    (SELECT COUNT(*) FROM execution_groups g WHERE g.user_id = u.id),
                    (
                        SELECT COUNT(*) FROM execution_groups g
                        WHERE g.user_id = u.id
                          AND g.status IN ('pending_submission', 'pending_active')
                    ),
                    (
                        SELECT MAX(g.created_at) FROM execution_groups g
                        WHERE g.user_id = u.id
                    )
                FROM users u
                LEFT JOIN mt5_accounts a ON a.id = (
                    SELECT a2.id FROM mt5_accounts a2
                    WHERE a2.user_id = u.id ORDER BY a2.id DESC LIMIT 1
                )
                LEFT JOIN execution_profiles p
                    ON p.user_id = u.id AND p.mt5_account_id = a.id
                ORDER BY u.id DESC
                """
            ).fetchall()

        return {
            "summary": {
                "users": int(summary[0] or 0),
                "active": int(summary[1] or 0),
                "paused": int(summary[2] or 0),
                "connected_accounts": int(connected_accounts or 0),
                "attention_accounts": int(attention_accounts or 0),
                "active_groups": int(active_groups or 0),
            },
            "users": [admin_user_payload(row) for row in rows],
        }

    def set_user_status(
        self,
        *,
        admin_telegram_user_id: int,
        target_user_id: int,
        status: str,
    ) -> dict[str, object]:
        if status not in {USER_STATUS_ACTIVE, USER_STATUS_PAUSED}:
            raise ValueError("Status de usuário inválido.")
        users = UserRepository(self.database_path)
        try:
            target = users.get_by_id(target_user_id)
            updated = users.set_status(target.id, status)
            users.log_admin_action(
                admin_telegram_user_id=admin_telegram_user_id,
                target_user_id=target.id,
                action_type="admin_panel_set_user_status",
                payload={
                    "previous_status": target.status,
                    "status": updated.status,
                    "source": "admin_panel",
                },
            )
        finally:
            users.close()
        return {
            "id": updated.id,
            "status": updated.status,
        }


def admin_user_payload(row: tuple[object, ...]) -> dict[str, object]:
    account_id = int(row[6]) if row[6] is not None else None
    return {
        "id": int(row[0]),
        "telegram_user_id": int(row[1]),
        "username": str(row[2]) if row[2] else None,
        "status": str(row[3]),
        "created_at": str(row[4]),
        "updated_at": str(row[5]),
        "account": None
        if account_id is None
        else {
            "id": account_id,
            "alias": str(row[7]),
            "masked_login": mask_login(str(row[8])),
            "server": str(row[9]),
            "type": str(row[10]),
            "connection_status": str(row[11]),
            "balance": decimal_text(row[12]),
            "equity": decimal_text(row[13]),
            "worker_heartbeat_at": str(row[14]) if row[14] else None,
            "last_error": str(row[15]) if row[15] else None,
        },
        "profile": None
        if row[16] is None
        else {
            "risk_mode": str(row[16]),
            "fixed_lot": decimal_text(row[17]),
            "risk_percent": decimal_text(row[18]),
            "daily_profit_target": decimal_text(row[19]),
            "daily_loss_limit": decimal_text(row[20]),
            "max_open_signals": int(row[21]),
        },
        "execution_count": int(row[22] or 0),
        "active_group_count": int(row[23] or 0),
        "last_execution_at": str(row[24]) if row[24] else None,
    }


def decimal_text(value: object) -> str | None:
    if value is None:
        return None
    return format(Decimal(str(value)), "f")


def render_admin_panel(script_nonce: str = "") -> str:
    nonce_attribute = f' nonce="{escape(script_nonce, quote=True)}"' if script_nonce else ""
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#07111f">
  <title>Painel Admin — Instituto Trader</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #07111f;
      --panel: #0d1b2e;
      --panel-soft: #11243b;
      --line: #213954;
      --text: #eef6ff;
      --muted: #8fa8c2;
      --cyan: #39d7c4;
      --blue: #4b8fff;
      --green: #45d483;
      --yellow: #f2bd4f;
      --red: #ff6b78;
      --shadow: 0 18px 55px rgba(0, 0, 0, .28);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at 15% -10%, rgba(57, 215, 196, .16), transparent 34rem),
        radial-gradient(circle at 100% 10%, rgba(75, 143, 255, .14), transparent 30rem),
        var(--bg);
      color: var(--text);
    }}
    button, input {{ font: inherit; }}
    .shell {{ max-width: 1280px; margin: 0 auto; padding: 26px 18px 60px; }}
    header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 18px; margin-bottom: 28px; }}
    .eyebrow {{ color: var(--cyan); font-size: 12px; font-weight: 800; letter-spacing: .16em; text-transform: uppercase; }}
    h1 {{ margin: 6px 0 4px; font-size: clamp(28px, 5vw, 44px); line-height: 1; letter-spacing: -.035em; }}
    .subtitle {{ color: var(--muted); margin: 0; }}
    .refresh {{
      border: 1px solid var(--line); border-radius: 12px; padding: 11px 15px;
      color: var(--text); background: rgba(17, 36, 59, .82); cursor: pointer;
    }}
    .refresh:hover {{ border-color: var(--cyan); }}
    .notice {{ padding: 14px 16px; border: 1px solid var(--line); background: var(--panel); border-radius: 14px; color: var(--muted); }}
    .notice.error {{ color: #ffd9dd; border-color: rgba(255,107,120,.55); background: rgba(92, 25, 38, .45); }}
    .summary {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; margin: 20px 0 26px; }}
    .metric {{ padding: 17px; border: 1px solid var(--line); border-radius: 16px; background: linear-gradient(145deg, rgba(17,36,59,.96), rgba(10,25,43,.96)); box-shadow: var(--shadow); }}
    .metric span {{ display: block; color: var(--muted); font-size: 12px; min-height: 30px; }}
    .metric strong {{ display: block; margin-top: 8px; font-size: 27px; letter-spacing: -.03em; }}
    .toolbar {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 15px; }}
    .search {{ flex: 1 1 280px; min-width: 0; padding: 12px 14px; border: 1px solid var(--line); border-radius: 12px; background: #091727; color: var(--text); outline: none; }}
    .search:focus {{ border-color: var(--cyan); box-shadow: 0 0 0 3px rgba(57,215,196,.1); }}
    .filters {{ display: flex; gap: 7px; overflow-x: auto; }}
    .filter {{ border: 1px solid var(--line); border-radius: 999px; padding: 9px 12px; color: var(--muted); background: transparent; cursor: pointer; white-space: nowrap; }}
    .filter.active {{ color: #06151a; background: var(--cyan); border-color: var(--cyan); font-weight: 800; }}
    .list {{ display: grid; gap: 12px; }}
    .user-card {{ border: 1px solid var(--line); border-radius: 18px; background: rgba(13, 27, 46, .94); box-shadow: var(--shadow); overflow: hidden; }}
    .user-main {{ display: grid; grid-template-columns: minmax(220px, 1.2fr) minmax(220px, 1fr) minmax(210px, .9fr) auto; gap: 18px; align-items: center; padding: 18px; }}
    .identity {{ min-width: 0; }}
    .identity h2 {{ margin: 0 0 5px; font-size: 18px; overflow-wrap: anywhere; }}
    .meta, .detail {{ color: var(--muted); font-size: 13px; line-height: 1.65; }}
    .status {{ display: inline-flex; align-items: center; gap: 7px; padding: 6px 9px; border-radius: 999px; font-size: 12px; font-weight: 800; }}
    .status::before {{ content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }}
    .status.active, .status.connected {{ color: var(--green); background: rgba(69,212,131,.1); }}
    .status.paused {{ color: var(--yellow); background: rgba(242,189,79,.1); }}
    .status.failed, .status.disconnected {{ color: var(--red); background: rgba(255,107,120,.1); }}
    .account-name {{ color: var(--text); font-weight: 750; }}
    .money {{ color: var(--text); }}
    .actions {{ display: flex; flex-direction: column; gap: 8px; min-width: 112px; }}
    .action {{ border: 0; border-radius: 11px; padding: 10px 12px; cursor: pointer; font-weight: 800; }}
    .action.activate {{ color: #06170e; background: var(--green); }}
    .action.pause {{ color: #251a00; background: var(--yellow); }}
    .action:disabled {{ opacity: .55; cursor: wait; }}
    .empty {{ padding: 35px; text-align: center; color: var(--muted); border: 1px dashed var(--line); border-radius: 16px; }}
    @media (max-width: 980px) {{
      .summary {{ grid-template-columns: repeat(3, 1fr); }}
      .user-main {{ grid-template-columns: 1fr 1fr; }}
      .actions {{ flex-direction: row; }}
    }}
    @media (max-width: 620px) {{
      .shell {{ padding: 19px 12px 45px; }}
      header {{ align-items: center; }}
      .subtitle {{ font-size: 13px; }}
      .summary {{ grid-template-columns: repeat(2, 1fr); }}
      .metric {{ padding: 14px; }}
      .user-main {{ grid-template-columns: 1fr; gap: 12px; padding: 15px; }}
      .actions {{ width: 100%; }}
      .action {{ flex: 1; }}
    }}
  </style>
  <script defer src="https://telegram.org/js/telegram-web-app.js"></script>
  <script{nonce_attribute} defer src="/admin.js"></script>
</head>
<body>
  <main class="shell">
    <header>
      <div>
        <div class="eyebrow">Instituto Trader · Master</div>
        <h1>Central de clientes</h1>
        <p class="subtitle">Acesso, conexão MT5 e atividade operacional em um único lugar.</p>
      </div>
      <button class="refresh" id="refresh" type="button">Atualizar</button>
    </header>
    <div class="notice" id="notice">Validando seu acesso administrativo pelo Telegram…</div>
    <section class="summary" id="summary" aria-label="Resumo operacional" hidden></section>
    <section id="workspace" hidden>
      <div class="toolbar">
        <input class="search" id="search" type="search" placeholder="Buscar nome, ID, conta ou servidor" aria-label="Buscar clientes">
        <div class="filters" id="filters" aria-label="Filtrar clientes">
          <button class="filter active" type="button" data-filter="all">Todos</button>
          <button class="filter" type="button" data-filter="active">Ativos</button>
          <button class="filter" type="button" data-filter="paused">Pausados</button>
          <button class="filter" type="button" data-filter="attention">Atenção</button>
        </div>
      </div>
      <div class="list" id="user-list"></div>
    </section>
  </main>
</body>
</html>"""


def render_admin_script() -> str:
    return r"""
(function () {
  "use strict";
  var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  var state = { users: [], summary: {}, csrf: "", filter: "all", query: "", busy: false };
  var notice = document.getElementById("notice");
  var summary = document.getElementById("summary");
  var workspace = document.getElementById("workspace");
  var list = document.getElementById("user-list");
  var search = document.getElementById("search");
  var filters = document.getElementById("filters");
  var refresh = document.getElementById("refresh");

  function encode(fields) {
    return Object.keys(fields).map(function (key) {
      return encodeURIComponent(key) + "=" + encodeURIComponent(fields[key] == null ? "" : fields[key]);
    }).join("&");
  }

  function post(path, fields) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: encode(fields)
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) { throw new Error(data.error || "Falha na solicitação."); }
        return data;
      });
    });
  }

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function money(value) {
    if (value == null || value === "") { return "—"; }
    var number = Number(value);
    return isFinite(number) ? "$ " + number.toFixed(2) : "$ " + esc(value);
  }

  function dateTime(value) {
    if (!value) { return "—"; }
    var parsed = new Date(value);
    return isNaN(parsed.getTime()) ? esc(value) : parsed.toLocaleString("pt-BR");
  }

  function statusLabel(value) {
    var labels = { active: "Ativo", paused: "Pausado", connected: "Conectada", failed: "Falha", disconnected: "Desconectada" };
    return labels[value] || value || "Sem status";
  }

  function showError(message) {
    notice.className = "notice error";
    notice.textContent = message;
    notice.hidden = false;
  }

  function renderSummary() {
    var items = [
      ["Clientes", state.summary.users],
      ["Ativos", state.summary.active],
      ["Pausados", state.summary.paused],
      ["MT5 conectadas", state.summary.connected_accounts],
      ["Precisam atenção", state.summary.attention_accounts],
      ["Sinais ativos", state.summary.active_groups]
    ];
    summary.innerHTML = items.map(function (item) {
      return '<article class="metric"><span>' + esc(item[0]) + '</span><strong>' + esc(item[1] || 0) + '</strong></article>';
    }).join("");
  }

  function matches(user) {
    if (state.filter === "active" && user.status !== "active") { return false; }
    if (state.filter === "paused" && user.status !== "paused") { return false; }
    if (state.filter === "attention" && (!user.account || (user.account.connection_status === "connected" && !user.account.last_error))) { return false; }
    var account = user.account || {};
    var haystack = [user.username, user.telegram_user_id, account.alias, account.masked_login, account.server].join(" ").toLowerCase();
    return !state.query || haystack.indexOf(state.query) !== -1;
  }

  function userCard(user) {
    var account = user.account;
    var profile = user.profile;
    var nextStatus = user.status === "active" ? "paused" : "active";
    var actionLabel = nextStatus === "active" ? "Ativar" : "Pausar";
    var accountHtml = account
      ? '<div><div class="account-name">' + esc(account.alias) + ' · ' + esc(account.masked_login) + '</div>' +
        '<div class="detail">' + esc(account.server) + '<br><span class="status ' + esc(account.connection_status) + '">' + esc(statusLabel(account.connection_status)) + '</span>' +
        '<br>Saldo <span class="money">' + money(account.balance) + '</span> · Equity <span class="money">' + money(account.equity) + '</span></div></div>'
      : '<div><div class="account-name">Sem conta MT5</div><div class="detail">Aguardando cadastro do cliente.</div></div>';
    var profileHtml = profile
      ? '<div class="detail">Gestão: ' + esc(profile.risk_mode) + '<br>Lote: ' + esc(profile.fixed_lot || "—") +
        ' · Risco: ' + esc(profile.risk_percent || "—") + '%<br>Máx. sinais: ' + esc(profile.max_open_signals) +
        ' · Ativos: ' + esc(user.active_group_count) + '<br>Última execução: ' + dateTime(user.last_execution_at) + '</div>'
      : '<div class="detail">Perfil operacional ainda não criado.<br>Execuções registradas: ' + esc(user.execution_count) + '</div>';
    var errorHtml = account && account.last_error ? '<br><span style="color:var(--red)">' + esc(account.last_error) + '</span>' : "";
    return '<article class="user-card" data-user-id="' + esc(user.id) + '">' +
      '<div class="user-main">' +
        '<div class="identity"><h2>' + esc(user.username ? "@" + user.username : "Cliente #" + user.id) + '</h2>' +
          '<div class="meta">Telegram ' + esc(user.telegram_user_id) + ' · Cadastro ' + dateTime(user.created_at) + '</div>' +
          '<span class="status ' + esc(user.status) + '">' + esc(statusLabel(user.status)) + '</span>' + errorHtml +
        '</div>' +
        accountHtml + profileHtml +
        '<div class="actions"><button class="action ' + (nextStatus === "active" ? "activate" : "pause") +
          '" type="button" data-action-status="' + nextStatus + '" data-user-id="' + esc(user.id) + '">' + actionLabel + '</button></div>' +
      '</div></article>';
  }

  function renderUsers() {
    var visible = state.users.filter(matches);
    list.innerHTML = visible.length ? visible.map(userCard).join("") : '<div class="empty">Nenhum cliente encontrado neste filtro.</div>';
  }

  function applyDashboard(data) {
    state.users = data.users || [];
    state.summary = data.summary || {};
    state.csrf = data.csrf_token || state.csrf;
    notice.hidden = true;
    summary.hidden = false;
    workspace.hidden = false;
    renderSummary();
    renderUsers();
  }

  function load() {
    if (!tg || !tg.initData) {
      showError("Abra o Painel Admin pelo botão dentro do bot de gestão.");
      return;
    }
    refresh.disabled = true;
    post("/api/admin/session", { init_data: tg.initData })
      .then(applyDashboard)
      .catch(function (error) { showError(error.message || "Não foi possível abrir o painel."); })
      .finally(function () { refresh.disabled = false; });
  }

  function changeStatus(userId, status, button) {
    if (state.busy) { return; }
    var verb = status === "active" ? "ativar" : "pausar";
    if (!window.confirm("Deseja " + verb + " este cliente?")) { return; }
    state.busy = true;
    button.disabled = true;
    post("/api/admin/user-status", {
      init_data: tg.initData,
      csrf_token: state.csrf,
      user_id: userId,
      status: status
    }).then(function () { return post("/api/admin/session", { init_data: tg.initData }); })
      .then(applyDashboard)
      .catch(function (error) { showError(error.message || "Não foi possível alterar o cliente."); })
      .finally(function () { state.busy = false; button.disabled = false; });
  }

  search.addEventListener("input", function () {
    state.query = search.value.trim().toLowerCase();
    renderUsers();
  });
  filters.addEventListener("click", function (event) {
    var button = event.target.closest("[data-filter]");
    if (!button) { return; }
    state.filter = button.getAttribute("data-filter");
    Array.prototype.forEach.call(filters.querySelectorAll(".filter"), function (item) {
      item.classList.toggle("active", item === button);
    });
    renderUsers();
  });
  list.addEventListener("click", function (event) {
    var button = event.target.closest("[data-action-status]");
    if (!button) { return; }
    changeStatus(button.getAttribute("data-user-id"), button.getAttribute("data-action-status"), button);
  });
  refresh.addEventListener("click", load);

  if (tg) {
    try { tg.ready(); tg.expand(); } catch (_error) {}
  }
  load();
}());
""".lstrip()
