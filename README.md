# Employee Attendance & Analytics API

FastAPI service backed by MongoDB for employee records, punch-in/out, audited attendance
corrections, and monthly/department analytics. The API follows `openapi.yaml`; the attendance
date and shift calculations use IST.

## Run locally

Requirements: Python 3.11+ and MongoDB 6.0+.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
python sample_seed.py
uvicorn app.main:app --port 8000
```

For MongoDB Atlas, set `MONGO_URI` to your connection string in the environment instead.
Do not commit `.env` files or credentials. The app creates its indexes when it starts.

Interactive API documentation is available at `http://localhost:8000/docs`.

## Files

- `app/main.py` — API, MongoDB aggregation pipelines, and business-rule helpers.
- `sample_data/` and `sample_seed.py` — sample records and a loader for a local database.
- `REVIEW.md` — starter-code review and corrections.
- `DECISIONS.md` — implementation and scaling decisions.

The sample records illustrate document shapes only; they do not cover every contract edge case.
