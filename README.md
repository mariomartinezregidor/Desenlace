# Desenlace

Desenlace resuelve cadenas de acortadores y redirecciones con anuncios
(esperas, formularios y contadores de publicidad) y descarga el archivo final.
Incluye una interfaz web local y una CLI.

## Características

- Resuelve wrappers, `wp-safelink`, AdLinkFly, `mediafire`, `meta refresh` y
  formularios automáticos, respetando los tiempos de espera.
- Soporta `zflexlink`, `iflylink`, `shortxlinks` y los redirectores
  `google.com/url`, extrayendo el destino real del parámetro sin cargar la
  página intermedia.
- Detecta cuándo una página ya es el destino final y deja de encadenar sus
  enlaces internos (útil con `--info` en artículos y blogs).
- Caché local de páginas finales estables (`--refresh` para ignorarla).
- Interfaz web en `127.0.0.1` con cuadro de texto para pegar los enlaces,
  selector nativo de carpeta del PC, barras de progreso y un modo de solo
  enlace final con copia al portapapeles.
- CLI con paralelismo, destino configurable y nombres de archivo únicos.

## Requisitos

- Python 3.10 o superior.
- `requests` y `beautifulsoup4` (ver `requirements.txt`).
- Para el selector de carpeta nativo: `tkinter` (viene con Python en Linux).

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## Uso: interfaz web

```bash
python3 desenlace_web.py                 # abre http://127.0.0.1:8765 en el navegador
python3 desenlace_web.py --port 9000     # otro puerto
python3 desenlace_web.py --no-browser    # no abrir el navegador
python3 desenlace_web.py -v              # registrar cada petición HTTP
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
`--max-hops`, `--max-wait`, `--wait-buffer`, `--full-chain`.

`links.txt` admite una URL por línea (las que empiezan por `#` se ignoran).

## Estructura

| Ruta | Contenido |
| --- | --- |
| `desenlace_web.py` | Servidor local y API de la interfaz web |
| `index.html` | Página de la interfaz (cuadro de enlaces y progreso) |
| `desenlace.py` | Motor de resolución y descarga (también CLI) |
| `tests/` | Tests `unittest` y fixtures HTML |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Notas

- No se imprimen tokens completos: los enlaces se muestran con `redact_url`.
- El archivo se descarga primero a un `.part` y se renombra al terminar, así no
  quedan archivos incompletos si la descarga falla o se cancela.
- Si el destino final es una página HTML (por ejemplo un artículo), en vez de
  fallar la descarga se guarda un acceso directo `enlace.url` con la URL
  resuelta; `--info` la muestra directamente.
- Las páginas con Cloudflare/captcha interactivo siguen requiriendo resolver el
  reto una vez; no se simula navegador.
