from fastapi import APIRouter

from app.api.v1 import (
    admin,
    admin_events,
    auth,
    discovery,
    machine_api,
    machine_ws,
    orders,
    reservations,
    users,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(discovery.router)
api_router.include_router(reservations.router)
api_router.include_router(orders.router)
api_router.include_router(admin.router)
api_router.include_router(admin_events.router)
api_router.include_router(machine_api.router)
api_router.include_router(machine_ws.router)
