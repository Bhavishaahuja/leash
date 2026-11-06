from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app import demo, mandates, orders, review, session, stats
from backend.app.config import cors_origins, get_settings
from backend.app.log_redaction import install_log_redaction

# before anything can be served, so no request gets logged with its token
install_log_redaction()

app = FastAPI(title="Leash API")

# no cookies anywhere, the dashboard sends its session as a header, so no credentials either
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(get_settings().frontend_origins),
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type"],
)


# mandates first since orders get checked against them
app.include_router(mandates.router)
app.include_router(orders.router)
app.include_router(orders.agent_router)
app.include_router(review.router)
app.include_router(session.router)
app.include_router(demo.router)
app.include_router(stats.router)


@app.get("/health")
def health():
    return {"status": "ok"}
