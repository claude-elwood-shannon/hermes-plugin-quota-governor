#!/usr/bin/env python3
"""test_bridge_assignee_selection.py — t_62ab412b (bridge v1.9).

POST /create-task must pick the assignee by available quota instead of
hardcoding pr-ollama:

  * explicit `assignee` in the POST body wins (user override)
  * otherwise the first profile WITHOUT a quota-governor STOP file, in
    PROFILE_PREFERENCE order (pr-ollama free tier first)
  * every profile stopped -> pr-ollama (fail-open, historical default)

The bridge module is imported by file path (spec_from_file_location, same
technique as tests/test_bridge_guards.py / tests/test_bridge_ideas.py).
`choose_assignee` is pure against os.path.exists; tests redirect the
quota root through the `hermes_home` parameter — no patching, no I/O
outside tmp dirs, fully offline.

Usage:
  /usr/bin/python3.12 -m pytest test_bridge_assignee_selection.py -q
  /usr/bin/python3.12 test_bridge_assignee_selection.py   (unittest fallback)
"""
import importlib.util
import os
import unittest

BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                      "scripts", "bridge", "open-webui-bridge.py")

_spec = importlib.util.spec_from_file_location("bridge_assignee_under_test",
                                               BRIDGE)
assert _spec is not None and _spec.loader is not None  # noqa: E501 (static narrowing)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


class ChooseAssigneeTests(unittest.TestCase):
    """Unit tests for bridge.choose_assignee (pure, tmp-path driven)."""

    def setUp(self):
        import tempfile
        self.home = tempfile.mkdtemp(prefix="t62ab412b_home_")

    def stop(self, profile):
        """Create a STOP file for `profile` exactly where the plugin writes it."""
        d = os.path.join(self.home, "profiles", profile, "quota-governor")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "STOP"), "w").close()

    # -- explicit override (card criterion 4) --------------------------------

    def test_explicit_assignee_wins_over_everything(self):
        self.stop("pr-ollama")
        got = bridge.choose_assignee("pr-vllm", hermes_home=self.home)
        self.assertEqual(got, "pr-vllm")

    def test_explicit_assignee_wins_even_if_that_profile_is_stopped(self):
        # The user's explicit choice is respected even when it points at a
        # stopped profile — create-task must not second-guess the override.
        self.stop("pr-opencode")
        got = bridge.choose_assignee("pr-opencode", hermes_home=self.home)
        self.assertEqual(got, "pr-opencode")

    def test_explicit_assignee_is_stripped(self):
        self.assertEqual(bridge.choose_assignee("  pr-nanogpt  ",
                                                hermes_home=self.home),
                         "pr-nanogpt")

    def test_blank_and_nonstring_explicit_are_ignored(self):
        self.assertEqual(bridge.choose_assignee("   ", hermes_home=self.home),
                         "pr-ollama")  # no STOP anywhere -> first preference
        self.assertEqual(bridge.choose_assignee(None, hermes_home=self.home),
                         "pr-ollama")
        self.assertEqual(bridge.choose_assignee(7, hermes_home=self.home),
                         "pr-ollama")

    # -- quota-based selection (card criteria 2, 3) --------------------------

    def test_first_preference_when_nobody_stopped(self):
        self.assertEqual(bridge.choose_assignee(hermes_home=self.home),
                         "pr-ollama")

    def test_skips_stopped_ollama_to_nanogpt(self):
        self.stop("pr-ollama")
        self.assertEqual(bridge.choose_assignee(hermes_home=self.home),
                         "pr-nanogpt")

    def test_skips_first_two_stopped_to_opencode(self):
        self.stop("pr-ollama")
        self.stop("pr-nanogpt")
        self.assertEqual(bridge.choose_assignee(hermes_home=self.home),
                         "pr-opencode")

    def test_picks_last_profile_when_others_stopped(self):
        for p in ("pr-ollama", "pr-nanogpt", "pr-opencode"):
            self.stop(p)
        self.assertEqual(bridge.choose_assignee(hermes_home=self.home),
                         "pr-vllm")

    # -- fail-open default (card criterion 5) ---------------------------------

    def test_all_stopped_falls_back_to_pr_ollama(self):
        for p in bridge.PROFILE_PREFERENCE:
            self.stop(p)
        self.assertEqual(bridge.choose_assignee(hermes_home=self.home),
                         "pr-ollama")

    # -- unknown / unlisted profiles -------------------------------------------

    def test_unknown_profile_not_in_preference_is_never_autochosen(self):
        # A profile outside PROFILE_PREFERENCE (e.g. pr-openrouter) with no
        # STOP file must not shadow the preference list.
        self.stop("pr-ollama")
        d = os.path.join(self.home, "profiles", "pr-openrouter",
                         "quota-governor")
        os.makedirs(d, exist_ok=True)
        self.assertEqual(bridge.choose_assignee(hermes_home=self.home),
                         "pr-nanogpt")

    def test_explicit_unknown_profile_is_honored(self):
        got = bridge.choose_assignee("pr-openrouter", hermes_home=self.home)
        self.assertEqual(got, "pr-openrouter")

    # -- _profile_has_quota edges ----------------------------------------------

    def test_profile_has_quota_missing_profile_dir(self):
        self.assertTrue(bridge._profile_has_quota("pr-ghost",
                                                  hermes_home=self.home))

    def test_profile_has_quota_invalid_input(self):
        self.assertFalse(bridge._profile_has_quota("", hermes_home=self.home))
        self.assertFalse(bridge._profile_has_quota(None,
                                                   hermes_home=self.home))
        self.assertFalse(bridge._profile_has_quota(7, hermes_home=self.home))

    def test_profile_has_quota_empty_home_is_fail_open(self):
        # hermes_home that does not exist at all -> no STOP readable anywhere
        # -> every profile eligible -> first preference wins.
        self.assertTrue(bridge._profile_has_quota(
            "pr-ollama", hermes_home=os.path.join(self.home, "nope")))
        self.assertEqual(
            bridge.choose_assignee(
                hermes_home=os.path.join(self.home, "nope")),
            "pr-ollama")

    # -- profile list shape -----------------------------------------------------

    def test_preference_order_is_documented_order(self):
        self.assertEqual(bridge.PROFILE_PREFERENCE,
                         ("pr-ollama", "pr-nanogpt", "pr-opencode",
                          "pr-vllm"))


class CreateTaskWiringTests(unittest.TestCase):
    """POST /create-task must actually USE choose_assignee (not just have it)."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.home = tempfile.mkdtemp(prefix="t62ab412b_wiring_")

    def test_handler_calls_choose_assignee_with_body_value(self):
        import inspect
        src = inspect.getsource(bridge.HermesBridge.do_POST)
        self.assertIn("choose_assignee", src)
        self.assertIn('data.get("assignee")', src)
        # the hardcoded literal must be gone from the command construction
        self.assertNotIn('"--assignee", "pr-ollama"', src)

    def test_openapi_declares_assignee_property_and_version(self):
        entry = bridge.OPENAPI_SPEC["paths"]["/create-task"]
        props = (entry["post"]["requestBody"]["content"]["application/json"]
                 ["schema"]["properties"])
        self.assertIn("assignee", props)
        self.assertEqual(bridge.OPENAPI_SPEC["info"]["version"], "1.10.0")

    def test_spec_still_valid_json_shape(self):
        # smoke: the giant literal still parses and carries the new text
        desc = bridge.OPENAPI_SPEC["paths"]["/create-task"]["post"][
            "description"]
        self.assertIn("quota-governor STOP file", desc)
        self.assertIn("fail-open", desc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
