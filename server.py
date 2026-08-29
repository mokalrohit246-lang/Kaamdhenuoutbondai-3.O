import os
import logging
from fastapi import FastAPI
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kaamdhenu-server")

app = FastAPI(title="Kaamdhenu AI Voice Service")

@app.get("/")
def root():
    """Root healthcheck endpoint returning 200 OK for Coolify / Traefik proxy."""
    return JSONResponse(
        content={
            "status": "online",
            "service": "Kaamdhenu AI 3.0 Voice SaaS",
            "livekit_url": os.getenv("LIVEKIT_URL", "not_configured"),
            "health": "ok"
        },
        status_code=200
    )

@app.get("/health")
def health():
    """Explicit health check endpoint."""
    return JSONResponse(
        content={"status": "healthy"},
        status_code=200
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
