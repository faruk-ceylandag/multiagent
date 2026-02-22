"""Hub API routers."""
from .agents import router as agents_router
from .tasks import router as tasks_router
from .messages import router as messages_router
from .git import router as git_router
from .logs import router as logs_router
from .costs import router as costs_router
from .credentials import router as credentials_router
from .analytics import router as analytics_router
from .health import router as health_router
from .websocket import router as ws_router
from .patterns import router as patterns_router
from .cache import router as cache_router
from .workspaces import router as workspaces_router

all_routers = [
    agents_router,
    tasks_router,
    messages_router,
    git_router,
    logs_router,
    costs_router,
    credentials_router,
    analytics_router,
    health_router,
    ws_router,
    patterns_router,
    cache_router,
    workspaces_router,
]
