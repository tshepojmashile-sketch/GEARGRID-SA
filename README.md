# AVMAN Inventory App

FastAPI app for AV equipment inventory, rentals, and quote generation.

## Features

- Equipment dashboard with rental status and overdue highlighting
- Add, edit, and delete equipment
- Client management
- Rent and return workflow with history tracking
- Quote builder with quantity, unit price, and rental days
- PDF quote download

## Run Locally

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Start the app:

```bash
uvicorn app:app --reload
```

4. Open:

`http://127.0.0.1:8000`

## Data

- SQLite database file: `rental.db`
- Generated quote file: `quote.pdf`
