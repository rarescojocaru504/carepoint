"""A small client for the server-sent-event streams, over a real server
(the `gateway_url` fixture). Shared by the operator and customer stream tests."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from app.core.security import create_access_token
from app.models import User, UserRole
from tests.world import World


async def user_with_token(world: World, role: UserRole) -> tuple[User, dict[str, str]]:
    user = User(email=f"{role.value}-{uuid.uuid4().hex[:10]}@example.com", password_hash="!",
                full_name="Someone", role=role)  # fmt: skip
    await world.add(user)
    return user, {"Authorization": f"Bearer {create_access_token(user.id, user.role)[0]}"}


def http_client(gateway_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=gateway_url.replace("ws://", "http://"), timeout=10)


class Notices:
    def __init__(self, response: httpx.Response) -> None:
        self._lines = response.aiter_lines()
        self.seen: list[dict[str, Any]] = []

    async def next(self, within: float = 5) -> dict[str, Any]:
        async with asyncio.timeout(within):
            async for line in self._lines:
                if line.startswith("data: "):
                    notice: dict[str, Any] = json.loads(line[6:])
                    self.seen.append(notice)
                    return notice
        raise AssertionError("stream ended")

    async def until(
        self, topic: str, change: str, machine_id: str | None, within: float = 5
    ) -> dict[str, Any]:
        """The next matching notice. Others are skipped: Redis pub/sub ignores
        database numbers, so a live stack on the same server (the Docker smoke
        test runs pytest next to six simulated machines) adds its own notices."""
        async with asyncio.timeout(within):
            while True:
                notice = await self.next(within)
                if (notice["topic"], notice["change"], notice["machine_id"]) == (
                    topic, change, machine_id,
                ):  # fmt: skip
                    return notice

    async def until_closed(self, within: float) -> None:
        async with asyncio.timeout(within):
            async for _ in self._lines:
                pass


@asynccontextmanager
async def open_stream(
    gateway_url: str, path: str, headers: dict[str, str]
) -> AsyncIterator[Notices]:
    async with (
        http_client(gateway_url) as http,
        http.stream("GET", path, headers=headers) as response,
    ):
        assert response.status_code == 200, response.status_code
        assert response.headers["content-type"].startswith("text/event-stream")
        notices = Notices(response)
        assert (await notices.next())["topic"] == "hello"
        yield notices
