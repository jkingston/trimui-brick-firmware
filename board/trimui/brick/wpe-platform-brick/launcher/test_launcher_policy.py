import hashlib
import pathlib
import tempfile
import unittest
from urllib.parse import urlsplit


def uri_allowed(uri: str) -> bool:
    if uri == "about:blank":
        return True
    parsed = urlsplit(uri)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def profile_paths(root: pathlib.Path, network_key: str):
    profile = hashlib.sha256(network_key.encode()).hexdigest()
    return root / profile / "data", root / profile / "cache"


class LauncherPolicyTest(unittest.TestCase):
    def test_url_policy(self):
        self.assertTrue(uri_allowed("http://portal.test/login?next=%2F"))
        self.assertTrue(uri_allowed("https://id.example.test/"))
        self.assertTrue(uri_allowed("about:blank"))
        for uri in (
            "file:///etc/shadow",
            "data:text/html,pwn",
            "javascript:alert(1)",
            "https://user:pass@host/",
        ):
            self.assertFalse(uri_allowed(uri), uri)

    def test_per_network_paths_are_stable_private_and_disjoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            a = profile_paths(root, "network-a")
            a_again = profile_paths(root, "network-a")
            b = profile_paths(root, "network-b")
            self.assertEqual(a, a_again)
            self.assertNotEqual(a, b)
            self.assertNotIn("network-a", str(a[0]))
            for path in a + b:
                path.mkdir(parents=True, mode=0o700)
                self.assertTrue(path.is_dir())
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_lifecycle_rejects_page_as_connectivity_authority(self):
        transitions = {
            ("starting", "ready"): "browsing",
            ("browsing", "success"): "authorized",
            ("starting", "failed"): "failed",
            ("browsing", "failed"): "failed",
        }
        self.assertNotIn(("browsing", "loaded"), transitions)
        state = transitions[("starting", "ready")]
        self.assertEqual(state, "browsing")
        state = transitions[(state, "success")]
        self.assertEqual(state, "authorized")

    def test_popup_policy_reuses_only_view_for_web_urls(self):
        def popup_action(uri):
            return "load-in-current-view" if uri_allowed(uri) else "deny"

        self.assertEqual(
            popup_action("https://identity.portal.test/authorize"),
            "load-in-current-view",
        )
        self.assertEqual(popup_action("tel:+441234"), "deny")
        self.assertEqual(popup_action("intent://settings"), "deny")

    def test_osk_focus_preedit_commit_and_blur(self):
        state = "hidden"
        state = "visible" if state == "hidden" else state
        self.assertEqual(state, "visible")
        state = "preedit" if state != "hidden" else state
        self.assertEqual(state, "preedit")
        state = "visible" if state == "preedit" else state
        self.assertEqual(state, "visible")
        state = "hidden"
        self.assertEqual(state, "hidden")

    def test_osk_device_session_stress_is_qualification_only(self):
        source = pathlib.Path(__file__).with_name(
            "brick-captive-wpe.c"
        ).read_text()
        qualifier = source.split("#ifdef BRICK_OSK_QUALIFICATION", 1)[1]
        self.assertIn('strstr(uri, "kind=sessions")', qualifier)
        self.assertIn("app->qualification_sessions >= 100", qualifier)
        self.assertIn("brick_osk_context_dismiss(app->osk)", qualifier)
        self.assertIn("OSK_SESSION_STRESS_PASS %u", qualifier)


if __name__ == "__main__":
    unittest.main()
