# Grouping Strategies: One Photo Set, Six Readings

The same trip photos support more than one true organisation. "What happened, in order" is one reading;
"which meals did we eat", "which photos are one memory worth telling" and "what was the best of each place" are
others, and travelers themselves disagree about which one their own photos deserve - the Week 4 eval sets
include a traveler whose manual groups bundle an activity with its follow-on reward, which the chronological
reading cuts in half. The pipeline therefore offers grouping **styles**: a fixed list of questions, each put to
the per-image text the visual model already produced. Photos are evaluated once; every style after that is a
text-only call costing cents, cached so switching back is free. On the demo they sit in a drop-down on a
finished run; on the CLI, `review-moments --style <id>`.

All examples below are real output from the five Week 4 eval sets (Acapulco, Berlin, Costa Rica, Nazaré,
Zurich), unedited. Mechanics of the review pass are in `moment_review.md`; measured accuracy is in
`week4_eval_results.md`.

Two shapes of style exist, and the difference matters to what a product can build on them:

- **Moments** is a partition: every photo belongs to exactly one moment, reviewed one outing at a time.
- The five **topic styles** are trip-wide selections: groups are sets of photos matching the style's question,
  and a photo that does not fit stays out. Empty output is a valid answer.

## Moments - what happened, in order

The default, and the only style measured against the traveler's own grouping. Rule-based moments from time and
movement are reviewed by the model reading each outing's photo descriptions with times, gaps and distances; it
may merge, split, re-cut, title and explain each moment, and every photo lands somewhere.

Zurich, after review: "In-flight views of snow-capped mountains", "Winter lakeside park gathering", "Walking
through historic Zürich streets", "Mountain picnic at a snowy lake overlook", "Cozy apartment gathering and
shared meal". Measured against the traveler: pair precision and recall both 0.929, the best grouping number
the pipeline has produced on any set.

**When it works:** trips that unfold as scenes - travel, arrive, walk, eat - which is most trips; and any
product that must account for every photo (a chronological recap, an album). **Where it strains:** travelers
who group by narrative arc rather than scene. On Berlin the review split "bouldering" from the post-climb beer
and a leafy walk from its café stop, and the traveler had kept each pair whole; F1 dropped from 0.706 to 0.583
against that traveler while other sets improved. A moment for one traveler is two scenes for another.

## Memories - which photos are one memory worth telling

Trip-wide; groups are memories with a title, a one-sentence "about", and why-tags from the traveler vocabulary
(awe, excitement, meaningful, calm, fun, connection). Photos that belong to no memory stay unassigned, which
makes this the only style with a built-in answer to "not everything is worth telling".

Acapulco produced twelve memories from 29 photos, four left out: "A Turtle Hatchling Heads for Surf" (awe,
meaningful), "Playing Games After Dark" (fun, connection, excitement), "Watching the Tropical Sunset" (awe,
calm). Berlin produced the two groups the moments review broke: "Bouldering and a Post-Climb Beer",
"A Leafy Walk with Café Treats" - this traveler's own shape, recovered by asking the memory question instead
of the place question.

**When it works:** recaps, journaling prompts, anything narrated; travelers whose mental model is stories, not
scenes. **Where it strains:** granularity. On Shanghai (Week 4) the memory view was far coarser than the
traveler's own memory document, twelve arcs against thirty fine-grained memories; the same tendency shows here
(Nazaré became four memories spanning whole days). It tells fewer, bigger stories than a traveler might.

## Landmark tour - one group per named place of interest

Trip-wide selection keyed to what the extraction identified by name. It is exactly as good as the per-image
landmark field, which the trust rules keep conservative: never name what is not visibly identifiable.

Nazaré: "Praça do Comércio, Lisbon", "Church and Convent of Graça", "Miradouro de Santa Luzia", "Miradouro do
Suberco, Nazaré", "Praia da Nazaré". Zurich: "Lake Zürich", "Zurich Opera House", "Fraumünster Church", "ETH
Zurich Main Building". Acapulco: no groups at all, correctly - a resort trip with nothing nameable in frame.
Berlin found only the Berliner Dom, which is the landmark *field's* weakness on that set (3% population), not
the style's.

**When it works:** sightseeing trips, and as a direct window onto extraction quality - London in Week 4
produced 35 named groups. **Where it strains:** trips without named sights return little or nothing, and that
must be presented as an honest empty, not padded; and it inherits every miss from the landmark field, which is
the pipeline's one weak extraction (see `week4_eval_results.md`).

## Foodie tour - every meal and food or drink stop

Trip-wide selection, titled by what was eaten. Nazaré: "Pastel de Nata", "Pastries and Coffee", "Burgers,
Fries, and Drinks". Berlin: "Currywurst and Fries", "Döner and Falafel Stop", "Seafood Noodles and Sushi".
Zurich: "Creamy Meatballs and Lager", "Avocado Toast Breakfast".

**When it works:** almost every trip - food is the most photographed subject in all five sets - and the titles
are concrete enough to caption directly. **Where it strains:** it is a list of stops, not a grouping; most
groups are one photo, so it reads as a themed index. That is the product it should be sold as.

## Location highlights - the best few photos of each place

Trip-wide selection: one group per place, holding the strongest photos of that place. Nazaré collapsed the trip
to its two poles, "Lisbon" and "Nazaré", and against the content-based reference that split scores F1 0.609,
the highest any style scored against it. Costa Rica produced twelve places from "Tropical Garden and
Smallholding" to "Sea Turtle Nesting Beach"; Zurich six, from "Zürich Old Town Riverfront" to "Alpine Lake
Overlook".

**When it works:** trip summaries and covers - a shortlist per place is exactly the postcard-picking question.
**Where it strains:** "place" is whatever granularity the model reads off the text, so one trip's places are
two cities and another's are twelve micro-locations; the granularity is not yet steerable.

## Enjoyable moments - photos by the mood they read as

Trip-wide selection into the six why-tags. Costa Rica: "Awe: mountains and dramatic coasts" (12 photos),
"Meaningful: sea turtle hatchlings", "Connection: shared travel and nightlife". Berlin: "Calm: leafy walks and
café pauses" (12), "Excitement: climbing, crowds and seafood soup".

**When it works:** mood-first browsing and recap sequencing; it is also the steadiest style against the
content-based reference (0.32-0.40 on every set) because content categories and moods largely coincide. **Where
it strains:** it leans on the per-photo `why_tags` extra, which is prompt v3's one field beyond the confirmed
eleven; if that field is ruled out under task 5, this style falls back to inferring mood from descriptions.

## Choosing, and what it costs

| Product question | Style shaped for it |
| --- | --- |
| Chronological album or recap, every photo accounted for | Moments |
| Story-shaped recap, journaling, "tell me about the trip" | Memories |
| Sightseeing summary, "what did we see" | Landmark tour |
| Food story, "what did we eat" | Foodie tour |
| Trip cover, postcards, best-of-place shortlist | Location highlights |
| Mood browsing, emotional sequencing | Enjoyable moments |

On the Week 4 eval sets, a full style run costs one to four cents per set (the whole five-set, six-style
exercise cost $0.73 in review calls on top of $1.66 of per-image extraction), takes seconds to tens of seconds,
and is cached under a hash of exactly what the model saw, so re-selecting a style is free until the photos, the
prompt or the boundaries change.

Two standing limits. Topic styles read the whole trip in one call up to 200 photos, then cut at the largest gap
- groups cannot reach across those cuts on very large trips. And every style reads the per-image text, never
the pixels: a style is only as good as the extraction beneath it, which is why the landmark tour mirrors the
landmark field's reliability exactly.
