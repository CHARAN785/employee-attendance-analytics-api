# Streamlit frontend

The frontend is a small Streamlit client for the FastAPI attendance service.

From the repository root, start the API in one terminal:

```powershell
cd backend
pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --reload --port 8000
```

In a second terminal, start the UI:

```powershell
cd frontend
pip install -r requirements.txt
$env:API_BASE_URL = "http://127.0.0.1:8000"
streamlit run app.py
```

The API URL can also be changed in the Streamlit sidebar. API errors are shown in the UI.
