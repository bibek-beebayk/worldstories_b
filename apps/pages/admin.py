from django.contrib import admin

from .models import Page, PageBlock, PageRedirect, PageTheme


class PageBlockInline(admin.TabularInline):
    model = PageBlock
    extra = 0
    fields = ("position", "type", "config")
    ordering = ("position",)


@admin.register(Page)
class PageAdmin(admin.ModelAdmin):
    """Fallback only — pages are built in the React admin panel (/admin/pages),
    which validates each block's content."""

    list_display = ("title", "slug", "status", "publish_at", "updated_at")
    list_filter = ("status", "template")
    search_fields = ("title", "slug")
    inlines = [PageBlockInline]


@admin.register(PageRedirect)
class PageRedirectAdmin(admin.ModelAdmin):
    list_display = ("old_slug", "page", "created_at")
    search_fields = ("old_slug", "page__title")


@admin.register(PageTheme)
class PageThemeAdmin(admin.ModelAdmin):
    list_display = ("name", "content_width", "updated_at")
    search_fields = ("name",)
