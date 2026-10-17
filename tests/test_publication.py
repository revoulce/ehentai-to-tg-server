import pytest

from ehtg.domain.errors import AppError
from ehtg.domain.publication import PublicationOptions, publication_plan, units
from ehtg.domain.selection import PageSelection
from ehtg.domain.snapshot import GallerySnapshot
from ehtg.service import GalleryService

URL = "https://e-hentai.org/g/123/abcdef0123/"


@pytest.mark.parametrize("count", [1, 2, 10, 11, 20, 21])
def test_album_sizes_and_real_page_ranges(count: int) -> None:
    snapshot = GallerySnapshot("Тест", tuple(str(i) for i in range(count)))
    import json

    plan = publication_plan(
        json.loads(snapshot.json), URL, list(range(1, count + 1)), PublicationOptions()
    )
    assert [p for part in plan["parts"] for p in part["pages"]] == list(range(1, count + 1))
    assert all(1 <= len(part["pages"]) <= 10 for part in plan["parts"])
    assert plan["options"]["disable_notification"] is True
    assert plan["options"]["has_spoiler"] is False


def test_caption_is_bounded_without_losing_title_or_source() -> None:
    import json

    metadata = json.loads(
        GallerySnapshot(
            "🙂" * 100,
            ("1", "2", "3"),
            tags=tuple(("misc", "long tag " + str(i)) for i in range(200)),
        ).json
    )
    plan = publication_plan(metadata, URL, [1, 3], PublicationOptions())
    caption = plan["parts"][0]["caption"]
    assert units(caption) <= 1024
    assert URL in caption and "🙂" * 100 in caption and "страницы 1, 3" in caption
    assert plan["hidden_tags"] > 0
    with pytest.raises(AppError, match="не помещаются"):
        publication_plan(metadata, URL, [1], PublicationOptions(title="🙂" * 600))


def test_batch_confirmation_rolls_back_and_replays(service: GalleryService) -> None:
    batch = service.create_batch([URL, URL.replace("/123/", "/124/")])
    a, b = batch["items"]
    for draft in (a, b):
        service.record_analysis(draft["id"], 1, GallerySnapshot("Test", ("one",)))
    with pytest.raises(AppError) as error:
        service.publish_batch(batch["id"], 1, [(a["id"], 2), (b["id"], 1)], "batch-key")
    assert error.value.code == "STALE_REVISION"
    assert service.list_jobs()["items"] == []
    result = service.publish_batch(batch["id"], 1, [(a["id"], 2), (b["id"], 2)], "batch-key")
    assert (
        service.publish_batch(batch["id"], 1, [(a["id"], 2), (b["id"], 2)], "batch-key") == result
    )
    assert len(service.list_jobs()["items"]) == 2
    job = service.get_job(result["items"][0]["job_id"])
    assert job["plan"]["parts"][0]["caption"].startswith("Test")


def test_options_are_frozen_in_confirmed_job(service: GalleryService) -> None:
    draft = service.create_draft(URL, PageSelection())
    service.record_analysis(draft["id"], 1, GallerySnapshot("Test", ("one", "two")))
    edited = service.edit_selection(
        draft["id"],
        2,
        PageSelection(exclude="2"),
        PublicationOptions(title="Новое", has_spoiler=True),
    )
    job = service.get_job(service.publish(draft["id"], edited["revision"], "key")["job_id"])
    assert job["plan"] == edited["preview"]
    assert job["plan"]["options"]["has_spoiler"] is True


def test_prototype_jobs_require_new_caption_confirmation(service: GalleryService) -> None:
    draft = service.create_draft(URL, PageSelection())
    service.record_analysis(draft["id"], 1, GallerySnapshot("Legacy", ("one",)))
    job_id = service.publish(draft["id"], 2, "legacy")["job_id"]
    with service.db.transaction() as connection:
        connection.execute("UPDATE jobs SET plan_json='{}' WHERE id=?", (job_id,))
        connection.execute("DELETE FROM schema_migrations WHERE name='007_legacy_confirmation.sql'")
    service.db.initialize()
    job = service.get_job(job_id)
    assert job["state"] == "needs_review"
    assert job["error_code"] == "PUBLICATION_RECONFIRM_REQUIRED"
    service.db.initialize()
    assert service.get_job(job_id)["revision"] == job["revision"]
