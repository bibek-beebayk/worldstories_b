from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify

from apps.story.models import (
    Author,
    Category,
    Chapter,
    Genre,
    Mood,
    Story,
    StoryType,
    Tag,
    Theme,
)

GENRES = ["Fantasy", "Mystery", "Romance", "Science Fiction", "Folktale", "Adventure"]
CATEGORIES = ["Classics", "Original Stories", "Folk & Legends"]
TAGS = ["magic", "journey", "friendship", "mountains", "mystery", "love"]
THEMES = ["Courage", "Loss", "Hope", "Home"]
MOODS = [("Cozy", "☕"), ("Thrilling", "⚡"), ("Wistful", "🌧️")]
STORY_TYPES = ["Short Story", "Novel", "Poetry"]

AUTHORS = [
    ("Asha Rai", "Writes about the hill villages of eastern Nepal."),
    ("Daniel Whitmore", "A former lighthouse keeper turned storyteller."),
    ("Mei Lin", "Writes speculative fiction about cities of the near future."),
]

STORIES = [
    {
        "title": "The Lantern of Namche",
        "author": "Asha Rai",
        "genres": ["Folktale", "Fantasy"],
        "category": "Folk & Legends",
        "tags": ["magic", "mountains"],
        "themes": ["Hope"],
        "moods": ["Cozy"],
        "about": "A lantern keeper in a Himalayan village discovers that her light guides more than travellers.",
        "chapters": [
            ("The Last Lamp", "<p>Every evening, Pema climbed the stone steps above Namche to light the old lantern. "
                              "Nobody remembered who had hung it there, only that the mountain was kinder when it burned.</p>"),
            ("A Stranger in the Snow", "<p>On the night of the first storm, a stranger knocked at her door, "
                                       "carrying nothing but a map drawn in a language she almost understood.</p>"),
            ("What the Light Remembers", "<p>By dawn the lantern had shown her where the map led: "
                                         "not to treasure, but home.</p>"),
        ],
    },
    {
        "title": "The Keeper of Grey Point",
        "author": "Daniel Whitmore",
        "genres": ["Mystery"],
        "category": "Original Stories",
        "tags": ["mystery"],
        "themes": ["Loss"],
        "moods": ["Thrilling", "Wistful"],
        "about": "A lighthouse keeper finds a message in a bottle that was written in his own handwriting.",
        "chapters": [
            ("The Bottle", "<p>The tide left it on the rocks below Grey Point, green glass, cork still sealed.</p>"),
            ("Ink and Salt", "<p>The note inside was dated next Tuesday. He recognised every looping letter.</p>"),
        ],
    },
    {
        "title": "Orbital Gardens",
        "author": "Mei Lin",
        "genres": ["Science Fiction", "Adventure"],
        "category": "Original Stories",
        "tags": ["journey", "friendship"],
        "themes": ["Courage", "Home"],
        "moods": ["Thrilling"],
        "about": "Two apprentices tend the last forest aboard a failing space station.",
        "chapters": [
            ("Root Systems", "<p>The forest deck hummed with pumps. Ren pressed her palm to a cedar and felt it breathe.</p>"),
            ("Pressure Drop", "<p>When the alarms started, the trees were the only things that did not panic.</p>"),
            ("Seeds", "<p>They loaded the seed vault into the escape pod, and a decision was made.</p>"),
        ],
    },
    {
        "title": "Letters Across the River",
        "author": "Asha Rai",
        "genres": ["Romance"],
        "category": "Classics",
        "tags": ["love", "friendship"],
        "themes": ["Hope", "Loss"],
        "moods": ["Wistful", "Cozy"],
        "about": "Two families on opposite banks exchange letters by boat, and so do their children.",
        "chapters": [
            ("The First Boat", "<p>The ferryman agreed to carry one letter, no more, and never to read it.</p>"),
            ("Monsoon", "<p>The river rose, and for three weeks no letter crossed.</p>"),
        ],
    },
]


class Command(BaseCommand):
    help = "Seed the database with demo genres, authors and stories for local development (idempotent)."

    @transaction.atomic
    def handle(self, *args, **options):
        genres = {n: Genre.objects.get_or_create(name=n, defaults={"slug": slugify(n)})[0] for n in GENRES}
        cats = {n: Category.objects.get_or_create(name=n, defaults={"slug": slugify(n)})[0] for n in CATEGORIES}
        tags = {n: Tag.objects.get_or_create(slug=slugify(n), defaults={"name": n})[0] for n in TAGS}
        themes = {n: Theme.objects.get_or_create(slug=slugify(n), defaults={"name": n})[0] for n in THEMES}
        moods = {
            n: Mood.objects.get_or_create(slug=slugify(n), defaults={"name": n, "icon": icon})[0]
            for n, icon in MOODS
        }
        types = {n: StoryType.objects.get_or_create(name=n)[0] for n in STORY_TYPES}
        authors = {n: Author.objects.get_or_create(name=n, defaults={"bio": bio})[0] for n, bio in AUTHORS}

        created = 0
        for i, s in enumerate(STORIES):
            story, was_created = Story.objects.get_or_create(
                slug=slugify(s["title"]),
                defaults={
                    "title": s["title"],
                    "about": s["about"],
                    "author": authors[s["author"]],
                    "story_type": types["Short Story"],
                    "language": "en",
                    "is_published": True,
                    "is_original": s["category"] == "Original Stories",
                    "is_completed": True,
                    "original_published_year": 1990 + i * 7,
                },
            )
            if not was_created:
                continue
            created += 1
            story.genres.set([genres[g] for g in s["genres"]])
            story.categories.set([cats[s["category"]]])
            story.tags.set([tags[t] for t in s["tags"]])
            story.themes.set([themes[t] for t in s["themes"]])
            for m in s["moods"]:
                story.story_moods.get_or_create(mood=moods[m])
            for order, (title, content) in enumerate(s["chapters"], start=1):
                Chapter.objects.create(
                    story=story, title=title, slug=slugify(title), content=content, order=order
                )

        self.stdout.write(self.style.SUCCESS(f"Seeded demo data ({created} new stories)."))
