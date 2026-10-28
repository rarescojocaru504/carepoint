from fastapi import APIRouter

from app.api.v1 import admin, auth, discovery, machine_api, reservations, users

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(discovery.router)
api_router.include_router(reservations.router)
api_router.include_router(admin.router)
api_router.include_router(machine_api.router)
