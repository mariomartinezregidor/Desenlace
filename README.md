# Desenlace

Desenlace es una herramienta para **convertir enlaces con anuncios o pasos
intermedios en el enlace de descarga final** y, si quieres, descargar el archivo
automáticamente.

Puedes usarla de dos formas:

- **Interfaz web**: pegas los enlaces, eliges una carpeta y pulsas un botón.
- **Línea de comandos (CLI)**: para pegar una orden en la terminal.

No necesitas saber programar para usarla.

---

## ¿Qué puedes hacer?

- Resolver enlaces de acortadores, safelinks y páginas con esperas.
- Descargar el archivo final, o solo obtener su enlace.
- Automatizar páginas que piden varios clics, esperas o cerrar ventanas.
- Resolver enlaces de canales públicos de Telegram.
- Descargar archivos enviados por bots de Telegram (requiere configuración
  opcional).

---

## Antes de empezar

Necesitas:

- Un ordenador con Windows, macOS o Linux.
- **Python 3.10 o superior** instalado.
- Conexión a Internet.
- Unos 200 MB libres si vas a usar el navegador automatizado.

### Comprobar que tienes Python

Abre una terminal y escribe:

```bash
python3 --version
```

En Windows prueba con:

```bash
python --version
```

Debe aparecer una versión **3.10 o superior**. Si no aparece, instala Python
desde <https://www.python.org/downloads/>. En Windows marca la casilla
**"Add Python to PATH"** durante la instalación.

---

## Instalación (solo la primera vez)

### 1. Descargar el proyecto

```bash
git clone https://github.com/mariomartinezregidor/Desenlace.git
cd Desenlace
```

Si no tienes `git`, descarga el proyecto como ZIP desde GitHub y descomprímelo.

### 2. Crear un entorno aislado e instalar las dependencias

**Linux o macOS:**

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**Windows (PowerShell o CMD):**

```bash
python -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Sabrás que el entorno está activo porque la terminal mostrará `(venv)` al
principio de la línea.

### 3. Instalar el navegador automatizado (recomendado)

```bash
playwright install chromium
```

Si el comando anterior falla, prueba:

```bash
python -m playwright install chromium
```

> Este paso descarga un navegador de unos 130 MB. Solo hay que hacerlo una vez.

---

## Uso: Interfaz Web (recomendado)

1. Activa el entorno si no lo está:

   ```bash
   source venv/bin/activate      # Linux / macOS
   venv\Scripts\activate         # Windows
   ```

2. Arranca la interfaz:

   ```bash
   python3 desenlace_web.py
   ```

   En Windows, si `python3` no funciona:

   ```bash
   python desenlace_web.py
   ```

3. Se abrirá tu navegador en `http://127.0.0.1:8765`. Si no se abre, copia esa
   dirección y pégala manualmente.

4. **Pega los enlaces**, uno por línea, en el cuadro de texto.

5. Pulsa **Seleccionar carpeta** y elige dónde guardar los archivos.

6. Elige las opciones que necesites:

   | Opción | Para qué sirve |
   | --- | --- |
   | **Solo obtener enlace final** | No descarga nada; solo muestra los enlaces resueltos y permite copiarlos. |
   | **Sobrescribir archivos existentes** | Reemplaza archivos con el mismo nombre. |
   | **Usar navegador** | Fuerza el navegador automatizado desde el principio. |
   | **Auto-navegador si falla** | Recomendado para páginas con varios pasos, esperas o verificaciones. |

7. Pulsa **Descargar** (o **Obtener enlaces** si marcaste el modo de solo
   enlace).

8. Verás el progreso, el archivo actual y un registro de lo que ocurre. Puedes
   pulsar **Cancelar** si quieres detenerlo.

9. Al terminar, si usaste **Solo obtener enlace final**, aparecerá un cuadro con
   los enlaces resueltos y el botón **Copiar todos**.

### Cambiar el puerto o no abrir el navegador

```bash
python3 desenlace_web.py --port 9000
python3 desenlace_web.py --no-browser
```

Por defecto la interfaz solo escucha en tu propio ordenador
(`127.0.0.1`), no queda expuesta a Internet.

---

## Uso: Línea de comandos (CLI)

La forma más rápida de resolver un enlace:

```bash
python3 desenlace.py "https://ejemplo.com/enlace-con-anuncios"
```

El archivo se guarda en la carpeta `downloads/`.

### Ejemplos útiles

```bash
# Elegir la carpeta de destino
python3 desenlace.py -o "Mis descargas" "https://ejemplo.com/enlace"

# Elegir el nombre del archivo (si es un único enlace)
python3 desenlace.py -o "pelicula.zip" "https://ejemplo.com/enlace"

# Solo mostrar el enlace final, sin descargar
python3 desenlace.py --info "https://ejemplo.com/enlace"

# Resolver varios enlaces de un archivo de texto
python3 desenlace.py -f enlaces.txt -o descargas

# Descargar varios a la vez
python3 desenlace.py -f enlaces.txt -j 4
```

### Formato de `enlaces.txt`

Un enlace por línea. Las líneas que empiezan por `#` se ignoran:

```text
# Mis enlaces pendientes
https://ejemplo.com/enlace-1
https://ejemplo.com/enlace-2
```

### Opciones más usadas

| Opción | Qué hace |
| --- | --- |
| `-o RUTA` | Carpeta o archivo de destino. |
| `--info` | Solo muestra el enlace final; no descarga. |
| `-f ARCHIVO` | Lee los enlaces desde un archivo de texto. |
| `-j N` | Procesa N enlaces en paralelo. |
| `--force` | Sobrescribe archivos existentes. |
| `--skip-existing` | No vuelve a descargar lo que ya existe. |
| `--auto-browser` | Usa el navegador automáticamente si la página se resiste. |
| `--browser` | Usa el navegador desde el principio. |
| `--cookies ARCHIVO` | Usa cookies para páginas que piden sesión. |
| `--user-agent TEXTO` | Cambia el navegador que se anuncia al servidor. |
| `-H "Nombre: valor"` | Añade una cabecera HTTP. |
| `--timeout SEGUNDOS` | Tiempo máximo por petición. |
| `--max-wait SEGUNDOS` | Tiempo máximo de espera en verificación o flujo automático. |
| `--retries N` | Reintentos ante errores de red. |
| `--refresh` | Ignora la caché y vuelve a resolver. |
| `--verbose` / `-v` | Muestra más detalles del proceso. |
| `--help` | Muestra todas las opciones disponibles. |

> Consejo: si una página no se resuelve, prueba primero con `--auto-browser`.

---

## Multi-Click Bypasser: páginas con varios pasos

Algunas webs no muestran el archivo hasta que haces una secuencia como:

1. Pulsar **"Paso 1: Clic aquí"**.
2. Esperar un contador de 10 segundos.
3. Cerrar una ventana publicitaria.
4. Pulsar **"Descargar"**.

Desenlace puede hacer esos pasos por ti con el **navegador automatizado**.

### En la CLI

Añade `--auto-browser`:

```bash
python3 desenlace.py --auto-browser "https://sitio-de-descarga.com/archivo"
```

Si quieres forzar el navegador desde el primer momento:

```bash
python3 desenlace.py --browser "https://sitio-de-descarga.com/archivo"
```

### En la interfaz web

Marca la casilla **Auto-navegador si falla** (o **Usar navegador** para
forzarlo).

### Qué esperar

- Pueden abrirse y cerrarse pestañas automáticamente.
- Las esperas de la página se aceleran.
- No toques el navegador mientras trabaja.
- Si tarda demasiado, aumenta el límite:

  ```bash
  python3 desenlace.py --auto-browser --max-wait 180 "https://sitio.com/archivo"
  ```

- Si sigue fallando, prueba con cookies:

  ```bash
  python3 desenlace.py --auto-browser --cookies cookies.txt "https://sitio.com/archivo"
  ```

> Los captchas que exigen que una persona resuelva un puzzle no se pueden
> saltar automáticamente. En esos casos necesitas usar una sesión válida con
> `--cookies` o resolverlo manualmente.

---

## Telegram Resolver

Puedes pegar enlaces de Telegram igual que cualquier otro enlace.

### Canales públicos

Enlaces como:

```text
https://t.me/nombre_del_canal/123
https://t.me/s/nombre_del_canal
```

```bash
python3 desenlace.py --auto-browser "https://t.me/nombre_del_canal/123"
```

El navegador abrirá la versión pública del canal y extraerá el archivo, vídeo o
audio disponible.

### Bots de Telegram

Enlaces como:

```text
https://t.me/NombreDelBot?start=abc123
```

Para estos enlaces necesitas configurar una sesión de Telegram. Solo hay que
hacerlo una vez.

#### 1. Copia el archivo de configuración

**Linux o macOS:**

```bash
cp .env.example .env
```

**Windows:**

```bash
copy .env.example .env
```

#### 2. Rellena tus datos

Abre `.env` con un editor de texto y sustituye los valores de ejemplo:

```text
TELEGRAM_API_ID=<TU_API_ID_AQUI>
TELEGRAM_API_HASH=<TU_API_HASH_AQUI>
TELEGRAM_SESSION_STRING=<TU_SESSION_STRING_AQUI>
```

No escribas estos datos en el README, en capturas ni en mensajes. Son privados.

#### 3. Obtén `API_ID` y `API_HASH`

1. Entra en <https://my.telegram.org>.
2. Inicia sesión con tu cuenta de Telegram.
3. Abre **API development tools**.
4. Crea una aplicación si no tienes una.
5. Copia el **App api_id** y el **App api_hash** en tu `.env`.

#### 4. Genera la sesión

Crea un archivo llamado `generar_sesion.py` con este contenido (sustituye los
placeholders por tus datos):

```python
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = <TU_API_ID_AQUI>
API_HASH = "<TU_API_HASH_AQUI>"

with TelegramClient(StringSession(), API_ID, API_HASH) as cliente:
    print(cliente.session.save())
```

Ejecútalo:

```bash
python generar_sesion.py
```

Te pedirá el teléfono y el código que recibirás en Telegram. Al terminar,
imprimirá una cadena larga. Cópiala en `TELEGRAM_SESSION_STRING` dentro de tu
`.env`.

#### 5. Usa el enlace del bot

```bash
python3 desenlace.py --auto-browser "https://t.me/NombreDelBot?start=abc123"
```

También puedes pegarlo en la interfaz web con **Auto-navegador si falla**
marcado.

#### Si algo no funciona

- **"Telegram no está configurado"**: revisa que `.env` exista y tenga los tres
  valores rellenos.
- **"La sesión no está autorizada"**: vuelve a generar `TELEGRAM_SESSION_STRING`.
- **El bot no responde**: comprueba el nombre del bot y que el enlace sea
  correcto.
- **No quiero configurar Telegram**: los canales públicos siguen funcionando;
  solo los bots necesitan esta configuración.

> Nunca subas tu archivo `.env` a GitHub ni lo compartas. El proyecto ya lo
> ignora por defecto.

---

## Problemas frecuentes

### "No se reconoce la página"

Prueba con el navegador:

```bash
python3 desenlace.py --auto-browser --verbose "https://ejemplo.com/enlace"
```

### La página pide Cloudflare o "No soy un robot"

Usa `--auto-browser`. Si el reto sigue apareciendo, espera un rato, prueba desde
otra red o usa cookies de una sesión válida:

```bash
python3 desenlace.py --auto-browser --cookies cookies.txt "https://ejemplo.com/enlace"
```

### `playwright: command not found`

Activa el entorno y vuelve a instalar el navegador:

```bash
source venv/bin/activate      # Linux / macOS
venv\Scripts\activate         # Windows
python -m playwright install chromium
```

En Linux, si faltan librerías del sistema:

```bash
python -m playwright install-deps chromium
```

### Se ha guardado un archivo `enlace.url` en vez del archivo

Significa que el destino final es una página web, no un archivo. Usa `--info`
para ver el enlace, o revisa que el enlace de origen sea el correcto.

### El archivo descargado no tiene extensión

La herramienta intenta deducirla del servidor. Si no puede, puedes renombrarlo
manualmente.

### No tengo permisos para guardar en esa carpeta

Elige otra carpeta, por ejemplo tu carpeta de Descargas o `downloads`.

### Windows no reconoce `python`

Vuelve a instalar Python marcando **"Add Python to PATH"**, o usa `py` en lugar
de `python`.

### Por seguridad no descarga una dirección local

La herramienta bloquea direcciones locales y privadas para evitar usos
peligrosos. Es el comportamiento esperado.

---

## Privacidad y seguridad

- Los tokens, cookies y sesiones **no se muestran** en los mensajes de la
  herramienta.
- No compartas tu archivo `.env`, tus cookies ni tu sesión de Telegram.
- Usa la herramienta solo con contenido que tengas derecho a descargar.
- La interfaz web escucha únicamente en tu ordenador.

---

## Actualizar la herramienta

Si instalaste con `git`:

```bash
git pull
pip install -r requirements.txt
playwright install chromium
```

---

## Repositorio

<https://github.com/mariomartinezregidor/Desenlace>

Si encuentras un problema, abre una incidencia en GitHub indicando:

- El enlace que falla (si no contiene datos privados).
- El mensaje de error.
- Tu sistema operativo.
