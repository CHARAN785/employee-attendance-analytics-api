# Employee Attendance & Analytics API

FastAPI service backed by MongoDB for employee records, punch-in/out, audited attendance
corrections, and monthly/department analytics. The API follows `openapi.yaml`; the attendance
date and shift calculations use IST.

## Run locally (PowerShell)

Requirements: Python 3.11+ and MongoDB 6.0+.

```powershell
cd backend
pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

For MongoDB Atlas, set `MONGO_URI` to your connection string instead. Do not commit `.env`
files or credentials. The app creates its indexes when it starts.

Interactive API documentation is available at `http://localhost:8000/docs`.

To load the sample dataset into the configured database, open another terminal in `backend/`
and run `python sample_seed.py`. **The seed script clears and replaces the `employees` and
`attendance_logs` collections in that database.**

## Streamlit frontend

The UI is in the sibling `frontend/` directory. With the API running, open another terminal:

```powershell
cd frontend
pip install -r requirements.txt
$env:API_BASE_URL = "http://127.0.0.1:8000"
streamlit run app.py
```

The API base URL can also be changed in the UI sidebar.

## Files

- `app/main.py` — API, MongoDB aggregation pipelines, and business-rule helpers.
- `sample_data/` and `sample_seed.py` — sample records and a loader for a local database.
- `REVIEW.md` — starter-code review and corrections.
- `DECISIONS.md` — implementation and scaling decisions.

The sample records illustrate document shapes only; they do not cover every contract edge case.
