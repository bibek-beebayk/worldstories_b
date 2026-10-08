import re

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from solo.models import SingletonModel

from core.libs.models import TimeStampModel


class SiteSettings(SingletonModel):
    """Site-wide switches edited from the admin panel's Customize → Site
    Settings page. One row (django-solo)."""

    # On: About, Contact and the footer name the publisher (person, city,
    # personal email). Off: only "WorldStories" and the site's own address.
    # The details themselves live in the frontend (src/lib/siteIdentity.ts).
    show_publisher_info = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Site settings"

    def __str__(self):
        return "Site settings"


class PageQuerySet(models.QuerySet):
    def published(self, now=None):
        """Publicly visible: published, and past its publish date if one is set."""
        now = now or timezone.now()
        return self.filter(
            Q(status=Page.STATUS_PUBLISHED) & (Q(publish_at__isnull=True) | Q(publish_at__lte=now))
        )


hex_color = RegexValidator(r"^#[0-9a-fA-F]{6}$", "Use a 6-digit hex colour such as #ed405a.")


def color_field(default):
    return models.CharField(max_length=7, default=default, validators=[hex_color])


# Google Fonts a theme may load, keyed by the CSS family name. "" means the
# site's own font. Keep in step with PAGE_FONTS in the frontend's
# src/components/pages/pageTheme.ts, which builds the stylesheet URL.
PAGE_FONT_NAMES = [
    "Inter", "Roboto", "Open Sans", "Lato", "Montserrat", "Poppins", "Nunito",
    "Space Grotesk", "DM Sans", "Raleway", "Merriweather", "Lora",
    "Playfair Display", "Libre Baskerville", "Crimson Pro", "EB Garamond",
    "Cormorant Garamond", "Cinzel", "Bebas Neue", "Oswald", "Abril Fatface",
    "Dancing Script", "Pacifico", "Caveat", "Creepster", "Nosifer",
    "Mountains of Christmas", "Special Elite", "Press Start 2P",
]
PAGE_FONT_CHOICES = [("", "Site default")] + [(name, name) for name in PAGE_FONT_NAMES]

MAX_CUSTOM_CSS = 20_000
# Custom CSS is written by superusers, so this isn't about trusting them —
# it keeps the stylesheet unable to close its own <style> tag (and so inject
# markup) and unable to pull in other stylesheets wholesale.
FORBIDDEN_CSS = [
    (re.compile(r"<"), "“<” isn't allowed in custom CSS."),
    (re.compile(r"@import", re.IGNORECASE), "@import isn't allowed — pick fonts from the list instead."),
    (re.compile(r"expression\s*\(|javascript:", re.IGNORECASE), "Scripted CSS isn't allowed."),
]


class ThemeLook(TimeStampModel):
    """The look shared by page themes and site themes: colours, fonts,
    corner shape, a background image and custom CSS."""

    name = models.CharField(max_length=80)

    # Colours
    background_color = color_field("#ffffff")
    surface_color = color_field("#ffffff")
    text_color = color_field("#1d2027")
    muted_text_color = color_field("#6b7280")
    heading_color = color_field("#1d2027")
    primary_color = color_field("#ed405a")
    primary_text_color = color_field("#ffffff")
    border_color = color_field("#e4e4e7")

    # Typography
    heading_font = models.CharField(max_length=40, choices=PAGE_FONT_CHOICES, blank=True)
    body_font = models.CharField(max_length=40, choices=PAGE_FONT_CHOICES, blank=True)

    radius = models.PositiveSmallIntegerField(default=12, validators=[MaxValueValidator(40)])

    # Background
    background_image = models.URLField(blank=True)
    background_overlay_color = color_field("#000000")
    background_overlay_opacity = models.PositiveSmallIntegerField(
        default=0, validators=[MaxValueValidator(95)]
    )

    custom_css = models.TextField(blank=True, max_length=MAX_CUSTOM_CSS)

    class Meta:
        abstract = True
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        for pattern, message in FORBIDDEN_CSS:
            if pattern.search(self.custom_css or ""):
                raise ValidationError({"custom_css": message})
        # Page themes nest the CSS inside the page's selector, so a stray "}"
        # would close that wrapper and let later rules style the rest of the site.
        depth = 0
        for char in self.custom_css or "":
            depth += {"{": 1, "}": -1}.get(char, 0)
            if depth < 0:
                break
        if depth != 0:
            raise ValidationError({"custom_css": "Unbalanced { } in custom CSS."})


class PageTheme(ThemeLook):
    """A reusable look for admin-built pages. Adds the page-only settings —
    body text size, heading style, width and spacing — to ThemeLook. Its
    custom CSS is scoped to the page. Pages without a theme use the site's."""

    WIDTH_NARROW = "narrow"
    WIDTH_NORMAL = "normal"
    WIDTH_WIDE = "wide"
    WIDTH_FULL = "full"
    WIDTH_CHOICES = [
        (WIDTH_NARROW, "Narrow (reading width)"),
        (WIDTH_NORMAL, "Normal"),
        (WIDTH_WIDE, "Wide"),
        (WIDTH_FULL, "Full width"),
    ]
    SPACING_CHOICES = [("compact", "Compact"), ("normal", "Normal"), ("relaxed", "Relaxed")]

    body_font_size = models.PositiveSmallIntegerField(
        default=18, validators=[MinValueValidator(14), MaxValueValidator(24)]
    )
    heading_weight = models.PositiveSmallIntegerField(
        default=700, validators=[MinValueValidator(300), MaxValueValidator(900)]
    )
    heading_uppercase = models.BooleanField(default=False)
    content_width = models.CharField(max_length=10, choices=WIDTH_CHOICES, default=WIDTH_NORMAL)
    section_spacing = models.CharField(max_length=10, choices=SPACING_CHOICES, default="normal")

    class Meta(ThemeLook.Meta):
        pass


# A path a site theme applies to: an exact path ("/library") or a prefix
# ending in "*" ("/story/*" — every story page). The admin panel is never themed.
SITE_THEME_PATH = re.compile(r"^/[A-Za-z0-9\-._~/]*\*?$")
MAX_SITE_THEME_PATHS = 50


class SiteThemeQuerySet(models.QuerySet):
    def make_only_active(self, theme):
        """Switch off every theme but `theme` — only one site theme is ever
        on. Locks the rows first so two editors activating different themes
        at once can't both win."""
        list(self.select_for_update().values_list("pk", flat=True))
        self.exclude(pk=theme.pk).exclude(mode=SiteTheme.MODE_OFF).update(
            mode=SiteTheme.MODE_OFF, updated_at=timezone.now()
        )

    def live(self, now=None):
        now = now or timezone.now()
        return self.filter(
            Q(mode=SiteTheme.MODE_ALWAYS)
            | Q(mode=SiteTheme.MODE_SCHEDULED, starts_at__lte=now, ends_at__gt=now)
        )


class SiteTheme(ThemeLook):
    """A look for the whole public site, or for chosen pages of it — always
    on, or for a scheduled window (Halloween week, a launch).

    Only one is ever active (mode other than "off"): turning one on switches
    the rest off (SiteThemeQuerySet.make_only_active, called on every save
    path in the admin API).
    """

    MODE_OFF = "off"
    MODE_ALWAYS = "always"
    MODE_SCHEDULED = "scheduled"
    MODE_CHOICES = [(MODE_OFF, "Off"), (MODE_ALWAYS, "Always on"), (MODE_SCHEDULED, "Scheduled")]

    APPLY_SITE = "site"
    APPLY_PAGES = "pages"
    APPLY_CHOICES = [(APPLY_SITE, "Whole site"), (APPLY_PAGES, "Selected pages")]

    mode = models.CharField(max_length=10, choices=MODE_CHOICES, default=MODE_OFF, db_index=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    apply_to = models.CharField(max_length=10, choices=APPLY_CHOICES, default=APPLY_SITE)
    page_paths = models.JSONField(default=list, blank=True)

    objects = SiteThemeQuerySet.as_manager()

    class Meta(ThemeLook.Meta):
        pass

    def clean(self):
        super().clean()
        errors = {}
        if self.mode == self.MODE_SCHEDULED:
            if not (self.starts_at and self.ends_at):
                errors["ends_at"] = "A scheduled theme needs a start and an end."
            elif self.ends_at <= self.starts_at:
                errors["ends_at"] = "The end must be after the start."

        if self.apply_to == self.APPLY_PAGES:
            paths = self.page_paths
            if not isinstance(paths, list) or not paths:
                errors["page_paths"] = "Choose at least one page."
            elif len(paths) > MAX_SITE_THEME_PATHS:
                errors["page_paths"] = f"Use at most {MAX_SITE_THEME_PATHS} paths."
            else:
                for path in paths:
                    if not isinstance(path, str) or not SITE_THEME_PATH.match(path) or "//" in path:
                        errors["page_paths"] = (
                            f'"{path}" isn\'t a valid path. Use one like /library, or /story/* for every story page.'
                        )
                        break
                    if path == "/admin" or path.startswith("/admin/"):
                        errors["page_paths"] = "The admin panel can't be themed."
                        break
        if errors:
            raise ValidationError(errors)


class Page(TimeStampModel):
    """An admin-built page served at /pages/<slug> on the main site.

    Its body is an ordered list of PageBlocks; the template only decides which
    blocks a new page starts with (see blocks.TEMPLATE_BLOCKS), so every page
    can grow into anything the block types allow.
    """

    TEMPLATE_STATIC = "static"
    TEMPLATE_BOOK_LIST = "book_list"
    TEMPLATE_BLANK = "blank"
    TEMPLATE_CHOICES = [
        (TEMPLATE_STATIC, "Static page"),
        (TEMPLATE_BOOK_LIST, "Book list"),
        (TEMPLATE_BLANK, "Blank"),
    ]

    STATUS_DRAFT = "draft"
    STATUS_PUBLISHED = "published"
    STATUS_CHOICES = [(STATUS_DRAFT, "Draft"), (STATUS_PUBLISHED, "Published")]

    title = models.CharField(max_length=160)
    slug = models.SlugField(max_length=160, unique=True)
    template = models.CharField(max_length=20, choices=TEMPLATE_CHOICES, default=TEMPLATE_STATIC)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)
    # Optional: a published page with a future date stays hidden until then.
    publish_at = models.DateTimeField(null=True, blank=True)

    # Blank meta fields fall back to the title / an excerpt of the content.
    meta_title = models.CharField(max_length=70, blank=True)
    meta_description = models.CharField(max_length=170, blank=True)
    og_image = models.URLField(blank=True)
    noindex = models.BooleanField(default=False)

    # None = the site's own look.
    theme = models.ForeignKey(PageTheme, null=True, blank=True, on_delete=models.SET_NULL, related_name="pages")

    objects = PageQuerySet.as_manager()

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title

    @property
    def path(self):
        return f"/pages/{self.slug}"

    def is_live(self, now=None):
        now = now or timezone.now()
        return self.status == self.STATUS_PUBLISHED and (self.publish_at is None or self.publish_at <= now)


class PageBlock(models.Model):
    TYPE_RICH_TEXT = "rich_text"
    TYPE_STORY_LIST = "story_list"
    TYPE_STORY_QUERY = "story_query"
    TYPE_BANNER = "banner"
    TYPE_IMAGE = "image"
    TYPE_FAQ = "faq"
    TYPE_CTA = "cta"
    TYPE_CHOICES = [
        (TYPE_RICH_TEXT, "Rich text"),
        (TYPE_STORY_LIST, "Hand-picked stories"),
        (TYPE_STORY_QUERY, "Automatic story list"),
        (TYPE_BANNER, "Banner"),
        (TYPE_IMAGE, "Image"),
        (TYPE_FAQ, "FAQ"),
        (TYPE_CTA, "Call to action"),
    ]

    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="blocks")
    position = models.PositiveIntegerField(default=0)
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    # Shape depends on `type`; always written through blocks.clean_block(),
    # never stored unvalidated.
    config = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.page.title} #{self.position} ({self.type})"


class PageRedirect(models.Model):
    """An old slug of a page that was live under it, so links and search
    rankings follow the page with a 301 when its address changes."""

    old_slug = models.SlugField(max_length=160, unique=True)
    page = models.ForeignKey(Page, on_delete=models.CASCADE, related_name="redirects")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"/pages/{self.old_slug} -> {self.page.path}"
