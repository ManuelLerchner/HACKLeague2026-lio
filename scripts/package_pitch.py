"""Package visually reviewed slide images as a portable, image-based PowerPoint deck."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

OUT = Path("artifacts")
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
WIDTH, HEIGHT = 12192000, 6858000


def xml(body: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + body


def relationships(items: list[tuple[str, str, str]]) -> str:
    return xml(
        f'<Relationships xmlns="{REL}">'
        + "".join(
            f'<Relationship Id="{identifier}" Type="{OFFICE}/{kind}" Target="{target}"/>'
            for identifier, kind, target in items
        )
        + "</Relationships>"
    )


def shape_tree(picture: str = "") -> str:
    return (
        '<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/>'
        '<p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/>'
        '<a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/>'
        "</a:xfrm></p:grpSpPr>" + picture + "</p:spTree>"
    )


def main() -> None:
    slides = [OUT / f"slide-{i}.png" for i in range(1, 6)]
    if not all(p.is_file() for p in slides):
        raise SystemExit("Run capture.mjs slides after app screenshots are available")
    parts: dict[str, str] = {}
    overrides = {
        "/ppt/presentation.xml": "presentation.main",
        "/ppt/slideMasters/slideMaster1.xml": "slideMaster",
        "/ppt/slideLayouts/slideLayout1.xml": "slideLayout",
    }
    for i in range(1, 6):
        overrides[f"/ppt/slides/slide{i}.xml"] = "slide"
    parts["[Content_Types].xml"] = xml(
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        + "".join(
            f'<Override PartName="{name}" ContentType="application/'
            f'vnd.openxmlformats-officedocument.presentationml.{kind}+xml"/>'
            for name, kind in overrides.items()
        )
        + "</Types>"
    )
    parts["_rels/.rels"] = relationships([("rId1", "officeDocument", "ppt/presentation.xml")])
    parts["ppt/presentation.xml"] = xml(
        f'<p:presentation xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{OFFICE}">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        "<p:sldIdLst>"
        + "".join(f'<p:sldId id="{255 + i}" r:id="rId{i + 1}"/>' for i in range(1, 6))
        + f'</p:sldIdLst><p:sldSz cx="{WIDTH}" cy="{HEIGHT}" type="screen16x9"/>'
        '<p:notesSz cx="6858000" cy="9144000"/></p:presentation>'
    )
    parts["ppt/_rels/presentation.xml.rels"] = relationships(
        [("rId1", "slideMaster", "slideMasters/slideMaster1.xml")]
        + [(f"rId{i + 1}", "slide", f"slides/slide{i}.xml") for i in range(1, 6)]
    )
    parts["ppt/slideMasters/slideMaster1.xml"] = xml(
        f'<p:sldMaster xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{OFFICE}">'
        "<p:cSld>" + shape_tree() + "</p:cSld>"
        '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1"'
        ' accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5"'
        ' accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
        '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
        "<p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles></p:sldMaster>"
    )
    parts["ppt/slideMasters/_rels/slideMaster1.xml.rels"] = relationships(
        [("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml")]
    )
    parts["ppt/slideLayouts/slideLayout1.xml"] = xml(
        f'<p:sldLayout xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{OFFICE}" type="blank" preserve="1">'
        '<p:cSld name="Blank">' + shape_tree() + "</p:cSld>"
        "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
    )
    parts["ppt/slideLayouts/_rels/slideLayout1.xml.rels"] = relationships(
        [("rId1", "slideMaster", "../slideMasters/slideMaster1.xml")]
    )
    for i in range(1, 6):
        picture = (
            f'<p:pic><p:nvPicPr><p:cNvPr id="2" name="Supplier Chaser slide {i}"/>'
            '<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>'
            '<p:blipFill><a:blip r:embed="rId2"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
            '<p:spPr><a:xfrm><a:off x="0" y="0"/>'
            f'<a:ext cx="{WIDTH}" cy="{HEIGHT}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>'
        )
        parts[f"ppt/slides/slide{i}.xml"] = xml(
            f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{OFFICE}"><p:cSld>'
            + shape_tree(picture)
            + "</p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
        )
        parts[f"ppt/slides/_rels/slide{i}.xml.rels"] = relationships(
            [
                ("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml"),
                ("rId2", "image", f"../media/slide-{i}.png"),
            ]
        )
    with ZipFile(OUT / "pitch.pptx", "w", ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
        for image in slides:
            archive.write(image, "ppt/media/" + image.name)
    print("Created artifacts/pitch.pptx (image-based slides; HTML source is editable)")


if __name__ == "__main__":
    main()
