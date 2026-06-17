# ScanMe — Photographer Service

Handles all photographer-facing operations: account signup/login, event CRUD, QR token generation, and publishing ingestion jobs to the Redis Stream when a new event is created.

**Port:** `8001`  
**Stack:** FastAPI · PostgreSQL · Redis Streams · JWT · bcrypt

---

## What This Service Does

| Endpoint | Auth | Description |
|---|---|---|
| `POST /auth/signup` | Public | Create a new photographer account |
| `POST /auth/login` | Public | Login and receive a JWT token |
| `GET /events/` | JWT | List all events for logged-in photographer |
| `POST /events/` | JWT | Create event + publish ingestion job to Redis |
| `PUT /events/{id}` | JWT | Update event name or Drive URL |
| `DELETE /events/{id}` | JWT | Delete event and all associated images |
| `GET /events/{id}` | JWT | Get single event details |
| `GET /health` | Public | Health check |

When a photographer creates an event, this service:
1. Saves the event to PostgreSQL with status `PENDING`
2. Publishes a message to the Redis Stream `photo.ingest`
3. The ingestion worker picks it up asynchronously

---

## Project Structure

```
sm-photographer/
├── app/
│   ├── main.py              # FastAPI app, lifespan, CORS, router registration
│   ├── models.py            # SQLAlchemy: User, Event, Image, EventStatus
│   ├── database.py          # Engine, SessionLocal, get_db, init_db
│   ├── api/
│   │   ├── auth.py          # Signup and login endpoints
│   │   └── events.py        # Event CRUD + Redis XADD
│   ├── schemas/
│   │   └── schemas.py       # Pydantic request/response models
│   ├── utils/
│   │   └── security.py      # JWT decode, bcrypt helpers, get_current_user
│   └── config/
│       └── settings.py      # Pydantic settings loaded from .env
├── requirements.txt
├── Dockerfile
└── .env
```

---

## Environment Variables

Create a `.env` file in the root of this service:

```bash
# PostgreSQL
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/scanme_db

# Redis — must match STREAM_NAME in ingestion-worker
REDIS_URL=redis://localhost:6379/0
STREAM_NAME=photo.ingest

# JWT — generate with: python -c "import secrets; print(secrets.token_hex(32))"
JWT_SECRET=your-super-secret-key-at-least-32-chars
JWT_ALGORITHM=HS256
JWT_EXPIRE_MINUTES=10080

# CORS
FRONTEND_ORIGIN=http://localhost:3000
```

> **Important:** `JWT_SECRET` must be the same value across all environments. Keep it out of version control — add `.env` to `.gitignore`.

---

## Running Manually (Local Development)

### Prerequisites

- Python 3.11+
- PostgreSQL running with `pgvector` extension
- Redis running
- Virtual environment activated

### Steps

```bash
# 1. Clone and enter the service
cd sm-photographer

# 2. Create and activate virtual environment
python -m venv venv

# Windows
venv\Scripts\activate

# macOS/Linux
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Create .env file and fill in values (see above)
copy .env.example .env     # Windows
cp .env.example .env       # macOS/Linux

# 5. Run the service
uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
```

The service will be available at `http://localhost:8001`  
Interactive API docs at `http://localhost:8001/docs`

---

## Running with Docker

### Build and run this service only

```bash
# Build
docker build -t scanme-photographer .

# Run (requires postgres and redis already running)
docker run -p 8001:8001 --env-file .env scanme-photographer
```

### Run with infrastructure (recommended)

Use the shared `docker-compose-infra.yml` from the root to start PostgreSQL and Redis first:

```bash
# From the root of the monorepo or your infra folder
docker-compose -f docker-compose-infra.yml up -d

# Then run this service
cd sm-photographer
docker build -t scanme-photographer .
docker run -p 8001:8001 --env-file .env --network host scanme-photographer
```

---

## Dependencies

| Package | Purpose |
|---|---|
| `fastapi` | HTTP framework |
| `uvicorn` | ASGI server |
| `sqlalchemy` | ORM |
| `psycopg2-binary` | PostgreSQL driver |
| `pgvector` | Vector column type |
| `python-jose[cryptography]` | JWT encode/decode |
| `passlib[bcrypt]` | Password hashing |
| `python-multipart` | OAuth2 form data parsing |
| `redis` | Redis XADD for stream publishing |
| `pydantic-settings` | `.env` → Settings class |

---

## Related Services

| Service | Repo | Description |
|---|---|---|
| Guest Service | `scanme-guest` | Gallery and selfie matching |
| Ingestion Worker | `scanme-ingestion-worker` | Processes events from Redis Stream |
| API Gateway | `scanme-gateway` | Nginx routing on port 80 |
| Frontend | `scanme-frontend` | Next.js photographer dashboard |

---

## Notes

- JWT tokens expire after 7 days (`JWT_EXPIRE_MINUTES=10080`)
- This service never handles photo files — it only stores metadata and publishes job IDs
- `face_engine`, `boto3`, and `google-api-python-client` are intentionally excluded from this service's dependencies
