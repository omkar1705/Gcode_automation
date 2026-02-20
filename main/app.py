from fastapi import FastAPI, UploadFile, File

import shutil

import os

from datetime import datetime



from services.grid import detect_grid

from services.toolpath import plan_rectangle

from services.gcode import generate_gcode

from services.mqtt_sender import send_gcode_job



app = FastAPI(

    title="CNC Backend Service",

    version="1.0.0"

)



UPLOAD_DIR = "uploads"

os.makedirs(UPLOAD_DIR, exist_ok=True)





# ---------------------------------------------

# Root endpoint

# ---------------------------------------------

@app.get("/")

def root():

    return {

        "service": "CNC Backend",

        "status": "running",

        "timestamp": datetime.utcnow().isoformat()

    }





# ---------------------------------------------

# Health check endpoint

# ---------------------------------------------

@app.get("/health")

def health():

    return {

        "status": "healthy"

    }





# ---------------------------------------------

# Process endpoint

# ---------------------------------------------

@app.post("/process")

async def process(image: UploadFile = File(...)):

    path = os.path.join(UPLOAD_DIR, image.filename)



    with open(path, "wb") as buffer:

        shutil.copyfileobj(image.file, buffer)



    rect = detect_grid(path)

    toolpaths = plan_rectangle(rect)

    gcode = generate_gcode(toolpaths)



    job_id = send_gcode_job(gcode)



    return {

        "job_id": job_id,

        "status": "sent"

    }