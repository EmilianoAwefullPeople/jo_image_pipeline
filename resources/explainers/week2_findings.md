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

## Coverage against the Week 2 validation plan

| Measurement | Status |
|-------------|--------|
| Presence rate | Measured for all photo, computed and detected fields currently extracted |
| Parse / processing success | Measured, 100 percent across 193 images |
| Agreement / accuracy | Not measured, requires comparison against known trip facts |
| False positives / false negatives | Not measured, no inferred labels produced yet |
| Grouping correction burden | Not measured, proposals generated but no review decisions recorded |
| Regression coverage | Not started |

Fetched metadata and visual-model output remain unmeasured and keep their Week 1 expected ratings until the
relevant stages run. Moment grouping now produces proposals with evidence, but their quality is unmeasured
until they are compared against the reference grouping.

## Open items

- The Shanghai dataset ships a human grouping document under `Grouped & Unpacked/`, which includes the
  traveler's own account of why each memory mattered. It is the comparison point for the Review stage and has
  not yet been parsed into a reference grouping.
- The 16 London videos are inventoried and hashed but excluded from extraction. Whether video belongs in the
  PoC grouping is an open product question.
- Group proposals are held in memory only. Persisting them, and the review decisions taken against them,
  requires the SQLite database to be initialised.
- No external call has been made, so cost, latency and third-party privacy exposure remain at zero and
  unmeasured.
