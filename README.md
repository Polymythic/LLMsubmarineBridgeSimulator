# Submarine Bridge Simulator (MVP)

FastAPI backend with a 20 Hz simulation loop and five station UIs served from one host.

## Requirements
- Python 3.11+

## Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run
```bash
uvicorn sub-bridge.backend.app:app --reload --host 0.0.0.0 --port 8000
```

Open:
- http://localhost:8000/
- http://localhost:8000/captain
- http://localhost:8000/helm
- http://localhost:8000/sonar
- http://localhost:8000/weapons
- http://localhost:8000/engineering

LAN access: http://192.168.1.100:8000/ (adjust to your host IP)
