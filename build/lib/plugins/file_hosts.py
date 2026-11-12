"""Hosts de archivos: MediaFire y conversión a descarga directa."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from plugins.base import Action, PageContext, Plugin, filename_from_url, host_matches


def normalize(url: str) -> str:
    """Convierte páginas de hostings conocidos en sus enlaces directos."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    path = parts.path or "/"
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    query = dict(query_pairs)

    if host_matches(host, ("drive.google.com", "docs.google.com")):
        match = re.match(
            r"^/(?:file|document|spreadsheets|presentation)/d/([\w-]{10,})", path
        )
        if match and "/export" not in path and "download" not in path:
            file_id = match.group(1)
            if path.startswith("/file/d/"):
                return f"https://drive.google.com/uc?export=download&id={file_id}"
            kind = path.split("/")[1]
            formats = {
                "document": "docx",
                "spreadsheets": "xlsx",
                "presentation": "pptx",
            }
            return (
                f"https://docs.google.com/{kind}/d/{file_id}/export"
                f"?format={formats.get(kind, 'pdf')}"
            )
        if path == "/open" and query.get("id") and "export" not in query:
            return f"https://drive.google.com/uc?export=download&id={query['id']}"

    if host_matches(host, ("dropbox.com",)):
        if not host.startswith("dl.") and (
            path.startswith("/s/")
            or path.startswith("/sh/")
            or path.startswith("/sc/")
        ):
            pairs = [(key, value) for key, value in query_pairs if key != "dl"]
            pairs.append(("dl", "1"))
            return urlunsplit(
                (
                    parts.scheme or "https",
                    "dl.dropboxusercontent.com",
                    path,
                    urlencode(pairs),
                    "",
                )
            )

    if host_matches(host, ("pixeldrain.com",)):
        match = re.match(r"^/(?:u|l)/([\w-]+)/?$", path)
        if match:
            return f"https://pixeldrain.com/api/file/{match.group(1)}?download"

    if host_matches(host, ("1drv.ms", "onedrive.live.com")):
        if query.get("download") != "1" and "/embed" not in path:
            pairs = list(query_pairs)
            pairs.append(("download", "1"))
            return urlunsplit(
                (parts.scheme or "https", parts.netloc, path, urlencode(pairs), "")
            )

    return url


def detect(ctx: PageContext) -> Action | None:
    for selector in ("#downloadButton", "#download-button", "#download_link"):
        node = ctx.soup.select_one(selector)
        if node is not None and node.get("href"):
            href = urljoin(ctx.base_url, node["href"].strip())
            return Action(
                "mediafire", url=href, filename=filename_from_url(href) or ""
            )
    return None


# Alias usado históricamente por la CLI y los tests.
normalize_file_host_url = normalize

PLUGIN = Plugin(
    name="file-hosts",
    priority=30,
    hosts=(
        "mediafire.com",
        "send.now",
        "1fichier.com",
        "file-upload.com",
        "pixeldrain.com",
        "drive.google.com",
        "docs.google.com",
        "dropbox.com",
        "1drv.ms",
        "onedrive.live.com",
    ),
    markers=(),
    detect=detect,
    normalize=normalize,
)
