from pathlib import Path
import unittest


class AddonTests(unittest.TestCase):
    def run_lua_fixture(self, name: str) -> None:
        try:
            from lupa.lua51 import LuaRuntime
        except ImportError:
            self.skipTest("Install requirements-test.txt to run the Lua 5.1 regressions")
        fixture = Path(__file__).with_name(name)
        LuaRuntime().execute(fixture.read_text(encoding="utf-8"))

    def test_lua_51_regressions(self):
        self.run_lua_fixture("addon_regression.lua")

    def test_published_route_validation(self):
        self.run_lua_fixture("route_validation.lua")

    def test_item_verification_and_timers(self):
        self.run_lua_fixture("item_verification.lua")

    def test_addon_does_not_use_network_interfaces(self):
        addon = Path(__file__).resolve().parents[1] / "addon" / "RXPQuestGuard"
        for path in addon.glob("*.lua"):
            with self.subTest(path=path.name):
                self.assertNotRegex(path.read_text(encoding="utf-8"),
                                    r"\b(?:C_HTTP|HTTPRequest|SendAddonMessage|socket|io\.popen|os\.execute)\b")
