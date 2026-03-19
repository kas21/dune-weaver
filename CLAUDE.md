# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Dune Weaver is an open-source kinetic sand art table control system. A Raspberry Pi runs a FastAPI backend + React PWA frontend, communicating over USB with a DLC32/ESP32 running FluidNC firmware to drive stepper motors in polar coordinates (theta/rho).

## Commands

### Development
```bash
npm run dev              # Run frontend (Vite :5173) + backend (FastAPI :8080) concurrently
npm run dev:frontend     # Frontend only
npm run dev:backend      # Backend only (python main.py)
```

### Build
```bash
npm run build            # Build frontend → ../static/dist
```

### Backend Tests
```bash
pytest tests/unit/ -v                    # Unit tests (no hardware needed)
pytest tests/unit/ -v --cov             # With coverage
pytest tests/unit/test_pattern_manager.py -v  # Single test file
pytest tests/unit/ -k "test_name"       # Single test by name
pytest tests/integration/ --run-hardware -v  # Hardware tests (Pi only)
```

### Frontend Tests
```bash
cd frontend && npm test                  # Vitest (single run)
cd frontend && npm run test:watch        # Watch mode
cd frontend && npm run test:coverage     # With coverage
cd frontend && npm run test:e2e          # Playwright E2E
cd frontend && npm run test:e2e:ui       # Playwright interactive
```

### Linting
```bash
ruff check .             # Python linting (add --fix to auto-correct)
cd frontend && npm run lint  # ESLint for TypeScript/React
```

### Pre-commit Hook
Installed automatically via `npm install` (prepare script). Runs Ruff on Python + Vitest on TypeScript.

## Architecture

### Backend (Python/FastAPI)
- **`main.py`** — Single FastAPI app with all REST (`/api/*`) and WebSocket (`/ws/*`) endpoints. This is a large file (~4200 lines) that serves as the application entry point.
- **`modules/core/`** — Business logic: `state.py` (global app state + persistence), `pattern_manager.py` (pattern file handling, polar coordinate parsing), `playlist_manager.py`, `cache_manager.py` (preview image generation)
- **`modules/connection/`** — Serial communication with FluidNC controller, G-code execution
- **`modules/led/`** — LED control (native DW LEDs + WLED integration)
- **`modules/mqtt/`** — Home Assistant integration via MQTT
- **`modules/wifi/`** — WiFi setup and captive portal

### Frontend (React 19 + TypeScript + Vite)
- **State**: Zustand stores (`stores/`) + React Query for server state
- **UI**: Radix UI primitives + TailwindCSS (CSS variable-based theming)
- **Multi-table**: `TableContext` enables controlling multiple tables; `apiClient.ts` handles dynamic base URL switching
- **Pages**: Route-based (`pages/`) — Browse, Playlists, TableControl, LED, Settings, WiFiSetup, CaptivePortal, Setup

### Communication
- REST API for CRUD operations
- WebSocket for real-time updates: `/ws/status`, `/ws/logs`, `/ws/cache-progress`
- Vite dev server proxies `/api` and `/ws` to backend at `:8080`

### Deployment (Production on Pi)
- nginx reverse proxy (port 80) → FastAPI (port 8080)
- systemd service (`dune-weaver.service`) runs as root for GPIO/USB access
- Frontend built to `static/dist/`, served by nginx

### Pattern System
- Patterns use polar coordinates: theta (angle in radians) and rho (0.0=center, 1.0=edge)
- Stored as `.thr` text files (one coordinate pair per line) in `patterns/`
- Backend converts to G-code for the FluidNC controller

## Key Technical Details

- Python 3.9+ required; pytest uses `asyncio_mode = "auto"`
- Hardware tests are marked `@pytest.mark.hardware` and skipped in CI
- Frontend build output goes to `static/dist/` (not the default `dist/`)
- Radix UI components are vendored into the React bundle to avoid TDZ errors (see recent commits)
- `dune-weaver-touch/` is a separate Qt/QML touchscreen add-on, not part of the main web app
