from django.db import transaction
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ModelViewSet

from apps.story.api import IsSuperUser
from core.libs.site_sources import is_nepalikatha_request

from .blocks import resolve_block
from .models import Page, PageBlock, PageRedirect, PageTheme, SiteTheme
from .serializers import (
    AdminPageListSerializer,
    AdminPageThemeSerializer,
    AdminSiteThemeSerializer,
    public_site_theme_payload,
    AdminPageSerializer,
    public_page_payload,
    unique_page_slug,
)


class PageAdminViewSet(ModelViewSet):
    """Page management for the React admin panel."""

    queryset = Page.objects.all()
    permission_classes = [IsSuperUser]
    pagination_class = None
    filter_backends = [SearchFilter]
    search_fields = ["title", "slug"]

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action != "list":
            queryset = queryset.prefetch_related("blocks")
        return queryset

    def get_serializer_class(self):
        return AdminPageListSerializer if self.action == "list" else AdminPageSerializer

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        """A draft copy to start a similar page from."""
        source = self.get_object()
        with transaction.atomic():
            copy = Page.objects.create(
                title=f"{source.title} (copy)"[:160],
                slug=unique_page_slug(f"{source.slug}-copy"),
                template=source.template,
                status=Page.STATUS_DRAFT,
                meta_title=source.meta_title,
                meta_description=source.meta_description,
                og_image=source.og_image,
                noindex=source.noindex,
                theme=source.theme,
            )
            PageBlock.objects.bulk_create(
                PageBlock(page=copy, position=block.position, type=block.type, config=block.config)
                for block in source.blocks.all()
            )
        return Response(AdminPageSerializer(copy).data, status=status.HTTP_201_CREATED)


class PageThemeAdminViewSet(ModelViewSet):
    """Reusable page themes. Deleting one returns its pages to the site look."""

    queryset = PageTheme.objects.all()
    serializer_class = AdminPageThemeSerializer
    permission_classes = [IsSuperUser]
    pagination_class = None

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        copy = PageTheme.objects.get(pk=self.get_object().pk)
        copy.pk = None
        copy.name = f"{copy.name} (copy)"[:80]
        copy.save()
        return Response(self.get_serializer(copy).data, status=status.HTTP_201_CREATED)


class SiteThemeAdminViewSet(ModelViewSet):
    """Site-wide / per-page themes for the admin panel."""

    queryset = SiteTheme.objects.all()
    serializer_class = AdminSiteThemeSerializer
    permission_classes = [IsSuperUser]
    pagination_class = None

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "now": timezone.now()}

    # Only one theme is ever on: saving one as on (from the editor's status
    # field) switches the others off, the same as the list's toggle.
    def perform_create(self, serializer):
        with transaction.atomic():
            theme = serializer.save()
            if theme.mode != SiteTheme.MODE_OFF:
                SiteTheme.objects.make_only_active(theme)

    def perform_update(self, serializer):
        self.perform_create(serializer)

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        """The list's toggle: turn this theme on and every other one off.

        A theme with a schedule that hasn't ended yet goes on for that window;
        otherwise it's simply on — so flipping the toggle always does what it
        looks like it does, never quietly activates an already-ended window.
        """
        theme = self.get_object()
        now = timezone.now()
        with transaction.atomic():
            SiteTheme.objects.make_only_active(theme)
            has_future_window = bool(theme.starts_at and theme.ends_at and theme.ends_at > now)
            theme.mode = SiteTheme.MODE_SCHEDULED if has_future_window else SiteTheme.MODE_ALWAYS
            theme.save(update_fields=["mode", "updated_at"])
        return Response(self.get_serializer(theme).data)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        theme = self.get_object()
        theme.mode = SiteTheme.MODE_OFF
        theme.save(update_fields=["mode", "updated_at"])
        return Response(self.get_serializer(theme).data)

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        """A copy that's switched off, so it can't change the site until an
        editor gives it a time and turns it on."""
        copy = SiteTheme.objects.get(pk=self.get_object().pk)
        copy.pk = None
        copy.name = f"{copy.name} (copy)"[:80]
        copy.mode = SiteTheme.MODE_OFF
        copy.starts_at = None
        copy.ends_at = None
        copy.save()
        return Response(self.get_serializer(copy).data, status=status.HTTP_201_CREATED)


def site_theme_priority(theme):
    """Sort key: scheduled before always-on, chosen pages before the whole
    site, then the most recently started (or edited) first."""
    started = theme.starts_at if theme.mode == SiteTheme.MODE_SCHEDULED else theme.updated_at
    return (
        theme.mode != SiteTheme.MODE_SCHEDULED,
        theme.apply_to != SiteTheme.APPLY_PAGES,
        -started.timestamp(),
    )


@method_decorator(cache_page(60), name="get")
class LiveSiteThemesAPIView(APIView):
    """Every site theme live right now, highest priority first. The frontend
    applies the first one that covers the page being shown. Cached briefly:
    it's read on every full page load, and a scheduled switch can take up to a
    minute to appear."""

    permission_classes = [AllowAny]

    def get(self, request):
        themes = sorted(SiteTheme.objects.live(), key=site_theme_priority)
        return Response([public_site_theme_payload(theme) for theme in themes])


class PublicPageAPIView(APIView):
    """A page as /pages/<slug> renders it.

    Not cached: pages are a handful of queries, and editors expect a save to
    show immediately. `?preview=1` from a superuser also returns drafts and
    scheduled pages (marked noindex). An old slug answers with the address the
    page moved to, which the frontend turns into a 301.
    """

    permission_classes = [AllowAny]

    def get(self, request, slug):
        # Pages belong to the main site only.
        if is_nepalikatha_request(request):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        wants_preview = request.query_params.get("preview", "").lower() in {"1", "true"}
        can_preview = wants_preview and request.user.is_authenticated and request.user.is_superuser
        queryset = Page.objects.all() if can_preview else Page.objects.published()

        page = queryset.select_related("theme").prefetch_related("blocks").filter(slug=slug).first()
        if page:
            blocks = [resolve_block(block, request) for block in page.blocks.all()]
            return Response(public_page_payload(page, blocks, preview=not page.is_live()))

        redirect = (
            PageRedirect.objects.filter(old_slug=slug, page__in=Page.objects.published())
            .select_related("page")
            .first()
        )
        if redirect:
            return Response({"redirect": redirect.page.path})

        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
