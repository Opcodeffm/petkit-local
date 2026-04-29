"""Unit tests for petkit_cloud helper module.

Mocks aiohttp.ClientSession.post — does not hit the real Petkit cloud.
"""
from __future__ import annotations

# IMPORTANT: load every stdlib module BEFORE adding the integration dir
# to sys.path. The integration ships `select.py` and `time.py` HA-platform
# stubs that would shadow the stdlib modules of the same name otherwise
# (asyncio → socket → selectors → select).
import asyncio  # noqa: F401  — load early to lock stdlib `select` resolution
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import petkit_cloud as pc  # noqa: E402


def _mock_session(status: int, body: str | dict):
    """Build a mock aiohttp.ClientSession whose .post(...) returns
    the given status + body. Body can be a dict (will be JSON-serialized)
    or a raw string (for malformed-response tests)."""
    text = json.dumps(body) if isinstance(body, dict) else body

    resp = AsyncMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)

    sess = MagicMock()
    sess.post = MagicMock(return_value=resp)
    return sess, resp


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class HashTest(unittest.TestCase):
    def test_md5_lowercase_hex(self):
        # Verified against Python: hashlib.md5(b"testpw123").hexdigest()
        self.assertEqual(
            pc._hash_password("testpw123"),
            "a61fc33e65bf2693b7f2641135daafb9",
        )

    def test_email_b64(self):
        self.assertEqual(
            pc._encode_username("test@example.com"),
            "dGVzdEBleGFtcGxlLmNvbQ==",
        )


class EnvelopeTest(unittest.TestCase):
    def test_success_returns_result(self):
        out = pc._check_response_envelope({"result": {"a": 1}}, "ctx")
        self.assertEqual(out, {"a": 1})

    def test_error_session_keyword_raises_auth(self):
        with self.assertRaises(pc.PetkitAuthError):
            pc._check_response_envelope(
                {"error": {"code": 1010, "msg": "session expired"}}, "ctx"
            )

    def test_error_session_message_raises_auth(self):
        with self.assertRaises(pc.PetkitAuthError):
            pc._check_response_envelope(
                {"error": {"code": 999, "msg": "Please login again"}}, "ctx"
            )

    def test_error_other_raises_cloud_error(self):
        with self.assertRaises(pc.PetkitCloudError) as ctx:
            pc._check_response_envelope(
                {"error": {"code": 4001, "msg": "device not found"}}, "ctx"
            )
        self.assertNotIsInstance(ctx.exception, pc.PetkitAuthError)

    def test_missing_result_raises(self):
        with self.assertRaises(pc.PetkitCloudError):
            pc._check_response_envelope({"foo": "bar"}, "ctx")


class LoginTest(unittest.TestCase):
    def test_login_happy_path(self):
        sess, resp = _mock_session(
            200,
            {
                "result": {
                    "session": {
                        "id": "deadbeefaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaybub",
                        "userId": "999999998",
                        "expiresIn": 129600,
                        "region": "DE",
                        "createdAt": "2026-04-28T06:24:25.228+0000",
                    },
                    "apiServers": ["https://api.eu-pet.com/6/"],
                    "user": {"id": "999999998", "deviceCount": 0},
                }
            },
        )
        out = _run(pc.login(sess, "test@example.com", "testpw123"))
        self.assertEqual(out["session"]["userId"], "999999998")
        self.assertEqual(out["user"]["deviceCount"], 0)

        # Check that we sent the right body
        sess.post.assert_called_once()
        call_kwargs = sess.post.call_args.kwargs
        body = call_kwargs["data"]
        self.assertEqual(body["encrypt"], "1")
        self.assertEqual(body["region"], "DE")
        self.assertEqual(body["password"], "a61fc33e65bf2693b7f2641135daafb9")
        self.assertEqual(body["username"], "dGVzdEBleGFtcGxlLmNvbQ==")
        # No X-Session header on initial login
        self.assertNotIn("X-Session", call_kwargs["headers"])

    def test_login_bad_password_raises_auth(self):
        sess, _ = _mock_session(
            200,
            {"error": {"code": 1010, "msg": "username or password incorrect"}},
        )
        with self.assertRaises(pc.PetkitAuthError):
            _run(pc.login(sess, "x", "y"))

    def test_login_http_500_raises_cloud(self):
        sess, _ = _mock_session(500, "internal error")
        with self.assertRaises(pc.PetkitCloudError):
            _run(pc.login(sess, "x", "y"))


class RefreshTest(unittest.TestCase):
    def test_refresh_returns_session(self):
        sess, _ = _mock_session(
            200,
            {
                "result": {
                    "session": {
                        "id": "same-token-as-input",
                        "userId": "999999998",
                        "expiresIn": 129600,
                        "region": "DE",
                        "createdAt": "2026-04-28T06:24:25.228+0000",
                    },
                    "apiServers": ["https://api.eu-pet.com/6/"],
                }
            },
        )
        out = _run(pc.refresh_session(sess, "some-token"))
        self.assertEqual(out["session"]["id"], "same-token-as-input")

        # Check X-Session header was set
        call_kwargs = sess.post.call_args.kwargs
        self.assertEqual(call_kwargs["headers"]["X-Session"], "some-token")

    def test_refresh_expired_token_raises_auth(self):
        sess, _ = _mock_session(
            200,
            {"error": {"code": 1010, "msg": "session expired, please login"}},
        )
        with self.assertRaises(pc.PetkitAuthError):
            _run(pc.refresh_session(sess, "old-token"))


class ValidateTest(unittest.TestCase):
    def test_validate_returns_true_on_success(self):
        sess, _ = _mock_session(200, {"result": {"hasCtw3": 1}})
        self.assertTrue(_run(pc.validate_session(sess, "tok")))

    def test_validate_returns_false_on_auth_error(self):
        sess, _ = _mock_session(
            200,
            {"error": {"code": 1010, "msg": "session expired"}},
        )
        self.assertFalse(_run(pc.validate_session(sess, "tok")))

    def test_validate_returns_false_on_network_error(self):
        sess, _ = _mock_session(503, "service unavailable")
        self.assertFalse(_run(pc.validate_session(sess, "tok")))


class FetchFountainTest(unittest.TestCase):
    def test_fetch_fountain_returns_secret(self):
        sess, _ = _mock_session(
            200,
            {
                "result": {
                    "id": 400000001,
                    "mac": "aabbcc112233",
                    "secret": "cafebabe1234",
                    "sn": "EXAMPLEFAKESN001",
                    "name": "EVERSWEET MAX 2 (CORDLESS)",
                    "firmware": 111,
                    "typeCode": 2,
                }
            },
        )
        out = _run(pc.fetch_fountain(sess, "tok", "aabbcc112233", "EXAMPLEFAKESN001"))
        self.assertEqual(out["id"], 400000001)
        self.assertEqual(out["secret"], "cafebabe1234")
        self.assertEqual(out["typeCode"], 2)

    def test_fetch_fountain_normalizes_mac_with_colons(self):
        sess, _ = _mock_session(
            200, {"result": {"id": 1, "mac": "aabbcc112233", "secret": "x", "sn": "y"}}
        )
        _run(pc.fetch_fountain(sess, "tok", "AA:BB:CC:11:22:33", "EXAMPLEFAKESN001"))
        body = sess.post.call_args.kwargs["data"]
        self.assertEqual(body["mac"], "aabbcc112233")
        self.assertEqual(body["sn"], "EXAMPLEFAKESN001")

    def test_fetch_fountain_invalid_mac_raises(self):
        sess, _ = _mock_session(200, {"result": {}})
        with self.assertRaises(ValueError):
            _run(pc.fetch_fountain(sess, "tok", "not-a-mac", "sn"))

    def test_fetch_fountain_missing_sn_raises(self):
        sess, _ = _mock_session(200, {"result": {}})
        with self.assertRaises(ValueError):
            _run(pc.fetch_fountain(sess, "tok", "aabbcc112233", ""))

    def test_fetch_fountain_unknown_device_raises_cloud_error(self):
        sess, _ = _mock_session(
            200, {"error": {"code": 4001, "msg": "device does not exist"}}
        )
        with self.assertRaises(pc.PetkitCloudError) as ctx:
            _run(pc.fetch_fountain(sess, "tok", "deadbeef0000", "FAKESN"))
        self.assertNotIsInstance(ctx.exception, pc.PetkitAuthError)


class HeadersTest(unittest.TestCase):
    """Make sure the headers we send match what the iOS app sends."""

    def test_default_headers_match_capture(self):
        h = pc._DEFAULT_HEADERS
        self.assertEqual(h["X-Api-Version"], "13.6.0")
        self.assertEqual(h["X-Locale"], "de_DE")
        self.assertEqual(h["X-TimezoneId"], "Europe/Berlin")
        self.assertTrue(h["User-Agent"].startswith("PETKIT/"))
        self.assertIn("application/x-www-form-urlencoded", h["Content-Type"])

    def test_client_info_has_required_fields(self):
        ci = pc._CLIENT_INFO
        for field in ("locale", "source", "platform", "osVersion", "version", "token"):
            self.assertIn(field, ci, f"missing {field}")
        self.assertEqual(ci["platform"], "ios")
        self.assertEqual(ci["source"], "app.petkit-ios-oversea")


if __name__ == "__main__":
    unittest.main()
