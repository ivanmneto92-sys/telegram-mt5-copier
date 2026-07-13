from __future__ import annotations

import importlib
import unittest


class PackageTests(unittest.TestCase):
    def test_package_and_login_module_can_be_imported(self) -> None:
        package = importlib.import_module("telegram_mt5_copier")
        telegram_login = importlib.import_module("telegram_mt5_copier.telegram_login")

        self.assertTrue(package.__file__)
        self.assertTrue(callable(telegram_login.main))


if __name__ == "__main__":
    unittest.main()
