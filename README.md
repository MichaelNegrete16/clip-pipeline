# Clip Pipeline

Automatiza el ciclo completo de un canal de clips: descubre los mejores momentos de
streamers de **Twitch y Kick**, los edita a formato Short o recopilatorio, y los publica
en **YouTube**.

La selección no va por vistas. Va por señales de que el momento *estuvo bueno*:
cuánta gente clipeó ese mismo instante, y si el chat explotó de risa en vivo.

---

## Qué hace

```
Descubrir  →  Puntuar  →  Elegir  →  Editar  →  Revisar  →  Publicar  →  Medir
  APIs        gracia      1 por      9:16 +     panel       YouTube      Analytics
              del clip    momento    censura    web
```

| Pieza | Qué resuelve |
|---|---|
| **Ingesta** | Clientes de Kick y Twitch con paginación y ventanas de tiempo |
| **Puntaje de gracia** | Densidad de clipeo + picos de chat + velocidad de vistas |
| **Bot en vivo** | Lee el chat de Twitch por IRC y detecta explosiones de risa |
| **Render** | Vertical 9:16, marca de agua, enganche, botones de CTA |
| **Censura** | Whisper con timestamps por palabra, pitido de 1 kHz y subtítulo enmascarado |
| **Recopilatorios** | Formato largo 16:9 con intro, rótulos, volumen igualado y capítulos |
| **Panel** | Revisión, aprobación y publicación desde el navegador |

---

## Instalación

### Dependencias del sistema

```bash
# Linux (Debian/Ubuntu)
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg fonts-dejavu-core

# Windows
winget install Gyan.FFmpeg
```

`ffmpeg` y las fuentes TrueType **no son opcionales**: sin fuentes, los textos salen
en un mapa de bits diminuto e ilegible.

### El proyecto

```bash
git clone <tu-repo> clip-pipeline
cd clip-pipeline

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # y rellenar las credenciales
```

---

## Credenciales

Editar `.env`. **Nunca se sube a git** (está en `.gitignore`).

### Twitch — [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps)

Requiere 2FA activo en la cuenta. Registrar una app y copiar:

```
TWITCH_CLIENT_ID=...
TWITCH_CLIENT_SECRET=...
```

Kick no necesita credenciales: su API de clips es pública.

### YouTube — [console.cloud.google.com](https://console.cloud.google.com)

1. Crear proyecto y habilitar **YouTube Data API v3** y **YouTube Analytics API**
2. *Google Auth Platform → Público* → **Publicar app**
   (en modo "Prueba" Google caduca el refresh token cada 7 días y la automatización muere)
3. *Clientes* → crear cliente OAuth tipo **Aplicación web**
4. URI de redirección autorizado — debe coincidir exacto:
   ```
   http://localhost:8000/oauth/callback
   ```
5. Copiar al `.env`:
   ```
   YOUTUBE_CLIENT_ID=...
   YOUTUBE_CLIENT_SECRET=...
   ```

---

## Uso

```bash
python panel/app.py            # panel en http://localhost:8000
```

Abrir siempre en **`localhost`**, no en `127.0.0.1`: Google los trata como orígenes
distintos y rechazaría el OAuth.

### Primeros pasos en el panel

1. **Fuentes** → pegar streamers (URLs o nombres, como sea)
2. **Canales de YouTube** → crear un canal, conectarlo por OAuth, asignarle fuentes
3. **Hoy** → *Proponer los de hoy* → *Renderizar* → revisar → *Subir*

### Bot de chat en vivo

Corre aparte, en su propio proceso:

```bash
python live/monitor.py
```

### Línea de comandos

```bash
python ingest/run.py lacobraaa                    # ingestar un canal
python ingest/scout.py --file seeds/streamers_es.txt   # evaluar candidatos
python ingest/seed.py seeds/streamers_es.txt      # ingesta masiva
python render/renderer.py <clip_id> --style crop  # render manual
python render/storage.py --sweep                  # limpiar disco
```

---

## Despliegue en VPS

### Servicios systemd

`/etc/systemd/system/clip-panel.service`:

```ini
[Unit]
Description=Clip Pipeline - panel
After=network.target

[Service]
User=clip
WorkingDirectory=/opt/clip-pipeline
ExecStart=/opt/clip-pipeline/.venv/bin/python panel/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/clip-live.service`:

```ini
[Unit]
Description=Clip Pipeline - bot de chat en vivo
After=network.target

[Service]
User=clip
WorkingDirectory=/opt/clip-pipeline
ExecStart=/opt/clip-pipeline/.venv/bin/python live/monitor.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now clip-panel clip-live
```

### Nota sobre el OAuth en la VPS

El redirect está fijado a `http://localhost:8000/oauth/callback`. Para conectar YouTube
desde una VPS sin navegador, lo práctico es un túnel SSH desde tu equipo:

```bash
ssh -L 8000:localhost:8000 usuario@tu-vps
```

Y abrir `http://localhost:8000` en tu navegador local. El token queda guardado en la
base de datos de la VPS y ya no hace falta repetirlo.

---

## Límites que conviene tener presentes

| Límite | Valor | Nota |
|---|---|---|
| Cuota de YouTube | 10.000 unidades/día | **Por proyecto**, no por canal |
| Coste de una subida | 1.600 unidades | Deja **6 subidas diarias** en total |
| Duración de un Short | ≤ 3 min y vertical | No hay API aparte; YouTube lo clasifica solo |
| Whisper en CPU | ~1× tiempo real | Un clip de 30 s tarda ~30 s en transcribir |

---

## Estructura

```
ingest/     descubrimiento, puntaje y clientes de plataforma
render/     edición de video, censura, recopilatorios y disco
live/       bot de chat en vivo (IRC de Twitch)
panel/      API FastAPI + interfaz web
seeds/      listas de streamers y blacklist base
data/       base SQLite (no se versiona)
media/      temporales de video (no se versiona)
```

---

## Aviso legal

Los clips pertenecen a sus creadores y a las plataformas. Este proyecto genera
atribución automática con enlace al original, pero **eso no sustituye el permiso del
creador**. Antes de monetizar, consíguelo por escrito. La casilla *Permiso* en la
pestaña Fuentes existe para llevar ese control.
