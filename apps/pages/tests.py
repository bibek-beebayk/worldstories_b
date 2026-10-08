from datetime import timedelta

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.story.models import Genre, Story
from apps.users.models import User

from .models import Page, PageBlock, PageRedirect, PageTheme, SiteTheme


class PageTestBase(APITestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(
            email="pageadmin@example.com", username="pageadmin",
            password="test-password", is_superuser=True, is_staff=True, is_active=True,
        )
        self.reader = User.objects.create_user(
            email="pagereader@example.com", username="pagereader", password="test-password"
        )

    def _page(self, slug="about-us", status=Page.STATUS_PUBLISHED, **kwargs):
        return Page.objects.create(title=kwargs.pop("title", "About us"), slug=slug, status=status, **kwargs)

    def _public(self, slug, **params):
        return self.client.get(reverse("public-page", kwargs={"slug": slug}), params)


class PublicPageTests(PageTestBase):
    def test_published_page_is_served_with_resolved_story_list_in_editor_order(self):
        first = Story.objects.create(title="First", slug="p-first", is_published=True)
        second = Story.objects.create(title="Second", slug="p-second", is_published=True)
        hidden = Story.objects.create(title="Hidden", slug="p-hidden", is_published=False)
        page = self._page()
        PageBlock.objects.create(page=page, position=1, type="rich_text", config={"html": "<p>Hi</p>"})
        PageBlock.objects.create(
            page=page, position=2, type="story_list",
            config={"heading": "Picks", "story_ids": [second.id, hidden.id, first.id], "layout": "grid"},
        )

        response = self._public("about-us")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["path"], "/pages/about-us")
        self.assertFalse(body["noindex"])
        self.assertEqual([b["type"] for b in body["blocks"]], ["rich_text", "story_list"])
        self.assertEqual([s["slug"] for s in body["blocks"][1]["stories"]], ["p-second", "p-first"])

    def test_story_query_filters_sorts_and_links_to_see_all(self):
        genre = Genre.objects.create(name="Horror", slug="horror")
        old = Story.objects.create(title="Old", slug="q-old", is_published=True, views=500)
        new = Story.objects.create(title="New", slug="q-new", is_published=True, views=10)
        Story.objects.create(title="Other", slug="q-other", is_published=True, views=9999)
        old.genres.add(genre)
        new.genres.add(genre)
        page = self._page()
        PageBlock.objects.create(
            page=page, position=1, type="story_query",
            config={"heading": "", "genre": "horror", "sort": "popular", "limit": 5, "layout": "grid"},
        )

        block = self._public("about-us").json()["blocks"][0]

        self.assertEqual([s["slug"] for s in block["stories"]], ["q-old", "q-new"])
        self.assertEqual(block["see_all_url"], "/genre/horror")

    def test_drafts_and_future_pages_are_not_found(self):
        self._page("draft", status=Page.STATUS_DRAFT)
        self._page("later", publish_at=timezone.now() + timedelta(days=1))
        self.assertEqual(self._public("draft").status_code, 404)
        self.assertEqual(self._public("later").status_code, 404)
        self.assertEqual(self._public("missing").status_code, 404)

    def test_superuser_preview_shows_drafts_marked_noindex(self):
        self._page("draft", status=Page.STATUS_DRAFT)
        self.client.force_authenticate(self.reader)
        self.assertEqual(self._public("draft", preview="1").status_code, 404)
        self.client.force_authenticate(self.admin)
        body = self._public("draft", preview="1").json()
        self.assertTrue(body["is_preview"])
        self.assertTrue(body["noindex"])

    def test_old_slug_of_a_live_page_redirects(self):
        page = self._page("old-name")
        self.client.force_authenticate(self.admin)
        self.client.patch(reverse("admin-page-detail", kwargs={"pk": page.pk}), {"slug": "new-name"}, format="json")
        self.client.force_authenticate(None)
        self.assertEqual(self._public("old-name").json(), {"redirect": "/pages/new-name"})

    def test_renaming_a_draft_leaves_no_redirect(self):
        page = self._page("draft-a", status=Page.STATUS_DRAFT)
        self.client.force_authenticate(self.admin)
        self.client.patch(reverse("admin-page-detail", kwargs={"pk": page.pk}), {"slug": "draft-b"}, format="json")
        self.assertFalse(PageRedirect.objects.exists())

    def test_not_served_to_the_nepali_site(self):
        self._page()
        self.assertEqual(self._public("about-us", show_in_nepali_site="true").status_code, 404)

    def test_sitemap_lists_live_indexable_pages_only(self):
        self._page("listed")
        self._page("hidden-from-search", noindex=True)
        self._page("not-yet", status=Page.STATUS_DRAFT)
        xml = self.client.get(reverse("sitemap")).content.decode()
        self.assertIn("/pages/listed<", xml)
        self.assertNotIn("/pages/hidden-from-search", xml)
        self.assertNotIn("/pages/not-yet", xml)


class AdminPageTests(PageTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)

    def test_requires_superuser(self):
        self.client.force_authenticate(self.reader)
        self.assertEqual(self.client.get(reverse("admin-page-list")).status_code, 403)

    def test_create_seeds_template_blocks_and_slug(self):
        response = self.client.post(
            reverse("admin-page-list"), {"title": "Best Ghost Stories", "template": "book_list"}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["slug"], "best-ghost-stories")
        self.assertEqual(body["status"], "draft")
        self.assertEqual([b["type"] for b in body["blocks"]], ["rich_text", "story_list"])

    def test_duplicate_slugs_are_rejected_and_auto_slugs_made_unique(self):
        self._page("about-us")
        auto = self.client.post(reverse("admin-page-list"), {"title": "About us"}, format="json")
        self.assertEqual(auto.json()["slug"], "about-us-2")
        clash = self.client.post(reverse("admin-page-list"), {"title": "X", "slug": "about-us"}, format="json")
        self.assertEqual(clash.status_code, 400)

    def test_blocks_are_replaced_in_order_and_sanitized(self):
        page = self._page()
        story = Story.objects.create(title="Picked", slug="picked", is_published=True)
        response = self.client.patch(
            reverse("admin-page-detail", kwargs={"pk": page.pk}),
            {
                "blocks": [
                    {"type": "story_list", "config": {"story_ids": [story.id, 999999]}},
                    {"type": "rich_text", "config": {"html": '<p onclick="x()">Hi<script>alert(1)</script></p>'}},
                ]
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        blocks = response.json()["blocks"]
        self.assertEqual(blocks[0]["config"]["story_ids"], [story.id])
        self.assertEqual(blocks[0]["stories"][0]["title"], "Picked")
        self.assertNotIn("script", blocks[1]["config"]["html"])
        self.assertNotIn("onclick", blocks[1]["config"]["html"])

    def test_invalid_blocks_are_rejected_with_a_useful_message(self):
        page = self._page()
        url = reverse("admin-page-detail", kwargs={"pk": page.pk})
        cases = [
            {"type": "cta", "config": {"label": "Go", "url": "javascript:alert(1)"}},
            {"type": "image", "config": {"url": "https://example.com/a.png"}},
            {"type": "story_query", "config": {"genre": "no-such-genre"}},
            {"type": "faq", "config": {"items": []}},
            {"type": "nonsense", "config": {}},
        ]
        for block in cases:
            response = self.client.patch(url, {"blocks": [block]}, format="json")
            self.assertEqual(response.status_code, 400, block)
            self.assertIn("Block 1", str(response.json()["blocks"]))

    def test_duplicate_is_an_unpublished_copy_with_blocks(self):
        page = self._page()
        PageBlock.objects.create(page=page, position=1, type="rich_text", config={"html": "<p>Hi</p>"})
        response = self.client.post(reverse("admin-page-duplicate", kwargs={"pk": page.pk}))
        self.assertEqual(response.status_code, 201)
        copy = Page.objects.get(pk=response.json()["id"])
        self.assertEqual(copy.status, Page.STATUS_DRAFT)
        self.assertEqual(copy.slug, "about-us-copy")
        self.assertEqual(copy.blocks.count(), 1)


class PageThemeTests(PageTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)

    def test_page_payload_carries_its_theme(self):
        theme = PageTheme.objects.create(name="Spooky", background_color="#120a1f", heading_font="Creepster")
        self._page(theme=theme)
        self.client.force_authenticate(None)
        body = self._public("about-us").json()
        self.assertEqual(body["theme"]["background_color"], "#120a1f")
        self.assertEqual(body["theme"]["heading_font"], "Creepster")
        self.assertNotIn("name", body["theme"])

    def test_unthemed_page_has_null_theme(self):
        self._page()
        self.assertIsNone(self._public("about-us").json()["theme"])

    def test_admin_creates_theme_and_assigns_it(self):
        response = self.client.post(
            reverse("admin-page-theme-list"),
            {"name": "Dark", "background_color": "#000000", "custom_css": "h2 { letter-spacing: .1em; }"},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        page = self._page()
        self.client.patch(
            reverse("admin-page-detail", kwargs={"pk": page.pk}), {"theme": response.json()["id"]}, format="json"
        )
        page.refresh_from_db()
        self.assertEqual(page.theme.name, "Dark")

    def test_custom_css_cannot_escape_its_scope_or_inject_markup(self):
        url = reverse("admin-page-theme-list")
        for css in [
            "} body { display: none; } .x {",
            "h2 { color: red; ",
            "</style><script>alert(1)</script>",
            "@import url(https://evil.example/x.css);",
            "a { background: url(javascript:alert(1)); }",
        ]:
            response = self.client.post(url, {"name": "Bad", "custom_css": css}, format="json")
            self.assertEqual(response.status_code, 400, css)
            self.assertIn("custom_css", response.json())

    def test_invalid_colours_and_fonts_are_rejected(self):
        url = reverse("admin-page-theme-list")
        self.assertEqual(self.client.post(url, {"name": "X", "primary_color": "red"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(url, {"name": "X", "heading_font": "Comic Sans"}, format="json").status_code, 400)

    def test_deleting_a_theme_returns_pages_to_the_site_look(self):
        theme = PageTheme.objects.create(name="Temp")
        page = self._page(theme=theme)
        self.client.delete(reverse("admin-page-theme-detail", kwargs={"pk": theme.pk}))
        page.refresh_from_db()
        self.assertIsNone(page.theme)

    def test_duplicate_theme(self):
        theme = PageTheme.objects.create(name="Base", primary_color="#123456")
        response = self.client.post(reverse("admin-page-theme-duplicate", kwargs={"pk": theme.pk}))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["name"], "Base (copy)")
        self.assertEqual(response.json()["primary_color"], "#123456")


class SiteThemeTests(PageTestBase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()

    def _theme(self, name, **kwargs):
        return SiteTheme.objects.create(name=name, **kwargs)

    def _live(self):
        cache.clear()
        return self.client.get(reverse("live-site-themes")).json()

    def test_only_live_themes_are_served(self):
        self._theme("Off")
        self._theme("Always", mode="always")
        self._theme("Now", mode="scheduled", starts_at=self.now - timedelta(hours=1), ends_at=self.now + timedelta(hours=1))
        self._theme("Later", mode="scheduled", starts_at=self.now + timedelta(days=1), ends_at=self.now + timedelta(days=2))
        self._theme("Over", mode="scheduled", starts_at=self.now - timedelta(days=2), ends_at=self.now - timedelta(days=1))
        self.assertEqual({t["id"] for t in self._live()}, set(SiteTheme.objects.filter(name__in=["Always", "Now"]).values_list("id", flat=True)))

    def test_priority_scheduled_then_specific_pages_then_latest(self):
        window = {"mode": "scheduled", "ends_at": self.now + timedelta(days=1)}
        always_site = self._theme("Always site", mode="always")
        sched_site_old = self._theme("Sched site old", starts_at=self.now - timedelta(days=3), **window)
        sched_site_new = self._theme("Sched site new", starts_at=self.now - timedelta(days=1), **window)
        sched_pages = self._theme(
            "Sched pages", starts_at=self.now - timedelta(days=5), apply_to="pages", page_paths=["/library"], **window
        )
        self.assertEqual(
            [t["id"] for t in self._live()],
            [sched_pages.id, sched_site_new.id, sched_site_old.id, always_site.id],
        )

    def test_payload_has_look_and_scope_but_no_admin_fields(self):
        self._theme("Spooky", mode="always", apply_to="pages", page_paths=["/pages/*"], heading_font="Creepster")
        theme = self._live()[0]
        self.assertEqual(theme["page_paths"], ["/pages/*"])
        self.assertEqual(theme["heading_font"], "Creepster")
        for hidden in ("name", "mode", "starts_at", "content_width"):
            self.assertNotIn(hidden, theme)

    def test_admin_requires_superuser(self):
        self.client.force_authenticate(self.reader)
        self.assertEqual(self.client.get(reverse("admin-site-theme-list")).status_code, 403)

    def test_admin_validation(self):
        self.client.force_authenticate(self.admin)
        url = reverse("admin-site-theme-list")
        cases = [
            ({"mode": "scheduled"}, "ends_at"),
            ({"mode": "scheduled", "starts_at": self.now.isoformat(), "ends_at": (self.now - timedelta(hours=1)).isoformat()}, "ends_at"),
            ({"apply_to": "pages", "page_paths": []}, "page_paths"),
            ({"apply_to": "pages", "page_paths": ["library"]}, "page_paths"),
            ({"apply_to": "pages", "page_paths": ["/story/*/x*"]}, "page_paths"),
            ({"apply_to": "pages", "page_paths": ["/admin/content"]}, "page_paths"),
            ({"custom_css": "</style><script>x</script>"}, "custom_css"),
            ({"primary_color": "red"}, "primary_color"),
        ]
        for payload, field in cases:
            response = self.client.post(url, {"name": "Bad", **payload}, format="json")
            self.assertEqual(response.status_code, 400, payload)
            self.assertIn(field, response.json(), payload)

    def test_admin_creates_and_reports_status(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            reverse("admin-site-theme-list"),
            {
                "name": "Christmas",
                "mode": "scheduled",
                "starts_at": (self.now + timedelta(days=1)).isoformat(),
                "ends_at": (self.now + timedelta(days=3)).isoformat(),
                "apply_to": "pages",
                "page_paths": ["/", "/story/*"],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["status"], "scheduled")

    def test_duplicate_is_switched_off(self):
        self.client.force_authenticate(self.admin)
        theme = self._theme("Live", mode="always")
        copy = self.client.post(reverse("admin-site-theme-duplicate", kwargs={"pk": theme.pk})).json()
        self.assertEqual(copy["name"], "Live (copy)")
        self.assertEqual(copy["mode"], "off")
        self.assertEqual(copy["status"], "off")


class SingleActiveSiteThemeTests(PageTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)
        self.now = timezone.now()

    def _url(self, theme, action):
        return reverse(f"admin-site-theme-{action}", kwargs={"pk": theme.pk})

    def test_activating_turns_every_other_theme_off(self):
        on = SiteTheme.objects.create(name="On", mode="always")
        sched = SiteTheme.objects.create(
            name="Sched", mode="scheduled", starts_at=self.now, ends_at=self.now + timedelta(days=1)
        )
        new = SiteTheme.objects.create(name="New")
        response = self.client.post(self._url(new, "activate"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "always")
        on.refresh_from_db()
        sched.refresh_from_db()
        self.assertEqual((on.mode, sched.mode), ("off", "off"))

    def test_activating_keeps_a_window_that_has_not_ended(self):
        theme = SiteTheme.objects.create(
            name="Xmas", starts_at=self.now + timedelta(days=1), ends_at=self.now + timedelta(days=3)
        )
        self.assertEqual(self.client.post(self._url(theme, "activate")).json()["mode"], "scheduled")

    def test_activating_with_an_ended_window_just_turns_it_on(self):
        theme = SiteTheme.objects.create(
            name="Old", starts_at=self.now - timedelta(days=3), ends_at=self.now - timedelta(days=1)
        )
        body = self.client.post(self._url(theme, "activate")).json()
        self.assertEqual((body["mode"], body["status"]), ("always", "always"))

    def test_deactivate(self):
        theme = SiteTheme.objects.create(name="On", mode="always")
        self.assertEqual(self.client.post(self._url(theme, "deactivate")).json()["mode"], "off")

    def test_turning_one_on_from_the_editor_also_turns_others_off(self):
        on = SiteTheme.objects.create(name="On", mode="always")
        other = SiteTheme.objects.create(name="Other")
        self.client.patch(
            reverse("admin-site-theme-detail", kwargs={"pk": other.pk}), {"mode": "always"}, format="json"
        )
        on.refresh_from_db()
        self.assertEqual(on.mode, "off")
        created = self.client.post(reverse("admin-site-theme-list"), {"name": "Born on", "mode": "always"}, format="json")
        self.assertEqual(created.status_code, 201)
        other.refresh_from_db()
        self.assertEqual(other.mode, "off")
        self.assertEqual(SiteTheme.objects.exclude(mode="off").count(), 1)

    def test_saving_an_off_theme_leaves_the_active_one_alone(self):
        on = SiteTheme.objects.create(name="On", mode="always")
        off = SiteTheme.objects.create(name="Off")
        self.client.patch(reverse("admin-site-theme-detail", kwargs={"pk": off.pk}), {"name": "Renamed"}, format="json")
        on.refresh_from_db()
        self.assertEqual(on.mode, "always")


class StorySiteThemeTests(PageTestBase):
    def setUp(self):
        super().setUp()
        self.theme = SiteTheme.objects.create(
            name="Spooky", background_color="#120a1f", apply_to="pages", page_paths=["/pages/*"]
        )
        self.story = Story.objects.create(title="Haunted", slug="haunted", is_published=True)

    def test_story_detail_carries_its_theme_as_whole_page(self):
        self.story.site_theme = self.theme
        self.story.save()
        theme = self.client.get(reverse("story-detail", kwargs={"slug": "haunted"})).json()["site_theme"]
        self.assertEqual(theme["background_color"], "#120a1f")
        # Its page selection elsewhere doesn't limit it on the story's own page.
        self.assertEqual((theme["apply_to"], theme["page_paths"]), ("site", []))
        self.assertNotIn("name", theme)

    def test_story_without_a_theme(self):
        self.assertIsNone(self.client.get(reverse("story-detail", kwargs={"slug": "haunted"})).json()["site_theme"])

    def test_admin_sets_and_clears_it_from_the_story_form(self):
        self.client.force_authenticate(self.admin)
        url = reverse("admin-story-detail", args=[self.story.id])
        response = self.client.patch(url, {"site_theme": str(self.theme.id)}, format="multipart")
        self.assertEqual(response.status_code, 200, response.data)
        self.story.refresh_from_db()
        self.assertEqual(self.story.site_theme, self.theme)
        response = self.client.patch(url, {"site_theme": ""}, format="multipart")
        self.assertEqual(response.status_code, 200, response.data)
        self.story.refresh_from_db()
        self.assertIsNone(self.story.site_theme)

    def test_deleting_the_theme_clears_it_from_stories(self):
        self.story.site_theme = self.theme
        self.story.save()
        self.theme.delete()
        self.story.refresh_from_db()
        self.assertIsNone(self.story.site_theme)


class SiteSettingsTests(PageTestBase):
    def test_publisher_info_is_hidden_by_default(self):
        self.assertEqual(self.client.get(reverse("site-settings")).json(), {"show_publisher_info": False})

    def test_admin_toggles_it_and_the_public_endpoint_follows(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch(reverse("admin-site-settings"), {"show_publisher_info": True}, format="json")
        self.assertEqual(response.json(), {"show_publisher_info": True})
        cache.clear()
        self.client.force_authenticate(None)
        self.assertTrue(self.client.get(reverse("site-settings")).json()["show_publisher_info"])

    def test_admin_endpoint_requires_superuser(self):
        self.client.force_authenticate(self.reader)
        self.assertEqual(self.client.get(reverse("admin-site-settings")).status_code, 403)
        self.assertEqual(
            self.client.patch(reverse("admin-site-settings"), {"show_publisher_info": True}, format="json").status_code,
            403,
        )
