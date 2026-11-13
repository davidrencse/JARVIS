"""Credit display units reach innerHTML in the console: only short plain tokens may pass."""
import tempfile
import unittest
from unittest.mock import patch

from jarvis import credits as mod

HOSTILE = ['</span><style>*{display:none}</style>', '<img src=x onerror=alert(1)>',
           '"><b>', "USD'\";", "a" * 40]


class UnitSanitizeTests(unittest.TestCase):
    def test_clean_unit_keeps_plain_tokens_and_drops_markup(self):
        for ok in ("USD", "CNY", "%", "tokens", "$", "€", "£", "¥", "credits 2"):
            self.assertEqual(mod._clean_unit(ok), ok)
        for bad in HOSTILE:
            out = mod._clean_unit(bad)
            self.assertLessEqual(len(out), 12)
            self.assertFalse(set(out) & set("<>\"'&;/=()*{}"), out)
        self.assertEqual(mod._clean_unit(None, "USD"), "USD")
        self.assertEqual(mod._clean_unit("<>", "USD"), "USD")

    def test_deepseek_currency_is_sanitized(self):
        for bad in HOSTILE:
            with tempfile.TemporaryDirectory() as state:
                c = mod.Credits(state)
                reading = {"facts": [], "meters": []}
                data = {"is_available": True, "balance_infos": [
                    {"currency": bad, "total_balance": "5", "granted_balance": "0",
                     "topped_up_balance": "5"}]}
                with patch.object(mod, "_get_json", return_value=data), \
                        patch.object(c, "_keyed", return_value="k"):
                    c._deepseek(reading, ("DEEPSEEK_API_KEY",))
                unit = reading["headline"]["unit"]
                self.assertFalse(set(unit) & set("<>\"'&;/="), unit)
                self.assertLessEqual(len(unit), 12)

    def test_manual_unit_is_sanitized_on_write_and_on_read(self):
        for bad in HOSTILE:
            with tempfile.TemporaryDirectory() as state:
                c = mod.Credits(state)
                c.set_manual("openrouter", 3, 10, unit=bad)
                stored = c.manual()["openrouter"]["unit"]
                self.assertFalse(set(stored) & set("<>\"'&;/="), stored)
                # a hand-edited / older manual.json with a hostile unit is cleaned when read
                data = c.manual()
                data["openrouter"]["unit"] = bad
                mod.atomic_write(c.manual_path, mod.json.dumps(data))
                reading = {"id": "openrouter", "source": "none", "status": "unconfigured",
                           "meters": [], "facts": []}
                c._apply_manual(reading)
                for u in (reading["headline"]["unit"], reading["meters"][0]["unit"]):
                    self.assertFalse(set(u) & set("<>\"'&;/="), u)


if __name__ == "__main__":
    unittest.main()
