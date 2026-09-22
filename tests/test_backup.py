from __future__ import annotations

import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from telegram_mt5_copier.backup import (
    B2Client,
    BackupError,
    build_backup_archive,
    build_mt5_accounts_manifest,
    cleanup_old_backups,
    decrypt_file,
    encrypt_file,
    generate_backup_encryption_key,
    run_backup,
    run_restore,
    snapshot_sqlite_database,
    verify_sqlite_integrity,
)
from telegram_mt5_copier.config import AppConfig


def _make_config(root: Path, *, instance_id: str = "main") -> AppConfig:
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "sessions").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "mt5_accounts").mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text("INSTANCE_ID=" + instance_id, encoding="utf-8")
    return AppConfig.load(
        project_root=root,
        env={
            "INSTANCE_ID": instance_id,
            "BACKUP_ENCRYPTION_KEY": generate_backup_encryption_key(),
            "B2_KEY_ID": "key-id",
            "B2_APPLICATION_KEY": "app-key",
            "B2_BUCKET_NAME": "meu-bucket",
        },
        create_dirs=False,
    )


class SqliteSnapshotTests(unittest.TestCase):
    def test_snapshot_copies_data_and_stays_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            source = root / "source.sqlite3"
            connection = sqlite3.connect(str(source))
            connection.execute("CREATE TABLE clientes (id INTEGER PRIMARY KEY, nome TEXT)")
            connection.execute("INSERT INTO clientes (nome) VALUES ('Ana')")
            connection.commit()
            connection.close()

            destination = root / "snapshot.sqlite3"
            snapshot_sqlite_database(source, destination)

            copy = sqlite3.connect(str(destination))
            rows = copy.execute("SELECT nome FROM clientes").fetchall()
            copy.close()
            self.assertEqual([("Ana",)], rows)

    def test_snapshot_of_missing_database_raises(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            with self.assertRaises(BackupError):
                snapshot_sqlite_database(Path(raw_dir) / "nao-existe.sqlite3", Path(raw_dir) / "out.sqlite3")

    def test_integrity_check_passes_for_a_healthy_database(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            database = Path(raw_dir) / "ok.sqlite3"
            connection = sqlite3.connect(str(database))
            connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            connection.commit()
            connection.close()
            verify_sqlite_integrity(database)  # nao deve levantar


class EncryptionTests(unittest.TestCase):
    def test_round_trip_recovers_original_content(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            source = root / "plain.bin"
            source.write_bytes(b"conteudo sensivel de teste")
            key = generate_backup_encryption_key()

            encrypted = root / "cipher.bin"
            encrypt_file(source, encrypted, key)
            self.assertNotEqual(source.read_bytes(), encrypted.read_bytes())

            decrypted = root / "plain-again.bin"
            decrypt_file(encrypted, decrypted, key)
            self.assertEqual(source.read_bytes(), decrypted.read_bytes())

    def test_wrong_key_fails_loudly_instead_of_returning_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            source = root / "plain.bin"
            source.write_bytes(b"dado")
            encrypted = root / "cipher.bin"
            encrypt_file(source, encrypted, generate_backup_encryption_key())

            with self.assertRaises(BackupError):
                decrypt_file(encrypted, root / "out.bin", generate_backup_encryption_key())


class ManifestAndArchiveTests(unittest.TestCase):
    def test_manifest_lists_account_folders_without_copying_terminal_files(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config = _make_config(root)
            account_dir = config.mt5_base_dir / "42"
            account_dir.mkdir(parents=True)
            (account_dir / "terminal64.exe").write_bytes(b"x" * 1000)

            manifest = build_mt5_accounts_manifest(config)

            self.assertEqual(1, len(manifest))
            self.assertEqual("42", manifest[0]["account_folder"])
            self.assertEqual(1000, manifest[0]["size_bytes"])

    def test_archive_contains_database_env_sessions_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config = _make_config(root)
            connection = sqlite3.connect(str(config.database_path))
            connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            connection.commit()
            connection.close()
            (config.session_dir / "main.session").write_bytes(b"sessao-fake")

            with tempfile.TemporaryDirectory() as staging_raw:
                archive_path = build_backup_archive(config, Path(staging_raw))
                with zipfile.ZipFile(archive_path) as archive:
                    names = set(archive.namelist())
                    self.assertIn("database.sqlite3", names)
                    self.assertIn(".env", names)
                    self.assertIn("sessions/main.session", names)
                    self.assertIn("mt5_accounts_manifest.json", names)
                    self.assertIn("backup_metadata.json", names)

                    metadata = json.loads(archive.read("backup_metadata.json"))
                    self.assertEqual("main", metadata["instance_id"])

    def test_robo_braba_archive_never_includes_the_shared_caddyfile(self) -> None:
        """So a instancia main leva o Caddyfile — duas copias no backup diario
        seriam redundantes, já que o arquivo é compartilhado pela mesma VPS."""
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config = _make_config(root, instance_id="robo_braba")
            connection = sqlite3.connect(str(config.database_path))
            connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            connection.commit()
            connection.close()

            with tempfile.TemporaryDirectory() as staging_raw:
                archive_path = build_backup_archive(config, Path(staging_raw))
                with zipfile.ZipFile(archive_path) as archive:
                    self.assertNotIn("Caddyfile", archive.namelist())


class B2ClientTests(unittest.TestCase):
    def _authorize_response(self) -> MagicMock:
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "apiUrl": "https://api-fake.backblazeb2.com",
                "downloadUrl": "https://download-fake.backblazeb2.com",
                "authorizationToken": "auth-token",
                "accountId": "acct-1",
                "allowed": {"bucketId": "bucket-123"},
            }
        ).encode("utf-8")
        return response

    @patch("telegram_mt5_copier.backup.urlopen")
    def test_upload_authorizes_then_gets_upload_url_then_uploads(self, mock_urlopen: MagicMock) -> None:
        upload_url_response = MagicMock()
        upload_url_response.read.return_value = json.dumps(
            {"uploadUrl": "https://upload-fake/x", "authorizationToken": "upload-token"}
        ).encode("utf-8")
        final_response = MagicMock()
        final_response.read.return_value = b"{}"

        mock_urlopen.return_value.__enter__.side_effect = [
            self._authorize_response(),
            upload_url_response,
            final_response,
        ]

        with tempfile.TemporaryDirectory() as raw_dir:
            local_file = Path(raw_dir) / "backup.zip.enc"
            local_file.write_bytes(b"conteudo-criptografado")

            client = B2Client("key-id", "app-key", "meu-bucket")
            client.upload(local_file, "main/backup-x.zip.enc")

        self.assertEqual(3, mock_urlopen.call_count)

    @patch("telegram_mt5_copier.backup.urlopen")
    def test_bucket_scoped_key_skips_list_buckets_call(self, mock_urlopen: MagicMock) -> None:
        """Uma chave ja restrita a um bucket devolve o bucketId na propria
        autorizacao — nao deveria precisar de uma chamada extra a mais."""
        mock_urlopen.return_value.__enter__.side_effect = [self._authorize_response()]

        client = B2Client("key-id", "app-key", "meu-bucket")
        client._authorize()  # noqa: SLF001 - teste de detalhe interno intencional

        self.assertEqual("bucket-123", client._bucket_id)
        self.assertEqual(1, mock_urlopen.call_count)


class CleanupOldBackupsTests(unittest.TestCase):
    def test_removes_only_versions_older_than_retention(self) -> None:
        import time

        now_ms = int(time.time() * 1000)
        one_day_ms = 24 * 60 * 60 * 1000
        client = MagicMock()
        client.list_file_versions.return_value = [
            {"fileName": "main/old.zip.enc", "fileId": "id-old", "uploadTimestamp": now_ms - 20 * one_day_ms},
            {"fileName": "main/recent.zip.enc", "fileId": "id-recent", "uploadTimestamp": now_ms - 2 * one_day_ms},
        ]

        removed = cleanup_old_backups(client, "main/", retention_days=14)

        self.assertEqual(1, removed)
        client.delete_file_version.assert_called_once_with("main/old.zip.enc", "id-old")


class OrchestrationTests(unittest.TestCase):
    def test_run_backup_requires_encryption_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config = _make_config(root)
            config = AppConfig.load(
                project_root=root,
                env={"INSTANCE_ID": "main", "B2_KEY_ID": "k", "B2_APPLICATION_KEY": "a", "B2_BUCKET_NAME": "b"},
                create_dirs=False,
            )
            with self.assertRaisesRegex(BackupError, "BACKUP_ENCRYPTION_KEY"):
                run_backup(config)

    def test_run_backup_requires_b2_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config = AppConfig.load(
                project_root=root,
                env={"INSTANCE_ID": "main", "BACKUP_ENCRYPTION_KEY": generate_backup_encryption_key()},
                create_dirs=False,
            )
            with self.assertRaisesRegex(BackupError, "Backblaze"):
                run_backup(config)

    @patch("telegram_mt5_copier.backup.B2Client")
    def test_run_backup_end_to_end_with_a_fake_b2_client(self, mock_client_class: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.list_file_versions.return_value = []
        mock_client_class.return_value = mock_client

        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config = _make_config(root)
            connection = sqlite3.connect(str(config.database_path))
            connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
            connection.commit()
            connection.close()

            remote_name = run_backup(config)

        self.assertTrue(remote_name.startswith("main/backup-main-"))
        mock_client.upload.assert_called_once()
        uploaded_path, uploaded_remote_name = mock_client.upload.call_args[0]
        self.assertEqual(remote_name, uploaded_remote_name)
        # O arquivo temporario enviado nao pode sobrar na VPS depois do backup.
        self.assertFalse(uploaded_path.exists())

    def test_restore_refuses_a_non_empty_destination(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            config = _make_config(root)
            destination = root / "restore-alvo"
            destination.mkdir()
            (destination / "ja-tem-algo.txt").write_text("oi")

            with self.assertRaisesRegex(BackupError, "nao esta vazia"):
                run_restore(config, "main/backup-x.zip.enc", destination)


if __name__ == "__main__":
    unittest.main()
