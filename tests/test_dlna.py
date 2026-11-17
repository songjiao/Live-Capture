"""DLNA 接收端的 SOAP / 设备描述测试（不需要真实手机）。"""

from __future__ import annotations

import unittest
import urllib.request
from xml.sax.saxutils import escape

from live_capture.dlna import DlnaCapture, parse_soap_action

SOAP_TEMPLATE = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body><u:{action} xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
    <InstanceID>0</InstanceID>{extra}
  </u:{action}></s:Body>
</s:Envelope>"""


class SoapParseTests(unittest.TestCase):
    def test_parse(self):
        service, action, node = parse_soap_action(SOAP_TEMPLATE.format(action="Play", extra="").encode())
        self.assertEqual(action, "Play")
        self.assertEqual(service, "urn:schemas-upnp-org:service:AVTransport:1")
        self.assertIsNotNone(node)


class DlnaCaptureTests(unittest.TestCase):
    def setUp(self):
        self.captured = []
        self.capture = DlnaCapture(
            name="测试电视", enable_ssdp=False, on_capture=self.captured.append
        ).start()
        self.base = f"http://127.0.0.1:{self.capture.http_port}"

    def tearDown(self):
        self.capture.stop()

    def _post(self, action: str, extra: str) -> str:
        body = SOAP_TEMPLATE.format(action=action, extra=extra).encode()
        req = urllib.request.Request(
            self.base + "/control",
            data=body,
            headers={
                "SOAPACTION": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
                "Content-Type": 'text/xml; charset="utf-8"',
            },
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            return resp.read().decode()

    def test_description(self):
        with urllib.request.urlopen(self.base + "/description.xml") as resp:
            text = resp.read().decode()
        self.assertIn("MediaRenderer:1", text)
        self.assertIn("测试电视", text)
        self.assertIn("AVTransport", text)

    def test_capture_url(self):
        url = "https://pull-l1.wxlivecdn.com/live/abc.m3u8?token=xyz&expire=1"
        self._post(
            "SetAVTransportURI",
            f"<CurrentURI>{escape(url)}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>",
        )
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(self.captured[0].url, url)
        self.assertEqual(self.captured[0].action, "SetAVTransportURI")

        self._post("Play", "")
        self.assertEqual(self.capture.transport_state, "PLAYING")
        payload = self._post("GetTransportInfo", "")
        self.assertIn("PLAYING", payload)

        self._post("Stop", "")
        self.assertEqual(self.capture.transport_state, "STOPPED")

    def test_plain_http_url_also_captured(self):
        url = "http://pull-l1.wxlivecdn.com/live/abc.flv?token=1"
        self._post("SetNextAVTransportURI", f"<CurrentURI>{url}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>")
        self.assertEqual(self.captured[0].url, url)

    def test_unknown_action_returns_fault(self):
        import urllib.error

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("NoSuchAction", "")
        self.assertEqual(ctx.exception.code, 500)

    def test_lan_ip_override(self):
        cap = DlnaCapture(enable_ssdp=False, lan_ip="192.168.99.7")
        self.assertEqual(cap.lan_ip, "192.168.99.7")
        self.assertTrue(cap.location.startswith("http://192.168.99.7:"))

    def test_ssdp_response_contains_location(self):
        response = self.capture._ssdp_response("urn:schemas-upnp-org:device:MediaRenderer:1")
        self.assertIn("HTTP/1.1 200 OK", response)
        self.assertIn(self.capture.location, response)
        self.assertIn("ST: urn:schemas-upnp-org:device:MediaRenderer:1", response)
        self.assertEqual(self.capture._match_search("ssdp:all")[0], "upnp:rootdevice")
        self.assertEqual(self.capture._match_search("urn:schemas-upnp-org:device:DimmableLight:1"), [])


if __name__ == "__main__":
    unittest.main()
