from datetime import timedelta

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.story.models import Genre, Story
from apps.users.models import User

from .models import Page, PageBlock, PageRedirect, PageTheme


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
