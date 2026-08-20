"""The preflight's json2video auth check.

A key being present proves nothing: a rejected key is indistinguishable from a
good one until the first render, which is where it has already cost a wait.
These pin the four answers the check has to tell apart, without touching the
network — the whole point of the check is what it does when the network says no.
"""
import io
import unittest
import urllib.error
from unittest import mock

from umcares import doctor

ENV = {"JSON2VIDEO_API_KEY": "k"}


def http_error(code: int, body: str):
    return urllib.error.HTTPError("u", code, "err", {},
                                  io.BytesIO(body.encode()))


def responds(body: str):
    """A urlopen context manager returning `body`."""
    r = mock.MagicMock()
    r.read.return_value = body.encode()
    cm = mock.MagicMock()
    cm.__enter__.return_value = r
    return cm


class Auth(unittest.TestCase):
    def test_missing_key_needs_no_request(self):
        with mock.patch("urllib.request.urlopen") as u:
            ok, detail = doctor.json2video_auth({})
        self.assertFalse(ok)
        self.assertEqual(detail, "no key")
        u.assert_not_called()

    def test_rejected_key_fails(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=http_error(401, '{"message":"Invalid API key"}')):
            ok, detail = doctor.json2video_auth(ENV)
        self.assertFalse(ok)
        self.assertEqual(detail, "key rejected")

    def test_payload_rejection_still_means_the_key_is_good(self):
        """An empty `scenes` list is invalid on purpose; only auth is under test."""
        with mock.patch("urllib.request.urlopen",
                        side_effect=http_error(400, '{"message":"scenes is empty"}')):
            ok, detail = doctor.json2video_auth(ENV)
        self.assertTrue(ok, detail)

    def test_a_clean_response_means_the_key_is_good(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=responds('{"success":true}')):
            ok, _ = doctor.json2video_auth(ENV)
        self.assertTrue(ok)

    def test_offline_is_a_failure_not_a_pass(self):
        """Before a render, 'cannot reach the API' is as blocking as a bad key."""
        with mock.patch("urllib.request.urlopen",
                        side_effect=OSError("Name or service not known")):
            ok, detail = doctor.json2video_auth(ENV)
        self.assertFalse(ok)
        self.assertIn("unreachable", detail)

    def test_the_endpoint_override_is_honoured(self):
        seen = {}

        def capture(req, timeout=None):
            seen["url"] = req.full_url
            seen["key"] = req.headers.get("X-api-key")
            return responds("{}")

        with mock.patch("urllib.request.urlopen", side_effect=capture):
            doctor.json2video_auth(dict(ENV, JSON2VIDEO_ENDPOINT="https://x/v2/m"))
        self.assertEqual(seen["url"], "https://x/v2/m")
        self.assertEqual(seen["key"], "k")


if __name__ == "__main__":
    unittest.main()
