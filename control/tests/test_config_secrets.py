import os
import tempfile
import unittest
import unittest.mock

from greybot_control import config


class HydrateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_a_directly_set_value_is_never_overwritten(self):
        with unittest.mock.patch.dict(os.environ, {"X": "set-here", "X_SSM": "/x"}, clear=True):
            config.hydrate("X")
            self.assertEqual(os.environ["X"], "set-here")

    def test_a_parameter_name_is_resolved_into_the_environment(self):
        with unittest.mock.patch.dict(os.environ, {"X_SSM": "/greybot/x"}, clear=True):
            with unittest.mock.patch.object(config, "secret", return_value="from-ssm") as fetched:
                config.hydrate("X")
            fetched.assert_called_once_with("X")
            self.assertEqual(os.environ["X"], "from-ssm")

    def test_neither_source_leaves_the_variable_unset(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            config.hydrate("X")
            self.assertNotIn("X", os.environ)

    def test_the_turnstile_secret_is_resolved_when_config_loads(self):
        env = {"GREYBOT_GUILD_ID": "1", "GREYBOT_CLIENT_ID": "9",
               "GREYBOT_STATE_DIR": self.temp.name, "GREYBOT_TURNSTILE_SECRET_SSM": "/greybot/turnstile/secret"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            with unittest.mock.patch.object(config, "secret", side_effect=lambda n: "turnstile" if "TURNSTILE" in n else ""):
                config.Config.from_env()
            self.assertEqual(os.environ["GREYBOT_TURNSTILE_SECRET"], "turnstile")

    def test_setting_both_sources_for_one_secret_is_refused(self):
        with unittest.mock.patch.dict(os.environ, {"X": "a", "X_SSM": "/x"}, clear=True):
            self.assertRaises(ValueError, config.secret, "X")


if __name__ == "__main__":
    unittest.main()
