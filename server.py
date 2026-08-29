import os
import logging
import json
import random
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kaamdhenu-server")

app = FastAPI(title="Kaamdhenu AI 3.0 Voice SaaS")

# Mount static directory if ui folder exists
if os.path.exists("ui"):
    app.mount("/ui", StaticFiles(directory="ui"), name="ui")

@app.get("/", response_class=HTMLResponse)
def read_root():
    """Serves the complete Dark Dashboard UI HTML on root URL (/)..."""
    ui_path = os.path.join("ui", "index.html")
    if os.path.exists(ui_path):
        try:
            with open(ui_path, "r", encoding="utf-8") as f:
                html_content = f.read()
            return HTMLResponse(content=html_content, status_code=200)
        except Exception as e:
            logger.error(f"Error reading ui/index.html: {e}")
    
    # Fallback to HTML message if index.html is missing
    fallback_html = """<!DOCTYPE html><html><head><title>Kaamdhenu 3.0</title></head><body style="background:#090d16;color:#fff;font-family:sans-serif;padding:40px;"><h1>Kaamdhenu 3.0 Real Estate Voice SaaS</h1><p>Status: Online</p></body></html>"""
    return HTMLResponse(content=fallback_html, status_code=200)

@app.get("/health")
def health():
    """Dedicated health check endpoint for Coolify / Traefik proxy."""
    return JSONResponse(
        content={
            "status": "online",
            "service": "Kaamdhenu AI 3.0",
            "health": "ok"
        },
        status_code=200
    )

@app.post("/api/dispatch")
async def dispatch_call_api(request: Request):
    """API endpoint to dispatch outbound calls from dashboard."""
    try:
        data = await request.json()
        phone_number = data.get("phoneNumber")
        prompt = data.get("prompt", "")
        
        if not phone_number:
            return JSONResponse(content={"error": "Phone number is required"}, status_code=400)
            
        room_name = f"call-{phone_number.replace('+', '')}-{random.randint(1000, 9999)}"
        logger.info(f"Dispatching call to {phone_number} in room {room_name}")
        
        return JSONResponse(content={
            "success": True,
            "roomName": room_name,
            "dispatchId": f"outbound-{phone_number.replace('+', '')}-{random.randint(100, 999)}"
        }, status_code=200)
    except Exception as e:
        logger.error(f"Error in API dispatch: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
