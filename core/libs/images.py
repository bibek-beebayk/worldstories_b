from rest_framework import serializers
from rest_framework.relations import ManyRelatedField
from versatileimagefield.utils import get_url_from_image_key
from html.parser import HTMLParser
from django.conf import settings
from django.db.models import Model
from versatileimagefield.image_warmer import VersatileImageFieldWarmer

class ImageFieldSerializer(serializers.ImageField):
    def __init__(self, size, *args, mode='crop', **kwargs):
        self.mode = mode
        self.size = size

        super().__init__(*args, **kwargs)

    def to_representation(self, value):
        """
        value: the image to transform
        returns: a url pointing at a scaled image
        """
        if not value:
            return None

        image = getattr(value, self.mode)[self.size]

        try:
            request = self.context.get('request', None)
            return request.build_absolute_uri(image.url)
        except:
            try:
                return super().to_representation(image)
            except AttributeError:
                return super().to_native(image.url)

    to_native = to_representation


class ImageKeySerializer(serializers.ImageField):
    def __init__(self, key, *args, **kwargs):
        self.key = key
        kwargs['read_only'] = True
        super().__init__(*args, **kwargs)

    def get(self, value, key_name, request):
        try:
            key = value.instance.SIZES[value.field.name][key_name]
        except KeyError:
            return
        url = get_url_from_image_key(value, key)
        # url = url.replace('%2520', '%20')

        if '%2520' in url:
            return None

        if request:
            return request.build_absolute_uri(url)
        else:
            try:
                rep = super().to_representation(url)
                if rep is None:
                    return url
                return rep
            except AttributeError:
                return super().to_native(url)

    def to_representation(self, value):
        if not value:
            return None
        request = self.context.get('request', None)
        if type(self.key) == str:
            return self.get(value, self.key, request)
        elif type(self.key) == list:
            urls = {}
            for key_name in self.key:
                urls[key_name] = self.get(value, key_name, request)
            return urls
        else:
            raise ValueError('Key must be either string or a list.')

    to_native = to_representation


def many_to_internal_value(self, data):
    if data == ['null']:
        data = []
    if isinstance(data, str) or not hasattr(data, '__iter__'):
        self.fail('not_a_list', input_type=type(data).__name__)
    if not self.allow_empty and len(data) == 0:
        self.fail('empty')

    return [
        self.child_relation.to_internal_value(item)
        for item in data
    ]


ManyRelatedField.to_internal_value = many_to_internal_value


class EmptyForNullTextField(serializers.CharField):
    def get_attribute(self, instance):
        attibute = super().get_attribute(instance)
        if attibute is None:
            attibute = ''
        return attibute


def warm(instance):
    if hasattr(instance, 'SIZES') and type(instance.SIZES) == dict:
        for field, set in instance.SIZES.items():
            if getattr(instance, field):
                for key, value in set.items():
                    warmer = VersatileImageFieldWarmer(
                        instance_or_queryset=instance,
                        rendition_key_set=[(key, value)],
                        image_attr=field
                    )
                    try:
                        warmer.warm()
                    except AttributeError:
                        pass


def warm_bulk(queryset):
    if hasattr(queryset.model, 'SIZES'):
        SIZES = queryset.model.SIZES
        if type(SIZES) == dict:
            for field, set in SIZES.items():

                for key, value in set.items():
                    warmer = VersatileImageFieldWarmer(
                        instance_or_queryset=queryset,
                        rendition_key_set=[(key, value)],
                        image_attr=field
                    )
                    try:
                        warmer.warm()
                    except AttributeError:
                        pass


class FirstImageParser(HTMLParser):
    first_image = None

    def handle_starttag(self, tag, attrs):
        if tag == 'img' and not self.first_image:
            for attr in attrs:
                if attr[0] == 'src':
                    self.first_image = attr[1]
                    # break


def get_first_image(markup):
    parser = FirstImageParser()
    parser.feed(markup)
    return parser.first_image


def save_thumbnail(instance: Model, *args, **kwargs):
    original = instance.thumbnail if instance.thumbnail else None
    post_save = False

    header_image = kwargs.pop('header_image', None)
    if not header_image:
        if hasattr(instance, 'header_image'):
            header_image = instance.header_image
        elif hasattr(instance, 'header_photo'):
            header_image = instance.header_photo

    if header_image:
        instance.thumbnail = header_image
        if not original or instance.thumbnail != original:
            post_save = True
    else:
        if hasattr(instance, 'description'):
            content = instance.description
        elif hasattr(instance, 'content'):
            content = instance.content
        else:
            raise AttributeError('Content attribute could not be detected!')

        first_image = get_first_image(content)
        if first_image:
            valid_media_prefixes = []
            if hasattr(settings, 'MEDIA_URL'):
                valid_media_prefixes.append(settings.MEDIA_URL)
                if not settings.MEDIA_URL.startswith('http') and hasattr(settings, 'BACKEND_URL'):
                    valid_media_prefixes.append('{}{}'.format(settings.BACKEND_URL, settings.MEDIA_URL))
            # TODO Generated media file url isn't absolute
            if hasattr(settings, 'ADDITIONAL_MEDIA_URLS'):
                valid_media_prefixes.extend(settings.ADDITIONAL_MEDIA_URLS)

            if first_image.startswith(tuple(valid_media_prefixes)):
                instance.thumbnail = first_image.split(settings.MEDIA_URL)[-1]
                if not original or instance.thumbnail != original.url:
                    post_save = True
            elif instance.thumbnail:
                instance.thumbnail = None
                post_save = True
        elif instance.thumbnail:
            instance.thumbnail = None
            post_save = True
    if post_save:
        if kwargs.get('force_insert'):
            kwargs['force_insert'] = False
        super(instance.__class__, instance).save(*args, **kwargs)