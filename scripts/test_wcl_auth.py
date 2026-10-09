"""Account grants must change API access without exposing private recaps publicly."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import wcl


def reset_wcl():
    wcl._renewed.clear()
    wcl._public_since.clear()
    wcl._app.update(client_id=None, client_secret=None)
    wcl.public_only.update(active=False, why="")


class UserGrantTests(unittest.TestCase):
    def setUp(self):
        reset_wcl()
        self.addCleanup(reset_wcl)

    def grant(self, **changes):
        return {"client_id": "client", "access_token": "test-user-token",
                "expires_at": 2000, **changes}

    def test_user_and_app_tokens_use_separate_endpoints(self):
        with patch.object(wcl, "_post", return_value={"data": {"ok": True}}) as post:
            token = wcl.get_token("client", "secret", now=1000, user_auth=self.grant())
            self.assertIsInstance(token, wcl.UserToken)
            self.assertNotIn("test-user-token", repr(token))
            self.assertEqual(wcl.query(token, "{userData{currentUser{id}}}"), {"ok": True})
            self.assertEqual(post.call_args.args[0], wcl.USER_API_URL)
            wcl.query("app-token", "{rateLimitData{limitPerHour}}")
            self.assertEqual(post.call_args.args[0], wcl.API_URL)

    def test_bad_grant_falls_back_to_public_and_says_so(self):
        """A dead grant costs only what it alone could see, and never silently."""
        for grant in (self.grant(expires_at=1060), self.grant(client_id="another-client"),
                      self.grant(access_token=""), self.grant(expires_at="invalid"), {}):
            reset_wcl()
            with self.subTest(grant_fields=sorted(grant)), \
                    patch.dict(wcl._token, {"value": None, "expires_at": 0}), \
                    patch.object(wcl, "_post", return_value={
                        "access_token": "app-token", "expires_in": 3600}) as post:
                token = wcl.get_token("client", "secret", now=1000, user_auth=grant)
                self.assertEqual(token, "app-token")
                self.assertNotIsInstance(token, wcl.UserToken)
                self.assertIn(b"client_credentials", post.call_args.args[1])
                self.assertTrue(wcl.public_only["active"])
                # Nothing private can be mistaken for publishable on the public token.
                self.assertTrue(wcl.reports_are_public(token, [{}]))

    def test_existing_app_auth_is_unchanged(self):
        with patch.dict(wcl._token, {"value": None, "expires_at": 0}), \
                patch.object(wcl, "_post", return_value={"access_token": "app-token", "expires_in": 3600}) as post:
            token = wcl.get_token("client", "secret", now=1000)
            self.assertEqual(token, "app-token")
            self.assertNotIsInstance(token, wcl.UserToken)
            self.assertIn(b"client_credentials", post.call_args.args[1])

    def test_mixed_private_unlisted_and_unknown_sources_never_publish(self):
        token = wcl.UserToken("test-user-token")
        self.assertTrue(wcl.reports_are_public(token, [{"visibility": "public"}]))
        for visibility in ("private", "unlisted", None, ""):
            self.assertFalse(wcl.reports_are_public(token, [{"visibility": "public"}, {"visibility": visibility}]))
        self.assertTrue(wcl.reports_are_public("app-token", [{}]))

    def test_report_queries_request_visibility(self):
        self.assertIn("visibility", wcl.REPORT_DETAIL_Q)
        self.assertIn("visibility", wcl.NIGHT_REPORTS_Q)
        self.assertIn("visibility", wcl.USER_NIGHT_REPORTS_Q)

    def test_private_recap_publication_requires_explicit_opt_in(self):
        token = wcl.UserToken("test-user-token")
        reports = [{"visibility": "private"}]
        self.assertFalse(wcl.may_publish_recap(token, reports))
        for value in (False, None, "false", "true", 1):
            self.assertFalse(wcl.may_publish_recap(token, reports, allow_private=value))
        self.assertTrue(wcl.may_publish_recap(token, reports, allow_private=True))
        self.assertTrue(wcl.may_publish_recap(token, [{"visibility": "public"}]))


class RejectedGrantTests(unittest.TestCase):
    """Warcraft Logs rejected a grant eleven days into a year. The 401 is the fact."""

    def setUp(self):
        reset_wcl()
        self.addCleanup(reset_wcl)

    def rejected(self):
        err = wcl.WCLError("HTTP 401 from /api/v2/user: ")
        err.status = 401
        return err

    def test_rejected_user_token_is_renewed_once_and_remembered(self):
        old = wcl.UserToken("old-user-token")
        calls = []

        def post(url, body, headers, timeout=20):
            calls.append(headers["Authorization"])
            if headers["Authorization"] == "Bearer old-user-token":
                raise self.rejected()
            return {"data": {"ok": True}}

        with patch.object(wcl, "_post", side_effect=post), \
                patch.object(wcl, "renew_user_auth", return_value="new-user-token") as renew:
            self.assertEqual(wcl.query(old, "{a}"), {"ok": True})
            # The caller still holds the old token for the rest of its run.
            self.assertEqual(wcl.query(old, "{b}"), {"ok": True})
            renew.assert_called_once_with("old-user-token")
            token = wcl.get_token("client", "secret", now=1000, user_auth={
                "client_id": "client", "access_token": "old-user-token", "expires_at": 2000})
            self.assertEqual(token, "new-user-token")
            self.assertIsInstance(token, wcl.UserToken)
        self.assertEqual(calls, ["Bearer old-user-token", "Bearer new-user-token",
                                 "Bearer new-user-token"])

    def test_unrenewable_grant_reads_public_reports_instead_of_failing(self):
        grant = {"client_id": "client", "access_token": "old-user-token", "expires_at": 2000}
        seen = []

        def post(url, body, headers, timeout=20):
            seen.append((url, headers["Authorization"]))
            if url == wcl.TOKEN_URL:
                return {"access_token": "app-token", "expires_in": 3600}
            if headers["Authorization"] == "Bearer old-user-token":
                raise self.rejected()
            return {"data": {"ok": True}}

        with patch.dict(wcl._token, {"value": None, "expires_at": 0}), \
                patch.object(wcl, "_post", side_effect=post), \
                patch.object(wcl, "renew_user_auth", return_value=None) as renew:
            token = wcl.get_token("client", "secret", now=1000, user_auth=grant)
            self.assertIsInstance(token, wcl.UserToken)
            self.assertFalse(wcl.public_only["active"])
            self.assertEqual(wcl.query(token, "{a}"), {"ok": True})
            self.assertTrue(wcl.public_only["active"])
            self.assertEqual(seen[-1], (wcl.API_URL, "Bearer app-token"))
            # The rest of the run, and the next one in this container, stay on public
            # without asking the provider or the renewer again.
            self.assertEqual(wcl.query(token, "{b}"), {"ok": True})
            wcl.public_only.update(active=False, why="")
            again = wcl.get_token("client", "secret", now=1000, user_auth=grant)
            self.assertEqual(again, "app-token")
            self.assertTrue(wcl.public_only["active"])
            renew.assert_called_once()
            self.assertEqual([a for _u, a in seen].count("Bearer old-user-token"), 1)
            # ...until an hour has passed, when renewal gets another chance.
            wcl._public_since["old-user-token"] -= wcl._RETRY_RENEWAL_SECONDS + 1
            renew.return_value = "new-user-token"
            self.assertEqual(wcl.query(token, "{c}"), {"ok": True})
            self.assertEqual(seen[-1], (wcl.USER_API_URL, "Bearer new-user-token"))

    def test_no_renewal_and_no_app_credentials_means_the_rejection_stands(self):
        for renewer in (None, lambda rejected: None, lambda rejected: rejected,
                        lambda rejected: 1 / 0):
            with self.subTest(renewer=renewer), \
                    patch.object(wcl, "_post", side_effect=self.rejected()) as post, \
                    patch.object(wcl, "renew_user_auth", renewer):
                with self.assertRaises(wcl.WCLError):
                    wcl.query(wcl.UserToken("old-user-token"), "{a}")
                self.assertEqual(post.call_count, 1)

    def test_only_a_401_on_the_account_endpoint_renews(self):
        busy = wcl.WCLError("HTTP 429 from /api/v2/user: ")
        busy.status = 429
        with patch.object(wcl, "renew_user_auth") as renew:
            with patch.object(wcl, "_post", side_effect=busy), self.assertRaises(wcl.WCLError):
                wcl.query(wcl.UserToken("old-user-token"), "{a}")
            with patch.object(wcl, "_post", side_effect=self.rejected()), \
                    self.assertRaises(wcl.WCLError):
                wcl.query("app-token", "{a}")
            renew.assert_not_called()

    def test_expired_grant_renews_instead_of_raising(self):
        grant = {"client_id": "client", "access_token": "old-user-token", "expires_at": 1000}
        with patch.object(wcl, "renew_user_auth", return_value="new-user-token"):
            self.assertEqual(wcl.get_token("client", "secret", now=1000, user_auth=grant),
                             "new-user-token")
        with patch.object(wcl, "renew_user_auth") as renew, \
                patch.dict(wcl._token, {"value": "app-token", "expires_at": 9e12}):
            # Another client's grant is not this app's to renew.
            self.assertEqual(wcl.get_token("client", "secret", now=1000,
                                           user_auth=dict(grant, client_id="another-client")),
                             "app-token")
            renew.assert_not_called()


class RenewalTests(unittest.TestCase):
    """Saving comes first: a refresh that cannot be stored strands the grant."""

    def setUp(self):
        import os
        os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
        import config
        self.config = config
        # Loading configuration installs the real renewer. These tests call it directly;
        # no other test may reach AWS through it.
        wcl.renew_user_auth = None
        self.stored = {"client_id": "client", "access_token": "old-user-token",
                       "refresh_token": "old-refresh", "expires_at": 9e12,
                       "user_id": 1, "user_name": "someone"}
        self.puts = []
        self.put_error = None
        outer = self

        class SSM:
            def get_parameter(self, Name, WithDecryption):
                return {"Parameter": {"Value": json.dumps(outer.stored)}}

            def get_parameters(self, Names, WithDecryption):
                return {"Parameters": [{"Name": config.WCL_CLIENT_ID, "Value": "client"},
                                       {"Name": config.WCL_CLIENT_SECRET, "Value": "secret"}]}

            def put_parameter(self, **kwargs):
                if outer.put_error:
                    raise outer.put_error
                self_value = json.loads(kwargs["Value"])
                outer.puts.append(self_value)
                outer.stored = self_value
                assert kwargs["Type"] == "SecureString" and kwargs["Overwrite"] is True
                assert "KeyId" not in kwargs

        for target, value in ((config, "ssm"),):
            patcher = patch.object(target, value, SSM())
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict(config._cache, {"wcl_user_auth": dict(self.stored)}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_renewal_proves_it_can_save_before_spending_the_refresh_token(self):
        with patch.object(wcl, "_post", return_value={
                "access_token": "new-user-token", "refresh_token": "new-refresh",
                "expires_in": 3600}) as post:
            self.assertEqual(self.config.renew_wcl_user_auth("old-user-token"), "new-user-token")
        self.assertIn(b"refresh_token=old-refresh", post.call_args.args[1])
        self.assertEqual([p["access_token"] for p in self.puts],
                         ["old-user-token", "new-user-token"])
        self.assertEqual(self.puts[1]["refresh_token"], "new-refresh")
        self.assertEqual(self.puts[1]["user_name"], "someone")
        self.assertEqual(self.config._cache["wcl_user_auth"]["access_token"], "new-user-token")

    def test_a_role_that_cannot_save_never_refreshes(self):
        self.put_error = RuntimeError("AccessDeniedException")
        with patch.object(wcl, "_post") as post:
            self.assertIsNone(self.config.renew_wcl_user_auth("old-user-token"))
            post.assert_not_called()

    def test_a_grant_another_run_renewed_is_adopted_without_a_refresh(self):
        self.stored = dict(self.stored, access_token="someone-elses-new-token")
        with patch.object(wcl, "_post") as post:
            self.assertEqual(self.config.renew_wcl_user_auth("old-user-token"),
                             "someone-elses-new-token")
            post.assert_not_called()
        self.assertEqual(self.puts, [])

    def test_a_refused_refresh_returns_nothing_and_logs_no_token(self):
        refused = wcl.WCLError("HTTP 400 from /oauth/token: old-refresh")
        refused.status = 400
        import contextlib
        import io
        out = io.StringIO()
        with patch.object(wcl, "_post", side_effect=refused), contextlib.redirect_stdout(out):
            self.assertIsNone(self.config.renew_wcl_user_auth("old-user-token"))
        self.assertNotIn("old-refresh", out.getvalue())
        self.assertNotIn("old-user-token", out.getvalue())
        self.assertIn("wcl_user_auth_renewal_failed", out.getvalue())

    def test_an_old_refresh_token_is_kept_when_none_comes_back(self):
        with patch.object(wcl, "_post", return_value={"access_token": "new-user-token",
                                                      "expires_in": 3600}):
            self.config.renew_wcl_user_auth("old-user-token")
        self.assertEqual(self.puts[-1]["refresh_token"], "old-refresh")


if __name__ == "__main__":
    unittest.main()
