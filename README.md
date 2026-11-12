# Desenlace

Desenlace resuelve cadenas de acortadores y redirecciones con anuncios
(esperas, formularios y contadores de publicidad) y descarga el archivo final.
Incluye una interfaz web local y una CLI.

## Características

- **Arquitectura de plugins**: cada familia de acortadores es un archivo dentro
  de `plugins/`. El registro los descubre automáticamente; añadir un sitio nuevo
  no requiere tocar el núcleo (ver [Plugins](#arquitectura-de-plugins)).
- **Protección SSRF**: se bloquean `file://`, `ftp://`, credenciales embebidas,
  loopback, rangos privados y `169.254.169.254`, validando **cada salto** de la
  cadena de redirecciones.
- **Anti-bot con navegador headless**: si aparece Cloudflare, Turnstile,
  reCAPTCHA, "Checking your browser" o "No soy un robot", se activa Playwright
  automáticamente (`--auto-browser`) para pulsar la verificación y continuar.
- Resuelve wrappers, `wp-safelink`, AdLinkFly y clones, `ouo`, `mediafire`,
  `meta refresh`, formularios automáticos y contadores, respetando los tiempos
  de espera.
- Redirectores reconocidos: `google.com/url`, `bing.com/ck/a`,
  `duckduckgo.com/l`, Yahoo, `l.facebook.com/l.php`, Reddit, VK, Tumblr,
  LinkedIn y pasarelas genéricas `/redirect?url=...`. El destino puede venir en
  claro, URL-encoded o en base64 (`?u=`, `?a1=`, `?uddg=`…).
- Acortadores con destino escondido en JavaScript: `window.location`, `atob()`,
  `adf.ly` (`ysmm`) y código ofuscado con el *packer* de Dean Edwards.
- Hosts de archivos convertidos a descarga directa antes de resolver: Google
  Drive/Docs, Dropbox, OneDrive y Pixeldrain.
- Detecta cuándo una página ya es el destino final y deja de encadenar sus
  enlaces internos (útil con `--info` en artículos y blogs).
- Reintentos automáticos con backoff ante errores de red y HTTP 429/5xx.
- Cookies (`--cookies`) y User-Agent/cabeceras configurables para acortadores
  protegidos por Cloudflare o que requieren sesión.
- Caché local de páginas finales estables (`--refresh` para ignorarla).
- Interfaz web en `127.0.0.1` con cuadro de texto para pegar los enlaces,
  selector nativo de carpeta del PC, barras de progreso y un modo de solo
  enlace final con copia al portapapeles.
- CLI con paralelismo, destino configurable y nombres de archivo únicos. Si el
  servidor no da un nombre con extensión, se deduce del `Content-Type`.

## Sitios y familias soportadas

| Familia | Ejemplos |
| --- | --- |
| AdLinkFly y clones | `gplinks`, `try2link`, `droplink`, `mdiskshortner`, `tnlink`, `urlsopen`, `softurl`, `zflexlink`, `iflylink`, `shortxlinks` |
| ShrinkMe / ShrinkE / Exe | `shrinkme.io`, `shrinke.me`, `exe.io`, `exey.io`, `za.gl`, `zshort.link` |
| wp-safelink / safelink Blogger | `newwpsafelink`, `safelinku`, `bloggerpemula`, `sfl.gl` |
| ouo y similares | `ouo.io`, `ouo.press`, `shrinkearn.com` |
| Acortadores clásicos | `adf.ly` (`ysmm`), `shorte.st`, `bc.vc`, `adfoc.us`, `linkvertise` |
| Redirectores | Google, Bing, DuckDuckGo, Yahoo, Facebook, Reddit, VK, Tumblr, LinkedIn |
| Hosts de archivos | MediaFire, Google Drive/Docs, Dropbox, OneDrive, Pixeldrain, send.now |

El motor aplica primero los plugins con prioridad menor (redirectores y
detectores específicos) y después heurísticas genéricas (formularios ocultos,
botones de continuar, `meta refresh`, JavaScript, iframes). Para sitios nuevos
sin detector propio, `--auto-browser` suele resolverlos con Chromium.

## Arquitectura de plugins

```
plugins/
├── __init__.py        # descubrimiento, registro y agregación de hosts/markers
├── base.py            # contrato Plugin + re-export de helpers
├── redirectors.py     # Google/Bing/DDG/Facebook/... y pasarelas ?url=
├── adlinkfly.py       # AdLinkFly, ShrinkMe, gplinks, try2link...
├── wp_safelink.py     # wp-safelink y safelinks de Blogger
├── adfly.py           # token ysmm
├── file_hosts.py      # MediaFire + normalización Drive/Dropbox/OneDrive/Pixeldrain
├── forms.py           # formularios de aterrizaje y formularios genéricos
├── js_redirects.py    # meta-refresh, JS, base64, packer, atob, fromCharCode
├── html_links.py      # enlaces/botones/data-* /iframes/onclick
└── known_shorteners.py# catálogo de hosts con plantillas genéricas
```

Un plugin es cualquier módulo que exponga `PLUGIN` o `PLUGINS`:

```python
# plugins/mi_sitio.py
from plugins.base import Action, PageContext, Plugin

def detect(ctx: PageContext) -> Action | None:
    boton = ctx.soup.find("a", id="mi-boton")
    if boton is None:
        return None
    return Action("goto", url=boton["href"])

PLUGIN = Plugin(
    name="mi-sitio",
    priority=50,                     # menor = antes
    hosts=("mi-sitio.example",),     # heurísticas y ?url= genéricos
    markers=("mi-boton",),           # cadenas de una página de espera
    detect=detect,
)
```

También existen `detect_url(url, hosts)` (destino en la propia URL) y
`normalize(url)` (convertir una página de hosting en enlace directo). Para
inspeccionar lo cargado: `python3 desenlace.py --list-plugins`.

## Seguridad SSRF

Cada petición —incluidos los redirects que sigue `requests`— pasa por
`desenlace_core.validate_url`:

- Sólo se permiten `http` y `https` (se rechazan `file://`, `ftp://`, etc.).
- Se rechazan `localhost`, loopback (`127.0.0.0/8`, `::1`), rangos privados
  (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`), link-local
  (`169.254.0.0/16`) y endpoints de metadatos de nube (`169.254.169.254`,
  `metadata.google.internal`).
- Se resuelve el DNS y se comprueba que **todas** las IPs sean globales; si no
  se puede resolver, se falla en cerrado.
- El navegador headless también intercepta sus peticiones y bloquea las que
  apunten a la red interna.
- `--max-redirects N` limita estrictamente los saltos HTTP (por defecto 10).

`--allow-private` desactiva las comprobaciones de IP (no las de esquema) y está
pensado **sólo para tests locales**:

```bash
python3 desenlace.py --allow-private --info "http://127.0.0.1:8000/prueba"
```

## Anti-bot con navegador headless

`browser_resolver.py` usa Playwright y se activa sólo cuando el resolver HTTP
detecta un challenge. Abre la URL, espera a que cambie el DOM, pulsa el checkbox
de "No soy un robot" (incluidos iframes de reCAPTCHA/Turnstile), captura
descargas, importa las cookies de vuelta al motor y devuelve la URL final.

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium   # descarga el binario (~130 MB)
# o sólo el shell headless:
python3 -m playwright install chromium-headless-shell

python3 desenlace.py --auto-browser "https://sitio-con-cloudflare/XXXX"
```

`--browser` fuerza el navegador desde el principio; `--auto-browser` lo usa
como capa secundaria automática y también reintenta desde cero si el flujo HTTP
falla. Si el reto exige interacción humana (p. ej. resolver un puzzle), no hay
bypass automático: usa `--cookies` con una sesión válida (`cf_clearance`).

## Requisitos

- Python 3.10 o superior.
- `requests`, `beautifulsoup4` y `playwright` (ver `requirements.txt`).
- Binario de Chromium: `python3 -m playwright install chromium`.
- Para el selector de carpeta nativo: `tkinter` (viene con Python en Linux).

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/playwright install chromium
```

## Uso: interfaz web

```bash
python3 desenlace_web.py                 # abre http://127.0.0.1:8765 en el navegador
python3 desenlace_web.py --port 9000     # otro puerto
python3 desenlace_web.py --no-browser    # no abrir el navegador
python3 desenlace_web.py -v              # registrar cada petición HTTP
python3 desenlace_web.py --cookies cookies.txt --user-agent "Mozilla/5.0 ..."
```

Pega los enlaces (uno por línea), pulsa **Seleccionar carpeta** y luego
**Descargar**. Verás una barra de progreso global y otra del archivo en curso,
un log en vivo y un botón para cancelar. La interfaz escucha solo en
`127.0.0.1`, así que no queda expuesta a la red.

Marca **Solo obtener enlace final** para resolver sin descargar: no hace falta
elegir carpeta y al terminar verás los enlaces resueltos con un botón para
copiarlos todos. Es el modo recomendado cuando el destino es una página HTML.

## Uso: línea de comandos

```bash
python3 desenlace.py "https://softurl.in/XXXX"
python3 desenlace.py -f links.txt -o downloads
python3 desenlace.py -f links.txt --info            # solo el enlace final
python3 desenlace.py -f links.txt -o downloads -v   # traza de saltos
```

Opciones útiles: `-j N` (paralelismo), `-o CARPETA` (destino), `--force`
(sobrescribir), `--skip-existing`, `--refresh` (ignorar caché),
`--max-hops`, `--max-redirects`, `--max-wait`, `--wait-buffer`, `--full-chain`,
`--auto-browser`, `--list-plugins`, `--allow-private`.

Para sitios con Cloudflare o que piden sesión:

```bash
python3 desenlace.py --cookies cookies.txt "https://ouo.io/XXXX"
python3 desenlace.py --user-agent "Mozilla/5.0 ..." -H "Accept-Language: es" ...
python3 desenlace.py --retries 4 -f links.txt
```

`--cookies` acepta el formato Netscape exportado por las extensiones del
navegador o una línea `nombre=valor; otra=valor`. Los valores nunca se escriben
en los logs. No subas esos archivos al repositorio: `.gitignore` y el hook
`pre-commit` ya bloquean los volcados de cookies.

`links.txt` admite una URL por línea (las que empiezan por `#` se ignoran).

## Estructura

| Ruta | Contenido |
| --- | --- |
| `desenlace_core.py` | Tipos, helpers, parsers y validación SSRF (sin dependencias del motor) |
| `plugins/` | Estrategias por familia de acortadores (descubrimiento automático) |
| `browser_resolver.py` | Capa anti-bot con Playwright |
| `desenlace.py` | Motor HTTP, descarga, caché, CLI y re-exports compatibles |
| `desenlace_web.py` | Servidor local y API de la interfaz web |
| `index.html` | Página de la interfaz (cuadro de enlaces y progreso) |
| `tests/` | Tests de parsers, plugins, SSRF, redirects y navegador mockeado |

## Tests

```bash
python3 -m pytest -q
# o sin pytest:
python3 -m unittest discover -s tests -v
```

La suite incluye: fixtures HTML de cada familia, tests de plugins, SSRF
(incluido redirect a `169.254.169.254`), límite de redirecciones y un test del
navegador headless con Playwright simulado.

## Notas

- No se imprimen tokens completos: los enlaces se muestran con `redact_url`.
- El archivo se descarga primero a un `.part` y se renombra al terminar, así no
  quedan archivos incompletos si la descarga falla o se cancela.
- Si el destino final es una página HTML (por ejemplo un artículo), en vez de
  fallar la descarga se guarda un acceso directo `enlace.url` con la URL
  resuelta; `--info` la muestra directamente.
- Las páginas con Cloudflare/captcha interactivo pueden necesitar
  `--auto-browser` (Chromium real) o un archivo de cookies con `cf_clearance`;
  un reto que exige interacción humana no se puede resolver sin intervención.
- Los hosts de archivos se convierten a su enlace directo antes de resolver. Si
  Google Drive pide confirmación para archivos grandes, el formulario oculto se
  envía automáticamente.
- La validación SSRF resuelve DNS antes de conectar. Es una defensa sólida
  contra SSRF clásico y redirects maliciosos; para entornos de altísima
  seguridad, añade además un proxy de salida con allowlist.
