import logging
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

OUTING = "outing"
TRIP = "trip"
PARTITION = "partition"
SELECTION = "selection"
MOMENT_REVIEW = "moment"
TOPIC_REVIEW = "topic"

MOMENTS = "moments"
MEMORIES = "memories"
LANDMARK_TOUR = "landmark_tour"
FOODIE_TOUR = "foodie_tour"
LOCATION_HIGHLIGHTS = "location_highlights"
ENJOYABLE_MOMENTS = "enjoyable_moments"

SELECTION_RULE = (
    "Photos that do not fit this view stay unassigned; do not force them into a group. "
    "An empty list of groups is a valid answer when nothing in the set fits."
)

MOMENTS_INSTRUCTIONS = "Group consecutive photos into moments: what happened, in order, one place or activity at a time."

MEMORIES_INSTRUCTIONS = (
    "Find the memories in this set. A memory is something the traveler would tell someone about afterwards: "
    "it may span several places and hours, and it is held together by what was going on rather than by where the camera was. "
    "A hike that ends at a temple is one memory; a walk across a city that ends at a view is one memory; a coffee with a friend is one memory even if it was in an airport. "
    "Title each memory by what it was about, not by where it was. In about, say in one sentence what was happening across these photos. "
    "Choose one to three why tags that say why it could matter to the traveler: awe, excitement, meaningful, calm, fun or connection. "
    "The why tags on the frames are a model's reading of each photo alone; use them as hints, not as the answer. "
    "Leave out photos that belong to no memory worth telling, such as practical screenshots or near repeats. " + SELECTION_RULE
)

LANDMARK_TOUR_INSTRUCTIONS = (
    "Group the photos by landmark or point of interest: one group per named landmark, building, monument, square or natural site, across the whole trip, "
    "even when the visits are hours or days apart. Use only landmark names and places the frame text itself names; never supply a name the text does not carry. "
    "Title each group with the landmark's name as the text gives it. In about, say in one sentence what the photos of it show. why may be empty. " + SELECTION_RULE
)

FOODIE_TOUR_INSTRUCTIONS = (
    "Group the photos into meals and food or drink stops: restaurants, cafes, bars, markets, street food, picnics, a coffee, a dessert. "
    "One group per meal or stop, across the whole trip. Title each group by what was eaten or drunk when the text names it, otherwise by the kind of stop. "
    "In about, say in one sentence what the stop was. why may be empty. Photos with no food or drink in them stay unassigned. " + SELECTION_RULE
)

LOCATION_HIGHLIGHTS_INSTRUCTIONS = (
    "Pick the best of each place. Decide which distinct places or areas the trip visited, from the landmarks, settings, environment styles and distances in the text, "
    "and make one group per place holding its strongest photos: at most five, preferring the ones with the highest quality score and different views of the place. "
    "Title each group with the place as the text names it. In about, say in one sentence what makes these the highlights of that place. why may be empty. "
    "Photos that are not among a place's highlights stay unassigned. " + SELECTION_RULE
)

ENJOYABLE_MOMENTS_INSTRUCTIONS = (
    "Find the photos that read as enjoyable and group them by the kind of enjoyment: fun, calm, awe, connection, excitement or meaningful, using the why tags on the frames as hints and the descriptions as evidence. "
    "One group per kind that is present, across the whole trip; a photo belongs to at most one group, the kind that fits it best. "
    "Title each group by the kind and what it contains, such as \"Calm: cafe stops and quiet streets\". In about, say in one sentence what these photos share. "
    "Set why to the single tag the group is about. People stay generic: the focal point categories single person, couple and group of people are the only grouping distinction allowed. "
    + SELECTION_RULE
)


@dataclass(frozen=True)
class GroupingStyle:
    id: str
    name: str
    description: str
    instructions: str
    scope: str
    coverage: str
    kind: str


STYLES = {
    MOMENTS: GroupingStyle(
        id=MOMENTS,
        name="Moments",
        description="What happened, in order: the rule-based moments, reviewed one outing at a time.",
        instructions=MOMENTS_INSTRUCTIONS,
        scope=OUTING,
        coverage=PARTITION,
        kind=MOMENT_REVIEW,
    ),
    MEMORIES: GroupingStyle(
        id=MEMORIES,
        name="Memories",
        description="Memories worth telling, titled by what they were about, with why they mattered. Photos that belong to none are left out.",
        instructions=MEMORIES_INSTRUCTIONS,
        scope=TRIP,
        coverage=SELECTION,
        kind=TOPIC_REVIEW,
    ),
    LANDMARK_TOUR: GroupingStyle(
        id=LANDMARK_TOUR,
        name="Landmark tour",
        description="One group per landmark or point of interest across the whole trip.",
        instructions=LANDMARK_TOUR_INSTRUCTIONS,
        scope=TRIP,
        coverage=SELECTION,
        kind=TOPIC_REVIEW,
    ),
    FOODIE_TOUR: GroupingStyle(
        id=FOODIE_TOUR,
        name="Foodie tour",
        description="Every meal and food or drink stop, titled by what was eaten.",
        instructions=FOODIE_TOUR_INSTRUCTIONS,
        scope=TRIP,
        coverage=SELECTION,
        kind=TOPIC_REVIEW,
    ),
    LOCATION_HIGHLIGHTS: GroupingStyle(
        id=LOCATION_HIGHLIGHTS,
        name="Location highlights",
        description="The best few photos of each place the trip visited, one group per place.",
        instructions=LOCATION_HIGHLIGHTS_INSTRUCTIONS,
        scope=TRIP,
        coverage=SELECTION,
        kind=TOPIC_REVIEW,
    ),
    ENJOYABLE_MOMENTS: GroupingStyle(
        id=ENJOYABLE_MOMENTS,
        name="Enjoyable moments",
        description="Photos that read as fun, calm, awe, connection, excitement or meaningful, grouped by that mood.",
        instructions=ENJOYABLE_MOMENTS_INSTRUCTIONS,
        scope=TRIP,
        coverage=SELECTION,
        kind=TOPIC_REVIEW,
    ),
}


def style_for(style_id: str) -> GroupingStyle:
    style = STYLES.get(style_id)
    if style is None:
        raise ValueError(f"no grouping style named {style_id}, known styles are {list(STYLES)}")
    LOGGER.debug(f"{style_id}: grouping style resolved, scope {style.scope}, coverage {style.coverage}")
    return style
