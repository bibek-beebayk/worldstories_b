from io import StringIO

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from apps.story.models import Audio, Audiobook, AudiobookSeries, AudiobookSeriesItem, Author

User = get_user_model()


class AudiobookSeriesItemModelTests(TestCase):
    def setUp(self):
        self.author = Author.objects.create(name="H.G. Wells")
        self.series_a = AudiobookSeries.objects.create(name="Sci-Fi Classics", slug="sci-fi-classics")
        self.series_b = AudiobookSeries.objects.create(name="Short Gems", slug="short-gems")
        self.ab = Audiobook.objects.create(
            title="The Time Machine",
            slug="the-time-machine",
            author=self.author,
            language="en",
            about="A journey through time.",
        )

    def test_unique_series_audiobook_pair(self):
        AudiobookSeriesItem.objects.create(series=self.series_a, audiobook=self.ab, order=1)
        AudiobookSeriesItem.objects.create(series=self.series_b, audiobook=self.ab, order=2)
        with self.assertRaises(IntegrityError):
            AudiobookSeriesItem.objects.create(series=self.series_a, audiobook=self.ab, order=5)

    def test_backfill_series_items_noop_and_audit(self):
        out_dry = StringIO()
        call_command("backfill_series_items", "--dry-run", stdout=out_dry)
        self.assertIn("nothing to do", out_dry.getvalue())

        out_real = StringIO()
        call_command("backfill_series_items", stdout=out_real)
        self.assertIn("nothing to do", out_real.getvalue())

        out_audit = StringIO()
        call_command("audit_audiobooks", stdout=out_audit)
        self.assertIn("Audit PASSED", out_audit.getvalue())


class AudiobookSeriesM2MApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_superuser(
            email="admin-s@example.com", username="admin_s", password="password123"
        )
        self.reader = User.objects.create_user(
            email="reader-s@example.com", username="reader_s", password="password123"
        )
        self.author = Author.objects.create(name="Jules Verne")
        self.s1 = AudiobookSeries.objects.create(name="Voyages", slug="voyages")
        self.s2 = AudiobookSeries.objects.create(name="Staff Picks", slug="staff-picks")
        self.s_empty = AudiobookSeries.objects.create(name="Empty Shelf", slug="empty-shelf")

        self.ab1 = Audiobook.objects.create(
            title="Twenty Thousand Leagues",
            slug="20000-leagues",
            author=self.author,
            language="en",
            about="Under the sea.",
            is_published=True,
        )
        self.ab2 = Audiobook.objects.create(
            title="Around the World",
            slug="around-the-world",
            author=self.author,
            language="en",
            about="In eighty days.",
            is_published=True,
        )
        self.ab3 = Audiobook.objects.create(
            title="Journey to the Center",
            slug="journey-center",
            author=self.author,
            language="en",
            about="Underground.",
            is_published=True,
        )
        for idx, ab in enumerate((self.ab1, self.ab2, self.ab3), start=1):
            Audio.objects.create(
                audiobook=ab,
                title=f"Track {idx}",
                slug=f"track-{idx}",
                audio_file=f"story_audios/t{idx}.mp3",
                order=1,
                duration_seconds=300.0,
            )

    def test_non_admin_forbidden(self):
        self.client.force_authenticate(user=self.reader)
        res1 = self.client.post(
            f"/api/admin/audiobook-series/{self.s1.id}/add-audiobooks/",
            {"audiobook_ids": [self.ab1.id]},
            format="json",
        )
        self.assertEqual(res1.status_code, 403)
        res2 = self.client.post(
            "/api/admin/audiobooks/bulk-series/",
            {"ids": [self.ab1.id], "series_ids": [self.s1.id], "mode": "add"},
            format="json",
        )
        self.assertEqual(res2.status_code, 403)

    def test_independent_orders_add_remove_reorder_and_delete_series(self):
        self.client.force_authenticate(user=self.admin)

        # Add ab1, ab2 to s1
        res_add1 = self.client.post(
            f"/api/admin/audiobook-series/{self.s1.id}/add-audiobooks/",
            {"audiobook_ids": [self.ab1.id, self.ab2.id]},
            format="json",
        )
        self.assertEqual(res_add1.status_code, 200)
        self.assertEqual(res_add1.data, {"added": 2, "skipped": 0})

        # Add ab2, ab1 to s2 (opposite order, independent of s1)
        res_add2 = self.client.post(
            f"/api/admin/audiobook-series/{self.s2.id}/add-audiobooks/",
            {"audiobook_ids": [self.ab2.id, self.ab1.id]},
            format="json",
        )
        self.assertEqual(res_add2.status_code, 200)
        self.assertEqual(res_add2.data, {"added": 2, "skipped": 0})

        # Re-adding ab1 to s1 skips it and leaves s2 untouched
        res_dup = self.client.post(
            f"/api/admin/audiobook-series/{self.s1.id}/add-audiobooks/",
            {"audiobook_ids": [self.ab1.id, self.ab3.id]},
            format="json",
        )
        self.assertEqual(res_dup.data, {"added": 1, "skipped": 1})

        # Verify independent orders
        self.assertEqual(
            AudiobookSeriesItem.objects.get(series=self.s1, audiobook=self.ab1).order, 1
        )
        self.assertEqual(
            AudiobookSeriesItem.objects.get(series=self.s2, audiobook=self.ab1).order, 2
        )

        # Reorder rejects foreign/missing IDs
        res_bad_reorder = self.client.post(
            f"/api/admin/audiobook-series/{self.s2.id}/reorder/",
            {"ids": [self.ab2.id, self.ab3.id]},
            format="json",
        )
        self.assertEqual(res_bad_reorder.status_code, 400)

        # Valid reorder on s1: [ab3, ab2, ab1]
        res_reorder = self.client.post(
            f"/api/admin/audiobook-series/{self.s1.id}/reorder/",
            {"ids": [self.ab3.id, self.ab2.id, self.ab1.id]},
            format="json",
        )
        self.assertEqual(res_reorder.status_code, 200)
        self.assertEqual(
            [a["id"] for a in res_reorder.data["audiobooks"]],
            [self.ab3.id, self.ab2.id, self.ab1.id],
        )

        # Remove ab2 from s1 renumbers s1 (1..2) and leaves s2 untouched
        res_rem = self.client.post(
            f"/api/admin/audiobook-series/{self.s1.id}/remove-audiobooks/",
            {"audiobook_ids": [self.ab2.id]},
            format="json",
        )
        self.assertEqual(res_rem.status_code, 200)
        self.assertEqual(res_rem.data, {"removed": 1})
        s1_orders = list(
            AudiobookSeriesItem.objects.filter(series=self.s1)
            .order_by("order")
            .values_list("audiobook_id", "order")
        )
        self.assertEqual(s1_orders, [(self.ab3.id, 1), (self.ab1.id, 2)])
        self.assertTrue(
            AudiobookSeriesItem.objects.filter(series=self.s2, audiobook=self.ab2).exists()
        )

        # Delete s2 deletes only s2 items, keeps audiobooks & s1 items
        res_del = self.client.delete(f"/api/admin/audiobook-series/{self.s2.id}/")
        self.assertEqual(res_del.status_code, 204)
        self.assertTrue(Audiobook.objects.filter(pk=self.ab1.pk).exists())
        self.assertEqual(AudiobookSeriesItem.objects.filter(series=self.s1).count(), 2)

    def test_bulk_series_limits_patch_series_items_filters_and_query_count(self):
        self.client.force_authenticate(user=self.admin)

        # Limits enforced
        res_too_many_ab = self.client.post(
            "/api/admin/audiobooks/bulk-series/",
            {"ids": list(range(1, 202)), "series_ids": [self.s1.id], "mode": "add"},
            format="json",
        )
        self.assertEqual(res_too_many_ab.status_code, 400)
        res_too_many_ser = self.client.post(
            "/api/admin/audiobooks/bulk-series/",
            {"ids": [self.ab1.id], "series_ids": list(range(1, 22)), "mode": "add"},
            format="json",
        )
        self.assertEqual(res_too_many_ser.status_code, 400)

        # Bulk add across 2 series
        res_bulk_add = self.client.post(
            "/api/admin/audiobooks/bulk-series/",
            {"ids": [self.ab1.id, self.ab2.id], "series_ids": [self.s1.id, self.s2.id], "mode": "add"},
            format="json",
        )
        self.assertEqual(res_bulk_add.status_code, 200)
        self.assertEqual(res_bulk_add.data, {"added": 4, "removed": 0, "skipped": 0})

        # Filters: series=<id>, series_filter=none, exclude_series=<id>
        r_mem = self.client.get(f"/api/admin/audiobooks/?series={self.s1.id}")
        self.assertEqual({r["id"] for r in r_mem.data["results"]}, {self.ab1.id, self.ab2.id})

        r_none = self.client.get("/api/admin/audiobooks/?series_filter=none")
        self.assertEqual([r["id"] for r in r_none.data["results"]], [self.ab3.id])

        r_excl = self.client.get(f"/api/admin/audiobooks/?exclude_series={self.s1.id}")
        self.assertEqual([r["id"] for r in r_excl.data["results"]], [self.ab3.id])

        # Bulk remove from s1
        res_bulk_rem = self.client.post(
            "/api/admin/audiobooks/bulk-series/",
            {"ids": [self.ab1.id], "series_ids": [self.s1.id], "mode": "remove"},
            format="json",
        )
        self.assertEqual(res_bulk_rem.status_code, 200)
        self.assertEqual(res_bulk_rem.data, {"added": 0, "removed": 1, "skipped": 0})
        # ab2 renumbered to 1 in s1
        self.assertEqual(
            AudiobookSeriesItem.objects.get(series=self.s1, audiobook=self.ab2).order, 1
        )

        # PATCH series_items replaces memberships
        res_patch = self.client.patch(
            f"/api/admin/audiobooks/{self.ab3.id}/",
            {"series_items": [{"series_id": self.s1.id, "order": None}, {"series_id": self.s2.id, "order": 10}]},
            format="json",
        )
        self.assertEqual(res_patch.status_code, 200)
        self.assertEqual(res_patch.data["series_count"], 2)
        orders_by_id = {s["id"]: s["order"] for s in res_patch.data["series"]}
        self.assertEqual(orders_by_id[self.s1.id], 2)
        self.assertEqual(orders_by_id[self.s2.id], 10)

        # Query count on admin list stays bounded (no N+1)
        with CaptureQueriesContext(connection) as ctx1:
            r_list1 = self.client.get("/api/admin/audiobooks/")
        self.assertEqual(r_list1.status_code, 200)
        q1 = len(ctx1)

        for i in range(4):
            extra = Audiobook.objects.create(
                title=f"Extra {i}", slug=f"extra-s-{i}", author=self.author, language="en", about="x"
            )
            AudiobookSeriesItem.objects.create(series=self.s1, audiobook=extra, order=10 + i)

        with CaptureQueriesContext(connection) as ctx2:
            r_list2 = self.client.get("/api/admin/audiobooks/")
        self.assertEqual(r_list2.status_code, 200)
        self.assertEqual(len(ctx2), q1)

    def test_public_series_list_overview_and_detail_series_context(self):
        AudiobookSeriesItem.objects.create(series=self.s1, audiobook=self.ab1, order=1)
        AudiobookSeriesItem.objects.create(series=self.s1, audiobook=self.ab2, order=2)
        AudiobookSeriesItem.objects.create(series=self.s2, audiobook=self.ab1, order=1)
        AudiobookSeriesItem.objects.create(series=self.s2, audiobook=self.ab3, order=2)

        # Public list returns series as a list and filters by series slug
        res_list = self.client.get(f"/api/audiobooks/?series={self.s1.slug}")
        self.assertEqual(res_list.status_code, 200)
        self.assertEqual({r["slug"] for r in res_list.data["results"]}, {self.ab1.slug, self.ab2.slug})
        ab1_row = next(r for r in res_list.data["results"] if r["slug"] == self.ab1.slug)
        self.assertEqual(len(ab1_row["series"]), 2)

        # Overview hides empty series and puts ab1 in both shelves
        res_ov = self.client.get("/api/audiobooks/overview/")
        self.assertEqual(res_ov.status_code, 200)
        shelf_slugs = [s["series"]["slug"] for s in res_ov.data["shelves"]]
        self.assertNotIn(self.s_empty.slug, shelf_slugs)
        self.assertEqual(set(shelf_slugs), {self.s1.slug, self.s2.slug})
        for shelf in res_ov.data["shelves"]:
            self.assertIn(self.ab1.slug, [a["slug"] for a in shelf["audiobooks"]])

        # Detail includes series_context with one entry per series containing ab1
        res_det = self.client.get(f"/api/audiobooks/{self.ab1.slug}/")
        self.assertEqual(res_det.status_code, 200)
        ctx = res_det.data["series_context"]
        self.assertEqual(len(ctx), 2)
        by_slug = {c["slug"]: [a["slug"] for a in c["audiobooks"]] for c in ctx}
        self.assertEqual(by_slug[self.s1.slug], [self.ab2.slug])
        self.assertEqual(by_slug[self.s2.slug], [self.ab3.slug])
