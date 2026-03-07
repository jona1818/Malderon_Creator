# 🎬 Malderon Creator

**Plataforma automatizada de creación de videos para YouTube**, construida con FastAPI. Genera videos completos a partir de un tema: script → voz → imágenes → animación → video final.

---

## 🧠 ¿Qué hace?

Malderon Creator orquesta múltiples APIs de inteligencia artificial para producir videos en piloto automático:

```
📝 Tema
  ↓
✍️  Script (Claude AI)
  ↓
🎙️ Voiceover + SRT (GenAIPro / ElevenLabs / OpenAI)
  ↓
🖼️  Imágenes por escena (Imagen 3 / Pollinations / WaveSpeed)
  ↓
🎞️  Animación (WaveSpeed i2v)
  ↓
🎬 Render + Subtítulos (NCA Toolkit)
  ↓
📁 Video final (.mp4)
```

---

## 🏗️ Tecnologías utilizadas

### Backend
| Tecnología | Uso |
|---|---|
| 🐍 **Python 3.14** | Lenguaje principal |
| ⚡ **FastAPI** | Framework web / API REST |
| 🦄 **Uvicorn** | Servidor ASGI |
| 🗄️ **SQLite + SQLAlchemy** | Base de datos con modo WAL para acceso concurrente |
| 🧵 **ThreadPoolExecutor** | Procesamiento paralelo de escenas |
| 📡 **SSE (Server-Sent Events)** | Streaming de logs en tiempo real al frontend |
| 🔧 **Pydantic** | Validación de datos y configuración |

### Inteligencia Artificial
| API | Uso |
|---|---|
| 🤖 **Anthropic Claude** (claude-opus-4-6, claude-haiku-4-5) | Generación de scripts, prompts de imágenes, prompts de movimiento, división de escenas con SRT |
| 🎙️ **GenAIPro TTS** | Text-to-speech principal + generación de subtítulos SRT con timestamps reales |
| 🔊 **ElevenLabs** | Proveedor TTS alternativo |
| 🗣️ **OpenAI TTS + Whisper** | TTS alternativo + transcripción a SRT |
| 🖼️ **Google Imagen 3** | Generación de imágenes de alta calidad |
| 🆓 **Pollinations.ai (FLUX)** | Generación de imágenes gratuita con soporte de imagen de referencia |
| 🚀 **WaveSpeed** | Generación de imágenes premium + animación imagen-a-video (i2v) |
| 🎬 **GenAIPro Veo** | Generación de video con IA |

### Media y Stock
| API | Uso |
|---|---|
| 📸 **Pexels** | Videos y fotos de stock gratuitas |
| 🖼️ **Pixabay** | Alternativa de media stock |

### Video Rendering
| Herramienta | Uso |
|---|---|
| 🎥 **NCA Toolkit** | Combina video + audio + subtítulos SRT en el clip final, concatena todos los clips en el video completo |
| 🎵 **ffmpeg** | Corte de audio por escena (stream copy) |

---

## 📁 Estructura del proyecto

```
Malderon_Creator/
│
├── 🚀 main.py                      # Punto de entrada FastAPI
├── 📋 requirements.txt             # Dependencias Python
├── ⚙️  .env                         # Variables de entorno (API keys)
├── 📖 .env.example                 # Plantilla de configuración
├── 🪟 start.bat                    # Lanzador Windows
│
├── app/
│   ├── ⚙️  config.py                # Configuración global (Pydantic Settings)
│   ├── 🗄️  database.py              # Setup SQLAlchemy + migraciones automáticas
│   ├── 📊 models.py                # Modelos ORM (Project, Chunk, Worker, Log, AppSetting)
│   ├── 📐 schemas.py               # Esquemas Pydantic para request/response
│   │
│   ├── routers/                    # Endpoints de la API
│   │   ├── 🎬 projects.py          # CRUD de proyectos + orquestación del pipeline
│   │   ├── 📜 logs.py              # Logs en tiempo real (SSE streaming)
│   │   ├── 👷 workers.py           # Estado de los workers del thread pool
│   │   ├── 🎙️  tts.py               # Listado de voces TTS
│   │   ├── ▶️  youtube.py           # Extracción de transcripciones de YouTube
│   │   └── ⚙️  settings.py          # Gestión de API keys globales
│   │
│   └── services/                   # Lógica de negocio
│       ├── 🤖 claude_service.py    # Generación de scripts + división de escenas con SRT
│       ├── 🏭 pipeline_service.py  # Orquestador principal del pipeline (fases 1-4)
│       ├── 🖼️  google_service.py    # Generación de imágenes con Imagen 3
│       ├── 🎬 nca_service.py       # Render y concatenación de video (NCA Toolkit)
│       ├── 🚀 wavespeed_service.py # Animación imagen-a-video
│       ├── 📸 pexels_service.py    # Búsqueda de stock en Pexels
│       ├── 🖼️  pixabay_service.py   # Búsqueda de stock en Pixabay
│       │
│       ├── tts/                    # Proveedores Text-to-Speech
│       │   ├── 🎙️  genaipro.py      # GenAIPro TTS (principal)
│       │   ├── 🔊 elevenlabs.py    # ElevenLabs TTS
│       │   └── 🗣️  openai_tts.py    # OpenAI TTS + Whisper
│       │
│       ├── image/                  # Proveedores de imágenes
│       │   ├── 🆓 pollinations_service.py    # FLUX gratuito (Pollinations)
│       │   └── 🚀 wavespeed_image_service.py # WaveSpeed premium
│       │
│       └── video/                  # Servicios de video
│           └── 🎞️  motion_service.py # Prompts de movimiento (Claude)
│
├── static/
│   ├── css/style.css               # Estilos del frontend (dark theme)
│   └── js/app.js                   # Lógica del frontend (vanilla JS)
│
├── templates/
│   └── index.html                  # SPA frontend (una sola página)
│
├── projects/                       # Salida: videos, audios e imágenes generadas
│   └── {project-slug}/
│       ├── voiceover/              # audio-completo.mp3 + subtitles.srt
│       └── chunk_{N}/              # Audio, imagen, video y render por escena
│
├── promptguide.txt                 # Guía de estilo para Claude (scripts)
├── documentary.txt                 # Guía de estilo — modo documental
└── top10style.txt                  # Guía de estilo — modo Top 10
```

---

## ⚙️ Variables de entorno

Copiá `.env.example` a `.env` y completá las claves que necesitás:

```env
# 🤖 Modelos de IA
ANTHROPIC_API_KEY=sk-ant-...         # Claude (obligatorio)
OPENAI_API_KEY=sk-proj-...           # OpenAI GPT + Whisper (opcional)
GOOGLE_API_KEY=AIzaSy...             # Google Imagen 3 (opcional)

# 🎙️ Text-to-Speech
GENAIPRO_API_KEY=eyJh...             # GenAIPro TTS + video (obligatorio)

# 🖼️ Generación de imágenes
POLLINATIONS_API_KEY=sk_...          # Pollinations FLUX (gratuito, opcional)
WAVESPEED_API_KEY=...                # WaveSpeed i2v (opcional)

# 📸 Stock media
PEXELS_API_KEY=                      # Pexels (opcional)
PIXABAY_API_KEY=                     # Pixabay (opcional)

# 🎬 Renderizado
NCA_TOOLKIT_URL=http://localhost:8090 # NCA Toolkit (obligatorio para render)
NCA_API_KEY=                          # API key NCA (si está protegido)

# ⚙️ Runtime
IMAGE_PROVIDER=pollinations           # "pollinations" | "wavespeed" | "google"
MAX_WORKERS=3                         # Hilos paralelos para procesar escenas
PROJECTS_DIR=./projects               # Directorio de salida
```

---

## 🚀 Cómo ejecutar

```bash
# 1. Clonar el repositorio
git clone https://github.com/jona1818/Malderon_Creator.git
cd Malderon_Creator

# 2. Crear entorno virtual
python -m venv venv

# Windows
venv\Scripts\activate

# Linux / Mac
source venv/bin/activate

# 3. Instalar dependencias
pip install -r requirements.txt

# 4. Configurar variables de entorno
cp .env.example .env
# Editar .env con tus API keys

# 5. Iniciar el servidor
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Abrí el navegador en **http://localhost:8000**

---

## 🔄 Flujo del pipeline

El pipeline se ejecuta en 4 fases automáticas:

```
Fase 1 — 📝 Generación de Script
  └─ Claude genera el script completo a partir del tema

Fase 2 — 🎙️ Configuración de Voz
  └─ Usuario selecciona voz y parámetros TTS
  └─ GenAIPro genera audio-completo.mp3 + subtitles.srt

Fase 3 — 🖼️ División de escenas + Imágenes
  └─ Claude divide el script usando los timestamps reales del SRT
     (bloques de ~60s para videos largos)
  └─ Se generan prompts visuales para cada escena
  └─ Se generan imágenes (Imagen 3 / Pollinations / WaveSpeed)

Fase 4 — 🎬 Animación + Render
  └─ WaveSpeed anima cada imagen (i2v)
  └─ NCA Toolkit renderiza video + audio + subtítulos por escena
  └─ NCA Toolkit concatena todas las escenas en el video final
```

---

## 🎭 Modos de video

| Modo | Descripción |
|---|---|
| 🎨 **Animated** | Imágenes generadas con IA + animación WaveSpeed |
| 📦 **Stock** | Videos de stock de Pexels o Pixabay |

---

## 🗄️ Base de datos

SQLite con modo WAL para acceso concurrente. Tablas principales:

| Tabla | Descripción |
|---|---|
| `projects` | Proyecto de video (estado, script, configuración TTS, paths) |
| `chunks` | Escenas individuales (texto, imagen, audio, video, timestamps) |
| `logs` | Historial completo del pipeline por proyecto |
| `workers` | Estado del thread pool |
| `settings` | Configuración global (API keys, proveedor de imágenes) |

---

## 📦 Dependencias principales

```
fastapi          # Framework web
uvicorn          # Servidor ASGI
sqlalchemy       # ORM base de datos
anthropic        # Claude AI
openai           # GPT + Whisper
google-genai     # Imagen 3 + Gemini
requests         # HTTP cliente
sse-starlette    # Server-Sent Events
pydantic-settings # Configuración tipada
youtube-transcript-api # Transcripciones YouTube
```

---

## 📝 Licencia

Proyecto privado — todos los derechos reservados.
