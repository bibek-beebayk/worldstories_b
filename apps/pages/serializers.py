from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import serializers

from apps.story.models import Story

from .blocks import TEMPLATE_BLOCKS, clean_block
from .models import Page, PageBlock, PageRedirect, PageTheme, SiteTheme, ThemeLook


class AdminPageThemeSerializer(serializers.ModelSerializer):
    page_count = serializers.SerializerMethodField()

    def get_page_count(self, obj):
        return obj.pages.count()

    def validate(self, attrs):
        # DRF never calls full_clean(); run the custom-CSS rules on the merged result.
        values = {f.name: getattr(self.instance, f.name) for f in PageTheme._meta.concrete_fields} if self.instance else {}
        try:
            PageTheme(**{**values, **attrs}).clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)
        return attrs

    class Meta:
        model = PageTheme
        exclude = ["created_at"]
        read_only_fields = ["updated_at"]


THEME_PUBLIC_FIELDS = [
    f.name for f in PageTheme._meta.concrete_fields if f.name not in {"created_at", "updated_at", "name"}
]


def public_theme_payload(theme):
    if theme is None:
        return None
    return {field: getattr(theme, field) for field in THEME_PUBLIC_FIELDS}


class AdminSiteThemeSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()

    def get_status(self, obj):
        now = self.context.get("now") or timezone.now()
        if obj.mode == SiteTheme.MODE_OFF:
            return "off"
        if obj.mode == SiteTheme.MODE_ALWAYS:
            return "always"
        if obj.starts_at and obj.starts_at > now:
            return "scheduled"
        if obj.ends_at and obj.ends_at <= now:
            return "ended"
        return "live"

    def validate(self, attrs):
        values = {f.name: getattr(self.instance, f.name) for f in SiteTheme._meta.concrete_fields} if self.instance else {}
        try:
            SiteTheme(**{**values, **attrs}).clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.message_dict)
        return attrs

    class Meta:
        model = SiteTheme
        exclude = ["created_at"]
        read_only_fields = ["updated_at"]


LOOK_FIELDS = [
    f.name for f in ThemeLook._meta.get_fields() if f.concrete and f.name not in {"created_at", "updated_at", "name"}
]


def public_site_theme_payload(theme):
    return {
        "id": theme.id,
        **{field: getattr(theme, field) for field in LOOK_FIELDS},
        "apply_to": theme.apply_to,
        "page_paths": theme.page_paths if theme.apply_to == SiteTheme.APPLY_PAGES else [],
    }


def page_status(page, now=None):
    now = now or timezone.now()
    if page.status != Page.STATUS_PUBLISHED:
        return "draft"
    if page.publish_at and page.publish_at > now:
        return "scheduled"
    return "live"


def unique_page_slug(base, exclude_pk=None):
    base = slugify(base)[:150] or "page"
    slug, n = base, 2
    while Page.objects.filter(slug=slug).exclude(pk=exclude_pk).exists():
        slug = f"{base}-{n}"
        n += 1
    return slug


class AdminPageListSerializer(serializers.ModelSerializer):
    status_label = serializers.SerializerMethodField()
    path = serializers.CharField(read_only=True)

    def get_status_label(self, obj):
        return page_status(obj)

    class Meta:
        model = Page
        fields = ["id", "title", "slug", "path", "template", "status", "status_label", "publish_at", "updated_at"]


class AdminPageSerializer(serializers.ModelSerializer):
    """The whole page as the editor works on it.

    `blocks` is written as a complete ordered list on every save, matching how
    the editor works (arrange, then save) — positions come from list order.
    """

    status_label = serializers.SerializerMethodField()
    path = serializers.CharField(read_only=True)
    # Written as a list; read back via to_representation() with editor extras.
    blocks = serializers.ListField(child=serializers.DictField(), required=False, write_only=True)
    slug = serializers.SlugField(max_length=160, required=False, allow_blank=True)

    class Meta:
        model = Page
        fields = [
            "id", "title", "slug", "path", "template", "status", "status_label", "publish_at",
            "meta_title", "meta_description", "og_image", "noindex", "theme",
            "blocks", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_status_label(self, obj):
        return page_status(obj)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        blocks = list(instance.blocks.all())
        # Story-list blocks carry titles too, so the editor can show what was
        # picked without a request per story.
        story_ids = {
            story_id
            for block in blocks
            if block.type == PageBlock.TYPE_STORY_LIST
            for story_id in block.config.get("story_ids", [])
        }
        stories = {
            story.id: {"id": story.id, "title": story.title, "is_published": story.is_published}
            for story in Story.objects.filter(id__in=story_ids).only("id", "title", "is_published")
        }
        data["blocks"] = [
            {
                "id": block.id,
                "type": block.type,
                "config": block.config,
                **(
                    {"stories": [stories[i] for i in block.config.get("story_ids", []) if i in stories]}
                    if block.type == PageBlock.TYPE_STORY_LIST
                    else {}
                ),
            }
            for block in blocks
        ]
        return data

    def validate_slug(self, value):
        if not value:
            return value
        if Page.objects.filter(slug=value).exclude(pk=getattr(self.instance, "pk", None)).exists():
            raise serializers.ValidationError("Another page already uses this address.")
        return value

    def validate_blocks(self, blocks):
        cleaned = []
        labels = dict(PageBlock.TYPE_CHOICES)
        for index, block in enumerate(blocks, start=1):
            block_type = block.get("type")
            try:
                config = clean_block(block_type, block.get("config", {}))
            except DjangoValidationError as exc:
                label = labels.get(block_type, block_type)
                raise serializers.ValidationError(f"Block {index} ({label}): {' '.join(exc.messages)}")
            cleaned.append((block_type, config))
        return cleaned

    def _write_blocks(self, page, blocks):
        page.blocks.all().delete()
        PageBlock.objects.bulk_create(
            PageBlock(page=page, position=position, type=block_type, config=config)
            for position, (block_type, config) in enumerate(blocks, start=1)
        )

    @transaction.atomic
    def create(self, validated_data):
        blocks = validated_data.pop("blocks", None)
        validated_data["slug"] = validated_data.get("slug") or unique_page_slug(validated_data["title"])
        page = Page.objects.create(**validated_data)
        if blocks is None:
            blocks = [
                (block_type, clean_block(block_type, config))
                for block_type, config in TEMPLATE_BLOCKS.get(page.template, [])
            ]
        self._write_blocks(page, blocks)
        return page

    @transaction.atomic
    def update(self, instance, validated_data):
        blocks = validated_data.pop("blocks", None)
        old_slug = instance.slug
        was_live = instance.is_live()
        if "slug" in validated_data and not validated_data["slug"]:
            validated_data["slug"] = unique_page_slug(validated_data.get("title", instance.title), instance.pk)

        page = super().update(instance, validated_data)

        if page.slug != old_slug:
            # A page that people (and search engines) could already reach
            # keeps its old address working as a redirect.
            if was_live:
                PageRedirect.objects.update_or_create(old_slug=old_slug, defaults={"page": page})
            # A slug a page now owns can't also be a redirect.
            PageRedirect.objects.filter(old_slug=page.slug).delete()
        if blocks is not None:
            self._write_blocks(page, blocks)
        return page


def public_page_payload(page, blocks, *, preview):
    return {
        "title": page.title,
        "slug": page.slug,
        "path": page.path,
        "meta_title": page.meta_title,
        "meta_description": page.meta_description,
        "og_image": page.og_image,
        # A preview of an unpublished page must never be indexed.
        "noindex": page.noindex or preview,
        "theme": public_theme_payload(page.theme),
        "is_preview": preview,
        "published_at": (page.publish_at or page.created_at).isoformat(),
        "updated_at": page.updated_at.isoformat(),
        "blocks": blocks,
    }
