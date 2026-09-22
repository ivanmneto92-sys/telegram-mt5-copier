"""Backup criptografado e fora da VPS dos dados operacionais de uma instancia.

Roda como `telegram-mt5-backup` (scripts/backup_vps.ps1 chama isso todo dia
pela Tarefa Agendada). Em cada execucao:

1. Tira um snapshot consistente do SQLite pela API de backup online do
   proprio sqlite3 (`Connection.backup`) — seguro mesmo com o banco aberto
   e em uso pelo supervisor, ao contrario de copiar o arquivo `.sqlite3`
   direto (que pode pegar o banco no meio de uma escrita).
2. Empacota nesse snapshot: `.env`, sessoes do Telegram (`*.session`),
   `Caddyfile` (se existir — so a instancia "main" precisa levar essa copia,
   ja que o Caddy e compartilhado) e um manifesto das contas MT5 (id,
   corretora, servidor, tamanho da pasta — NUNCA os binarios do terminal,
   que sao grandes e 100% reproduziveis a partir do template configurado
   em MT5_BROKER_TEMPLATES; o que realmente nao se recupera sem backup e
   o registro no banco + a chave de criptografia).
3. Criptografa o pacote com Fernet, usando BACKUP_ENCRYPTION_KEY — uma
   chave dedicada, nunca a mesma MT5_CREDENTIAL_KEY (misturar propositos
   de chave e uma pratica ruim: comprometer uma nunca deveria comprometer
   a outra).
4. Envia o pacote criptografado para um bucket privado do Backblaze B2
   (API nativa HTTP, sem dependencia nova alem da biblioteca padrao).
5. Apaga no B2 as versoes mais velhas que BACKUP_RETENTION_DAYS.
6. Nao deixa nada (nem o zip criptografado) armazenado na VPS depois de
   terminar — tudo roda num diretorio temporario apagado ao final, mesmo
   se a execucao falhar no meio.

Tambem oferece um modo de restauracao (`--restore <nome-do-arquivo-no-b2>
--into <pasta-de-destino>`), pensado para o teste real de restauracao: ele
baixa, descriptografa, descompacta e roda `PRAGMA integrity_check` no
banco restaurado, sem tocar em nada da instancia rodando de verdade.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import base64
import zipfile

from .config import AppConfig

logger = logging.getLogger(__name__)

B2_API_BASE = "https://api.backblazeb2.com"
_UPLOAD_TIMEOUT_SECONDS = 120.0


class BackupError(Exception):
    """Falha em qualquer etapa do backup ou da restauracao."""


# --------------------------------------------------------------------------
# Fernet minimo (mesmo formato usado por credential_service.py para as
# senhas MT5) — reimplementado aqui so para nao acoplar este modulo ao
# resto do pacote de credenciais, que e sobre um assunto diferente
# (senha de investidor, nao backup de operacao).
# --------------------------------------------------------------------------


def _require_cryptography():
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError as exc:  # pragma: no cover - dependencia ja obrigatoria do projeto
        raise BackupError(
            "O pacote 'cryptography' nao esta instalado neste ambiente."
        ) from exc
    return Fernet, InvalidToken


def generate_backup_encryption_key() -> str:
    """Gera uma BACKUP_ENCRYPTION_KEY nova, no mesmo formato de MT5_CREDENTIAL_KEY."""
    Fernet, _ = _require_cryptography()
    return Fernet.generate_key().decode("ascii")


def encrypt_file(source: Path, destination: Path, key: str) -> None:
    Fernet, _ = _require_cryptography()
    fernet = Fernet(key.encode("ascii"))
    destination.write_bytes(fernet.encrypt(source.read_bytes()))


def decrypt_file(source: Path, destination: Path, key: str) -> None:
    Fernet, InvalidToken = _require_cryptography()
    fernet = Fernet(key.encode("ascii"))
    try:
        destination.write_bytes(fernet.decrypt(source.read_bytes()))
    except InvalidToken as exc:
        raise BackupError(
            "Nao foi possivel descriptografar o backup — BACKUP_ENCRYPTION_KEY errada "
            "ou arquivo corrompido."
        ) from exc


# --------------------------------------------------------------------------
# Etapa 1: snapshot consistente do SQLite.
# --------------------------------------------------------------------------


def snapshot_sqlite_database(source_path: Path, destination_path: Path) -> None:
    """Copia o banco usando a API de backup online do sqlite3.

    Diferente de uma copia de arquivo comum, isto e seguro com o banco
    aberto por outro processo (o supervisor continua escrevendo nele
    durante o backup): o SQLite coordena a leitura consistente das
    paginas, pagina por pagina, sem exigir um lock exclusivo prolongado.
    """
    if not source_path.exists():
        raise BackupError(f"Banco nao encontrado em {source_path}.")
    source = sqlite3.connect(str(source_path))
    try:
        destination = sqlite3.connect(str(destination_path))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def verify_sqlite_integrity(database_path: Path) -> None:
    connection = sqlite3.connect(str(database_path))
    try:
        result = connection.execute("PRAGMA integrity_check;").fetchone()
    finally:
        connection.close()
    if result is None or result[0] != "ok":
        raise BackupError(f"Banco restaurado falhou na verificacao de integridade: {result}")


# --------------------------------------------------------------------------
# Etapa 2: montagem do pacote.
# --------------------------------------------------------------------------


def build_mt5_accounts_manifest(config: AppConfig) -> list[dict[str, object]]:
    """Lista as pastas de conta MT5 (id, tamanho, ultima modificacao).

    So o manifesto, nunca os arquivos do terminal em si — ver o docstring
    do modulo para o motivo. O manifesto sozinho ja permite conferir
    rapidamente, numa restauracao, quais contas existiam e se o numero
    bate com o que o banco registra.
    """
    manifest: list[dict[str, object]] = []
    if not config.mt5_base_dir.exists():
        return manifest
    for entry in sorted(config.mt5_base_dir.iterdir()):
        if not entry.is_dir():
            continue
        total_size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
        manifest.append(
            {
                "account_folder": entry.name,
                "size_bytes": total_size,
                "modified_at": datetime.fromtimestamp(
                    entry.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
            }
        )
    return manifest


def build_backup_archive(config: AppConfig, staging_dir: Path) -> Path:
    """Monta o zip (nao criptografado ainda) num diretorio temporario."""
    archive_path = staging_dir / "backup.zip"
    with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        db_snapshot = staging_dir / "database.sqlite3"
        snapshot_sqlite_database(config.database_path, db_snapshot)
        archive.write(db_snapshot, arcname="database.sqlite3")
        db_snapshot.unlink()

        env_path = config.project_root / ".env"
        if env_path.exists():
            archive.write(env_path, arcname=".env")
        else:
            logger.warning("backup_env_missing path=%s", env_path)

        session_count = 0
        if config.session_dir.exists():
            for session_file in sorted(config.session_dir.glob("*.session")):
                archive.write(session_file, arcname=f"sessions/{session_file.name}")
                session_count += 1
        if session_count == 0:
            logger.warning("backup_no_session_files session_dir=%s", config.session_dir)

        # O Caddyfile e compartilhado entre as duas instancias na mesma VPS;
        # so a instancia "main" precisa levar essa copia no backup dela,
        # para nao duplicar o mesmo arquivo em dois backups todo dia.
        if config.instance_id == "main":
            caddyfile = Path(r"C:\Caddy\Caddyfile")
            if caddyfile.exists():
                archive.write(caddyfile, arcname="Caddyfile")

        manifest = build_mt5_accounts_manifest(config)
        archive.writestr(
            "mt5_accounts_manifest.json",
            json.dumps(manifest, indent=2, ensure_ascii=False),
        )

        metadata = {
            "instance_id": config.instance_id,
            "brand_name": config.brand_name,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "mt5_account_folders": len(manifest),
        }
        archive.writestr("backup_metadata.json", json.dumps(metadata, indent=2, ensure_ascii=False))

    return archive_path


# --------------------------------------------------------------------------
# Etapa 3/4/5: Backblaze B2 (API nativa, so biblioteca padrao).
# --------------------------------------------------------------------------


class B2Client:
    def __init__(self, key_id: str, application_key: str, bucket_name: str) -> None:
        self.key_id = key_id
        self.application_key = application_key
        self.bucket_name = bucket_name
        self._api_url: str | None = None
        self._download_url_base: str | None = None
        self._auth_token: str | None = None
        self._account_id: str | None = None
        self._bucket_id: str | None = None

    def _authorize(self) -> None:
        credentials = base64.b64encode(f"{self.key_id}:{self.application_key}".encode("ascii"))
        request = Request(
            f"{B2_API_BASE}/b2api/v2/b2_authorize_account",
            headers={"Authorization": f"Basic {credentials.decode('ascii')}"},
        )
        payload = _json_request(request)
        self._api_url = str(payload["apiUrl"])
        self._download_url_base = str(payload["downloadUrl"])
        self._auth_token = str(payload["authorizationToken"])
        self._account_id = str(payload["accountId"])
        allowed = payload.get("allowed") or {}
        bucket_id = allowed.get("bucketId")
        if bucket_id:
            self._bucket_id = str(bucket_id)
        else:
            self._bucket_id = self._find_bucket_id()

    def _find_bucket_id(self) -> str:
        # So chega aqui com uma chave mestra (sem restricao de bucket) — uma
        # chave criada ja restrita a um bucket devolve o bucketId direto em
        # `allowed` na autorizacao, sem precisar desta chamada extra.
        request = Request(
            f"{self._api_url}/b2api/v2/b2_list_buckets",
            data=json.dumps(
                {"accountId": self._account_id, "bucketName": self.bucket_name}
            ).encode("utf-8"),
            headers={"Authorization": self._auth_token, "Content-Type": "application/json"},
        )
        payload = _json_request(request)
        buckets = payload.get("buckets") or []
        if not buckets:
            raise BackupError(f"Bucket B2 '{self.bucket_name}' nao encontrado com esta chave.")
        return str(buckets[0]["bucketId"])

    def _ensure_authorized(self) -> None:
        if self._auth_token is None:
            self._authorize()

    def upload(self, local_path: Path, remote_name: str) -> None:
        self._ensure_authorized()
        upload_url_request = Request(
            f"{self._api_url}/b2api/v2/b2_get_upload_url",
            data=json.dumps({"bucketId": self._bucket_id}).encode("utf-8"),
            headers={"Authorization": self._auth_token, "Content-Type": "application/json"},
        )
        upload_info = _json_request(upload_url_request)

        content = local_path.read_bytes()
        sha1 = hashlib.sha1(content).hexdigest()
        request = Request(
            str(upload_info["uploadUrl"]),
            data=content,
            method="POST",
            headers={
                "Authorization": str(upload_info["authorizationToken"]),
                "X-Bz-File-Name": quote(remote_name),
                "Content-Type": "b2/x-auto",
                "X-Bz-Content-Sha1": sha1,
                "Content-Length": str(len(content)),
            },
        )
        _json_request(request, timeout=_UPLOAD_TIMEOUT_SECONDS)

    def download(self, remote_name: str, destination: Path) -> None:
        self._ensure_authorized()
        assert self._download_url_base is not None
        request = Request(
            f"{self._download_url_base}/file/{quote(self.bucket_name, safe='')}/{quote(remote_name)}",
            headers={"Authorization": self._auth_token},
        )
        try:
            with urlopen(request, timeout=_UPLOAD_TIMEOUT_SECONDS) as response:
                destination.write_bytes(response.read())
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise BackupError(f"Falha ao baixar '{remote_name}' do B2 ({exc.code}): {body}") from exc
        except URLError as exc:
            raise BackupError(f"Falha de rede ao baixar do B2: {exc}") from exc

    def list_file_versions(self, prefix: str) -> list[dict[str, object]]:
        self._ensure_authorized()
        request = Request(
            f"{self._api_url}/b2api/v2/b2_list_file_names",
            data=json.dumps(
                {"bucketId": self._bucket_id, "prefix": prefix, "maxFileCount": 10_000}
            ).encode("utf-8"),
            headers={"Authorization": self._auth_token, "Content-Type": "application/json"},
        )
        payload = _json_request(request)
        return list(payload.get("files") or [])

    def delete_file_version(self, file_name: str, file_id: str) -> None:
        self._ensure_authorized()
        request = Request(
            f"{self._api_url}/b2api/v2/b2_delete_file_version",
            data=json.dumps({"fileName": file_name, "fileId": file_id}).encode("utf-8"),
            headers={"Authorization": self._auth_token, "Content-Type": "application/json"},
        )
        _json_request(request)


def _json_request(request: Request, timeout: float = 30.0) -> dict[str, object]:
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise BackupError(f"B2 recusou a chamada ({exc.code}): {body}") from exc
    except URLError as exc:
        raise BackupError(f"Falha de rede ao chamar o B2: {exc}") from exc


def cleanup_old_backups(client: B2Client, prefix: str, retention_days: int) -> int:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for entry in client.list_file_versions(prefix):
        uploaded_ms = entry.get("uploadTimestamp")
        if not isinstance(uploaded_ms, (int, float)):
            continue
        uploaded_at = datetime.fromtimestamp(uploaded_ms / 1000, tz=timezone.utc)
        if uploaded_at < cutoff:
            client.delete_file_version(str(entry["fileName"]), str(entry["fileId"]))
            removed += 1
            logger.info("backup_old_version_removed file_name=%s", entry["fileName"])
    return removed


# --------------------------------------------------------------------------
# Orquestracao.
# --------------------------------------------------------------------------


def run_backup(config: AppConfig) -> str:
    """Executa o backup completo e devolve o nome do arquivo enviado ao B2."""
    if not config.backup_encryption_key:
        raise BackupError("BACKUP_ENCRYPTION_KEY nao configurada no .env desta instancia.")
    if not (config.b2_key_id and config.b2_application_key and config.b2_bucket_name):
        raise BackupError(
            "Configuracao do Backblaze B2 incompleta (B2_KEY_ID / B2_APPLICATION_KEY / "
            "B2_BUCKET_NAME)."
        )

    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    remote_name = f"{config.instance_id}/backup-{config.instance_id}-{timestamp}.zip.enc"

    with tempfile.TemporaryDirectory(prefix="mt5-backup-") as raw_staging_dir:
        staging_dir = Path(raw_staging_dir)
        logger.info("backup_started instance_id=%s", config.instance_id)

        archive_path = build_backup_archive(config, staging_dir)
        encrypted_path = staging_dir / "backup.zip.enc"
        encrypt_file(archive_path, encrypted_path, config.backup_encryption_key)
        archive_path.unlink()

        size_mb = encrypted_path.stat().st_size / (1024 * 1024)
        logger.info("backup_archive_ready size_mb=%.1f", size_mb)

        client = B2Client(config.b2_key_id, config.b2_application_key, config.b2_bucket_name)
        client.upload(encrypted_path, remote_name)
        logger.info("backup_uploaded remote_name=%s", remote_name)

        removed = cleanup_old_backups(client, f"{config.instance_id}/", config.backup_retention_days)
        logger.info("backup_retention_applied removed=%d", removed)

    # O `with tempfile.TemporaryDirectory` acima ja apaga staging_dir (e tudo
    # dentro, criptografado ou nao) ao sair do bloco, mesmo se algo acima
    # levantar excecao — nada do backup fica para tras na VPS.
    return remote_name


def run_restore(config: AppConfig, remote_name: str, destination: Path) -> None:
    """Baixa, descriptografa e descompacta um backup numa pasta de teste.

    Nunca escreve em cima dos dados reais da instancia — `destination` deve
    ser uma pasta vazia dedicada ao teste de restauracao.
    """
    if not config.backup_encryption_key:
        raise BackupError("BACKUP_ENCRYPTION_KEY nao configurada no .env desta instancia.")
    if not (config.b2_key_id and config.b2_application_key and config.b2_bucket_name):
        raise BackupError("Configuracao do Backblaze B2 incompleta.")
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise BackupError(f"'{destination}' nao esta vazia — use uma pasta so para o teste.")

    with tempfile.TemporaryDirectory(prefix="mt5-restore-") as raw_staging_dir:
        staging_dir = Path(raw_staging_dir)
        encrypted_path = staging_dir / "backup.zip.enc"
        client = B2Client(config.b2_key_id, config.b2_application_key, config.b2_bucket_name)
        client.download(remote_name, encrypted_path)

        archive_path = staging_dir / "backup.zip"
        decrypt_file(encrypted_path, archive_path, config.backup_encryption_key)

        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(destination)

    restored_db = destination / "database.sqlite3"
    verify_sqlite_integrity(restored_db)
    logger.info("restore_verified destination=%s", destination)


def _configure_logging(config: AppConfig) -> None:
    log_path = config.log_dir / "backup.log"
    config.log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    root_logger.addHandler(logging.StreamHandler(sys.stdout))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="telegram-mt5-backup")
    parser.add_argument(
        "--restore",
        metavar="NOME_DO_ARQUIVO",
        help="Baixa e restaura o backup indicado (ex.: main/backup-main-20260922-030000.zip.enc) "
        "numa pasta de teste, em vez de fazer um backup novo.",
    )
    parser.add_argument(
        "--into",
        metavar="PASTA",
        help="Pasta vazia onde restaurar (obrigatorio junto com --restore).",
    )
    parser.add_argument(
        "--generate-key",
        action="store_true",
        help="So gera e imprime uma BACKUP_ENCRYPTION_KEY nova; nao faz backup nem restauracao.",
    )
    args = parser.parse_args(argv)

    if args.generate_key:
        print(generate_backup_encryption_key())
        return 0

    config = AppConfig.load(create_dirs=False)
    _configure_logging(config)

    try:
        if args.restore:
            if not args.into:
                parser.error("--restore exige --into <pasta>")
            run_restore(config, args.restore, Path(args.into))
            print(f"Restaurado e verificado em: {args.into}")
        else:
            remote_name = run_backup(config)
            print(f"Backup enviado: {remote_name}")
        return 0
    except BackupError as exc:
        logger.error("backup_failed reason=%s", exc)
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
