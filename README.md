# Employee Attendance & Analytics API

A REST API built using FastAPI and MongoDB.

## Features
- Create and list employees
- Attendance punch-in and punch-out
- Retrieve and update attendance records
- Calculate work hours and overtime

## Tech Stack
- Python
- FastAPI
- MongoDB
- Pytest

## Setup
1. Install dependencies using pip install -r requirements.txt
2. Configure MONGO_URI in your local .env file.
3. Run python -m uvicorn app.main:app --host 127.0.0.1 --port 8002
4. Open http://127.0.0.1:8002/docs

## Tests
Run python -m pytest -v
