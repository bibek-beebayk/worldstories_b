import re

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Max, Q
from django.utils.text import slugify

from apps.story.models import (
    Audio,
    AudioTranscriptCue,
    Author,
    Blog,
    Category,
    Chapter,
    Genre,
    Mood,
    Story,
    StoryType,
    Tag,
    Theme,
    Video,
    VideoType,
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

# Additional stories. `audio` adds a narrated track whose transcript doubles as
# a Read Along (timed cues are generated from the sentences).
MORE_STORIES = [
    {
        "title": "The Weaver of Kathmandu",
        "author": "Asha Rai",
        "genres": ["Folktale", "Fantasy"],
        "category": "Folk & Legends",
        "tags": ["magic", "love"],
        "themes": ["Home", "Hope"],
        "moods": ["Cozy"],
        "about": "A weaver's loom starts producing scenes from the lives of the people who sleep under her blankets.",
        "chapters": [
            ("Warp and Weft", "<p>Sunita had woven blankets in Patan for forty years. The loom was older than she was.</p>"),
            ("The Pattern Nobody Drew", "<p>One morning a cloth came off the loom with a river on it. By evening, a flood warning arrived.</p>"),
        ],
        "audio": [
            "Sunita had woven blankets in Patan for forty years. The loom was older than she was. "
            "Each morning she oiled the wooden beams and spoke to it softly. "
            "One day the cloth came off the loom with a river woven into it. "
            "By evening, the radio warned of a flood."
        ],
    },
    {
        "title": "Salt Road",
        "author": "Daniel Whitmore",
        "genres": ["Adventure"],
        "category": "Original Stories",
        "tags": ["journey", "friendship"],
        "themes": ["Courage"],
        "moods": ["Thrilling"],
        "about": "A caravan guide and a runaway apprentice cross a dry lake bed with a cargo no one will name.",
        "chapters": [
            ("The Crossing", "<p>The lake had been dry for nine generations, and the road across it was white as bone.</p>"),
            ("Mirage", "<p>On the third day the apprentice swore she saw a city. The guide said nothing, and changed course.</p>"),
            ("Landfall", "<p>They reached the far shore with one water skin, one mule, and every secret intact.</p>"),
        ],
        "audio": [
            "The lake had been dry for nine generations, and the road across it was white as bone. "
            "The guide walked ahead, counting steps. The apprentice walked behind, counting reasons to turn back.",
            "On the third day the apprentice swore she saw a city shimmering on the horizon. "
            "The guide said nothing, and quietly changed course.",
        ],
    },
    {
        "title": "The Clockmaker's Daughter",
        "author": "Mei Lin",
        "genres": ["Mystery", "Fantasy"],
        "category": "Classics",
        "tags": ["mystery", "magic"],
        "themes": ["Loss"],
        "moods": ["Wistful", "Thrilling"],
        "about": "When every clock in the city stops at 3:07, a clockmaker's daughter follows the silence to its source.",
        "chapters": [
            ("3:07", "<p>The cathedral bell did not ring. Neither did the station clock, or the one on her father's wrist.</p>"),
            ("Gears Beneath the Square", "<p>Under the old square she found a machine the size of a house, and it was holding its breath.</p>"),
        ],
        "audio": [
            "The cathedral bell did not ring. Neither did the station clock, or the one on her father's wrist. "
            "Mira put her ear to the nearest wall and heard nothing at all."
        ],
    },
    {
        "title": "Monsoon Recipes",
        "author": "Asha Rai",
        "genres": ["Romance"],
        "category": "Original Stories",
        "tags": ["love", "friendship"],
        "themes": ["Home"],
        "moods": ["Cozy"],
        "about": "Two strangers share a rented kitchen and slowly learn each other through the dishes they cook when it rains.",
        "chapters": [
            ("Shared Shelf", "<p>His spices were labelled in tidy capitals. Hers were not labelled at all.</p>"),
            ("Dal for Rainy Days", "<p>The first time it poured, she left a bowl outside his door with a note: salt to taste.</p>"),
        ],
    },
    {
        "title": "Wind Garden",
        "author": "Mei Lin",
        "genres": ["Science Fiction"],
        "category": "Original Stories",
        "tags": ["journey", "mountains"],
        "themes": ["Hope", "Courage"],
        "moods": ["Wistful"],
        "about": "On a terraformed mountain, a botanist teaches the wind to plant seeds.",
        "chapters": [
            ("Thin Air", "<p>At four thousand metres nothing grew, which was exactly why they sent her.</p>"),
            ("First Green", "<p>The first sprout appeared on the leeward side of a boulder, and she wept without noticing.</p>"),
        ],
        "audio": [
            "At four thousand metres nothing grew, which was exactly why they sent her. "
            "She carried seeds in her coat, and a thermos of tea gone cold."
        ],
    },
    {
        "title": "The Last Ferry to Harbourside",
        "author": "Daniel Whitmore",
        "genres": ["Mystery", "Adventure"],
        "category": "Classics",
        "tags": ["mystery", "journey"],
        "themes": ["Loss", "Home"],
        "moods": ["Wistful", "Thrilling"],
        "about": "A late ferry carries exactly one passenger, who is not on the manifest.",
        "chapters": [
            ("Manifest", "<p>The purser counted forty-one tickets and forty-two people.</p>"),
            ("Fog", "<p>The ferry slowed to a crawl, and the extra passenger began to hum.</p>"),
            ("Harbourside", "<p>When the lights finally came into view, nobody on deck could say who had been humming.</p>"),
        ],
    },
]


EXTRA_STORIES = [
    {
        "title": "The Tea Merchant's Compass",
        "author": "Daniel Whitmore", "genres": ["Adventure", "Folktale"], "category": "Classics",
        "tags": ["journey", "magic"], "themes": ["Courage"], "moods": ["Thrilling"],
        "about": "A compass that points to whatever its owner has lost sends a merchant across three kingdoms.",
        "summary": "<p>A tea merchant inherits a compass that does not point north, and follows it to what he lost.</p>",
        "chapters": [
            ("The Inheritance", "<p>The compass arrived wrapped in tea leaves, its needle trembling toward the kitchen door.</p>"),
            ("Three Kingdoms", "<p>Each border took a season, and each season took something he thought he could spare.</p>"),
        ],
        "audio": [
            "The compass arrived wrapped in tea leaves, its needle trembling toward the kitchen door. "
            "He followed it out of the house, and then out of the valley."
        ],
    },
    {
        "title": "Paper Boats",
        "author": "Asha Rai", "genres": ["Romance", "Folktale"], "category": "Original Stories",
        "tags": ["love", "friendship"], "themes": ["Home", "Loss"], "moods": ["Wistful", "Cozy"],
        "about": "Every monsoon, a girl floats a paper boat with a wish, and every monsoon one comes back.",
        "summary": "<p>Each rainy season a paper boat returns to a girl's doorstep, answered in unfamiliar handwriting.</p>",
        "chapters": [
            ("First Rain", "<p>She folded the boat from a page of her school notebook and set it on the flooded lane.</p>"),
            ("The Answer", "<p>Three days later it came back, wetter and wiser, with a line of ink she had not written.</p>"),
        ],
    },
    {
        "title": "Station Nineteen",
        "author": "Mei Lin", "genres": ["Science Fiction", "Mystery"], "category": "Original Stories",
        "tags": ["mystery", "journey"], "themes": ["Courage"], "moods": ["Thrilling"],
        "about": "A relay station keeps broadcasting a signal from a ship that never launched.",
        "summary": "<p>An engineer on a lonely relay station decodes a distress call from a ship that was never built.</p>",
        "chapters": [
            ("Static", "<p>The signal arrived every eleven minutes, always the same four notes.</p>"),
            ("Handshake", "<p>When she finally answered, the voice on the other end used her own name.</p>"),
        ],
        "audio": [
            "The signal arrived every eleven minutes, always the same four notes. "
            "She logged it for a week before she told anyone. "
            "When she finally answered, the voice used her own name."
        ],
    },
    {
        "title": "What the Hills Remember",
        "author": "Asha Rai", "genres": ["Folktale"], "category": "Folk & Legends",
        "tags": ["mountains", "magic"], "themes": ["Hope", "Home"], "moods": ["Cozy", "Wistful"],
        "about": "Grandmother's stories about the hills turn out to be a map.",
        "summary": "<p>A grandmother's bedtime tales hide directions to the spring the village thought had dried up.</p>",
        "chapters": [
            ("Bedtime Geography", "<p>Left at the crooked pine, she would say, and right where the goats refuse to go.</p>"),
            ("The Spring", "<p>The water was exactly where the story said, cold enough to ache.</p>"),
        ],
    },
    {
        "title": "Night Bus to Pokhara",
        "author": "Daniel Whitmore", "genres": ["Mystery", "Romance"], "category": "Original Stories",
        "tags": ["mystery", "love"], "themes": ["Hope"], "moods": ["Cozy", "Thrilling"],
        "about": "Seven strangers, one broken heater, and a stolen violin somewhere between Kathmandu and the lake.",
        "summary": "<p>A midnight bus ride becomes a whodunit when a violin vanishes from the overhead rack.</p>",
        "chapters": [
            ("Seat Fourteen", "<p>The violin case was on the rack at Kalanki. By Mugling it was gone.</p>"),
            ("Tea Stop", "<p>Everyone had an alibi, and everyone's alibi involved someone else's sleep.</p>"),
            ("Lakeside", "<p>At dawn the music started, and the thief turned out to be the only one who could play.</p>"),
        ],
        "audio": [
            "The violin case was on the rack at Kalanki. By Mugling it was gone. "
            "Seven strangers looked at one another, and the heater coughed once and died."
        ],
    },
    {
        "title": "The Cartographer of Small Things",
        "author": "Mei Lin", "genres": ["Fantasy", "Adventure"], "category": "Classics",
        "tags": ["magic", "journey"], "themes": ["Courage", "Home"], "moods": ["Cozy"],
        "about": "A mapmaker who charts only the overlooked: puddles, ant roads, the shortcut between two chairs.",
        "summary": "<p>A mapmaker of tiny places discovers that the smallest map in her collection leads somewhere enormous.</p>",
        "chapters": [
            ("Scale", "<p>Her maps were drawn at one inch to one footstep, and every one of them was accurate.</p>"),
            ("The Smallest Map", "<p>It showed a crack in the floor, and under it, a staircase.</p>"),
        ],
    },
]

# Short summaries for the earlier stories, so Quick Reads has material.
SUMMARIES = {
    "The Lantern of Namche": "A lantern keeper in a Himalayan village learns what her light has really been guiding.",
    "The Keeper of Grey Point": "A lighthouse keeper finds a message in a bottle written in his own hand, dated next Tuesday.",
    "Orbital Gardens": "Two apprentices race to save the last forest aboard a failing space station.",
    "Letters Across the River": "Two families on opposite banks, and the children who write across the water.",
    "The Weaver of Kathmandu": "A loom starts weaving the future into the blankets it makes.",
    "Salt Road": "A guide and a runaway apprentice cross a dry lake bed with a secret cargo.",
}

BLOGS = [
    {
        "title": "How We Choose the Stories on WorldStories",
        "author": "WorldStories Team",
        "excerpt": "A look behind the curtain at how stories are selected, edited and published.",
        "content": "<p>Every story here starts with a reader or a writer who cared enough to send it in. "
                   "We read each submission twice, once for craft and once for heart.</p>",
        "stories": ["The Lantern of Namche"],
    },
    {
        "title": "Read Along: Listening With Your Eyes",
        "author": "WorldStories Team",
        "excerpt": "Why highlighting words as they are spoken helps new readers and tired ones alike.",
        "content": "<p>Read Along pairs a narrated track with a transcript that lights up as it is read. "
                   "It is gentle on beginners and surprisingly good for late-night reading.</p>",
        "stories": ["Salt Road", "Station Nineteen"],
    },
    {
        "title": "Five Folktales to Read This Monsoon",
        "author": "Asha Rai",
        "excerpt": "Rain, rivers and ferrymen: five tales that feel better with weather outside.",
        "content": "<p>Monsoon is storytelling season in the hills. Here are five tales to read with tea.</p>",
        "stories": ["Letters Across the River", "Paper Boats", "What the Hills Remember"],
    },
    {
        "title": "Writing Short: Notes From Our Authors",
        "author": "Mei Lin",
        "excerpt": "Three authors on what a short story can do that a novel cannot.",
        "content": "<p>A short story has no room to hide. Every sentence has to carry its weight twice.</p>",
        "stories": [],
    },
]

VIDEO_TYPES = ["Narration", "Trailer", "Interview", "Behind the Scenes", "Animation"]

# (title, youtube_id, type, story title or None, duration seconds, aspect)
# The ids are real, public, embeddable YouTube videos used as placeholders.
VIDEOS = [
    ("The Lantern of Namche - Narrated", "YE7VzlLtp-4", "Narration", "The Lantern of Namche", 596, "16:9"),
    ("Orbital Gardens - Official Trailer", "eRsGyueVLvQ", "Trailer", "Orbital Gardens", 888, "16:9"),
    ("Letters Across the River - Chapter One", "R6MlUcmOul8", "Narration", "Letters Across the River", 734, "16:9"),
    ("Why We Still Tell Folktales", "TLkA0RELQ1g", "Interview", None, 654, "16:9"),
    ("A Day with Asha Rai", "jNQXAC9IVRw", "Interview", None, 19, "16:9"),
    ("Drawing the Mountains of Namche", "aqz-KE-bpKQ", "Behind the Scenes", None, 635, "16:9"),
    ("The Keeper of Grey Point - Animated", "YE7VzlLtp-4", "Animation", None, 596, "16:9"),
    ("Salt Road - Teaser", "eRsGyueVLvQ", "Trailer", "Salt Road", 888, "16:9"),
    ("How Read Along Works", "jNQXAC9IVRw", "Behind the Scenes", None, 19, "9:16"),
    ("Monsoon Recipes - Reading", "R6MlUcmOul8", "Narration", "Monsoon Recipes", 734, "16:9"),
    ("Paper Boats - Narrated", "YE7VzlLtp-4", "Narration", "Paper Boats", 596, "16:9"),
    ("Station Nineteen - Teaser", "eRsGyueVLvQ", "Trailer", "Station Nineteen", 888, "16:9"),
    ("Meet the Authors of WorldStories", "TLkA0RELQ1g", "Interview", None, 654, "16:9"),
    ("From Page to Audio: Recording a Story", "aqz-KE-bpKQ", "Behind the Scenes", None, 635, "16:9"),
    ("The Cartographer of Small Things - Animated", "R6MlUcmOul8", "Animation", "The Cartographer of Small Things", 734, "16:9"),
    ("60 Seconds: A Folktale", "jNQXAC9IVRw", "Trailer", None, 19, "9:16"),
]

SECONDS_PER_SENTENCE = 4


def silent_mp3(seconds):
    """Return bytes of a valid, silent MPEG-1 Layer III file (128 kbps, 44.1 kHz).

    Lets demo audio tracks carry a real duration without needing ffmpeg or
    any recorded narration."""
    frame = b"\xff\xfb\x90\x00" + b"\x00" * 413  # 417-byte frame, ~26.12 ms
    return frame * max(1, round(seconds / 0.026122))


def split_sentences(text):
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p]


class Command(BaseCommand):
    help = (
        "Seed the database with demo genres, authors, stories, audio, read-alongs and videos "
        "for local development (idempotent)."
    )

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
        all_stories = STORIES + MORE_STORIES + EXTRA_STORIES
        for i, s in enumerate(all_stories):
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
                    "summary": s.get("summary") or SUMMARIES.get(s["title"]),
                    # A spread of popularity so sort-by-views and by-rating differ.
                    "views": 4000 - i * 230 + (i % 3) * 90,
                    "rating": round(4.9 - (i % 7) * 0.2, 1),
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

        storage = FileSystemStorage(location=settings.MEDIA_ROOT)
        audios_created = cues_created = 0
        for s in all_stories:
            story = Story.objects.get(slug=slugify(s["title"]))
            for order, text in enumerate(s.get("audio", []), start=1):
                sentences = split_sentences(text)
                duration = len(sentences) * SECONDS_PER_SENTENCE
                title = f"{s['title']} - Part {order}" if len(s["audio"]) > 1 else s["title"]
                name = f"story_audios/demo-{story.slug}-{order}.mp3"
                audio, was_created = Audio.objects.get_or_create(
                    story=story,
                    order=order,
                    defaults={
                        "title": title,
                        "slug": slugify(title),
                        "audio_file": name,
                        "transcript": "".join(f"<p>{sent}</p>" for sent in sentences),
                        "duration_seconds": float(duration),
                    },
                )
                if not was_created:
                    continue
                audios_created += 1
                data = silent_mp3(duration)
                if not storage.exists(name):
                    storage.save(name, ContentFile(data))
                Audio.objects.filter(pk=audio.pk).update(file_size_bytes=len(data))
                # Timed cues (one per sentence) are what makes it a Read Along.
                AudioTranscriptCue.objects.bulk_create(
                    AudioTranscriptCue(
                        audio=audio,
                        order=n,
                        start_ms=(n - 1) * SECONDS_PER_SENTENCE * 1000,
                        end_ms=n * SECONDS_PER_SENTENCE * 1000,
                        text=sent,
                    )
                    for n, sent in enumerate(sentences, start=1)
                )
                cues_created += len(sentences)

        vtypes = {n: VideoType.objects.get_or_create(name=n)[0] for n in VIDEO_TYPES}
        videos_created = 0
        standalone_order = 0
        story_orders = {}
        for title, yt_id, vtype, story_title, duration, aspect in VIDEOS:
            story = Story.objects.get(slug=slugify(story_title)) if story_title else None
            if Video.objects.filter(title=title).exists():
                continue
            if story:
                order = story_orders.get(story.pk) or (
                    (Video.objects.filter(story=story).aggregate(m=Max("order"))["m"] or 0) + 1
                )
                story_orders[story.pk] = order + 1
            else:
                # unique_together is (story, order); NULL stories never collide,
                # but keep a stable order anyway.
                standalone_order += 1
                order = standalone_order
            Video.objects.create(
                story=story,
                video_type=vtypes[vtype],
                title=title,
                slug=slugify(title),
                youtube_id=yt_id,
                youtube_url=(
                    f"https://www.youtube.com/shorts/{yt_id}" if aspect == "9:16"
                    else f"https://www.youtube.com/watch?v={yt_id}"
                ),
                order=order,
                duration_seconds=float(duration),
                aspect_ratio=aspect,
            )
            videos_created += 1

        # Stories seeded earlier (before summaries existed) get theirs now.
        for title, text in SUMMARIES.items():
            Story.objects.filter(slug=slugify(title)).filter(
                Q(summary__isnull=True) | Q(summary__exact="")
            ).update(summary=f"<p>{text}</p>")

        blogs_created = 0
        for b in BLOGS:
            blog, was_created = Blog.objects.get_or_create(
                slug=slugify(b["title"]),
                defaults={
                    "title": b["title"],
                    "excerpt": b["excerpt"],
                    "content": b["content"],
                    "author_name": b["author"],
                    "is_published": True,
                },
            )
            if was_created:
                blogs_created += 1
                blog.linked_stories.set(Story.objects.filter(slug__in=[slugify(t) for t in b["stories"]]))

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded demo data ({created} new stories, {blogs_created} blog posts, {audios_created} audio tracks, "
                f"{cues_created} read-along cues, {videos_created} videos)."
            )
        )
