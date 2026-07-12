from unittest.mock import MagicMock, patch

from src.opendtu_client import OpenDTUClient


def _fake_response(payload: bytes):
    resp = MagicMock()
    resp.read.return_value = payload
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


@patch("src.opendtu_client.urllib.request.urlopen")
def test_get_sends_basic_auth_header_when_credentials_set(mock_urlopen):
    mock_urlopen.return_value = _fake_response(b'{"inverters": []}')
    client = OpenDTUClient("http://192.168.1.50", username="admin", password="secret")
    client.get_live_power_w(["123"])

    sent_request = mock_urlopen.call_args[0][0]
    assert sent_request.get_header("Authorization") == "Basic YWRtaW46c2VjcmV0"


@patch("src.opendtu_client.urllib.request.urlopen")
def test_get_sends_no_auth_header_by_default(mock_urlopen):
    mock_urlopen.return_value = _fake_response(b'{"inverters": []}')
    client = OpenDTUClient("http://192.168.1.50")
    client.get_live_power_w(["123"])

    sent_request = mock_urlopen.call_args[0][0]
    assert sent_request.get_header("Authorization") is None


@patch("src.opendtu_client.urllib.request.urlopen")
def test_post_also_sends_basic_auth_header(mock_urlopen):
    mock_urlopen.return_value = _fake_response(b"{}")
    client = OpenDTUClient("http://192.168.1.50", username="admin", password="secret")
    client.set_relative_limit_pct("123", 50)

    sent_request = mock_urlopen.call_args[0][0]
    assert sent_request.get_header("Authorization") == "Basic YWRtaW46c2VjcmV0"


@patch("src.opendtu_client.urllib.request.urlopen")
def test_username_without_password_still_authenticates(mock_urlopen):
    mock_urlopen.return_value = _fake_response(b'{"inverters": []}')
    client = OpenDTUClient("http://192.168.1.50", username="admin", password=None)
    client.get_live_power_w(["123"])

    sent_request = mock_urlopen.call_args[0][0]
    assert sent_request.get_header("Authorization") == "Basic YWRtaW46"
