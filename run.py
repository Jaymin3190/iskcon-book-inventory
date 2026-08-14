import uvicorn

from app.database import init_db
from app.seed import seed

if __name__ == "__main__":
    init_db()
    seed()
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
