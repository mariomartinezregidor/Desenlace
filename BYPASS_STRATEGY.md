# Estrategia de Bypass Avanzado para Laberintos de Descarga

## Descripción General

Este documento describe la estrategia implementada en `browser_resolver.py` para resolver **laberintos profundos de descarga con publicidad agresiva**, donde los botones de descarga están ocultos detrás de múltiples capas de engaño.

## Patrón Detectado

El patrón típico es:
1. **Página inicial**: Muestra un botón "cebo" (ej: "Continuar", "Siguiente", "Ir al enlace")
2. **Al hacer clic en el cebo**: Se abre una pestaña de publicidad (cam4.com, dating, etc)
3. **La publicidad bloquea el flujo**: El usuario humano la cierra manualmente
4. **El usuario pulsa "Atrás"**: Vuelve a la página anterior
5. **Ahora aparece el botón REAL**: "Descarga Directa", "Download", o un enlace `.txt`
6. **Descarga**: Se obtiene el archivo con credenciales

## Implementación: Secuencia "BAIT-AND-BACK"

### Funciones Principales

#### 1. `_find_bait_button(page)`
**Objetivo**: Detectar el botón cebo (fake/engañoso)

```python
def _find_bait_button(self, page: Any) -> Any | None:
    """Busca botón cebo: Continuar, Siguiente, etc (NO Descarga Directa)."""
    keywords = ['continuar', 'continue', 'siguiente', 'next', 'pulsa', 'click', 'acceso', 'ver']
    # Busca botones con estos keywords
    # Excluye botones que digan "Descarga Directa"
    return button or None
```

**Palabras clave detectadas**:
- "Continuar"
- "Continue"
- "Siguiente"
- "Next"
- "Pulsa"
- "Click"
- "Acceso"
- "Ver"

#### 2. `_find_real_download_button(page)`
**Objetivo**: Detectar el botón REAL de descarga (después de volver atrás)

```python
def _find_real_download_button(self, page: Any) -> Any | None:
    """Busca botón real: Descarga Directa, .txt, Download, etc."""
    keywords = ['descarga directa', 'direct download', 'download', 'descargar archivo', 'enlace directo']
    # Primero busca enlaces <a href="*.txt">
    # Luego busca botones con keywords de descarga
    return button or None
```

**Prioridades**:
1. **Enlaces .txt directo**: `<a href="file.txt">`
2. **Botones download**: "Descarga Directa", "Download", "Descargar Archivo"

#### 3. `_bait_and_back_sequence(page, context)`
**Objetivo**: Ejecutar la secuencia completa (3 intentos máximo)

```python
def _bait_and_back_sequence(self, page: Any, context: Any) -> bool:
    """Secuencia: cebo → popup → atrás → descarga real."""
    for attempt in range(1, 4):  # 3 intentos
        1. Buscar botón cebo
        2. Pulsar botón cebo (con force=True)
        3. Esperar 2 segundos (popup se abre)
        4. Cerrar popups automáticamente
        5. page.go_back() - volver atrás
        6. Esperar carga con networkidle
        7. Buscar botón real
        8. Pulsar botón real
        9. Verificar si hay descarga
    return True if descargó else False
```

### Integración en `_run_maze()`

Cuando el maze detecta un **loop stuck** (mismo botón, misma URL sin progreso):

```python
if not clic_exitoso:
    url_check = getattr(page, "url", "") or ""
    if url_check == url_actual and btn_hash in botones_intentados:
        # ← AQUÍ: Intentar secuencia BAIT-AND-BACK
        self._log(f"[MAZE] LOOP STUCK - intentando secuencia CEBO...")
        if self._bait_and_back_sequence(page, context):
            # ✓ Funcionó - se encontró la descarga
            outcome.extracted_url = getattr(page, "url", "")
            break
```

## Características Defensivas

### 1. Eliminación de Overlays (`_remove_blocking_overlays`)
- Elimina divs con `z-index > 50`
- Elimina elementos `position: fixed` que cubren pantalla
- Elimina iframes de publicidad
- Elimina divs con clases `[class*="ad"]`, `[class*="popup"]`

### 2. Cierre de Popups (`_close_popups`)
- Detecta dominios bloqueados: cam4.com, adult, sex, porn, dating
- Cierra automáticamente popups maliciosos
- Devuelve el foco a la página principal

### 3. Estrategias de Click (`_click_locator`)
- **Scroll**: Scroll a elemento + pausa + clic
- **Normal**: Clic estándar
- **Force**: Clic con force=True (atraviesa overlays)
- **JS**: Clic puro JavaScript

### 4. Pausa Inteligente
- `time.sleep(2)` después de cada clic
- `page.wait_for_load_state('networkidle', timeout=15000)`
- Pausas adicionales antes de reintentar

## Parámetros de Configuración

```python
max_iterations = 200        # Máximo de iteraciones del maze
max_sequence_seconds = 1200  # 20 minutos de timeout
browser_timeout = 600       # 10 minutos timeout general
slow_mo = 500              # Para debug: 500ms entre acciones
headless = False           # Para debug: navegador visible
```

## Flujo Completo

```
[Página inicial]
    ↓
[Detectar botón cebo: "Continuar"]
    ↓
[Pulsar cebo → se abre publicidad]
    ↓
[Cerrar publicidad]
    ↓
[page.go_back()]
    ↓
[Esperar carga (networkidle)]
    ↓
[Detectar botón real: "Descarga Directa"]
    ↓
[Pulsar botón real]
    ↓
[Verificar URL de archivo (.txt)]
    ↓
✓ DESCARGA COMPLETADA
```

## Casos de Uso

### ✓ Funciona En:
- Laberintos con botón cebo + publicidad + botón real
- Múltiples niveles de redireccionamiento
- Popups de cam4.com, dating sites, etc
- URLs que terminan en `.txt`

### ✗ No Funciona En:
- Captchas no resueltos
- Páginas que requieren interacción humana manual
- Descargadores que usan WebSocket o protocolos especiales

## Testing

### Ejecución Básica
```bash
cd /home/dergli/Proyectos/Desenlace
python3 /tmp/opencode/test_bait_and_back.py
```

### Con Debug Visible
```bash
python3 /tmp/opencode/test_bait_and_back.py
# headless=False → navegador visible
# slow_mo=500 → acciones lentas (500ms)
```

### Desde CLI
```bash
desenlace -v --auto-browser "https://web-api-13.vercel.app/?url=https://iflylink.com/ZeB4"
```

## Commits Asociados

- `bdf8a1f`: feat: aggressive ad bypass
- `cd9184b`: feat: deep maze persistence
- `2f6d3c9`: fix: bait-and-back sequence

## Logs de Debug

Cuando se ejecuta con `verbose=True` o `debug=True`:

```
[MAZE-1] LOOP STUCK - intentando secuencia CEBO...
[BAIT-1] Buscando botón cebo...
[BAIT] Candidato: Continuar al siguiente paso
[BAIT-1] Pulsando: Continuar al siguiente paso
[BAIT-1] Cerrando popup...
[BAIT-1] Volviendo atrás...
[BAIT-1] Buscando botón real...
[BAIT-1] ✓ Encontrado: Descarga Directa
[BAIT-1] Pulsando descarga...
[BAIT-1] ✓✓✓ DESCARGA: https://example.com/file.txt
```

## Futuras Mejoras

- [ ] Detección de CAPTCHA dentro del flujo bait-and-back
- [ ] Soporte para múltiples niveles de cebo (triple cebo)
- [ ] Machine learning para detectar patrones de botones cebo
- [ ] WebSocket support para descargas streaming
