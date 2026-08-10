# Week 2 Findings: Measured Metadata Reliability

Measured on 2026-08-09 against the three supplied datasets. These numbers replace the expected reliability
ratings in the Week 1 Metadata Reliability Matrix for every field the local pipeline currently extracts.

## Scope and method

Stages 1 to 4 and stage 9 of the Week 2 pipeline are implemented and were run end to end: Configure, Inventory,
Extract, Normalize and Group. No external service was called and no image left the workstation, so the
fetched-metadata and visual-model fields remain unmeasured.

Every dataset was inventoried into an immutable versioned manifest first, so each measurement below traces to a
fixed file set by SHA-256. Extraction reads EXIF and decoded dimensions through Pillow with HEIF support, and
derives deterministic visual signals through OpenCV from a fixed 512-pixel derivative with a seeded k-means
palette. Normalization maps raw tags into canonical fields, recording the source, the method version and an
explicit reason whenever a value is absent.

| Component | Version |
|-----------|---------|
| Python | 3.14.5 |
| Pillow | 12.3.0 |
| pillow-heif | 1.5.0 |
| OpenCV | 5.0.0 |
| NumPy | 2.5.2 |
| Extraction method | extract-1 |
| Visual signal method | opencv-1 |
| Normalization method | normalize-1 |
| Timezone method | timezone-1 |
| Grouping method | group-1 |

## Datasets processed

| Dataset | Files | Images extracted | Skipped | Extraction failures |
|---------|-------|------------------|---------|---------------------|
| Glenn_Pictures_Cruise_2024 | 21 | 21 HEIC | 0 | 0 |
| Glenn_London | 127 | 111 HEIC | 15 MP4, 1 MOV | 0 |
| Emiliano_s_Pictures_Shanghai 2026 | 62 | 61 JPEG | 1 reference document | 0 |

Parse and processing success was 100 percent across all 193 images. No file in any dataset failed to decode, and
no malformed EXIF structure caused an extraction error. The Shanghai set is exported or downloaded copies rather
than native library assets, which is where the losses below come from.

## Metadata presence by dataset

Presence is the share of extracted images where the field resolved to a value. Absence is recorded with a
reason rather than a null, so these rates are produced by the pipeline's own records.

| Field | Category | Cruise (21) | London (111) | Shanghai (61) |
|-------|----------|-------------|--------------|---------------|
| capture_local_time | photo | 100% | 100% | 95% |
| capture_utc_offset | photo | 100% | 100% | 92% |
| capture_timestamp_utc | computed | 100% | 100% | 92% |
| timezone_candidate | computed | 76% | 70% | 85% |
| gps_latitude / gps_longitude | photo | 76% | 70% | 85% |
| gps_altitude | photo | 76% | 66% | 85% |
| orientation | photo | 100% | 99% | 23% |
| device_make / device_model | photo | 100% | 100% | 92% |
| lens_model | photo | 100% | 100% | 92% |
| image_width / image_height | photo | 100% | 100% | 100% |
| palette, blur_score, brightness, difference_hash | detected | 100% | 100% | 100% |

Deterministic signals derived from decoded pixels are available for every image in every dataset, because they
depend on the file decoding rather than on what the source library preserved.

## Signal overlap: what can anchor a photo

Presence per field understates the grouping problem, because grouping depends on which signals a photo has
together. Counting images by whether they resolved a UTC timestamp and a coordinate:

| Dataset | Time and GPS | Time only | Neither |
|---------|--------------|-----------|---------|
| Glenn_Pictures_Cruise_2024 | 16 | 5 | 0 |
| Glenn_London | 78 | 33 | 0 |
| Emiliano_s_Pictures_Shanghai 2026 | 52 | 4 | 5 |
| **Total (193 images)** | **146 (76%)** | **42 (22%)** | **5 (3%)** |

**No image in any dataset carried GPS without also carrying a usable timestamp.** Location is a strict subset of
time. This is the most consequential finding of the week: time is the only signal that can serve as the primary
grouping axis, and place can only ever refine or explain a boundary that time proposed.

The 42 time-only images are not an edge case to defer. They are 22 percent of the corpus and 30 percent of the
London set, so the rule allowing a GPS-missing image to join a time-adjacent group determines the outcome for
roughly one photo in five.

The 5 unanchored Shanghai images have neither a timestamp nor a coordinate. Nothing in the source metadata can
place them. They can only be positioned by file sequence or visual similarity, and both are weaker claims that
should surface as low-confidence proposals rather than silent placements.

## Observed failure modes

The predicted failure patterns for exported and downloaded media were confirmed, and their severity now has
numbers attached.

- **Orientation loss on export.** Present for 23 percent of Shanghai images against 99 to 100 percent for the
  native HEIC sets. Decoded pixel dimensions remain reliable at 100 percent everywhere, so the decoded shape is
  the trustworthy source and the orientation tag is not.
- **Timestamp loss on export.** Three Shanghai images carry no capture timestamp tag at all. A further two
  carry a local time with no UTC offset, so their local time is known but their absolute position on a timeline
  is not. That distinction is preserved: capture_local_time resolves for 58 of 61 while capture_timestamp_utc
  resolves for 56.
- **Device provenance loss.** Make, model and lens are absent for the same 5 Shanghai images, consistent with
  metadata stripping during export rather than with per-field loss.
- **GPS is the least dependable native field.** Even in the two clean iPhone sets, coordinates are missing for
  24 percent and 30 percent of images. Location permissions, indoor capture and stripped copies all produce the
  same result, and the pipeline cannot distinguish between those causes from the file alone.
- **Exact duplicates occur in supplied data.** The cruise set contains one exact duplicate pair identified by
  SHA-256 (`IMG_5348.HEIC` and `IMG_5348(1).HEIC`). Duplicate detection is required at the file level before any
  grouping runs, not only at the perceptual level.

## Implications for moment grouping

- Build the grouping baseline on capture_timestamp_utc, with the adaptive time window as the primary mechanism.
  Location can tighten or explain a boundary but cannot be a precondition for one.
- Treat a missing coordinate as normal rather than exceptional. A photo with time alone must still be groupable
  and must not be pushed into a separate low-quality bucket.
- Where an image has local time but no offset, the timezone candidate must come from GPS or from the trip
  context of its neighbours. Where an image has neither, no timezone claim should be made at all.
- Collapse exact duplicates by content hash before grouping, and keep the membership link so the duplicate stays
  visible in the record rather than being deleted.
- The visual signals are available on every image without exception, which makes them the only universally
  present evidence in the corpus. That makes them useful as a tiebreaker for the 22 percent of images without
  location, and it is the strongest argument so far for the visual model earning its place.

## Offline timezone derivation and timestamp cross-check

A timezone candidate is derived from coordinates using timezonefinder, which is a local dataset lookup with no
network call and no third-party exposure. It resolves for every photo carrying GPS, so its presence rate is
identical to the coordinate rate.

Its designed purpose, recovering a UTC timestamp when EXIF carries no offset, is worth nothing on this corpus:
the only 2 photos missing an offset also have no coordinates, so there is nothing to derive a zone from. The
capability is retained because it costs nothing and the reliability matrix specifies it, but it should not be
counted as a benefit until data arrives that needs it.

The valuable outcome is different. Where both an EXIF offset and a derived zone exist, they can be compared,
which is an independent check on the timestamp using a signal the file did not supply.

| Dataset | Derived zones | Offset agrees | Offset disagrees | Not checkable |
|---------|---------------|---------------|------------------|---------------|
| Glenn_Pictures_Cruise_2024 | Europe/Athens 7, Europe/Rome 6, Europe/Zagreb 3 | 16 | 0 | 5 |
| Glenn_London | Europe/London 78 | 78 | 0 | 33 |
| Emiliano_s_Pictures_Shanghai 2026 | Asia/Shanghai 51, Europe/Berlin 1 | 52 | 0 | 9 |

**All 146 checkable photos agree, with no disagreements anywhere.** Where an iPhone records both a coordinate
and a UTC offset, the two are consistent, including across the cruise set where the traveler moved between
three national timezones. That materially raises confidence in capture time as the primary grouping signal,
because the confirmation comes from a signal independent of the timestamp itself. The 47 photos that cannot be
checked are exactly those without coordinates.

Two incidental results worth noting. The cruise itinerary falls out of the zone data alone, moving through
Greece, Italy and Croatia, without any geocoding call or external service. And a single Shanghai photo resolves
to Europe/Berlin, which is either a genuine transit photo or a coordinate error; it is surfaced as a candidate
for review rather than treated as either.

The comparison is stored as evidence on the capture timestamp rather than as a separate field, so a photo whose
offset disagreed with its zone would remain usable while carrying the disagreement in its record.

## Moment grouping results

The grouping baseline implements the Week 1 hypothesis constants unchanged, so these results test those
constants rather than a tuned algorithm. Boundaries are proposed when a time gap exceeds an adaptive window,
which is the median of the last five gaps multiplied by eight and clamped between 15 and 45 minutes, or when
two consecutive located photos are more than 150 metres apart. Exact duplicates are collapsed by content hash,
perceptually similar frames within 6 bits of difference hash are collapsed as duplicates, and those captured
within 3 seconds of each other are marked as bursts. Every collapsed asset stays attached to its group with the
canonical link and the measured distance recorded, so nothing is removed from the record.

| Dataset | Images | Groups | Single-photo groups | Largest group | Widest spread |
|---------|--------|--------|---------------------|---------------|---------------|
| Glenn_Pictures_Cruise_2024 | 21 | 14 | 7 | 2 | 122 m |
| Glenn_London | 111 | 39 | 15 | 18 | 729 m |
| Emiliano_s_Pictures_Shanghai 2026 | 61 | 32 | 16 | 7 | 195 m |

Duplicate and burst detection produced 4 links across 193 images: one exact duplicate in the cruise set at 0 bits
distance, two burst pairs in London at 1 and 2 seconds apart and 3 bits distance, and one near duplicate in
Shanghai at 5 seconds and 5 bits. Every link fell within 5 seconds of its canonical image, so no perceptual
match collapsed photos taken on different days. The risk of a distant false positive exists but did not occur in
this corpus, and it stays visible because duplicates are attached rather than deleted.

Three observations for the Week 2 review:

- **Roughly half of all groups contain a single photo** in every dataset, from 7 of 14 to 16 of 32. These are
  curated selections rather than complete camera rolls, so photos are genuinely sparse in time. Whether a
  single photo should be presented as a moment is a product question, not a threshold-tuning question.
- **The place threshold applies to consecutive pairs, so distance accumulates within a group.** One London
  group spans 729 metres without any single step exceeding 150 metres, which is correct for a continuous walk
  and wrong for a group meant to describe one place. A group-level distance ceiling is the obvious next test.
- **The 5 unanchored Shanghai images are proposed as a separate group** with a score of zero and an explicit
  reason, rather than being guessed into the timeline.

Group scoring is a versioned rule combining time cohesion at 40 percent, the share of members carrying
coordinates at 30 percent, and mean sharpness at 30 percent. The representative image is the sharpest member by
Laplacian variance. Both are starting hypotheses with no traveler feedback behind them yet, and the component
values are stored alongside each proposal so the weighting can be revised without rerunning extraction.

## Comparison against the traveler's reference grouping

The Shanghai dataset ships the traveler's own grouping as a Word document containing the photos themselves
rather than filenames. The reference stage reconstructs it by reading the document structure, where each
paragraph of images is one memory, and matching every embedded image back to a dataset photo using the same
difference hash the pipeline already computes. The document yields 30 reference memories covering 49 photos,
plus 8 photos under a heading marking them as intentionally left out.

Matching recovered 51 of the 57 embedded images within the 6-bit threshold. The remaining 6 are recorded as
unmatched with their closest candidate rather than being forced onto a photo, because their nearest neighbour
sat between 7 and 18 bits away, which is consistent with the document holding a cropped or edited copy.

| Measure | Result |
|---------|--------|
| Assets comparable in both | 44 |
| Reference memories | 30 |
| Proposed groups | 32 |
| Pair precision | 0.89 |
| Pair recall | 0.70 |
| Pair F1 | 0.78 |
| Reference memories split across proposals | 5 |
| Proposals merging separate memories | 1 |
| Intentionally excluded photos placed in a group | 7 of 7 |

Precision materially exceeds recall, and splits outnumber merges five to one. The untuned baseline is
consistently more conservative than the traveler: when it is wrong, it cuts a memory in two rather than fusing
two memories together. Given that a traveler correcting a split needs only to merge, while an incorrect merge
requires them to pick the photos apart, erring toward splitting is the cheaper failure. The time window is the
obvious lever, since the traveler groups across wider gaps than 45 minutes allows.

Every one of the 7 matched photos the traveler deliberately left out was placed into a group by the pipeline.
Exclusion is a product concept the current system does not model at all: it has no notion that a photo might be
part of a trip but not part of any memory worth keeping. That is a gap in the grouping model rather than a
tuning error, and it is a decision for the team rather than something to infer from the data.

The comparison is written to a local JSON artifact under `data/runs/` holding the comparison, the reconstructed
reference and every proposal with its evidence, so decisions can be recorded against specific proposals.

## Coverage against the Week 2 validation plan

| Measurement | Status |
|-------------|--------|
| Presence rate | Measured for all photo, computed and detected fields currently extracted |
| Parse / processing success | Measured, 100 percent across 193 images |
| Agreement / accuracy | Capture time cross-checked against derived timezone on 146 photos with no disagreement; grouping measured on Shanghai at 0.78 pair F1; remaining source fields still need known trip facts |
| False positives / false negatives | Not measured, no inferred labels produced yet |
| Grouping correction burden | Measured on Shanghai: 5 splits and 1 merge across 30 reference memories |
| Regression coverage | Not started |

Fetched metadata and visual-model output remain unmeasured and keep their Week 1 expected ratings until the
relevant stages run. Moment grouping now produces proposals with evidence, but their quality is unmeasured
until they are compared against the reference grouping.

## Open items

- The cruise and London datasets have no reference grouping supplied, so the comparison above covers Shanghai
  only. The traveler's account of why each memory mattered is present in the same document but is not yet used.
- The 16 London videos are inventoried and hashed but excluded from extraction. Whether video belongs in the
  PoC grouping is an open product question.
- Group proposals are held in memory only. Persisting them, and the review decisions taken against them,
  requires the SQLite database to be initialised.
- No external call has been made, so cost, latency and third-party privacy exposure remain at zero and
  unmeasured.
