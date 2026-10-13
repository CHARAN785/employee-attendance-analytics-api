# Employee Attendance & Analytics

This repository contains the FastAPI backend in `backend/` and a Streamlit frontend in
`frontend/`. The root `app/main.py` exposes the assignment-required
`uvicorn app.main:app` entry point.

## Start the API

Requirements: Python 3.11+ and MongoDB 6.0+.

```powershell
pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

API documentation is at `http://127.0.0.1:8000/docs`.

## Start the Streamlit frontend

In a second terminal:

```powershell
pip install -r frontend/requirements.txt
$env:API_BASE_URL = "http://127.0.0.1:8000"
streamlit run frontend/app.py
```

See [backend/README.md](backend/README.md) and [frontend/README.md](frontend/README.md) for
details. Do not commit `.env` files or database credentials.
