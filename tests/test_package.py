from __future__ import annotations

import importlib
import unittest


class PackageTests(unittest.TestCase):
    def test_package_and_login_module_can_be_imported(self) -> None:
        package = importlib.import_module("telegram_mt5_copier")
        telegram_login = importlib.import_module("telegram_mt5_copier.telegram_login")

        self.assertTrue(package.__file__)
        self.assertTrue(callable(telegram_login.main))

    def test_mt5_layer_imports_without_metatrader5_installed(self) -> None:
        client_module = importlib.import_module("telegram_mt5_copier.mt5.client")
        service_module = importlib.import_module("telegram_mt5_copier.mt5.account_service")
        web_server = importlib.import_module("telegram_mt5_copier.web_server")

        self.assertTrue(hasattr(client_module, "MT5Client"))
        self.assertTrue(hasattr(service_module, "MT5AccountService"))
        self.assertTrue(callable(web_server.main))


if __name__ == "__main__":
    unittest.main()
