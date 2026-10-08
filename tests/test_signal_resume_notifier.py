from datetime import datetime, timedelta, timezone

from telegram_mt5_copier.database import connect_database, initialize_database, utc_now
from telegram_mt5_copier.signal_resume_notifier import notify_resumed_signal_pauses


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def send(self, telegram_user_id: int, message: str) -> bool:
        self.sent.append((telegram_user_id, message))
        return True


def _user(database_path, telegram_id: int, pause_until: str | None) -> None:
    with connect_database(database_path) as db:
        db.execute(
            "INSERT INTO users (telegram_user_id, telegram_username, status, daily_signal_pause_until,"
            " created_at, updated_at) VALUES (?, ?, 'active', ?, ?, ?)",
            (telegram_id, f"u{telegram_id}", pause_until, utc_now(), utc_now()),
        )


def test_aviso_de_retomada_so_quando_a_pausa_termina_e_uma_vez(tmp_path):
    database_path = tmp_path / "resume.sqlite3"
    initialize_database(database_path)
    now = datetime.now(timezone.utc)
    _user(database_path, 1, (now - timedelta(minutes=5)).isoformat())  # acabou agora
    _user(database_path, 2, (now + timedelta(hours=3)).isoformat())  # ainda pausado
    _user(database_path, 3, "9999-12-31T00:00:00+00:00")  # ate religar
    _user(database_path, 4, (now - timedelta(days=2)).isoformat())  # antiga, sem aviso
    notifier = Recorder()

    assert notify_resumed_signal_pauses(database_path, notifier, None) == 1
    assert [telegram_id for telegram_id, _ in notifier.sent] == [1]
    assert "SINAIS RETOMADOS" in notifier.sent[0][1]

    assert notify_resumed_signal_pauses(database_path, notifier, None) == 0
    assert len(notifier.sent) == 1
