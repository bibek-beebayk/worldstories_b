from django.db import transaction


@transaction.atomic
def merge_authors(source, target):
    """Move every reverse FK/M2M relation from source to target, then delete source."""
    if source.pk == target.pk:
        raise ValueError("Source and target authors must be different.")
    moved = []
    for relation in source._meta.related_objects:
        field = relation.field
        model = relation.related_model
        label = f"{model._meta.app_label}.{model._meta.model_name}.{field.name}"
        if relation.one_to_many or relation.one_to_one:
            queryset = model._default_manager.filter(**{field.name: source})
            count = queryset.count()
            if count:
                queryset.update(**{field.name: target})
                moved.append((label, count))
        elif relation.many_to_many:
            through = field.remote_field.through
            source_field = relation.field.m2m_reverse_field_name()
            target_field = relation.field.m2m_field_name()
            rows = through._default_manager.filter(**{source_field: source})
            count = rows.count()
            if count:
                rows.update(**{source_field: target})
                moved.append((f"{label} ({target_field})", count))
    source.delete()
    return moved
