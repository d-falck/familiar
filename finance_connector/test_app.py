import unittest

from finance_connector.app import (
    OB_READ_PREFIXES,
    T212_READ_PATHS,
    _canonical_read_path,
    _valid_ob_path,
)


class ReadOnlyBoundaryTests(unittest.TestCase):
    def test_trading_paths_are_get_resources_only(self):
        self.assertIn("/equity/account/summary", T212_READ_PATHS)
        self.assertIn("/equity/positions", T212_READ_PATHS)
        self.assertNotIn("/equity/orders/market", T212_READ_PATHS)
        self.assertNotIn("/equity/pies", T212_READ_PATHS)

    def test_open_banking_account_information_allowed(self):
        for path in ("accounts", "accounts/abc/transactions", "direct-debits", "pots"):
            self.assertTrue(_valid_ob_path(path))

    def test_open_banking_payment_paths_rejected(self):
        for path in ("payments", "domestic-payments", "funds-confirmations"):
            self.assertFalse(_valid_ob_path(path))

    def test_path_traversal_and_ambiguous_paths_rejected(self):
        for path in (
            "accounts/../payments",
            "accounts/./transactions",
            "accounts//transactions",
            "accounts/%2e%2e/payments",
            "accounts/account id/transactions",
        ):
            self.assertIsNone(_canonical_read_path(path))
            self.assertFalse(_valid_ob_path(path))


if __name__ == "__main__":
    unittest.main()
