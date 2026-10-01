from __future__ import annotations

import socket
import sys
from unittest.mock import Mock

import pytest

from trade_news_analysis import cli


def test_occupied_port_fails_before_loading_application(monkeypatch: pytest.MonkeyPatch) -> None:
    server = Mock(side_effect=AssertionError("Application must not start"))
    monkeypatch.setattr(cli.uvicorn, "Server", server)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        monkeypatch.setattr(sys, "argv", ["trade-news", "serve", "--port", str(port)])
        with pytest.raises(SystemExit) as caught:
            cli.main()
    assert caught.value.code != 0
    server.assert_not_called()


@pytest.mark.parametrize("failure", [None, RuntimeError("Startup failed")])
def test_serve_reserves_port_and_releases_socket(
    monkeypatch: pytest.MonkeyPatch, failure: Exception | None,
) -> None:
    sockets_used: list[socket.socket] = []

    def run(*, sockets: list[socket.socket]) -> None:
        sock = sockets[0]
        sockets_used.append(sock)
        with socket.socket() as competing:
            competing.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with pytest.raises(OSError):
                competing.bind(sock.getsockname())
        if failure:
            raise failure

    server = Mock(started=True, run=Mock(side_effect=run))
    monkeypatch.setattr(cli.uvicorn, "Server", Mock(return_value=server))
    monkeypatch.setattr(sys, "argv", ["trade-news", "serve", "--port", "0"])
    if failure:
        with pytest.raises(RuntimeError, match="Startup failed"):
            cli.main()
    else:
        cli.main()
    assert len(sockets_used) == 1
    assert sockets_used[0].fileno() == -1


def test_unsuccessful_startup_returns_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.uvicorn, "Server", Mock(return_value=Mock(started=False)))
    monkeypatch.setattr(sys, "argv", ["trade-news", "serve", "--port", "0"])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code != 0
