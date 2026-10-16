"""
`http.tls: false` - the HTTP listener speaks plain HTTP/WS so that a
TLS-terminating reverse proxy without TLS backend support can sit in front of
it. The clients below stand in for that proxy: the browser-facing side is
HTTPS, so cookies stay `Secure` and are sent on by a browser-like cookie policy.
"""

import os
import signal
import socket
import ssl
import struct
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest
import requests
import yaml
from websocket import WebSocketBadStatusException, create_connection

from .api_client import admin_client, sdk
from .conftest import ProcessManager, WarpgateProcess, cargo_root, binary_path
from .test_http_common import *  # noqa
from .util import _wait_timeout, wait_port


def plain_url(wg: WarpgateProcess) -> str:
    return f"http://localhost:{wg.http_port}"


def tls_url(wg: WarpgateProcess) -> str:
    return f"https://localhost:{wg.http_port}"


def insecure_tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def speaks_tls(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=5) as raw:
            with insecure_tls_context().wrap_socket(raw, server_hostname="localhost"):
                return True
    except (ssl.SSLError, OSError):
        return False


def raw_http_get(port: int, path: str) -> bytes:
    """A request on a bare TCP socket: no TLS handshake can be involved."""
    with socket.create_connection(("localhost", port), timeout=5) as s:
        s.sendall(
            f"GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
        )
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    return data


def wait_http(url: str, timeout=60, verify=False):
    def wait():
        while True:
            try:
                if requests.get(f"{url}/@warpgate/api/info", verify=verify, timeout=5).ok:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.2)

    _wait_timeout(wait, f"{url} is not serving", timeout=timeout)


class BrowserSession(requests.Session):
    """Like a browser behind the TLS proxy: keeps sending `Secure` cookies,
    since from its side the connection is HTTPS. (`requests` copies cookies into
    a default-policy jar per request, so they are relaxed on the way out.)"""

    def prepare_request(self, request):
        for cookie in self.cookies:
            cookie.secure = False
        return super().prepare_request(request)


def browser_session() -> requests.Session:
    return BrowserSession()


def cookie_header(session: requests.Session) -> str:
    return "; ".join(f"{k}={v}" for k, v in session.cookies.get_dict().items())


def create_user_with_echo_target(url: str, echo_server_port: int):
    with admin_client(url) as api:
        role = api.create_role(sdk.RoleDataRequest(name=f"role-{uuid4()}"))
        user = api.create_user(sdk.CreateUserRequest(username=f"user-{uuid4()}"))
        api.create_password_credential(user.id, sdk.NewPasswordCredential(password="123"))
        api.add_user_role(user.id, role.id)
        target = api.create_target(
            sdk.TargetDataRequest(
                name=f"echo-{uuid4()}",
                require_approval=False,
                ticket_requests_disabled=False,
                ticket_require_approval=False,
                options=sdk.TargetOptions(
                    sdk.TargetOptionsTargetHTTPOptions(
                        kind="Http",
                        headers={},
                        url=f"http://localhost:{echo_server_port}",
                        tls=sdk.Tls(mode=sdk.TlsMode.DISABLED, verify=False),
                    )
                ),
            )
        )
        api.add_target_role(target.id, role.id)
    return user, target


def login(session: requests.Session, url: str, username: str, verify=True):
    response = session.post(
        f"{url}/@warpgate/api/auth/login",
        json={"username": username, "password": "123"},
        verify=verify,
    )
    assert response.status_code // 100 == 2, response.text
    return response


def start_plaintext_wg(processes: ProcessManager, timeout, config_patch=None, **kwargs):
    patch = {"http": {"tls": False}}
    for key, value in (config_patch or {}).get("http", {}).items():
        patch["http"][key] = value
    wg = processes.start_wg(config_patch=patch, **kwargs)
    wait_port(wg.http_port, for_process=wg.process, recv=False, timeout=timeout)
    wait_http(plain_url(wg), timeout=timeout)
    return wg


def start_failing_wg(processes: ProcessManager, config_patch, log_path: Path) -> str:
    """Start an instance that is expected to refuse to start; return its log."""
    with log_path.open("w") as log:
        wg = processes.start_wg(
            config_patch=config_patch, stdout=log, stderr=subprocess.STDOUT
        )
        try:
            wg.process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            pytest.fail("Warpgate started although its HTTP TLS setup is broken")
    assert wg.process.returncode != 0
    return log_path.read_text()


@pytest.fixture(scope="module")
def plaintext_wg(processes: ProcessManager, timeout) -> WarpgateProcess:
    return start_plaintext_wg(processes, timeout)


class TestPlaintextHTTP:
    def test_serves_http_without_tls_handshake(self, plaintext_wg: WarpgateProcess):
        response = raw_http_get(plaintext_wg.http_port, "/@warpgate/api/info")
        assert response.startswith(b"HTTP/1.1 200"), response[:200]

        # A TLS client gets no handshake out of it
        assert not speaks_tls(plaintext_wg.http_port)
        with pytest.raises(requests.exceptions.SSLError):
            requests.get(f"{tls_url(plaintext_wg)}/@warpgate/api/info", verify=False, timeout=5)

    def test_web_ui_and_admin_api(self, plaintext_wg: WarpgateProcess):
        url = plain_url(plaintext_wg)

        for path in ["/@warpgate", "/@warpgate/admin"]:
            response = requests.get(f"{url}{path}", timeout=5)
            assert response.status_code == 200, path
            assert "text/html" in response.headers["content-type"]
            # Browser-facing security headers are untouched
            assert "max-age" in response.headers["strict-transport-security"]
            assert "content-security-policy" in response.headers

        with admin_client(url) as api:
            assert any(u.username == "admin" for u in api.get_users())

    def test_login_and_authenticated_api(
        self, echo_server_port, plaintext_wg: WarpgateProcess
    ):
        url = plain_url(plaintext_wg)
        user, target = create_user_with_echo_target(url, echo_server_port)

        session = browser_session()
        assert session.get(f"{url}/@warpgate/api/info").json().get("username") is None

        response = login(session, url, user.username)
        # The session cookie is still made for an HTTPS browser
        set_cookie = response.headers["set-cookie"].lower()
        assert "secure" in set_cookie and "httponly" in set_cookie

        assert session.get(f"{url}/@warpgate/api/info").json()["username"] == user.username
        targets = session.get(f"{url}/@warpgate/api/targets").json()
        assert target.name in [t["name"] for t in targets]

        # Proxied HTTP target, with streaming body passthrough
        response = session.get(
            f"{url}/some/path?a=b&warpgate-target={target.name}", allow_redirects=False
        )
        assert response.status_code == 200
        assert response.json()["path"] == "/some/path"

        response = session.post(f"{url}/@warpgate/api/auth/logout")
        assert response.status_code // 100 == 2
        assert session.get(f"{url}/@warpgate/api/info").json().get("username") is None

    def test_failed_login_is_rejected(self, echo_server_port, plaintext_wg):
        url = plain_url(plaintext_wg)
        user, _ = create_user_with_echo_target(url, echo_server_port)
        response = browser_session().post(
            f"{url}/@warpgate/api/auth/login",
            json={"username": user.username, "password": "wrong"},
        )
        assert response.status_code // 100 != 2

    def test_websocket_upgrades(self, echo_server_port, plaintext_wg: WarpgateProcess):
        url = plain_url(plaintext_wg)
        user, target = create_user_with_echo_target(url, echo_server_port)

        # Unauthenticated upgrades are still refused
        with pytest.raises(WebSocketBadStatusException):
            create_connection(
                f"ws://localhost:{plaintext_wg.http_port}/@warpgate/api/auth/web-auth-requests/stream"
            )

        session = browser_session()
        login(session, url, user.username)
        cookie = cookie_header(session)

        # Warpgate's own websocket endpoint
        ws = create_connection(
            f"ws://localhost:{plaintext_wg.http_port}/@warpgate/api/auth/web-auth-requests/stream",
            cookie=cookie,
        )
        assert ws.getstatus() == 101
        ws.close()

        # Websocket proxied to an HTTP target
        ws = create_connection(
            f"ws://localhost:{plaintext_wg.http_port}/socket?warpgate-target={target.name}",
            cookie=cookie,
        )
        ws.send("test")
        assert ws.recv() == "test"
        ws.send_binary(b"\x00binary")
        assert ws.recv() == b"\x00binary"
        ws.ping()
        ws.close()

    def test_abrupt_disconnects_and_reconnects(
        self, echo_server_port, plaintext_wg: WarpgateProcess
    ):
        url = plain_url(plaintext_wg)
        port = plaintext_wg.http_port
        user, target = create_user_with_echo_target(url, echo_server_port)
        session = browser_session()
        login(session, url, user.username)
        cookie = cookie_header(session)
        ws_url = f"ws://localhost:{port}/socket?warpgate-target={target.name}"

        for _ in range(3):
            # Websocket dropped without a close frame
            ws = create_connection(ws_url, cookie=cookie)
            ws.send("before drop")
            assert ws.recv() == "before drop"
            ws.shutdown()

            # Half-sent request
            with socket.create_connection(("localhost", port), timeout=5) as s:
                s.sendall(b"GET /@warpgate/api/info HTTP/1.1\r\nHost: loc")

            # Connection reset (RST) right after connecting
            s = socket.create_connection(("localhost", port), timeout=5)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            s.close()

            # A TLS client hello the listener can't make sense of
            assert not speaks_tls(port)

            # Disconnect while the response is being written
            with socket.create_connection(("localhost", port), timeout=5) as s:
                s.sendall(b"GET /@warpgate HTTP/1.1\r\nHost: localhost\r\n\r\n")
                s.recv(1)

            # ...and the listener keeps serving, websockets included
            assert session.get(f"{url}/@warpgate/api/info").json()["username"] == user.username
            ws = create_connection(ws_url, cookie=cookie)
            ws.send("after reconnect")
            assert ws.recv() == "after reconnect"
            ws.close()

        assert plaintext_wg.process.poll() is None

    def test_healthcheck_and_check_commands(self, plaintext_wg: WarpgateProcess):
        for command in ["healthcheck", "check"]:
            result = subprocess.run(
                [
                    os.path.join(cargo_root, binary_path),
                    "--config",
                    str(plaintext_wg.config_path),
                    command,
                ],
                cwd=cargo_root,
                capture_output=True,
                timeout=60,
            )
            assert result.returncode == 0, (command, result.stdout, result.stderr)


class TestPlaintextHTTPStartup:
    def test_missing_certificates_do_not_prevent_startup(
        self, processes: ProcessManager, timeout
    ):
        wg = start_plaintext_wg(
            processes,
            timeout,
            config_patch={
                "http": {
                    "certificate": "/nonexistent/tls.certificate.pem",
                    "key": "/nonexistent/tls.key.pem",
                    "sni_certificates": [
                        {"certificate": "/nonexistent/sni.pem", "key": "/nonexistent/sni.key"}
                    ],
                }
            },
        )
        assert raw_http_get(wg.http_port, "/@warpgate/api/info").startswith(b"HTTP/1.1 200")

        # Nothing was deleted or rewritten in the config
        config = yaml.safe_load(wg.config_path.open())
        assert config["http"]["tls"] is False
        assert config["http"]["certificate"] == "/nonexistent/tls.certificate.pem"
        assert Path(config["mysql"]["certificate"]).exists()
        assert Path(config["mysql"]["key"]).exists()

    def test_tls_enabled_with_missing_certificate_fails(
        self, processes: ProcessManager, tmp_path: Path
    ):
        log = start_failing_wg(
            processes,
            {"http": {"tls": True, "certificate": "/nonexistent/tls.certificate.pem"}},
            tmp_path / "wg.log",
        )
        assert "HTTP listener: TLS setup failed" in log

    def test_tls_enabled_with_invalid_certificate_fails(
        self, processes: ProcessManager, tmp_path: Path
    ):
        bogus = tmp_path / "bogus.pem"
        bogus.write_text("not a certificate\n")
        log = start_failing_wg(
            processes, {"http": {"certificate": str(bogus)}}, tmp_path / "wg.log"
        )
        assert "HTTP listener: TLS setup failed" in log

    def test_tls_enabled_without_certificate_fails(
        self, processes: ProcessManager, tmp_path: Path
    ):
        log = start_failing_wg(
            processes,
            {"http": {"certificate": "", "key": ""}},
            tmp_path / "wg.log",
        )
        assert "HTTP listener: no TLS certificate/key configured" in log


class TestExplicitTLS:
    def test_explicit_tls_true_serves_https_and_wss(
        self, echo_server_port, processes: ProcessManager, timeout
    ):
        wg = processes.start_wg(config_patch={"http": {"tls": True}})
        wait_port(wg.http_port, for_process=wg.process, recv=False, timeout=timeout)
        wait_http(tls_url(wg), timeout=timeout)
        url = tls_url(wg)

        assert speaks_tls(wg.http_port)
        assert not raw_http_get(wg.http_port, "/@warpgate/api/info").startswith(b"HTTP/1.1 200")

        user, target = create_user_with_echo_target(url, echo_server_port)
        session = requests.Session()
        login(session, url, user.username, verify=False)
        info = session.get(f"{url}/@warpgate/api/info", verify=False).json()
        assert info["username"] == user.username

        ws = create_connection(
            f"wss://localhost:{wg.http_port}/socket?warpgate-target={target.name}",
            cookie=cookie_header(session),
            sslopt={"cert_reqs": ssl.CERT_NONE},
        )
        ws.send("test")
        assert ws.recv() == "test"
        ws.close()

    def test_config_without_tls_key_serves_https(self, processes: ProcessManager, timeout):
        first = processes.start_wg()
        wait_port(first.http_port, for_process=first.process, recv=False, timeout=timeout)
        first.process.send_signal(signal.SIGINT)
        first.process.wait(timeout=30)

        # A config written before the option existed
        config = yaml.safe_load(first.config_path.open())
        config["http"].pop("tls", None)
        with first.config_path.open("w") as f:
            yaml.safe_dump(config, f)

        wg = processes.start_wg(share_with=first)
        assert "tls" not in yaml.safe_load(wg.config_path.open())["http"]
        wait_port(wg.http_port, for_process=wg.process, recv=False, timeout=timeout)
        wait_http(tls_url(wg), timeout=timeout)
        assert speaks_tls(wg.http_port)


class TestHTTPTLSReload:
    def test_switches_listener_mode_on_config_reload(
        self, processes: ProcessManager, timeout
    ):
        wg = start_plaintext_wg(processes, timeout)

        def set_tls(value: bool):
            config = yaml.safe_load(wg.config_path.open())
            config["http"]["tls"] = value
            with wg.config_path.open("w") as f:
                yaml.safe_dump(config, f)

        set_tls(True)
        wait_http(tls_url(wg), timeout=timeout)
        assert not raw_http_get(wg.http_port, "/@warpgate/api/info").startswith(b"HTTP/1.1 200")

        set_tls(False)
        wait_http(plain_url(wg), timeout=timeout)
        assert not speaks_tls(wg.http_port)


@pytest.mark.parametrize("tls", [False, True], ids=["plaintext", "tls"])
def test_shutdown_with_open_connections(
    tls, echo_server_port, processes: ProcessManager, timeout
):
    wg = processes.start_wg(config_patch={"http": {"tls": tls}})
    wait_port(wg.http_port, for_process=wg.process, recv=False, timeout=timeout)
    url = tls_url(wg) if tls else plain_url(wg)
    wait_http(url, timeout=timeout)

    user, target = create_user_with_echo_target(url, echo_server_port)
    session = browser_session()
    login(session, url, user.username, verify=False)
    ws = create_connection(
        f"{'wss' if tls else 'ws'}://localhost:{wg.http_port}/socket?warpgate-target={target.name}",
        cookie=cookie_header(session),
        sslopt={"cert_reqs": ssl.CERT_NONE},
    )
    ws.send("test")
    assert ws.recv() == "test"
    idle = socket.create_connection(("localhost", wg.http_port), timeout=5)

    wg.process.send_signal(signal.SIGINT)
    try:
        assert wg.process.wait(timeout=30) == 0
    finally:
        idle.close()
        ws.shutdown()
    with pytest.raises(OSError):
        socket.create_connection(("localhost", wg.http_port), timeout=2).close()
