from fastapi import FastAPH
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import os

app = FastAPI()

# Serve static files
assets_path = os.path.join("site", "assets")
if os.path.exists(assets_path):
    app.mount("/assets", StaticFiles(directory=assets_path), name="assets")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/hello")
def hello():
    return {"message": "hello world"}

@app.get("/", response_class=HTMLResponse)
def read_index():
    index_path = os.path.join("site", "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/about", response_class=HTMLResponse)
def read_about():
    about_path = os.path.join("site", "about.html")
    with open(about_path, "r", encoding="utf-8") as f:
        return f.read()
