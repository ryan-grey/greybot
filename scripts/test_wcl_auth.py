"""Account grants must change API access without exposing private recaps publicly."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import wcl


class UserGrantTests(unittest.TestCase):
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

    def test_bad_grant_never_silently_falls_back_to_public(self):
        for grant in (self.grant(expires_at=1060), self.grant(client_id="another-client"),
                      self.grant(access_token=""), self.grant(expires_at="invalid"), {}):
            with self.subTest(grant_fields=sorted(grant)), patch.object(wcl, "_post") as post:
                with self.assertRaises(wcl.WCLError) as exc:
                    wcl.get_token("client", "secret", now=1000, user_auth=grant)
                self.assertNotIn("test-user-token", str(exc.exception))
                post.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
