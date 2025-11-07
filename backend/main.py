from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(
    title="ScenePulse Backend",
    description="Lumetric Labs - ScenePulse predictive ad testing API",
    version="1.0.0"
)

@app.get("/")
def root():
    return JSONResponse({"status": "ok", "message": "ScenePulse API running"})

@app.get("/ping")
def ping():
    return JSONResponse({"response": "pong"})

@app.get("/hello")
def hello():
    return JSONResponse({"message": "Hello from ScenePulse"})
