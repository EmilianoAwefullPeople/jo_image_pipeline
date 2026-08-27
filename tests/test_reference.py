import json
import zipfile
from io import BytesIO

import numpy
from PIL import Image

from jo_pipeline.assets import AssetSignals
from jo_pipeline.extract import image_difference_hash
from jo_pipeline.reference import JsonReferenceReader, ReferenceReader, reference_reader

RELATIONSHIP_TEMPLATE = '<Relationship Id="{identifier}" Type="http://image" Target="media/{media}"/>'
DRAWING_TEMPLATE = '<w:drawing><a:blip r:embed="{identifier}"/></w:drawing>'


def make_image(seed):
    generator = numpy.random.default_rng(seed)
    return Image.fromarray(generator.integers(0, 255, (48, 48, 3), dtype=numpy.uint8))


def png_bytes(image):
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def build_document(paragraphs):
    body = ""
    for text, identifiers in paragraphs:
        drawings = "".join(DRAWING_TEMPLATE.format(identifier=identifier) for identifier in identifiers)
        body += f"<w:p><w:r><w:t>{text}</w:t></w:r>{drawings}</w:p>"
    return f"<w:document><w:body>{body}</w:body></w:document>"


def write_docx(path, paragraphs, images):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", build_document(paragraphs))
        relationships = "".join(RELATIONSHIP_TEMPLATE.format(identifier=identifier, media=f"{identifier}.png") for identifier in images)
        archive.writestr("word/_rels/document.xml.rels", f"<Relationships>{relationships}</Relationships>")
        for identifier, image in images.items():
            archive.writestr(f"word/media/{identifier}.png", png_bytes(image))


def signal_for(path, image):
    return AssetSignals(
        relative_path=path,
        sha256=path,
        captured_utc=None,
        latitude=None,
        longitude=None,
        blur_score=1.0,
        difference_hash=image_difference_hash(image),
    )


def test_each_paragraph_of_images_becomes_a_reference_group(tmp_path):
    images = {"rId1": make_image(1), "rId2": make_image(2), "rId3": make_image(3)}
    document = tmp_path / "reference.docx"
    write_docx(document, [
        ("Photos grouped into memories", []),
        ("", ["rId1", "rId2"]),
        ("", ["rId3"]),
    ], images)
    signals = [signal_for("a.jpeg", images["rId1"]), signal_for("b.jpeg", images["rId2"]), signal_for("c.jpeg", images["rId3"])]

    grouping = ReferenceReader(document).read("sample", signals)

    assert [group.asset_paths for group in grouping.groups] == [["a.jpeg", "b.jpeg"], ["c.jpeg"]]


def test_images_after_the_excluded_heading_are_not_grouped(tmp_path):
    images = {"rId1": make_image(1), "rId2": make_image(2)}
    document = tmp_path / "reference.docx"
    write_docx(document, [
        ("", ["rId1"]),
        ("Intentionally left out:", []),
        ("", ["rId2"]),
    ], images)
    signals = [signal_for("kept.jpeg", images["rId1"]), signal_for("dropped.jpeg", images["rId2"])]

    grouping = ReferenceReader(document).read("sample", signals)

    assert [group.asset_paths for group in grouping.groups] == [["kept.jpeg"]]
    assert grouping.excluded_paths == ["dropped.jpeg"]


def test_narrative_images_are_ignored(tmp_path):
    images = {"rId1": make_image(1), "rId2": make_image(2)}
    document = tmp_path / "reference.docx"
    write_docx(document, [
        ("", ["rId1"]),
        ("Memories unpacked", []),
        ("Selected Scene 1", ["rId2"]),
    ], images)
    signals = [signal_for("kept.jpeg", images["rId1"]), signal_for("retold.jpeg", images["rId2"])]

    grouping = ReferenceReader(document).read("sample", signals)

    assert len(grouping.groups) == 1
    assert grouping.excluded_paths == []


def test_an_image_with_no_close_match_is_recorded_as_unmatched(tmp_path):
    images = {"rId1": make_image(1), "rId2": make_image(99)}
    document = tmp_path / "reference.docx"
    write_docx(document, [("", ["rId1", "rId2"])], images)
    signals = [signal_for("only.jpeg", images["rId1"])]

    grouping = ReferenceReader(document).read("sample", signals)

    assert grouping.groups[0].asset_paths == ["only.jpeg"]
    assert grouping.unmatched_media == ["rId2.png"]


def write_reference_json(path, groups, excluded):
    payload = {
        "dataset_id": "sample",
        "variant": "moments",
        "source": "reference.pdf",
        "groups": [{"index": index, "title": f"Group {index}", "asset_paths": paths} for index, paths in enumerate(groups, start=1)],
        "excluded_paths": excluded,
    }
    path.write_text(json.dumps(payload))


def test_json_reference_yields_groups_and_excluded_paths(tmp_path):
    # A converted PDF reference lists asset file names directly; groups and exclusions must round-trip
    document = tmp_path / "sample_moments.json"
    write_reference_json(document, [["a.jpeg", "b.jpeg"], ["c.jpeg"]], ["d.jpeg"])
    signals = [signal_for(name, make_image(seed)) for seed, name in enumerate(["a.jpeg", "b.jpeg", "c.jpeg", "d.jpeg"])]

    grouping = JsonReferenceReader(document).read("sample", signals)

    assert [group.asset_paths for group in grouping.groups] == [["a.jpeg", "b.jpeg"], ["c.jpeg"]]
    assert grouping.excluded_paths == ["d.jpeg"]
    assert grouping.unmatched_media == []


def test_json_reference_flags_files_missing_from_the_dataset(tmp_path):
    # A reference naming a file the dataset does not hold must surface it as unmatched, not fail
    document = tmp_path / "sample_moments.json"
    write_reference_json(document, [["a.jpeg", "gone.jpeg"]], [])
    signals = [signal_for("a.jpeg", make_image(1))]

    grouping = JsonReferenceReader(document).read("sample", signals)

    assert grouping.groups[0].asset_paths == ["a.jpeg"]
    assert grouping.unmatched_media == ["gone.jpeg"]


def test_reference_reader_dispatches_on_file_suffix(tmp_path):
    # benchmark --reference accepts either the docx reference or a converted JSON one
    assert isinstance(reference_reader(tmp_path / "ref.json"), JsonReferenceReader)
    assert isinstance(reference_reader(tmp_path / "ref.docx"), ReferenceReader)
