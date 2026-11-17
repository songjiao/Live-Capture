"""伪 DLNA 接收端：让微信把直播流地址"投"过来。

原理：手机投屏用的是标准 UPnP/DLNA 协议。我们在局域网里宣告自己是一台
MediaRenderer（电视），微信发现设备后，会通过 SOAP 的 ``SetAVTransportURI``
把真正的播放地址（视频号直播通常是 m3u8）发给"电视"。我们只收地址、
不真的解码播放，所以：不改微信客户端、不抓包、不装证书、不需要 root。

协议栈只用标准库：SSDP（UDP 组播 239.255.255.250:1900）+ 一个 HTTP 服务
（设备描述 / SOAP 控制 / 事件订阅）。

注意：本模块只实现 DLNA。iPhone 上的微信有时走 AirPlay 而不是 DLNA，
那种情况本工具发现不了设备（可改用支持 AirPlay 的现成工具，或换 Android 手机）。
"""

from __future__ import annotations

import logging
import random
import socket
import threading
import time
import uuid as uuid_lib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from xml.sax.saxutils import escape

log = logging.getLogger("wxcap.dlna")

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaRenderer:1"
SERVICE_TYPES = {
    "AVTransport": "urn:schemas-upnp-org:service:AVTransport:1",
    "RenderingControl": "urn:schemas-upnp-org:service:RenderingControl:1",
    "ConnectionManager": "urn:schemas-upnp-org:service:ConnectionManager:1",
}


def detect_lan_ip() -> str:
    """拿到本机在局域网里的 IP（不会真的发包）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


DESCRIPTION_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>{device_type}</deviceType>
    <friendlyName>{friendly_name}</friendlyName>
    <manufacturer>wxcap</manufacturer>
    <manufacturerURL>https://github.com/</manufacturerURL>
    <modelDescription>wxcap live stream capture</modelDescription>
    <modelName>wxcap Media Renderer</modelName>
    <modelNumber>1.0</modelNumber>
    <serialNumber>1</serialNumber>
    <UDN>{udn}</UDN>
    <dlna:X_DLNADOC>DMR-1.50</dlna:X_DLNADOC>
    <serviceList>
      <service>
        <serviceType>{avt}</serviceType>
        <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
        <SCPDURL>/scpd/avt.xml</SCPDURL>
        <controlURL>/control</controlURL>
        <eventSubURL>/event</eventSubURL>
      </service>
      <service>
        <serviceType>{rcs}</serviceType>
        <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
        <SCPDURL>/scpd/rcs.xml</SCPDURL>
        <controlURL>/control</controlURL>
        <eventSubURL>/event</eventSubURL>
      </service>
      <service>
        <serviceType>{cms}</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/scpd/cms.xml</SCPDURL>
        <controlURL>/control</controlURL>
        <eventSubURL>/event</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>
"""

SCPD_TEMPLATE = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
{actions}
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>TransportState</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>Volume</name><dataType>ui2</dataType></stateVariable>
  </serviceStateTable>
</scpd>
"""

ACTION_XML = """    <action><name>{name}</name><argumentList>{args}</argumentList></action>"""


def _action(name: str, args: list[tuple[str, str, str]] = ()) -> str:
    arguments = "".join(
        f'<argument><name>{n}</name><direction>{d}</direction><relatedStateVariable>{v}</relatedStateVariable></argument>'
        for n, d, v in args
    )
    return ACTION_XML.format(name=name, args=arguments)


@dataclass
class CapturedStream:
    url: str
    at: float
    action: str
    metadata: str = ""


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_text(node: ET.Element, name: str) -> str:
    for child in node.iter():
        if _strip_ns(child.tag) == name and child.text:
            return child.text.strip()
    return ""


class _SoapError(Exception):
    def __init__(self, code: int, description: str):
        super().__init__(description)
        self.code = code
        self.description = description


def parse_soap_action(body: bytes) -> tuple[str, str, ET.Element]:
    """从 SOAP 请求里取出 (service, action, 根节点)。"""
    root = ET.fromstring(body)
    body_node = None
    for child in root:
        if _strip_ns(child.tag) == "Body":
            body_node = child
            break
    if body_node is None or len(body_node) == 0:
        raise _SoapError(402, "Invalid Args")
    action_node = body_node[0]
    action = _strip_ns(action_node.tag)
    service = ""
    if action_node.tag.startswith("{"):
        service = action_node.tag[1:].split("}", 1)[0]
    return service, action, action_node


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "wxcap/0.1 UPnP/1.1"

    # -- 基础响应 ---------------------------------------------------------- #

    def log_message(self, fmt, *args):  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes = b"", content_type: str = "text/xml; charset=\"utf-8\"", extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _device(self) -> "DlnaCapture":
        return self.server.device  # type: ignore[attr-defined]

    # -- GET --------------------------------------------------------------- #

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        device = self._device()
        if path == "/description.xml":
            self._send(200, device.description_xml().encode("utf-8"))
        elif path.startswith("/scpd/"):
            name = path.rsplit("/", 1)[-1].replace(".xml", "")
            self._send(200, device.scpd_xml(name).encode("utf-8"))
        else:
            self._send(404, b"not found", "text/plain")

    # -- SOAP -------------------------------------------------------------- #

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        device = self._device()
        soap_action = self.headers.get("SOAPACTION", "").strip('"')
        service = soap_action.split("#", 1)[0] if "#" in soap_action else ""
        try:
            if not service or "#" not in soap_action:
                _, _, node = parse_soap_action(body)
                service, action = "", _strip_ns(node.tag)
            else:
                action = soap_action.split("#", 1)[1]
            inner = device.handle_action(service, action, body)
            payload = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                f"<s:Body><u:{action}Response xmlns:u=\"{service}\">{inner}</u:{action}Response></s:Body>"
                "</s:Envelope>"
            )
            self._send(200, payload.encode("utf-8"), extra={"EXT": ""})
        except _SoapError as exc:
            fault = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                "<s:Fault><faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring>"
                f"<detail><UPnPError xmlns=\"urn:schemas-upnp-org:control-1-0\">"
                f"<errorCode>{exc.code}</errorCode><errorDescription>{escape(exc.description)}</errorDescription>"
                "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
            )
            self._send(500, fault.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("处理 SOAP 请求失败: %s", exc)
            self._send(500, b"error", "text/plain")

    # -- 事件订阅 ---------------------------------------------------------- #

    def do_SUBSCRIBE(self):  # noqa: N802
        sid = f"uuid:{uuid_lib.uuid4()}"
        self._send(
            200,
            b"",
            extra={"SID": sid, "TIMEOUT": "Second-1800", "SERVER": "wxcap/0.1 UPnP/1.1"},
        )

    def do_UNSUBSCRIBE(self):  # noqa: N802
        self._send(200, b"")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, device):
        super().__init__(addr, handler)
        self.device = device


class DlnaCapture:
    """在局域网里扮演一台电视，捕获投屏过来的直播地址。"""

    def __init__(
        self,
        name: str = "wxcap 电视",
        http_port: int = 0,
        ssdp_port: int = SSDP_PORT,
        on_capture: Callable[[CapturedStream], None] | None = None,
        enable_ssdp: bool = True,
        udn: str | None = None,
        lan_ip: str | None = None,
    ):
        self.name = name
        self.http_port = http_port
        self.ssdp_port = ssdp_port
        self.enable_ssdp = enable_ssdp
        self.on_capture = on_capture
        self.udn = udn or f"uuid:{uuid_lib.uuid4()}"
        # 多网卡 / 开着 VPN 时自动探测可能选错网卡，允许手动指定
        self.lan_ip = lan_ip or detect_lan_ip()
        self.transport_state = "STOPPED"
        self.current_uri = ""
        self.current_metadata = ""
        self.volume = 50
        self.muted = False
        self.captures: list[CapturedStream] = []
        self._httpd: _Server | None = None
        self._http_thread: threading.Thread | None = None
        self._ssdp_sock: socket.socket | None = None
        self._ssdp_thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- 生命周期 ---------------------------------------------------------- #

    def start(self) -> "DlnaCapture":
        self._httpd = _Server(("0.0.0.0", self.http_port), _Handler, self)
        self.http_port = self._httpd.server_address[1]
        self._http_thread = threading.Thread(target=self._httpd.serve_forever, name="wxcap-http", daemon=True)
        self._http_thread.start()
        if self.enable_ssdp:
            self._start_ssdp()
        log.info("DLNA 已启动: http://%s:%d/description.xml (名称: %s)", self.lan_ip, self.http_port, self.name)
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._ssdp_sock:
            try:
                self._ssdp_sock.close()
            except OSError:
                pass
            self._ssdp_sock = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.stop()

    @property
    def location(self) -> str:
        return f"http://{self.lan_ip}:{self.http_port}/description.xml"

    # -- SSDP -------------------------------------------------------------- #

    def _start_ssdp(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            sock.bind(("", self.ssdp_port))
        except OSError as exc:
            sock.close()
            log.error(
                "绑定 SSDP 端口 %d 失败（%s）。可能已有其它 DLNA 服务占用，"
                "请先停掉它们；改端口会导致手机搜不到设备。", self.ssdp_port, exc,
            )
            raise
        group = socket.inet_aton(SSDP_ADDR)
        try:
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                group + socket.inet_aton(self.lan_ip),
            )
        except OSError:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, group + socket.inet_aton("0.0.0.0"))
        sock.settimeout(1.0)
        self._ssdp_sock = sock
        self._ssdp_thread = threading.Thread(target=self._ssdp_loop, name="wxcap-ssdp", daemon=True)
        self._ssdp_thread.start()
        threading.Thread(target=self._notify_loop, name="wxcap-notify", daemon=True).start()

    def _ssdp_loop(self) -> None:
        assert self._ssdp_sock
        while not self._stop.is_set():
            try:
                data, addr = self._ssdp_sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("utf-8", errors="replace")
            if not text.startswith("M-SEARCH"):
                continue
            st = ""
            for line in text.splitlines():
                if line.lower().startswith("st:"):
                    st = line.split(":", 1)[1].strip()
                    break
            targets = self._match_search(st)
            for target in targets:
                resp = self._ssdp_response(target)
                try:
                    # 随机延迟，避免多设备同时应答撞包
                    time.sleep(random.uniform(0, 0.05))
                    self._ssdp_sock.sendto(resp.encode("utf-8"), addr)
                except OSError:
                    pass
            if targets:
                log.debug("应答 SSDP 搜索 %s -> %s", st, addr)

    def _match_search(self, st: str) -> list[str]:
        st = st.strip()
        if st in ("ssdp:all", "*"):
            return ["upnp:rootdevice", self.udn, DEVICE_TYPE, *SERVICE_TYPES.values()]
        if st in (self.udn, "upnp:rootdevice", DEVICE_TYPE, *SERVICE_TYPES.values()):
            return [st]
        return []

    def _ssdp_response(self, st: str) -> str:
        usn = self.udn if st == self.udn else f"{self.udn}::{st}"
        return (
            "HTTP/1.1 200 OK\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            "DATE: " + time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()) + "\r\n"
            "EXT:\r\n"
            f"LOCATION: {self.location}\r\n"
            "SERVER: Linux/1.0 UPnP/1.1 wxcap/0.1\r\n"
            f"ST: {st}\r\n"
            f"USN: {usn}\r\n"
            "BOOTID.UPNP.ORG: 1\r\n"
            "CONFIGID.UPNP.ORG: 1\r\n"
            "\r\n"
        )

    def _notify_loop(self) -> None:
        targets = ["upnp:rootdevice", self.udn, DEVICE_TYPE, *SERVICE_TYPES.values()]
        while not self._stop.is_set():
            for st in targets:
                usn = self.udn if st == self.udn else f"{self.udn}::{st}"
                msg = (
                    "NOTIFY * HTTP/1.1\r\n"
                    "HOST: 239.255.255.250:1900\r\n"
                    "CACHE-CONTROL: max-age=1800\r\n"
                    f"LOCATION: {self.location}\r\n"
                    "SERVER: Linux/1.0 UPnP/1.1 wxcap/0.1\r\n"
                    f"NT: {st}\r\nNTS: ssdp:alive\r\n"
                    f"USN: {usn}\r\n"
                    "BOOTID.UPNP.ORG: 1\r\nCONFIGID.UPNP.ORG: 1\r\n\r\n"
                )
                try:
                    self._ssdp_sock.sendto(msg.encode("utf-8"), (SSDP_ADDR, self.ssdp_port))  # type: ignore[union-attr]
                except OSError:
                    pass
            self._stop.wait(30)

    # -- 设备描述 ---------------------------------------------------------- #

    def description_xml(self) -> str:
        return DESCRIPTION_TEMPLATE.format(
            device_type=DEVICE_TYPE,
            friendly_name=escape(self.name),
            udn=self.udn,
            avt=SERVICE_TYPES["AVTransport"],
            rcs=SERVICE_TYPES["RenderingControl"],
            cms=SERVICE_TYPES["ConnectionManager"],
        )

    def scpd_xml(self, name: str) -> str:
        if name == "avt":
            actions = "".join(
                _action(
                    n,
                    [("InstanceID", "in", "TransportState"), ("CurrentURI", "in", "TransportState"),
                     ("CurrentURIMetaData", "in", "TransportState")],
                )
                for n in ("SetAVTransportURI", "SetNextAVTransportURI")
            )
            actions += "".join(
                _action(n, [("InstanceID", "in", "TransportState")])
                for n in ("Play", "Pause", "Stop", "Next", "Previous", "GetTransportInfo",
                          "GetPositionInfo", "GetMediaInfo", "GetTransportSettings",
                          "GetCurrentTransportActions", "SetPlayMode")
            )
            actions += _action("Seek", [("InstanceID", "in", "TransportState"), ("Unit", "in", "TransportState"), ("Target", "in", "TransportState")])
            return SCPD_TEMPLATE.format(actions=actions)
        if name == "rcs":
            actions = _action("GetVolume", [("InstanceID", "in", "Volume"), ("Channel", "in", "Volume"), ("CurrentVolume", "out", "Volume")])
            actions += _action("SetVolume", [("InstanceID", "in", "Volume"), ("Channel", "in", "Volume"), ("DesiredVolume", "in", "Volume")])
            actions += _action("GetMute", [("InstanceID", "in", "Volume"), ("Channel", "in", "Volume"), ("CurrentMute", "out", "Volume")])
            actions += _action("SetMute", [("InstanceID", "in", "Volume"), ("Channel", "in", "Volume"), ("DesiredMute", "in", "Volume")])
            return SCPD_TEMPLATE.format(actions=actions)
        actions = _action("GetProtocolInfo", [("Source", "out", "TransportState"), ("Sink", "out", "TransportState")])
        actions += _action("GetCurrentConnectionIDs", [("ConnectionIDs", "out", "TransportState")])
        actions += _action("GetCurrentConnectionInfo", [("ConnectionID", "in", "TransportState"), ("RcsID", "out", "TransportState"), ("AVTransportID", "out", "TransportState"), ("ProtocolInfo", "out", "TransportState"), ("PeerConnectionManager", "out", "TransportState"), ("PeerConnectionID", "out", "TransportState"), ("Direction", "out", "TransportState"), ("Status", "out", "TransportState")])
        return SCPD_TEMPLATE.format(actions=actions)

    # -- SOAP 动作 --------------------------------------------------------- #

    def handle_action(self, service: str, action: str, body: bytes) -> str:
        node = None
        if body:
            try:
                _, _, node = parse_soap_action(body)
            except ET.ParseError as exc:
                raise _SoapError(402, "Invalid Args") from exc

        if action in ("SetAVTransportURI", "SetNextAVTransportURI"):
            assert node is not None
            uri = _find_text(node, "CurrentURI")
            metadata = _find_text(node, "CurrentURIMetaData")
            if uri:
                self.current_uri = uri
                self.current_metadata = metadata
                self.transport_state = "STOPPED"
                stream = CapturedStream(url=uri, at=time.time(), action=action, metadata=metadata)
                self.captures.append(stream)
                log.info("捕获到投屏地址（%s）: %s", action, uri)
                self._notify(stream)
            return ""
        if action == "Play":
            self.transport_state = "PLAYING"
            return ""
        if action == "Pause":
            self.transport_state = "PAUSED_PLAYBACK"
            return ""
        if action == "Stop":
            self.transport_state = "STOPPED"
            return ""
        if action == "Seek":
            return ""
        if action == "Next" or action == "Previous":
            return ""
        if action == "GetTransportInfo":
            return (
                f"<CurrentTransportState>{self.transport_state}</CurrentTransportState>"
                "<CurrentTransportStatus>OK</CurrentTransportStatus>"
                "<CurrentSpeed>1</CurrentSpeed>"
            )
        if action == "GetPositionInfo":
            return (
                "<Track>1</Track><TrackDuration>00:00:00</TrackDuration>"
                f"<TrackMetaData>{escape(self.current_metadata)}</TrackMetaData>"
                f"<TrackURI>{escape(self.current_uri)}</TrackURI>"
                "<RelTime>00:00:00</RelTime><AbsTime>00:00:00</AbsTime>"
                "<RelCount>0</RelCount><AbsCount>0</AbsCount>"
            )
        if action == "GetMediaInfo":
            return (
                "<NrTracks>1</NrTracks><MediaDuration>00:00:00</MediaDuration>"
                f"<CurrentURI>{escape(self.current_uri)}</CurrentURI>"
                f"<CurrentURIMetaData>{escape(self.current_metadata)}</CurrentURIMetaData>"
                "<NextURI></NextURI><NextURIMetaData></NextURIMetaData>"
                "<PlayMedium>NETWORK</PlayMedium>"
                "<RecordMedium>NOT_IMPLEMENTED</RecordMedium>"
                "<WriteStatus>NOT_IMPLEMENTED</WriteStatus>"
            )
        if action == "GetTransportSettings":
            return "<PlayMode>NORMAL</PlayMode><RecQualityMode>NOT_IMPLEMENTED</RecQualityMode>"
        if action == "GetCurrentTransportActions":
            return "<Actions>Play,Stop,Pause,Seek</Actions>"
        if action == "SetPlayMode":
            return ""
        if action == "GetVolume":
            return f"<CurrentVolume>{self.volume}</CurrentVolume>"
        if action == "SetVolume":
            volume = _find_text(node, "DesiredVolume") if node is not None else ""
            if volume.isdigit():
                self.volume = int(volume)
            return ""
        if action == "GetMute":
            return f"<CurrentMute>{1 if self.muted else 0}</CurrentMute>"
        if action == "SetMute":
            mute = _find_text(node, "DesiredMute") if node is not None else ""
            self.muted = mute in ("1", "true", "True")
            return ""
        if action == "GetProtocolInfo":
            return (
                "<Source></Source><Sink>"
                "http-get:*:video/mp4:*,"
                "http-get:*:video/x-flv:*,"
                "http-get:*:application/vnd.apple.mpegurl:*,"
                "http-get:*:application/x-mpegURL:*,"
                "http-get:*:video/mpeg:*,"
                "http-get:*:video/MP2T:*"
                "</Sink>"
            )
        if action == "GetCurrentConnectionIDs":
            return "<ConnectionIDs>0</ConnectionIDs>"
        if action == "GetCurrentConnectionInfo":
            return (
                "<RcsID>0</RcsID><AVTransportID>0</AVTransportID>"
                "<ProtocolInfo>http-get:*:*:*</ProtocolInfo>"
                "<PeerConnectionManager></PeerConnectionManager>"
                "<PeerConnectionID>-1</PeerConnectionID>"
                "<Direction>Input</Direction><Status>OK</Status>"
            )
        if action in ("GetDeviceCapabilities", "GetSortCapabilities", "GetSearchCapabilities"):
            return ""
        log.debug("未实现的 SOAP 动作: %s %s", service, action)
        raise _SoapError(401, "Invalid Action")

    def _notify(self, stream: CapturedStream) -> None:
        callback = self.on_capture
        if callback is None:
            return
        try:
            callback(stream)
        except Exception:  # noqa: BLE001 - 回调失败不能拖垮投屏
            log.exception("on_capture 回调出错")
