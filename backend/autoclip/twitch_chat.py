"""Optional Twitch VOD chat replay retrieval and asset caching.

Twitch does not expose historical VOD chat replay through the public Helix API.
This module uses the same first-party GraphQL replay data Twitch's web clients
and established VOD-chat tools consume. It is imported only by the review API
after the user explicitly clicks "Load Twitch chat".

Nothing in ingest, transcription, or highlight detection imports this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

GQL_URL = "https://gql.twitch.tv/gql"
TWITCH_WEB_CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"
TWITCH_BADGE_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
COMMENTS_QUERY_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"
_REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
MAX_PAGES = 150
MAX_MESSAGES = 5000
CHAT_CACHE_VERSION = 2
_ASSET_CONCURRENCY = 8

_TWITCH_HOSTS = {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}
_TWITCH_ASSET_HOSTS = {"static-cdn.jtvnw.net"}
_VIDEO_ID_RE = re.compile(r"(?:^|/)(?:videos|v|video)/(\d+)(?:/|$)")


class TwitchChatError(RuntimeError):
    """Chat replay could not be loaded."""


@dataclass(frozen=True)
class TwitchChatBadge:
    set_id: str
    version: str
    title: str = ""
    image_url: str | None = None
    asset_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TwitchChatBadge":
        return cls(
            set_id=str(data.get("set_id") or ""),
            version=str(data.get("version") or ""),
            title=str(data.get("title") or ""),
            image_url=str(data["image_url"]) if data.get("image_url") else None,
            asset_id=str(data["asset_id"]) if data.get("asset_id") else None,
        )


@dataclass(frozen=True)
class TwitchChatFragment:
    text: str
    emote_id: str | None = None
    image_url: str | None = None
    asset_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TwitchChatFragment":
        return cls(
            text=str(data.get("text") or ""),
            emote_id=str(data["emote_id"]) if data.get("emote_id") else None,
            image_url=str(data["image_url"]) if data.get("image_url") else None,
            asset_id=str(data["asset_id"]) if data.get("asset_id") else None,
        )


@dataclass(frozen=True)
class TwitchChatMessage:
    id: str
    offset_s: float
    username: str
    message: str
    user_color: str | None = None
    badges: tuple[TwitchChatBadge, ...] = ()
    fragments: tuple[TwitchChatFragment, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TwitchChatMessage":
        return cls(
            id=str(data.get("id") or ""),
            offset_s=float(data.get("offset_s") or 0.0),
            username=str(data.get("username") or ""),
            message=str(data.get("message") or ""),
            user_color=(str(data["user_color"]) if data.get("user_color") else None),
            badges=tuple(TwitchChatBadge.from_dict(item) for item in data.get("badges", [])),
            fragments=tuple(
                TwitchChatFragment.from_dict(item) for item in data.get("fragments", [])
            ),
        )


def vod_id_from_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (parsed.hostname or "").lower() not in _TWITCH_HOSTS:
        return None
    match = _VIDEO_ID_RE.search(parsed.path or "")
    return match.group(1) if match else None


def _emote_image_url(emote_id: str) -> str:
    return (
        "https://static-cdn.jtvnw.net/emoticons/v2/"
        f"{emote_id}/default/dark/2.0"
    )


async def _fetch_badge_catalog(
    broadcaster_id: str | None,
) -> dict[tuple[str, str], tuple[str, str]]:
    """Return (set, version) -> (title, 2x image URL)."""
    queries = [
        "query{badges{imageURL(size:DOUBLE),description,title,setID,version}}",
    ]
    if broadcaster_id and broadcaster_id.isdigit():
        queries.append(
            "query{user(id: "
            + broadcaster_id
            + "){broadcastBadges{imageURL(size:DOUBLE),description,title,setID,version}}}"
        )

    catalog: dict[tuple[str, str], tuple[str, str]] = {}
    headers = {"Client-ID": TWITCH_BADGE_CLIENT_ID}
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, headers=headers) as client:
        for index, query in enumerate(queries):
            try:
                response = await client.post(GQL_URL, json={"query": query, "variables": {}})
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError):
                # Badge metadata is a visual enhancement. Chat text remains useful
                # even when this separate Twitch request fails.
                continue

            data = body.get("data") or {}
            rows = data.get("badges") if index == 0 else (data.get("user") or {}).get(
                "broadcastBadges"
            )
            for row in rows or []:
                set_id = str(row.get("setID") or "")
                version = str(row.get("version") or "")
                image_url = str(row.get("imageURL") or "")
                if not set_id or not version or not image_url:
                    continue
                catalog[(set_id, version)] = (
                    str(row.get("title") or ""),
                    image_url,
                )
    return catalog


async def fetch_vod_chat(
    vod_id: str,
    *,
    start_s: float,
    end_s: float,
) -> list[TwitchChatMessage]:
    """Fetch chat replay messages intersecting one clip's source-time range."""
    if end_s <= start_s:
        return []

    messages: list[TwitchChatMessage] = []
    seen: set[str] = set()
    cursor: str | None = None
    latest_offset = max(0.0, start_s - 1.0)
    broadcaster_id: str | None = None

    headers = {"Client-ID": TWITCH_WEB_CLIENT_ID}
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT, headers=headers) as client:
        for _ in range(MAX_PAGES):
            variables: dict[str, Any] = {"videoID": vod_id}
            if cursor:
                variables["cursor"] = cursor
            else:
                variables["contentOffsetSeconds"] = max(0, int(start_s))

            payload = {
                "operationName": "VideoCommentsByOffsetOrCursor",
                "variables": variables,
                "extensions": {
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": COMMENTS_QUERY_HASH,
                    }
                },
            }

            try:
                response = await client.post(GQL_URL, json=payload)
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise TwitchChatError(
                    "Twitch chat replay could not be loaded. Twitch's VOD chat interface "
                    "is unavailable or changed."
                ) from exc

            if body.get("errors"):
                raise TwitchChatError("Twitch returned an error while loading VOD chat replay.")

            video = ((body.get("data") or {}).get("video") or {})
            creator = video.get("creator") or {}
            if creator.get("id"):
                broadcaster_id = str(creator["id"])

            comments = video.get("comments")
            if comments is None:
                raise TwitchChatError(
                    "This Twitch VOD has no available chat replay, or Twitch did not return it."
                )

            edges = comments.get("edges") or []
            if not edges:
                break

            for edge in edges:
                node = (edge or {}).get("node") or {}
                try:
                    offset = float(node.get("contentOffsetSeconds"))
                except (TypeError, ValueError):
                    continue
                latest_offset = max(latest_offset, offset)
                if offset < start_s or offset > end_s:
                    continue

                commenter = node.get("commenter") or {}
                username = str(commenter.get("displayName") or commenter.get("login") or "").strip()
                message_data = node.get("message") or {}
                fragment_rows = message_data.get("fragments") or []
                text = "".join(
                    str(fragment.get("text") or "") for fragment in fragment_rows
                ).strip()
                if not username or not text:
                    continue

                message_id = str(node.get("id") or f"{offset:.3f}:{username}:{text[:32]}")
                if message_id in seen:
                    continue
                seen.add(message_id)

                fragments: list[TwitchChatFragment] = []
                for fragment in fragment_rows:
                    fragment_text = str(fragment.get("text") or "")
                    emote = fragment.get("emote") or {}
                    emote_id = str(emote.get("emoteID") or emote.get("id") or "")
                    fragments.append(
                        TwitchChatFragment(
                            text=fragment_text,
                            emote_id=emote_id or None,
                            image_url=_emote_image_url(emote_id) if emote_id else None,
                        )
                    )

                badges = tuple(
                    TwitchChatBadge(
                        set_id=str(badge.get("setID") or ""),
                        version=str(badge.get("version") or ""),
                    )
                    for badge in (message_data.get("userBadges") or [])
                    if badge.get("setID") and badge.get("version")
                )

                color = message_data.get("userColor")
                messages.append(
                    TwitchChatMessage(
                        id=message_id,
                        offset_s=offset,
                        username=username,
                        message=text,
                        user_color=str(color) if color else None,
                        badges=badges,
                        fragments=tuple(fragments),
                    )
                )
                if len(messages) >= MAX_MESSAGES:
                    break

            if len(messages) >= MAX_MESSAGES:
                break

            page_info = comments.get("pageInfo") or {}
            if latest_offset >= end_s or not page_info.get("hasNextPage"):
                break

            cursor = str((edges[-1] or {}).get("cursor") or "")
            if not cursor:
                break

    if not messages:
        return []

    catalog = await _fetch_badge_catalog(broadcaster_id)
    if catalog:
        messages = [
            replace(
                message,
                badges=tuple(
                    replace(
                        badge,
                        title=catalog.get((badge.set_id, badge.version), ("", ""))[0],
                        image_url=catalog.get((badge.set_id, badge.version), ("", ""))[1] or None,
                    )
                    for badge in message.badges
                ),
            )
            for message in messages
        ]

    return sorted(messages, key=lambda item: item.offset_s)


def is_valid_asset_id(asset_id: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{20}\.(?:png|jpg|gif|webp)", asset_id))


def _asset_extension(content_type: str) -> str:
    value = content_type.split(";", 1)[0].strip().lower()
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(value, ".png")


def _allowed_asset_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in _TWITCH_ASSET_HOSTS


async def cache_message_assets(
    messages: list[TwitchChatMessage],
    directory: Path,
) -> list[TwitchChatMessage]:
    """Cache Twitch badge/emote images after the user explicitly loads chat."""
    urls = {
        url
        for message in messages
        for url in [
            *(badge.image_url for badge in message.badges),
            *(fragment.image_url for fragment in message.fragments),
        ]
        if url and _allowed_asset_url(url)
    }
    if not urls:
        return messages

    directory.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(_ASSET_CONCURRENCY)
    asset_ids: dict[str, str] = {}

    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        async def fetch(url: str) -> None:
            async with semaphore:
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                except httpx.HTTPError:
                    return
                extension = _asset_extension(response.headers.get("content-type", ""))
                digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
                asset_id = digest + extension
                path = directory / asset_id
                if not path.exists():
                    path.write_bytes(response.content)
                asset_ids[url] = asset_id

        await asyncio.gather(*(fetch(url) for url in sorted(urls)))

    return [
        replace(
            message,
            badges=tuple(
                replace(
                    badge,
                    asset_id=asset_ids.get(badge.image_url or ""),
                )
                for badge in message.badges
            ),
            fragments=tuple(
                replace(
                    fragment,
                    asset_id=asset_ids.get(fragment.image_url or ""),
                )
                for fragment in message.fragments
            ),
        )
        for message in messages
    ]
