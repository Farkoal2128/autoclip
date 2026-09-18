"""REST API contract tests.

Exercised through the real app with its lifespan running, so the database
migration, event broker binding, and job queue startup are all covered too.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator

import pytest
from autoclip import app as app_module
from autoclip import config
from autoclip.app import create_app
from autoclip.db import store
from autoclip.db.models import Clip, Job, Source, new_id
from fastapi.testclient import TestClient


@pytest.fixture
def client(autoclip_home, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # The background worker is off here so queued jobs stay queued: these tests
    # are about the HTTP contract, and a worker racing to pick jobs up would
    # make every status assertion flaky. The worker has its own tests.
    monkeypatch.setenv(app_module.ENV_NO_WORKER, "1")
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def source() -> Source:
    return store.create_source(
        Source(
            id=new_id(),
            type="upload",
            path="C:/media/video.mp4",
            title="Test source",
            duration_s=600.0,
            width=1920,
            height=1080,
            fps=30.0,
        )
    )


class TestHealthAndSystem:
    def test_health(self, client: TestClient) -> None:
        response = client.get("/api/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_system_reports_this_machine(self, client: TestClient) -> None:
        body = client.get("/api/system").json()

        # Compared against the running interpreter, not a hardcoded version:
        # the project supports 3.11 and 3.12, and pinning the assertion to one
        # of them made the test contradict the CI matrix it runs under.
        running = f"{sys.version_info.major}.{sys.version_info.minor}"
        assert body["python_version"].startswith(running)
        assert (3, 11) <= sys.version_info[:2] < (3, 13)

        assert body["accel"] in ("cuda", "mps", "cpu")
        assert isinstance(body["nvenc_works"], bool)

    def test_unknown_api_route_is_404_not_the_spa(self, client: TestClient) -> None:
        # The SPA catch-all must never swallow a mistyped API path — that turns
        # a clear 404 into an HTML page the client can't parse.
        assert client.get("/api/does-not-exist").status_code == 404


    def test_open_local_data_location(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, autoclip_home
    ) -> None:
        from autoclip import paths
        from autoclip.api import settings as settings_api

        opened = []
        monkeypatch.setattr(settings_api, "_open_folder", opened.append)

        response = client.post("/api/system/open-location/data")

        assert response.status_code == 204
        assert opened == [paths.data_root()]

    def test_open_install_location(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from autoclip import paths
        from autoclip.api import settings as settings_api

        opened = []
        monkeypatch.setattr(settings_api, "_open_folder", opened.append)

        response = client.post("/api/system/open-location/install")

        assert response.status_code == 204
        assert opened == [paths.install_dir()]

    def test_unknown_open_location_is_404(self, client: TestClient) -> None:
        assert client.post("/api/system/open-location/nope").status_code == 404


class TestCaptionStyles:
    def test_lists_all_four(self, client: TestClient) -> None:
        styles = client.get("/api/caption-styles").json()

        assert {s["key"] for s in styles} == {
            "bold_pop",
            "karaoke_fill",
            "clean_lower",
            "boxed",
        }

    def test_includes_preview_metadata(self, client: TestClient) -> None:
        styles = client.get("/api/caption-styles").json()
        preview = next(s for s in styles if s["key"] == "bold_pop")["preview"]

        assert preview["accent"]
        assert preview["allCaps"] is True
        assert preview["maxWords"] > 0
        assert preview["scalePercent"] == 118
        assert preview["boxAlpha"] >= 0
        assert "bold" in preview


class TestSettings:
    def test_get_returns_defaults(self, client: TestClient) -> None:
        body = client.get("/api/settings").json()

        assert body["active_provider"] == "anthropic"
        assert body["whisper"]["model"] == "small"

    def test_partial_update_leaves_other_sections_alone(self, client: TestClient) -> None:
        client.put("/api/settings", json={"whisper": {"model": "large-v3"}})

        body = client.get("/api/settings").json()
        assert body["whisper"]["model"] == "large-v3"
        assert body["clips"]["max_clips"] == 10

    def test_provider_switch(self, client: TestClient) -> None:
        response = client.put("/api/settings", json={"active_provider": "ollama"})

        assert response.status_code == 200
        assert response.json()["active_provider"] == "ollama"

    def test_unknown_provider_is_rejected(self, client: TestClient) -> None:
        response = client.put("/api/settings", json={"active_provider": "skynet"})

        assert response.status_code == 400

    def test_inverted_clip_lengths_are_rejected(self, client: TestClient) -> None:
        response = client.put(
            "/api/settings", json={"clips": {"min_duration_s": 90, "max_duration_s": 20}}
        )

        assert response.status_code == 400

    def test_secrets_are_never_returned(self, client: TestClient, fake_keyring) -> None:
        client.put("/api/settings/secrets", json={"key": "anthropic", "value": "sk-secret-value"})

        body = client.get("/api/settings").json()

        assert "sk-secret-value" not in str(body)
        assert body["keys_present"]["anthropic"] is True

    def test_empty_secret_is_rejected(self, client: TestClient, fake_keyring) -> None:
        response = client.put("/api/settings/secrets", json={"key": "anthropic", "value": "   "})

        assert response.status_code == 400

    def test_unknown_secret_key_is_rejected(self, client: TestClient) -> None:
        response = client.put("/api/settings/secrets", json={"key": "aws", "value": "x"})

        assert response.status_code == 400

    def test_secret_can_be_deleted(self, client: TestClient, fake_keyring) -> None:
        client.put("/api/settings/secrets", json={"key": "openai", "value": "sk-x"})
        client.delete("/api/settings/secrets/openai")

        assert client.get("/api/settings").json()["keys_present"]["openai"] is False

    def test_server_can_be_quit_gracefully(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from autoclip.api import settings as settings_api

        called: list[bool] = []
        monkeypatch.setattr(settings_api.server_control, "shutdown_available", lambda: True)
        monkeypatch.setattr(
            settings_api.server_control,
            "request_shutdown",
            lambda: called.append(True) or True,
        )

        response = client.post("/api/system/shutdown")

        assert response.status_code == 202
        assert response.json() == {"status": "stopping"}
        assert called == [True]

    def test_server_quit_is_blocked_while_a_job_is_queued(
        self, client: TestClient, source: Source, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from autoclip.api import settings as settings_api

        monkeypatch.setattr(settings_api.server_control, "shutdown_available", lambda: True)
        store.create_job(Job(id=new_id(), source_id=source.id, status="queued"))

        response = client.post("/api/system/shutdown")

        assert response.status_code == 409
        assert "queued/running" in response.json()["detail"]

    def test_desktop_shortcut_can_be_created_and_removed(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from autoclip import desktop
        from autoclip.api import settings as settings_api

        shortcut = tmp_path / "Desktop" / "AutoClip.lnk"
        state = {"exists": False}

        def status():
            return desktop.DesktopShortcutStatus(
                supported=True,
                exists=state["exists"],
                path=shortcut,
            )

        def create():
            state["exists"] = True
            return status()

        def remove():
            state["exists"] = False
            return status()

        monkeypatch.setattr(settings_api.desktop, "shortcut_status", status)
        monkeypatch.setattr(settings_api.desktop, "create_shortcut", create)
        monkeypatch.setattr(settings_api.desktop, "remove_shortcut", remove)

        initial = client.get("/api/system/desktop-shortcut")
        assert initial.status_code == 200
        assert initial.json()["exists"] is False

        created = client.post("/api/system/desktop-shortcut")
        assert created.status_code == 200
        assert created.json()["exists"] is True
        assert created.json()["path"] == str(shortcut)

        removed = client.delete("/api/system/desktop-shortcut")
        assert removed.status_code == 204
        assert client.get("/api/system/desktop-shortcut").json()["exists"] is False

    def test_storage_can_move_to_another_folder(
        self, client: TestClient, tmp_path
    ) -> None:
        from autoclip import paths

        paths.ensure_layout()
        (paths.media_dir() / "media.txt").write_text("media", encoding="utf-8")
        (paths.work_dir() / "work.txt").write_text("work", encoding="utf-8")
        (paths.exports_dir() / "export.txt").write_text("export", encoding="utf-8")
        control_root = paths.root()
        target = tmp_path / "other-drive"

        response = client.put("/api/storage", json={"path": str(target)})

        assert response.status_code == 200
        assert response.json()["path"] == str(target.resolve())
        assert paths.data_root() == target.resolve()
        assert (target / "media" / "media.txt").read_text(encoding="utf-8") == "media"
        assert (target / "work" / "work.txt").read_text(encoding="utf-8") == "work"
        assert (target / "exports" / "export.txt").read_text(encoding="utf-8") == "export"
        assert paths.config_path().parent == control_root
        assert paths.db_path().parent == control_root

    def test_storage_move_stream_reports_progress_and_completion(
        self, client: TestClient, tmp_path
    ) -> None:
        from autoclip import paths

        paths.ensure_layout()
        (paths.media_dir() / "media.txt").write_text("media", encoding="utf-8")
        (paths.work_dir() / "work.txt").write_text("work", encoding="utf-8")
        (paths.exports_dir() / "export.txt").write_text("export", encoding="utf-8")
        target = tmp_path / "streamed-move"

        with client.stream(
            "POST",
            "/api/storage/stream",
            json={"path": str(target)},
        ) as response:
            events = [json.loads(line) for line in response.iter_lines() if line]

        assert response.status_code == 200
        assert any(item["type"] == "status" for item in events)
        progress = [item for item in events if item["type"] == "progress"]
        assert progress
        assert progress[-1]["progress"] == pytest.approx(1.0)
        done = next(item for item in events if item["type"] == "done")
        assert done["storage"]["path"] == str(target.resolve())

    def test_storage_move_is_blocked_while_a_job_is_queued(
        self, client: TestClient, source: Source, tmp_path
    ) -> None:
        job = store.create_job(Job(id=new_id(), source_id=source.id, status="queued"))

        response = client.put(
            "/api/storage",
            json={"path": str(tmp_path / "other-drive")},
        )

        assert response.status_code == 409
        assert store.get_job(job.id) is not None

    def test_storage_browse_returns_selected_folder(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from autoclip.api import settings as settings_api

        selected = tmp_path / "picked"
        monkeypatch.setattr(settings_api, "_choose_directory", lambda initial: selected)

        response = client.post("/api/storage/browse")

        assert response.status_code == 200
        assert response.json()["path"] == str(selected)


class TestSources:
    def test_empty_initially(self, client: TestClient) -> None:
        assert client.get("/api/sources").json() == []

    def test_lists_a_created_source(self, client: TestClient, source: Source) -> None:
        body = client.get("/api/sources").json()

        assert len(body) == 1
        assert body[0]["title"] == "Test source"

    def test_never_exposes_the_filesystem_path(self, client: TestClient, source: Source) -> None:
        body = client.get(f"/api/sources/{source.id}").json()

        assert "path" not in body
        assert "C:/media" not in str(body)

    def test_missing_source_is_404(self, client: TestClient) -> None:
        assert client.get("/api/sources/nope").status_code == 404

    def test_non_youtube_url_is_rejected(self, client: TestClient) -> None:
        response = client.post("/api/sources/youtube", json={"url": "https://vimeo.com/12345"})

        assert response.status_code == 400

    def test_remote_url_endpoint_accepts_twitch_vod_shape(self, client: TestClient) -> None:
        from autoclip.pipeline import ingest

        assert ingest.is_supported_url("https://www.twitch.tv/videos/123456789")
        assert ingest.is_twitch_vod_url("https://www.twitch.tv/videos/123456789?t=1h2m")
        assert not ingest.is_twitch_vod_url("https://www.twitch.tv/somechannel")

    def test_remote_url_endpoint_rejects_unsupported_site(self, client: TestClient) -> None:
        response = client.post("/api/sources/url", json={"url": "https://vimeo.com/12345"})

        assert response.status_code == 400


    def test_remote_ingest_stream_reports_activity_and_result(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        from autoclip.pipeline import ingest

        def fake_ingest(
            url,
            settings,
            *,
            on_progress=None,
            on_status=None,
            on_download_progress=None,
        ):
            assert settings is not None
            if on_status:
                on_status("Connecting to Twitch and selecting media streams")
                on_status("Downloading Twitch media")
            if on_progress:
                on_progress(0.5)
                on_progress(1.0)
            if on_download_progress:
                on_download_progress(
                    ingest.DownloadProgress(
                        progress=0.5,
                        downloaded_bytes=500,
                        total_bytes=1000,
                        speed_bytes_s=250.0,
                        total_is_estimate=True,
                    )
                )
            return Source(
                id=new_id(),
                type="youtube",
                path=str(tmp_path / "source.mp4"),
                title="Streamed Twitch VOD",
                url=url,
                duration_s=60.0,
                width=1920,
                height=1080,
            )

        monkeypatch.setattr(ingest, "ingest_url", fake_ingest)

        with client.stream(
            "POST",
            "/api/sources/url/stream",
            json={"url": "https://www.twitch.tv/videos/123456789"},
        ) as response:
            events = [
                json.loads(line)
                for line in response.iter_lines()
                if line
            ]

        assert response.status_code == 200
        assert any(item["type"] == "status" for item in events)
        assert any(
            item["type"] == "progress" and item["progress"] == pytest.approx(0.5)
            for item in events
        )
        metrics = next(
            item
            for item in events
            if item["type"] == "progress" and item.get("total_bytes") == 1000
        )
        assert metrics["downloaded_bytes"] == 500
        assert metrics["speed_bytes_s"] == pytest.approx(250.0)
        assert metrics["total_is_estimate"] is True
        done = next(item for item in events if item["type"] == "done")
        assert done["source"]["title"] == "Streamed Twitch VOD"

    def test_unsupported_upload_type_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/sources/upload",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )

        assert response.status_code == 415
        assert "supported" in response.json()["detail"]["message"]


class TestJobs:
    def test_create_and_fetch(self, client: TestClient, source: Source) -> None:
        created = client.post("/api/jobs", json={"source_id": source.id})

        assert created.status_code == 201
        job_id = created.json()["id"]

        fetched = client.get(f"/api/jobs/{job_id}").json()
        assert fetched["status"] == "queued"
        assert fetched["source"]["title"] == "Test source"

    def test_overrides_are_applied(self, client: TestClient, source: Source) -> None:
        response = client.post(
            "/api/jobs",
            json={
                "source_id": source.id,
                "settings": {"provider": "ollama", "max_clips": 3},
            },
        )

        assert response.json()["provider"] == "ollama"

    def test_missing_source_is_404(self, client: TestClient) -> None:
        response = client.post("/api/jobs", json={"source_id": "nope"})

        assert response.status_code == 404

    def test_inverted_durations_are_rejected(self, client: TestClient, source: Source) -> None:
        response = client.post(
            "/api/jobs",
            json={
                "source_id": source.id,
                "settings": {"min_duration_s": 90, "max_duration_s": 20},
            },
        )

        assert response.status_code == 400

    def test_cancel_a_queued_job(self, client: TestClient, source: Source) -> None:
        job_id = client.post("/api/jobs", json={"source_id": source.id}).json()["id"]

        response = client.post(f"/api/jobs/{job_id}/cancel")

        assert response.status_code == 200
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "cancelled"

    def test_cancelling_a_finished_job_conflicts(self, client: TestClient, source: Source) -> None:
        job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))

        assert client.post(f"/api/jobs/{job.id}/cancel").status_code == 409

    def test_retry_requeues_a_failed_job(self, client: TestClient, source: Source) -> None:
        job = store.create_job(Job(id=new_id(), source_id=source.id, status="failed", error="boom"))

        response = client.post(f"/api/jobs/{job.id}/retry")

        assert response.status_code == 200
        assert response.json()["status"] == "queued"
        assert response.json()["error"] is None

    def test_retrying_a_running_job_conflicts(self, client: TestClient, source: Source) -> None:
        job = store.create_job(Job(id=new_id(), source_id=source.id, status="running"))

        assert client.post(f"/api/jobs/{job.id}/retry").status_code == 409

    def test_missing_job_is_404(self, client: TestClient) -> None:
        assert client.get("/api/jobs/nope").status_code == 404
        assert client.post("/api/jobs/nope/cancel").status_code == 404
        assert client.post("/api/jobs/nope/retry").status_code == 404
        assert client.delete("/api/jobs/nope").status_code == 404

    def test_delete_finished_job_removes_record_and_artifacts(
        self, client: TestClient, source: Source
    ) -> None:
        from autoclip import paths

        job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))
        work = paths.job_work_dir(job.id)
        exports = paths.exports_dir() / job.id
        media = paths.source_media_dir(source.id)
        for directory in (work, exports, media):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "sentinel.txt").write_text("x", encoding="utf-8")

        response = client.delete(f"/api/jobs/{job.id}")

        assert response.status_code == 204
        assert store.get_job(job.id) is None
        assert store.get_source(source.id) is None
        assert not work.exists()
        assert not exports.exists()
        assert not media.exists()

    def test_delete_job_keeps_source_media_when_another_job_uses_it(
        self, client: TestClient, source: Source
    ) -> None:
        from autoclip import paths

        first = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))
        second = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))
        media = paths.source_media_dir(source.id)
        media.mkdir(parents=True, exist_ok=True)
        (media / "source.mp4").write_text("x", encoding="utf-8")

        response = client.delete(f"/api/jobs/{first.id}")

        assert response.status_code == 204
        assert store.get_job(first.id) is None
        assert store.get_job(second.id) is not None
        assert store.get_source(source.id) is not None
        assert media.exists()

    def test_delete_running_job_is_rejected(self, client: TestClient, source: Source) -> None:
        job = store.create_job(Job(id=new_id(), source_id=source.id, status="running"))

        response = client.delete(f"/api/jobs/{job.id}")

        assert response.status_code == 409
        assert store.get_job(job.id) is not None


class TestClips:
    def test_preview_media_recovers_after_storage_move(
        self, client: TestClient, source: Source
    ) -> None:
        from autoclip import paths

        recovered = paths.source_media_dir(source.id) / "source.mp4"
        recovered.parent.mkdir(parents=True, exist_ok=True)
        recovered.write_bytes(b"preview-bytes")
        job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))

        response = client.get(f"/api/jobs/{job.id}/media")

        assert response.status_code == 200
        assert response.content == b"preview-bytes"
        assert response.headers["cache-control"] == "private, max-age=3600"
        assert store.get_source(source.id).path == str(recovered)

    @pytest.fixture
    def job_with_clips(self, source: Source) -> Job:
        job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))
        store.replace_clips(
            job.id,
            [
                Clip(
                    id=new_id(),
                    job_id=job.id,
                    rank=rank,
                    start_s=rank * 60.0,
                    end_s=rank * 60.0 + 40.0,
                    start_word=rank * 100,
                    end_word=rank * 100 + 80,
                    title=f"Clip {rank}",
                    score=90 - rank,
                )
                for rank in (1, 2, 3)
            ],
        )
        return job

    def test_list_clips_for_a_job(self, client: TestClient, job_with_clips: Job) -> None:
        clips = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()

        assert len(clips) == 3
        assert [c["rank"] for c in clips] == [1, 2, 3]
        assert clips[0]["duration_s"] == pytest.approx(40.0)

    def test_clip_output_hides_legacy_transcript_index_tags(
        self, client: TestClient, source: Source
    ) -> None:
        job = store.create_job(Job(id=new_id(), source_id=source.id, status="done"))
        clip = Clip(
            id=new_id(),
            job_id=job.id,
            rank=1,
            start_s=0.0,
            end_s=30.0,
            title="[2326] Weird game",
            hook="Yeah [2326]That [2327]was [2328]a [2329]weird [2330]game",
            reason="Closing [2330]game lands cleanly.",
        )
        store.replace_clips(job.id, [clip])

        body = client.get(f"/api/jobs/{job.id}/clips").json()[0]

        assert body["title"] == "Weird game"
        assert body["hook"] == "Yeah That was a weird game"
        assert body["reason"] == "Closing game lands cleanly."

    def test_patch_title_and_status(self, client: TestClient, job_with_clips: Job) -> None:
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        response = client.patch(
            f"/api/clips/{clip_id}", json={"title": "Renamed", "status": "kept"}
        )

        assert response.status_code == 200
        assert response.json()["title"] == "Renamed"
        assert response.json()["status"] == "kept"

    def test_inverted_trim_is_rejected(self, client: TestClient, job_with_clips: Job) -> None:
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        response = client.patch(f"/api/clips/{clip_id}", json={"start_s": 90.0, "end_s": 30.0})

        assert response.status_code == 400

    def test_caption_style_is_persisted(self, client: TestClient, job_with_clips: Job) -> None:
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        response = client.patch(
            f"/api/clips/{clip_id}/captions", json={"caption_style": "karaoke_fill"}
        )

        assert response.status_code == 200
        assert response.json()["caption_style"] == "karaoke_fill"

    def test_captions_can_be_disabled_per_clip(
        self, client: TestClient, job_with_clips: Job
    ) -> None:
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        disabled = client.patch(
            f"/api/clips/{clip_id}/captions", json={"burn_captions": False}
        )
        assert disabled.status_code == 200
        assert disabled.json()["burn_captions"] is False

        restyled = client.patch(
            f"/api/clips/{clip_id}/captions", json={"caption_style": "clean_lower"}
        )
        assert restyled.json()["burn_captions"] is False

    def test_unknown_caption_style_is_rejected(
        self, client: TestClient, job_with_clips: Job
    ) -> None:
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        response = client.patch(
            f"/api/clips/{clip_id}/captions", json={"caption_style": "explosion"}
        )

        assert response.status_code == 400

    def test_internal_cuts_are_saved_merged_and_shorten_duration(
        self, client: TestClient, job_with_clips: Job
    ) -> None:
        clip = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]

        response = client.patch(
            f"/api/clips/{clip['id']}/cuts",
            json={
                "cuts": [
                    {
                        "start_s": clip["start_s"] + 5.0,
                        "end_s": clip["start_s"] + 10.0,
                    },
                    {
                        "start_s": clip["start_s"] + 9.0,
                        "end_s": clip["start_s"] + 12.0,
                    },
                ]
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["cuts"] == [
            {"start_s": clip["start_s"] + 5.0, "end_s": clip["start_s"] + 12.0}
        ]
        assert body["duration_s"] == pytest.approx(33.0)

    def test_internal_cut_cannot_replace_end_trim(
        self, client: TestClient, job_with_clips: Job
    ) -> None:
        clip = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]

        response = client.patch(
            f"/api/clips/{clip['id']}/cuts",
            json={
                "cuts": [
                    {
                        "start_s": clip["start_s"],
                        "end_s": clip["start_s"] + 2.0,
                    }
                ]
            },
        )

        assert response.status_code == 400
        assert "trim handles" in response.json()["detail"]

    def test_custom_layout_can_be_saved_and_cleared(
        self, client: TestClient, job_with_clips: Job
    ) -> None:
        clip = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]
        layout = {
            "base_center_x": 0.9,
            "base_center_y": 0.5,
            "overlays": [
                {
                    "id": "vtuber",
                    "label": "VTuber",
                    "source": {"x": 0.72, "y": 0.1, "width": 0.25, "height": 0.7},
                    "destination": {"x": 0.05, "y": 0.04, "width": 0.9, "height": 0.28},
                }
            ],
        }
        response = client.patch(
            f"/api/clips/{clip['id']}/layout", json={"layout": layout}
        )
        assert response.status_code == 200
        assert response.json()["layout"]["base_center_x"] == pytest.approx(0.9)
        assert response.json()["layout"]["overlays"][0]["label"] == "VTuber"

        cleared = client.patch(
            f"/api/clips/{clip['id']}/layout", json={"layout": None}
        )
        assert cleared.status_code == 200
        assert cleared.json()["layout"] is None

    def test_custom_layout_rejects_rectangles_outside_frame(
        self, client: TestClient, job_with_clips: Job
    ) -> None:
        clip = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]
        response = client.patch(
            f"/api/clips/{clip['id']}/layout",
            json={
                "layout": {
                    "base_center_x": 0.5,
                    "base_center_y": 0.5,
                    "overlays": [
                        {
                            "id": "bad",
                            "label": "Bad",
                            "source": {"x": 0.8, "y": 0.0, "width": 0.4, "height": 1.0},
                            "destination": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
                        }
                    ],
                }
            },
        )
        assert response.status_code == 400

    def test_missing_clip_is_404(self, client: TestClient) -> None:
        assert client.get("/api/clips/nope").status_code == 404
        assert client.patch("/api/clips/nope", json={"title": "x"}).status_code == 404
        assert client.get("/api/clips/nope/crop-path").status_code == 404

    def test_crop_path_is_404_before_reframe(self, client: TestClient, job_with_clips: Job) -> None:
        # The review player treats this as "fall back to a centre crop", which
        # is what the renderer does for these clips too.
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        assert client.get(f"/api/clips/{clip_id}/crop-path").status_code == 404

    def test_crop_path_is_served_when_present(
        self, client: TestClient, job_with_clips: Job, autoclip_home
    ) -> None:
        from autoclip.pipeline.reframe.croppath import centre_crop
        from autoclip.pipeline.runner import JobWorkspace

        clip = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]
        path = centre_crop(1920, 1080, clip["duration_s"])
        path.save(JobWorkspace(job_with_clips.id).crop_path(clip["id"]))

        body = client.get(f"/api/clips/{clip['id']}/crop-path").json()

        assert body["source_width"] == 1920
        assert body["source_height"] == 1080
        assert len(body["segments"]) == 1
        # The client needs these to position the video; a missing field means a
        # silently centre-cropped preview that disagrees with the export.
        segment = body["segments"][0]
        assert {"start_s", "end_s", "width", "height", "keyframes", "fit"} <= segment.keys()

    def test_words_need_a_transcript(self, client: TestClient, job_with_clips: Job) -> None:
        # The job has clips but no transcript on disk, which is exactly the
        # state a partially-restored workspace would be in.
        clip_id = client.get(f"/api/jobs/{job_with_clips.id}/clips").json()[0]["id"]

        assert client.get(f"/api/clips/{clip_id}/words").status_code == 404

    def test_empty_caption_edit_hides_all_words(
        self, client: TestClient, job_with_clips: Job
    ) -> None:
        from autoclip.pipeline.runner import JobWorkspace
        from autoclip.pipeline.transcript import Transcript, Word

        clip = store.list_clips(job_with_clips.id)[0]
        store.update_clip(
            clip.id,
            start_s=0.0,
            end_s=2.0,
            start_word=0,
            end_word=1,
        )
        Transcript(
            words=[
                Word(text="hello", start=0.0, end=0.8),
                Word(text="world", start=1.0, end=1.8),
            ]
        ).save(JobWorkspace(job_with_clips.id).transcript)

        before = client.get(f"/api/clips/{clip.id}/words")
        assert [word["text"] for word in before.json()] == ["hello", "world"]

        response = client.patch(f"/api/clips/{clip.id}/captions", json={"words": []})
        assert response.status_code == 200
        assert client.get(f"/api/clips/{clip.id}/words").json() == []

    def test_caption_edit_can_insert_words(
        self, client: TestClient, job_with_clips: Job
    ) -> None:
        from autoclip.pipeline.runner import JobWorkspace
        from autoclip.pipeline.transcript import Transcript, Word

        clip = store.list_clips(job_with_clips.id)[0]
        store.update_clip(
            clip.id,
            start_s=0.0,
            end_s=2.0,
            start_word=0,
            end_word=1,
        )
        Transcript(
            words=[
                Word(text="hello", start=0.0, end=0.7),
                Word(text="world", start=1.1, end=1.8),
            ]
        ).save(JobWorkspace(job_with_clips.id).transcript)

        edited = [
            {"text": "hello", "start": 0.0, "end": 0.7, "speaker": None},
            {"text": "new", "start": 0.75, "end": 0.9, "speaker": None},
            {"text": "word", "start": 0.9, "end": 1.05, "speaker": None},
            {"text": "world", "start": 1.1, "end": 1.8, "speaker": None},
        ]
        response = client.patch(
            f"/api/clips/{clip.id}/captions", json={"words": edited}
        )

        assert response.status_code == 200
        words = client.get(f"/api/clips/{clip.id}/words").json()
        assert [word["text"] for word in words] == ["hello", "new", "word", "world"]
        assert words[1]["start"] == pytest.approx(0.75)
        assert words[2]["end"] == pytest.approx(1.05)


class TestProviderStatus:
    def test_reports_every_provider(self, client: TestClient, fake_keyring) -> None:
        statuses = client.get("/api/providers/status").json()

        assert {s["name"] for s in statuses} == {
            "anthropic",
            "openai",
            "gemini",
            "ollama",
        }

    def test_missing_key_is_reported_as_unavailable(self, client: TestClient, fake_keyring) -> None:
        statuses = {s["name"]: s for s in client.get("/api/providers/status").json()}

        assert statuses["anthropic"]["has_key"] is False
        assert statuses["anthropic"]["available"] is False

    def test_ollama_never_requires_a_key(self, client: TestClient) -> None:
        statuses = {s["name"]: s for s in client.get("/api/providers/status").json()}

        assert statuses["ollama"]["requires_key"] is False


class TestEventStream:
    def test_finished_job_streams_a_snapshot_and_closes(
        self, client: TestClient, source: Source
    ) -> None:
        job = store.create_job(Job(id=new_id(), source_id=source.id, status="done", progress=1.0))

        with client.stream("GET", f"/api/jobs/{job.id}/events") as response:
            assert response.status_code == 200
            body = "".join(response.iter_text())

        # A client opening the stream after completion must still learn the
        # final state rather than hanging on an empty connection.
        assert "snapshot" in body
        assert "done" in body

    def test_missing_job_is_404(self, client: TestClient) -> None:
        assert client.get("/api/jobs/nope/events").status_code == 404


class TestSecretsNeverLeak:
    def test_settings_response_has_no_secret_values(self, client: TestClient, fake_keyring) -> None:
        for key in ("anthropic", "openai", "gemini"):
            client.put("/api/settings/secrets", json={"key": key, "value": f"sk-{key}-DEADBEEF"})

        payload = str(client.get("/api/settings").json())

        assert "DEADBEEF" not in payload

    def test_config_file_has_no_secrets_when_keyring_works(
        self, client: TestClient, fake_keyring, autoclip_home
    ) -> None:
        client.put("/api/settings/secrets", json={"key": "anthropic", "value": "sk-DEADBEEF"})
        client.put("/api/settings", json={"whisper": {"model": "medium"}})

        from autoclip import paths

        assert "DEADBEEF" not in paths.config_path().read_text(encoding="utf-8")
        assert config.get_secret("anthropic") == "sk-DEADBEEF"
