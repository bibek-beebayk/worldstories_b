from django.db import transaction
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
from .models import Page, PageBlock, PageRedirect, PageTheme
from .serializers import (
    AdminPageListSerializer,
    AdminPageThemeSerializer,
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
