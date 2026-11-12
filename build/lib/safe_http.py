"""Cliente HTTP con protección SSRF que evita el DNS rebinding.

El problema clásico: la validación resuelve el nombre, comprueba que la IP es
pública, y después la librería vuelve a resolver el nombre al conectar. Un
servidor malicioso puede responder distinto en cada consulta.

Aquí la validación y la conexión usan la misma resolución: el socket se abre
directamente a una IP ya comprobada. El nombre original se conserva para SNI y
para verificar el certificado TLS, así HTTPS sigue siendo seguro.
"""

from __future__ import annotations

import socket
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError

from desenlace_core import UnsafeUrlError, validate_url, validated_addresses


def _pinned_socket(conn: Any, dns_host: str, port: int, allow_private: bool) -> socket.socket:
    addresses = validated_addresses(dns_host, port, allow_private=allow_private)
    last_error: OSError | None = None
    for address in addresses:
        try:
            sock = socket.create_connection(
                (address, port),
                conn.timeout,
                source_address=conn.source_address,
            )
        except OSError as exc:
            last_error = exc
            continue
        for option in conn.socket_options or []:
            sock.setsockopt(*option)
        return sock
    raise NewConnectionError(
        conn, f"No se pudo establecer la conexión con {dns_host}: {last_error}"
    )


def _pool_classes(allow_private: bool) -> dict[str, type]:
    """Crea clases de conexión ligadas a la política de red de una sesión."""

    class PinnedHTTPConnection(HTTPConnection):
        def _new_conn(self) -> socket.socket:  # type: ignore[override]
            return _pinned_socket(self, self._dns_host, self.port, allow_private)

    class PinnedHTTPSConnection(HTTPSConnection):
        def _new_conn(self) -> socket.socket:  # type: ignore[override]
            return _pinned_socket(self, self._dns_host, self.port, allow_private)

    class PinnedHTTPPool(HTTPConnectionPool):
        ConnectionCls = PinnedHTTPConnection

    class PinnedHTTPSPool(HTTPSConnectionPool):
        ConnectionCls = PinnedHTTPSConnection

    return {"http": PinnedHTTPPool, "https": PinnedHTTPSPool}


class PinnedAdapter(HTTPAdapter):
    """Adaptador que sólo abre conexiones a IPs validadas."""

    def __init__(self, *, allow_private: bool = False, **kwargs: Any) -> None:
        self.allow_private = allow_private
        super().__init__(**kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):  # type: ignore[override]
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)
        self.poolmanager.pool_classes_by_scheme = _pool_classes(self.allow_private)


class SafeSession(requests.Session):
    """Sesión HTTP segura.

    - Valida el esquema y el host en cada petición, incluidos los redirects.
    - Conecta sólo a IPs públicas validadas en el mismo momento (sin rebinding).
    - Ignora proxies y ficheros de entorno (``trust_env=False``) para que no
      puedan saltarse la validación.
    """

    def __init__(self, *, allow_private: bool = False, max_redirects: int = 10) -> None:
        super().__init__()
        self.allow_private = allow_private
        self.trust_env = False
        self.max_redirects = max(1, int(max_redirects))
        adapter = PinnedAdapter(allow_private=allow_private)
        self.mount("http://", adapter)
        self.mount("https://", adapter)

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        validate_url(request.url or "", allow_private=self.allow_private)
        return super().send(request, **kwargs)


__all__ = ["PinnedAdapter", "SafeSession", "UnsafeUrlError"]
