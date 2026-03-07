from http.client import HTTPException

from fastapi import FastAPI 
import os
from datetime import datetime

from services.grid import detect_grid
from services.toolpath import plan_rectangle
from services.gcode import generate_gcode
from services.mqtt_sender import send_gcode_job

from pydantic import BaseModel


app = FastAPI(
    title="CNC Backend Service",
    version="1.0.0"
)

UPLOAD_DIR = "/home/omkar/Desktop/backend/uploads/cnc"

class ProcessRequest(BaseModel):
    image_name: str

@app.get("/")
def root():
    return {
        "service": "CNC Backend",
        "status": "running",
        "timestamp": datetime.utcnow().isoformat()
    }


@app.get("/health")
def health():
    return {"status": "healthy"}



@app.post("/process")
async def process(request: ProcessRequest):
    # Construct path to the already-uploaded file
    path = os.path.join(UPLOAD_DIR, request.image_name)
    
    # Verify file exists
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Image not found")
    
    # Process the image
    rect = detect_grid(path)
    toolpaths = plan_rectangle(rect)
    gcode = generate_gcode(toolpaths)
    
    job_id = send_gcode_job(gcode)
    
    return {
        "job_id": job_id,
        "status": "sent"
    }