---
title: FastAPI
type: tool
tags: [python, web-framework, REST-API, async, OpenAPI, backend]
created: 2026-02-26T00:00:00Z
updated: 2026-04-05T14:34:52Z
status: active
---

## Overview

FastAPI is a modern Python web framework for building REST APIs with automatic OpenAPI documentation, type hint-based validation, and native async support. It has become the dominant choice for AI/ML API backends due to its performance, developer experience, and tight integration with Python's type system.

It is built on **Starlette** (ASGI framework) and **Pydantic** (data validation).

## Key Features

### Performance
- Among the fastest Python frameworks — comparable to NodeJS and Go for I/O-bound workloads
- Async-first: `async def` endpoints run without blocking
- Uvicorn ASGI server (or Hypercorn for HTTP/2)

### Type Safety
- Pydantic v2 integration: request bodies, query params, path params all validated automatically
- IDE autocomplete works end-to-end (request → response models)
- Runtime validation with clear error messages (422 Unprocessable Entity)

### Automatic Documentation
- OpenAPI 3.x spec generated automatically from type hints
- Swagger UI at `/docs`
- ReDoc at `/redoc`
- No extra configuration needed

### Dependency Injection
- Clean mechanism for shared dependencies (database sessions, auth, config)
- Supports async and sync dependencies
- Testable without mocking HTTP

## Usage Patterns

### Basic Endpoint

```python
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

class Item(BaseModel):
    name: str
    price: float
    in_stock: bool = True

@app.post("/items/", response_model=Item)
async def create_item(item: Item):
    return item

@app.get("/items/{item_id}")
async def read_item(item_id: int, q: str | None = None):
    return {"item_id": item_id, "q": q}
```

### Dependency Injection

```python
from fastapi import Depends

async def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/users/")
async def list_users(db: Session = Depends(get_db)):
    return db.query(User).all()
```

### Background Tasks

```python
from fastapi import BackgroundTasks

def send_email(email: str, message: str):
    ...  # runs after response is sent

@app.post("/notify/")
async def notify(background_tasks: BackgroundTasks, email: str):
    background_tasks.add_task(send_email, email, "Hello!")
    return {"message": "Notification scheduled"}
```

## Common Patterns for AI/ML APIs

### Streaming Responses (LLM output)

```python
from fastapi.responses import StreamingResponse

@app.post("/generate/")
async def generate(prompt: str):
    async def stream_tokens():
        async for token in model.astream(prompt):
            yield f"data: {token}\n\n"
    return StreamingResponse(stream_tokens(), media_type="text/event-stream")
```

### File Upload (for VLM/OCR endpoints)

```python
from fastapi import File, UploadFile
import io
from PIL import Image

@app.post("/analyze-image/")
async def analyze_image(file: UploadFile = File(...)):
    contents = await file.read()
    image = Image.open(io.BytesIO(contents))
    result = vlm_model.analyze(image)
    return {"result": result}
```

## Deployment

- **Development**: `fastapi dev main.py` (auto-reload)
- **Production**: `uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4`
- **Docker**: standard Python image + uvicorn
- **Containerized with GPU**: use nvidia/cuda base image

## Comparison with Alternatives

| Framework | Use Case | Strengths |
|---|---|---|
| FastAPI | REST APIs, AI backends | Speed, type safety, docs |
| Flask | Small apps, quick prototypes | Simplicity, ecosystem |
| Django | Full web apps | Batteries included, ORM |
| Starlette | Low-level ASGI | Direct control |

## Related Links

[[relatedTo::MCP Server Architecture]]
[[relatedTo::Async FastMCP Embedding Pattern (aiohttp)]]
[[relatedTo::Server-Side Chat Session Management Pattern]]
[[relatedTo::REST API Design Patterns]]
[[relatedTo::Docker Container DNS Caching Gotcha]]
