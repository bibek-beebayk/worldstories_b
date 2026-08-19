from django.core.exceptions import ValidationError
from django.template.defaultfilters import filesizeformat


class FileSizeValidator:
    """Rejects an uploaded file larger than `max_bytes`. Django has no
    built-in equivalent to `FileExtensionValidator` for size, so this fills
    that gap for FileField/ImageField declarations."""

    def __init__(self, max_bytes):
        self.max_bytes = max_bytes

    def __call__(self, file):
        if file.size > self.max_bytes:
            raise ValidationError(
                f"File is too large ({filesizeformat(file.size)}). "
                f"Maximum size is {filesizeformat(self.max_bytes)}."
            )

    def __eq__(self, other):
        return isinstance(other, FileSizeValidator) and self.max_bytes == other.max_bytes

    def deconstruct(self):
        return (
            "core.libs.validators.FileSizeValidator",
            [self.max_bytes],
            {},
        )
