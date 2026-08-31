import unittest
import tempfile
import zipfile

from finance_connector.app import (
    OB_READ_PREFIXES,
    T212_READ_PATHS,
    _canonical_read_path,
    _valid_ob_path,
    _read_emma_export,
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

    def test_emma_xlsx_reader(self):
        shared = "".join(
            f"<si><t>{value}</t></si>"
            for value in (
                "ID", "Date", "Amount", "Account", "Bank", "Currency",
                "Category", "Subcategory", "Type", "Tags", "Counterparty",
                "Custom Name", "Merchant", "Additional details", "Notes",
                "Linked transaction ID", "tx1", "2026-01-02", "-12.34",
                "Personal", "Monzo", "GBP", "Food", "Purchase", "Cafe",
            )
        )
        cells = "".join(
            f'<c r="{chr(65 + i)}1" t="s"><v>{i}</v></c>' for i in range(16)
        )
        cells2 = "".join(
            f'<c r="{chr(65 + i)}2" t="s"><v>{index}</v></c>'
            for i, index in enumerate((16, 17, 18, 19, 20, 21, 22, 0, 23, 0, 24))
        )
        with tempfile.NamedTemporaryFile(suffix=".xlsx") as handle:
            with zipfile.ZipFile(handle.name, "w") as archive:
                archive.writestr(
                    "xl/sharedStrings.xml",
                    f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{shared}</sst>',
                )
                archive.writestr(
                    "xl/worksheets/sheet1.xml",
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    f'<sheetData><row r="1">{cells}</row><row r="2">{cells2}</row></sheetData></worksheet>',
                )
            rows = _read_emma_export(handle.name)
        self.assertEqual(rows[0]["ID"], "tx1")
        self.assertEqual(rows[0]["Amount"], "-12.34")
        self.assertEqual(rows[0]["Bank"], "Monzo")

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
